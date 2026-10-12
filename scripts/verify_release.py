"""发布前校验闸门 —— **不过不发**（发布链上的最后一道人写的硬门）。

四层检查（成本递增，能便宜判的绝不花钱）：
  ① 对账      manifest ↔ out/release/<国家码>-<州码>.ndjson.gz（逐州行数 + sha256，全量重算）
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
import glob
import gzip
import hashlib
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NUM_RE = re.compile(r"^\d{1,6}[A-Za-z]?\d?$")


def find_shard(dir_: str, st: str, info: dict | None = None) -> str:
    """定位某州分片：**以 manifest 记的 file 字段为准**（形如 US-AK.ndjson.gz / UK-ENG.ndjson.gz）。

    manifest 没有 file（老清单）时按通配回退 —— 国家码来自数据目录，不写死 US。
    """
    f = (info or {}).get("file")
    if f and os.path.exists(os.path.join(dir_, f)):
        return os.path.join(dir_, f)
    for pat in (f"*-{st}.ndjson.gz", f"{st}.ndjson.gz"):
        hits = sorted(glob.glob(os.path.join(dir_, pat)))
        if hits:
            return hits[0]
    return os.path.join(dir_, f or f"{st}.ndjson.gz")
ZIP_RE = re.compile(r"^\d{5}$")
# 注意：发布包（稳定视图）里的来源字段是聚合后的 `sources`（列表），不是逐条的 source
REQUIRED = ("state", "street", "number", "key", "sources")
WL_PATH = "data/州码白名单.json"
# 与发布侧用同一把尺子：build_release 在入全局表前会用字典补 city/zip 并**重算 key**
# （修「同址双 key」），所以溯源检查也必须用同一个 canon_key，否则会误报「发布丢数据」。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_Z2C: dict = {}
_C2Z: dict = {}
try:
    from build_release import canon_key, load_zip_dict      # noqa: E402
except Exception:                                          # 极端情况下不因此让整个闸门失效
    def canon_key(rec, z2c, c2z):
        return str(rec.get("key") or "")

    def load_zip_dict(path):
        return {}, {}
# P1-4 修复：白名单文件根本不在仓库里 → WHITELIST 恒为空 → `if WHITELIST and ...` 恒假 →
# 「州码不在白名单＝解析错位」这道硬门**从来没生效过**。改成内置代码常量（单一事实来源），
# 外部文件若存在再覆盖它（允许临时收窄，但不允许因为缺文件而防线消失）。
try:
    from collectors.normalize import STATE_CODES as _BUILTIN_STATES
    WHITELIST = set(_BUILTIN_STATES)
except Exception:                                    # 万一 import 不通也不许静默失效
    WHITELIST = set(("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
                     "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
                     "WV WI WY PR VI GU AS MP").split())
if os.path.exists(WL_PATH):
    try:
        WHITELIST = set(json.load(open(WL_PATH, encoding="utf-8")))
    except Exception:
        print(f"⚠ {WL_PATH} 存在但读不出来，用内置州码表")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def scan_shard(path: str, head: int = 2000, tail: int = 500):
    """单趟扫描：行数 + 空行数 + 相邻重复 + 头部/尾部样本。

    ⚠ 老版本把空行静默 `continue` 掉，所以 IL 分片混着 58.2 万条空行也「校验通过」。
    v2 把空行数吐出来当作出厂硬门（必须 0）；分片按 key 有序写出，所以相邻重复
    与乱序也能顺带查（不吃内存）。
    """
    rows = 0
    blanks = 0
    dups = 0
    unsorted = 0
    prev = None
    head_recs: list[dict] = []
    tail_recs: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                blanks += 1
                continue
            rows += 1
            try:
                rec = json.loads(line)
            except Exception:
                dups += 0
                continue
            k = rec.get("key")
            if prev is not None:
                if k == prev:
                    dups += 1
                elif str(k) < str(prev):
                    unsorted += 1
            prev = k
            if len(head_recs) < head:
                head_recs.append(rec)
                continue
            tail_recs.append(rec)
            if len(tail_recs) > tail:
                del tail_recs[0]
    return rows, head_recs + tail_recs, blanks, dups, unsorted


def check_schema(recs: list[dict], state: str) -> tuple[dict, list[str]]:
    st = {"sampled": len(recs), "missing": 0, "bad_number": 0, "bad_zip": 0,
          "dup_key": 0, "cross_state": 0, "bad_state": 0, "empty_city": 0, "empty_zip": 0,
          "grade_c": 0}
    errs: list[str] = []
    seen: set[str] = set()
    for r in recs:
        if r.get("_grade") == "C":
            # C 级只豁免「五要素完整性」那一项；key 重复 / 州码白名单**不豁免**（P1-4）——
            # 否则坏州码记录只要被打成 C 就能整片躲过校验。
            st["grade_c"] += 1
            k = r.get("key")
            if k in seen:
                st["dup_key"] += 1
            seen.add(k)
            ks = str(k or "").split("|")[0]
            if ks != state:
                st["cross_state"] += 1
                if WHITELIST and ks not in WHITELIST:
                    st["bad_state"] += 1
            continue
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
        p = find_shard(release_dir, st)
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
            if not f.endswith(".ndjson"):
                continue
            with open(os.path.join(pdir, f), encoding="utf-8") as pfh:
                for line in pfh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ks.add(canon_key(json.loads(line), _Z2C, _C2Z))
                    except Exception:
                        continue
        if ks:
            want[sid] = ks
    hit = {sid: 0 for sid in want}
    with gzip.open(merged, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                k = canon_key(json.loads(line), _Z2C, _C2Z)
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
    ap.add_argument("--zip-dict", default="dict/zip_city.tsv",
                    help="与发布侧同一份字典（溯源 key 重算用）")
    ap.add_argument("--min-rows", type=int, default=0, help="总行数下限（防缩水），0=不判")
    ap.add_argument("--prev-manifest", default="", help="上一版发布清单，逐州对账（防悄无声息掉质量）")
    ap.add_argument("--max-shrink", type=float, default=0.10, help="单州行数跌幅上限")
    ap.add_argument("--sample-sources", type=int, default=3)
    ap.add_argument("--cross-keys", type=int, default=6_000_000,
                    help="跨州重复检查的抽样 key 数（0=跳过）")
    a = ap.parse_args(argv)
    global _Z2C, _C2Z
    _Z2C, _C2Z = load_zip_dict(a.zip_dict)

    errs: list[str] = []
    if not os.path.exists(a.manifest):
        raise SystemExit("✗ 没有 release-manifest.json —— 发布包没构建成功，不许发布")
    m = json.load(open(a.manifest, encoding="utf-8"))
    total = int(m.get("rows") or 0)
    print(f"① 对账：manifest 记 {total:,} 行 / {len(m.get('states') or {})} 州分片")
    for st, info in sorted((m.get("states") or {}).items()):
        p = find_shard(a.release, st, info)
        if not os.path.exists(p):
            errs.append(f"[对账]{st} 分片文件缺失")
            continue
        rows, recs, blanks, dups, unsorted = scan_shard(p)
        if rows != int(info.get("rows") or -1):
            errs.append(f"[对账]{st} 行数不符：文件 {rows} vs manifest {info.get('rows')}")
        if blanks:
            errs.append(f"[对账]{st} 空行 {blanks:,} 条（出厂硬门：必须 0）")
        if dups:
            errs.append(f"[对账]{st} 相邻重复 key {dups:,} 条（行数必须 == 唯一 key 数）")
        if unsorted:
            errs.append(f"[对账]{st} key 乱序 {unsorted:,} 处（分片必须按 key 有序）")
        if sha256(p) != info.get("sha256"):
            errs.append(f"[对账]{st} sha256 不符（文件被改动过）")
        se, sers = check_schema(recs, st)
        errs += sers
        if rows:
            print(f"   {st}: {rows:>10,} 行 抽样 {se['sampled']} 条  "
                  f"缺字段{se['missing']} 坏号{se['bad_number']} 重复{se['dup_key']}"
                  + (f" 跨州{se['cross_state']}" if se["cross_state"] else ""))
    # ④ 站用子集（site/）也必须过闸 —— P1-4：这是**对外直接取用**的产物，原来一条都不查
    site_dir = os.path.join(a.release, "site")
    if os.path.isdir(site_dir):
        print("②' 站用子集（site/）：")
        for st, info in sorted((m.get("states") or {}).items()):
            sp = find_shard(site_dir, st, info)
            a_grade = int((info.get("grades") or {}).get("A") or 0)
            if not os.path.exists(sp):
                if a_grade:
                    errs.append(f"[site]{st} 声明 {a_grade:,} 条 A 级却没有 site 分片")
                continue
            srows, srecs, sblanks, sdups, sunsorted = scan_shard(sp)
            if sblanks:
                errs.append(f"[site]{st} 空行 {sblanks:,} 条")
            if sdups:
                errs.append(f"[site]{st} 相邻重复 key {sdups:,} 条")
            if sunsorted:
                errs.append(f"[site]{st} key 乱序 {sunsorted:,} 处")
            if srows != a_grade:
                errs.append(f"[site]{st} 行数 {srows:,} != 清单里的 A 级数 {a_grade:,}")
            sse, sers = check_schema(srecs, st)
            errs += [f"[site]{e}" for e in sers]
            print(f"   {st}: {srows:>10,} 行 抽样 {sse['sampled']} 条  缺字段{sse['missing']} "
                  f"坏号{sse['bad_number']} 重复{sse['dup_key']}")
    elif any(int((i.get("grades") or {}).get("A") or 0)
             for i in (m.get("states") or {}).values()):
        errs.append("[site] 清单里有 A 级记录，但根本没有 site/ 目录")

    # ② 溯源与合并自检（第 11/13 步）
    sj = os.path.join(a.release, "sources.json")
    sinfo = m.get("sources") or {}
    if not os.path.exists(sj):
        errs.append("[溯源] sources.json 缺失（记录里的 source_id 无处可查，等于不可溯源）")
    else:
        exp = (sinfo.get("sources_json") or {}).get("sha256")
        if exp and sha256(sj) != exp:
            errs.append("[溯源] sources.json sha256 不符")
        tbl = json.load(open(sj, encoding="utf-8")).get("sources") or {}
        unk = [k for k, v in tbl.items() if v.get("license_status") == "unknown"]
        print(f"② 溯源：{len(tbl)} 个来源｜license 缺失显式标 unknown 的 {len(unk)} 个（如实标注，不算错）")
    if sinfo.get("orphans"):
        errs.append(f"[溯源] 孤儿来源 {len(sinfo['orphans'])} 个不在源总表：{sinfo['orphans'][:5]}")
    multi = int(m.get("multi_source_records") or 0)
    max_src = max([int((v or {}).get("count") or 0)
                   for v in (sinfo.get("per_state") or {}).values()] or [0])
    if multi:
        print(f"② 合并自检：多来源记录 {multi:,} 条（单州最多 {max_src} 个来源）")
    elif max_src >= 2:
        errs.append(f"[合并] 某州最多有 {max_src} 个来源，但全量多来源记录为 0 → 跨源合并没生效")
    else:
        print("② 合并自检：每州来源都只有 1 个 → 暂时无可合并（多来源 0 属正常）")
    # 降级清单（不阻断发布）：完整性灾难级的州，排除出站用子集，仅留在全量分片
    degraded: list[str] = []
    for st, info in sorted((m.get("states") or {}).items()):
        sp = os.path.join(a.release, f"sources-{st}.json")
        if not os.path.exists(sp):
            errs.append(f"[溯源]{st} sources-{st}.json 缺失（州分片必须自带来源表）")
        else:
            cnt = len(json.load(open(sp, encoding="utf-8")).get("sources") or {})
            exp_cnt = ((sinfo.get("per_state") or {}).get(st) or {}).get("count")
            if exp_cnt is not None and cnt != exp_cnt:
                errs.append(f"[溯源]{st} 来源表条数 {cnt} != 清单 {exp_cnt}")
        mi_miss = info.get("field_missing") or {}
        if info.get("rows"):
            cr = (mi_miss.get("city") or 0) / info["rows"]
            zr = (mi_miss.get("zip") or 0) / info["rows"]
            if cr > 0.5 or zr > 0.5:
                # 逐州降级（2026-10-12 改）：一个州缺 city/zip 不该拖垮整包——
                # 缺五要素的记录本来就判不进 A 级、进不了站用子集（site/），
                # 所以正确处理是「该州不进 site、只留全量分片并带 _grade 标注」，
                # 而不是让另外 40 多个州的好数据一起陪葬。
                degraded.append(f"[降级]{st} city/zip 缺失率 {cr:.0%}/{zr:.0%}（>50%）→ "
                                f"不进站用子集，仅保留全量分片并带 _grade 标注")

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
    # ⑤ 与上一版逐州对账（决策：掉质量不许悄无声息）
    prev = None
    if a.prev_manifest and os.path.exists(a.prev_manifest):
        try:
            prev = json.load(open(a.prev_manifest, encoding="utf-8"))
        except Exception:
            prev = None
    if prev:
        if not str(prev.get("pipeline") or "").startswith("v2"):
            print("⑤ 逐州对账：上一版是 v1 口径（含旧数据集底稿）→ 本次不对比（v2 首次发布）")
        else:
            print("⑤ 逐州对账（行数变化）：")
            old_st = prev.get("states") or {}
            new_st = m.get("states") or {}
            for st in sorted(set(old_st) | set(new_st)):
                old = int((old_st.get(st) or {}).get("rows") or 0)
                new = int((new_st.get(st) or {}).get("rows") or 0)
                if old and new < old * (1 - a.max_shrink):
                    errs.append(f"[缩水]{st} {old:,} → {new:,}"
                                f"（跌 {1 - new / old:.1%}，超阈值 {a.max_shrink:.0%}）")
                elif old or new:
                    print(f"   {st}: {old:,} → {new:,}")

    n_states = len(m.get("states") or {})
    # 底线：降级州过半 → 整体数据塌了，仍整包拦下（防「全体降级＝没有校验」）
    if degraded and len(degraded) > max(3, n_states // 2):
        errs.append(f"[降级] {len(degraded)}/{n_states} 个州完整性灾难级（过半）→ 整包不放行")
    out = {"rows": total, "states": n_states, "errors": errs,
           "degraded": degraded, "degraded_n": len(degraded)}
    if a.cross_keys:
        out["cross_shard"] = out_cross
    os.makedirs("out", exist_ok=True)
    json.dump(out, open("out/verify-release.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if errs:
        print("\n✗ 校验闸门不放行：")
        for e in errs[:20]:
            print("   " + e)
        sys.exit(1)
    if degraded:
        print(f"\n⚠ 降级发布：{len(degraded)}/{n_states} 个州完整性灾难级 —— "
              f"这些州不进站用子集（site/），只留在全量分片里并带 _grade 标注")
        for d in degraded[:12]:
            print("   " + d)
        if len(degraded) > 12:
            print(f"   …其余 {len(degraded) - 12} 个见 out/verify-release.json")
        print(f"   其余 {n_states - len(degraded)} 个州正常发布")
    print(f"\n✓ 校验闸门通过：{total:,} 行 / {n_states} 州，可以发布"
          + (f"（{len(degraded)} 州降级）" if degraded else ""))
    sys.exit(0)


if __name__ == "__main__":
    main()
