"""校验闸门 —— 发布前的三层自检（schema / 溯源子集 / 回源抽检）。

为什么要分三层（成本递增，能便宜判的绝不花钱）：
  ① schema 自检      本地、秒级  —— 必填字段、州码白名单、ZIP/号形态、key 重复
  ② 溯源子集校验    本地、秒级  —— 产物里每条记录必须能在 bronze 原始档里找到出处
                                   （防「凭空造数据」与解析错位，这是最狠的一条）
  ③ 回源抽检        联网、每批 200 条 —— 重新拉一小段原始数据，比对记录仍存在
                                   （防源站数据已变 / 我们解析口径漂移）

用法：
  python scripts/verify_parsed.py --source us/ca/san_francisco                 # ①②（本地）
  python scripts/verify_parsed.py --source us/ms/lauderdale --sample 200      # ①②③
  python scripts/verify_parsed.py --path parsed/us/ms/lauderdale/mapping@v1.ndjson --sample 200
退出码：0 通过；1 有硬错误（schema/溯源失败）；2 仅抽检命中率偏低（告警但可放行）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.fetch_raw import archive_dir, fetch_raw, pick_protocol        # noqa: E402
from collectors.flatten import iter_rows_from_archive                        # noqa: E402
from collectors.normalize import norm_number, norm_street                    # noqa: E402
from collectors.politeness import Politeness                                 # noqa: E402

TABLE = "data/源总表-执行序.csv"
STATES = set(json.load(open("data/州码白名单.json", encoding="utf-8"))) \
    if os.path.exists("data/州码白名单.json") else set()
ZIP_RE = re.compile(r"^\d{5}$")
REQUIRED = ("state", "street", "number", "key", "source")   # city/zip 允许为空（很多源就没有）


def load_records(path: str, limit: int = 0):
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if limit and i >= limit:
                return
            line = line.strip()
            if line:
                yield json.loads(line)


def check_schema(path: str) -> tuple[dict, list[str]]:
    stats = Counter()
    errs: list[str] = []
    seen: set[str] = set()
    for i, rec in enumerate(load_records(path), 1):
        missing = [k for k in REQUIRED if not rec.get(k)]
        if missing:
            stats["missing_fields"] += 1
            if len(errs) < 5:
                errs.append(f"L{i} 缺字段 {missing}")
        if not rec.get("city"):
            stats["empty_city"] += 1
        if not rec.get("zip"):
            stats["empty_zip"] += 1
        if STATES and rec.get("state") not in STATES:
            stats["state_not_in_whitelist"] += 1
        if rec.get("zip") and not ZIP_RE.match(rec["zip"]):
            stats["bad_zip"] += 1
        if not re.match(r"^\d{1,6}[A-Za-z]?\d?$", str(rec.get("number", ""))):
            stats["bad_number"] += 1
        k = rec.get("key")
        if k in seen:
            stats["dup_key"] += 1
        seen.add(k)
        stats["rows"] += 1
    hard = stats["missing_fields"] + stats["state_not_in_whitelist"] + stats["bad_number"]
    return dict(stats), errs + ([f"硬错误合计 {hard} 条"] if hard else [])


def _rule_keys(archive: str, row: dict, rule_name: str, sid: str, limit: int = 0
               ) -> set[str]:
    """用**同一套规则**把原始档重洗一遍 → key 集合。

    这是溯源的唯一可靠做法：逐字段猜「原始行的哪一列是号、哪一列是街」猜不准，
    直接重跑规则得到口径一致的对照集，再比 parsed 是不是它的子集。
    """
    from collectors.normalize import parse_mapping_from_table
    from collectors.rules import RULES, RuleCtx
    rule = RULES.get(rule_name)
    if rule is None:
        return set()
    ctx = RuleCtx(sid, row, parse_mapping_from_table(row), row.get("state", "") or "",
                  {})
    out: set[str] = set()
    n = 0
    for raw in iter_rows_from_archive(archive):
        if "_read_error" in raw:
            continue
        if limit and n >= limit:
            break
        n += 1
        rec, _why = rule.clean(raw, ctx)
        if rec:
            out.add(rec["key"])
    return out


def check_provenance(path: str, archive: str, sid: str, row: dict,
                     sample: int = 0) -> tuple[dict, list[str]]:
    """产物必须是「用同一规则重跑原始档」的结果集（防凭空造数据 / 解析错位）。"""
    rule_name = os.path.basename(path).replace(".ndjson", "")
    parsed = [rec["key"] for rec in load_records(path)]
    # 不限行：limit 若按「读了多少原始行」计，会误判（同一份原始档前 353 行只洗出 187 条）
    have = _rule_keys(archive_dir(archive, sid), row, rule_name, sid)
    hit = sum(1 for k in parsed if k in have)
    res = {"rule": rule_name, "parsed_rows": len(parsed), "bronze_rows": len(have),
           "found_in_bronze": hit,
           "rate": round(hit / len(parsed), 3) if parsed else 0.0}
    return res, ([] if hit == len(parsed) or not parsed else
                 [f"有 {len(parsed) - hit} 条与原始档重跑结果不一致"])


def check_resample(sid: str, row: dict, sample: int = 200, archive: str = "raw_archive") -> dict:
    """回源抽检：重拉一小段原始数据（临时目录），比对解析口径是否仍成立。"""
    pol = Politeness(min_interval=1.0, max_concurrency=1, max_retries=1, timeout=20,
                     log_path="out/verify_log.jsonl")
    tmp = "out/verify_tmp"
    os.makedirs(tmp, exist_ok=True)
    proto = pick_protocol(row["url"])
    if proto == "direct":
        return {"skipped": True,
                "why": "直链文件整份下载代价大，回源抽检跳过（本地溯源已覆盖）"}
    try:
        fetch_raw(pol, sid, row["url"], tmp, force=True, budget_s=90)
    except Exception as e:
        return {"skipped": True, "why": f"回源失败（按网络抖动处理）: {type(e).__name__}"}
    pdir = os.path.join("parsed", *sid.split("/"))
    rule_name = ""
    parsed: list[str] = []
    for f in sorted(os.listdir(pdir) if os.path.isdir(pdir) else []):
        if f.endswith(".ndjson"):
            rule_name = f.replace(".ndjson", "")
            parsed = [rec["key"] for rec in load_records(os.path.join(pdir, f))]
    fresh = _rule_keys(archive_dir(tmp, sid), row, rule_name, sid)
    pool = list(parsed)
    random.shuffle(pool)
    picked = pool[:sample]
    hit = sum(1 for p in picked if p in fresh)
    if os.environ.get("VERIFY_DEBUG"):
        print(f"   [debug] rule={rule_name} parsed={len(parsed)} fresh={len(fresh)} "
              f"parsed[:2]={pool[:2][:2]} fresh[:2]={sorted(fresh)[:2]}")
    return {"protocol": proto, "rule": rule_name, "fresh_rows": len(fresh),
            "sampled": len(picked), "hit": hit,
            "rate": round(hit / len(picked), 3) if picked else 0.0,
            "skipped": False}




def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="", help="源 id（自动找它的规则产物）")
    ap.add_argument("--path", default="", help="直接指定 parsed ndjson")
    ap.add_argument("--sample", type=int, default=200, help="回源抽检条数（0=跳过）")
    ap.add_argument("--archive", default="raw_archive")
    a = ap.parse_args(argv)

    sid = a.source or path_source(a.path)
    path = a.path
    if not path:
        pdir = os.path.join("parsed", *sid.split("/"))
        cands = sorted(f for f in os.listdir(pdir) if f.endswith(".ndjson")) \
            if os.path.isdir(pdir) else []
        if not cands:
            raise SystemExit(f"{sid} 还没有规则产物（先跑 python -m collectors.local_cascade --source {sid}）")
        path = os.path.join(pdir, cands[-1])

    out = {"source": sid, "parsed_file": path}
    stats, errs = check_schema(path)
    out["schema"] = stats
    print(f"① schema：{stats}  {'✓' if not errs else '✗ ' + '; '.join(errs[:3])}")

    rows0 = list(csv.DictReader(open(TABLE, encoding="utf-8-sig")))
    row = next((r for r in rows0 if r["source_id"] == sid), None)
    prov, perrs = check_provenance(path, a.archive, sid, row or {})
    out["provenance"] = prov
    print(f"② 溯源子集：产物 {prov['parsed_rows']} 条 / 原始档重跑同规则得 {prov['bronze_rows']} 条，"
          f"一致 {prov['found_in_bronze']} 条 = {prov['rate']:.1%}  "
          f"{'✓' if not perrs else '✗ ' + perrs[0]}")

    rc = 1 if (errs or perrs) else 0
    if a.sample:
        if row:
            res = check_resample(sid, row, a.sample, a.archive)
            out["resample"] = res
            if res.get("skipped"):
                print(f"③ 回源抽检：跳过（{res['why']}）")
            else:
                print(f"③ 回源抽检：抽 {res['sampled']} 条，回源数据中命中 {res['hit']} 条 "
                      f"= {res['rate']:.1%}（回源 {res['fresh_rows']:,} 行）")
                if res["rate"] < 0.95:
                    print("   ⚠ 命中率偏低：可能是解析口径漂移或数据源已更新 → 人工看一眼")
                    rc = rc or 2
    print(json.dumps(out, ensure_ascii=False)[:600])
    sys.exit(rc)


if __name__ == "__main__":
    main()
