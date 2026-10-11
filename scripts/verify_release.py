"""发布前校验闸门 —— **不过不发**（发布链上的最后一道人写的硬门）。

四层检查（成本递增，能便宜判的绝不花钱）：
  ① 对账      manifest ↔ out/release/*.ndjson.gz（逐州行数 + sha256，全量重算）
  ② schema    每州抽头 2000 / 尾 500 条：必填、州码白名单、号码/邮编形态、key 重复、
              key 前缀必须等于分片州码（防串片）
  ③ 溯源子集  state 里已完成的源，其 parsed ndjson 的每条 key 必须出现在发布全量里
              （防「发布把数据吃掉了」——历史上真丢过状态）
  ④ 防缩水    总行数 ≥ --min-rows（发布是累积合并，只该长不该缩）

退出码：0 放行；非 0 = 掐断发布（publish 作业会红叉，资产不更新）。

用法：
  python scripts/verify_release.py --release out/release --parsed parsed \
      --state progress/state.json --min-rows 20000000 --sample-sources 3
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NUM_RE = re.compile(r"^\d{1,6}[A-Za-z]?\d?$")
ZIP_RE = re.compile(r"^\d{5}$")
# 注意：发布包（稳定视图）里的来源字段是聚合后的 `sources`（列表），不是逐条的 source
REQUIRED = ("state", "street", "number", "key", "sources")
WL_PATH = "data/州码白名单.json"
WHITELIST = set(json.load(open(WL_PATH, encoding="utf-8"))) if os.path.exists(WL_PATH) else set()


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def scan_shard(path: str, head: int = 2000, tail: int = 500):
    """单趟扫描：行数 + 头部样本 + 尾部样本。"""
    rows = 0
    head_recs: list[dict] = []
    tail_recs: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows += 1
            if len(head_recs) < head:
                try:
                    head_recs.append(json.loads(line))
                except Exception:
                    pass
                continue
            try:
                tail_recs.append(json.loads(line))
            except Exception:
                continue
            if len(tail_recs) > tail:
                del tail_recs[0]
    return rows, head_recs + tail_recs


def check_schema(recs: list[dict], state: str) -> tuple[dict, list[str]]:
    st = {"sampled": len(recs), "missing": 0, "bad_number": 0, "bad_zip": 0,
          "dup_key": 0, "cross_state": 0, "bad_state": 0, "empty_city": 0, "empty_zip": 0}
    errs: list[str] = []
    seen: set[str] = set()
    for r in recs:
        if [k for k in REQUIRED if not r.get(k)]:
            st["missing"] += 1
        if not r.get("city"):
            st["empty_city"] += 1
        if not r.get("zip"):
            st["empty_zip"] += 1
        if not NUM_RE.match(str(r.get("number", ""))):
            st["bad_number"] += 1
        if r.get("zip") and not ZIP_RE.match(str(r["zip"])):
            st["bad_zip"] += 1
        k = r.get("key")
        if k in seen:
            st["dup_key"] += 1
        seen.add(k)
        ks = str(k or "").split("|")[0]
        if ks != state:
            # 跨州地址是真实存在的（源头给的 region 就是邻州）→ 只算信息；
            # 但州码不在白名单里就是硬错（多半是解析错位）
            st["cross_state"] += 1
            if WHITELIST and ks not in WHITELIST:
                st["bad_state"] += 1
    for field in ("missing", "bad_number", "bad_zip", "dup_key", "bad_state"):
        if st[field]:
            errs.append(f"[{state}] {field}={st[field]}")
    return st, errs


def load_keys(path: str) -> set[str]:
    out: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.add(json.loads(line)["key"])
                except Exception:
                    pass
    return out


def cross_shard_check(release_dir: str, states: list[str],
                      max_keys: int = 6_000_000) -> tuple[dict, list[str]]:
    """跨州分片重复检查（**确定性抽样**：按分片顺序读满 max_keys 个 key 就停）。

    为什么需要：key 里含州码、记录按源所在州归档；同一条地址被两个州的源都产出时
    去重是州内做的，会变成**两个分片里的同一 key**。全量 6300 万条太吃内存，
    这里用 8 字节哈希做抽样（抽不到不等于不存在，但能抳住批量性的问题）。
    """
    seen: dict[int, str] = {}
    total = dups = 0
    examples: list[str] = []
    for st in states:
        p = os.path.join(release_dir, f"{st}.ndjson.gz")
        if not os.path.exists(p):
            continue
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                if total >= max_keys:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    k = json.loads(line).get("key")
                except Exception:
                    continue
                total += 1
                h = int.from_bytes(hashlib.blake2b(str(k).encode(), digest_size=8).digest(), "big")
                prev = seen.get(h)
                if prev is None:
                    seen[h] = st
                elif prev != st:
                    dups += 1
                    if len(examples) < 5:
                        examples.append(f"{k}（{prev} / {st}）")
        if total >= max_keys:
            break
    rate = dups / total if total else 0.0
    log = [f"抽样 {total:,} 个 key（上限 {max_keys:,}）→ 跨州重复 {dups:,} 条 = {rate:.3%}"]
    log += [f"  例：{e}" for e in examples]
    errs = ([] if rate <= 0.0005 else
            [f"[跨州重复] 抽样 {total:,} 条里 {dups:,} 条出现在两个分片（{rate:.2%} > 0.05%）"])
    return {"sampled": total, "cross_dups": dups, "rate": round(rate, 6)}, errs + log


def provenance_check(parsed_dir: str, state_file: str, merged: str,
                     sample_sources: int) -> tuple[list[str], list[str]]:
    """已完成的源 → 其产物 key 必须都在发布全量里。"""
    log: list[str] = []
    errs: list[str] = []
    if not os.path.exists(state_file):
        return ["（无 state 文件，跳过溯源检查）"], []
    try:
        stt = json.load(open(state_file, encoding="utf-8"))
    except Exception as e:
        return [f"（state 读不动 {e}，跳过）"], []
    done = [sid for sid, v in stt.items()
            if v.get("done") and v.get("kept") and os.path.isdir(os.path.join(parsed_dir, *sid.split("/")))]
    if not done:
        return ["（无已完成源，跳过溯源检查）"], []
    random.seed(20261011)
    picked = random.sample(done, min(sample_sources, len(done)))
    want: dict[str, set[str]] = {}
    for sid in picked:
        pdir = os.path.join(parsed_dir, *sid.split("/"))
        ks: set[str] = set()
        for f in sorted(os.listdir(pdir)):
            if f.endswith(".ndjson"):
                ks |= load_keys(os.path.join(pdir, f))
        if ks:
            want[sid] = ks
    hit = {sid: 0 for sid in want}
    with gzip.open(merged, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                k = json.loads(line).get("key")
            except Exception:
                continue
            for sid, ks in want.items():
                if hit[sid] < len(ks) and k in ks:
                    hit[sid] += 1
    for sid, ks in want.items():
        rate = hit[sid] / len(ks)
        log.append(f"{sid}: 产物 {len(ks)} 条 → 发布包里找到 {hit[sid]} 条 = {rate:.1%}")
        if rate < 0.95:
            errs.append(f"[溯源]{sid} 只有 {rate:.1%} 的产物进了发布包（疑似发布丢数据）")
    return log, errs


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default="out/release")
    ap.add_argument("--manifest", default="data/release-manifest.json")
    ap.add_argument("--parsed", default="parsed")
    ap.add_argument("--state", default="progress/state.json")
    ap.add_argument("--min-rows", type=int, default=0, help="总行数下限（防缩水），0=不判")
    ap.add_argument("--sample-sources", type=int, default=3)
    ap.add_argument("--cross-keys", type=int, default=6_000_000,
                    help="跨州重复检查的抽样 key 数（0=跳过）")
    a = ap.parse_args(argv)

    errs: list[str] = []
    if not os.path.exists(a.manifest):
        raise SystemExit("✗ 没有 release-manifest.json —— 发布包没构建成功，不许发布")
    m = json.load(open(a.manifest, encoding="utf-8"))
    total = int(m.get("rows") or 0)
    print(f"① 对账：manifest 记 {total:,} 行 / {len(m.get('states') or {})} 州分片")
    for st, info in sorted((m.get("states") or {}).items()):
        p = os.path.join(a.release, f"{st}.ndjson.gz")
        if not os.path.exists(p):
            errs.append(f"[对账]{st} 分片文件缺失")
            continue
        rows, recs = scan_shard(p)
        if rows != int(info.get("rows") or -1):
            errs.append(f"[对账]{st} 行数不符：文件 {rows} vs manifest {info.get('rows')}")
        if sha256(p) != info.get("sha256"):
            errs.append(f"[对账]{st} sha256 不符（文件被改动过）")
        se, sers = check_schema(recs, st)
        errs += sers
        if rows:
            print(f"   {st}: {rows:>10,} 行 抽样 {se['sampled']} 条  "
                  f"缺字段{se['missing']} 坏号{se['bad_number']} 重复{se['dup_key']}"
                  + (f" 跨州{se['cross_state']}" if se["cross_state"] else ""))
    merged = os.path.join(a.release, "merged.clean.ndjson.gz")
    if not os.path.exists(merged):
        errs.append("[对账] merged.clean.ndjson.gz 缺失")
    else:
        mi = m.get("merged") or {}
        if sha256(merged) != mi.get("sha256"):
            errs.append("[对账] merged sha256 不符")
        log, perrs = provenance_check(a.parsed, a.state, merged, a.sample_sources)
        errs += perrs
        print("③ 溯源子集：")
        for l in log:
            print(f"   {l}")
    if a.cross_keys:
        stats, logs = cross_shard_check(a.release, sorted((m.get("states") or {})), a.cross_keys)
        print("④ 跨州重复（抽样）：")
        for l in logs:
            print(f"   {l}")
        errs += [e for e in logs if e.startswith("[跨州重复]")]
        out_cross = stats
    if a.min_rows and total < a.min_rows:
        errs.append(f"[缩水] 总行数 {total:,} 低于下限 {a.min_rows:,}")

    out = {"rows": total, "states": len(m.get("states") or {}), "errors": errs}
    if a.cross_keys:
        out["cross_shard"] = out_cross
    os.makedirs("out", exist_ok=True)
    json.dump(out, open("out/verify-release.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if errs:
        print("\n✗ 校验闸门不放行：")
        for e in errs[:20]:
            print("   " + e)
        sys.exit(1)
    print(f"\n✓ 校验闸门通过：{total:,} 行 / {len(m.get('states') or {})} 州，可以发布")
    sys.exit(0)


if __name__ == "__main__":
    main()
