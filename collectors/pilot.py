"""一期样跑：从「源总表」里挑源 → 采集 → 标准化 → 合并 → 出报告。

用法：
  python -m collectors.pilot --sources us/de/statewide,us/ri/statewide \
      --out out/pilot --max-records 20000 --limit-pages 0

设计要点：
  * 全程 polite（见 politeness.py）：每主机最小间隔 0.8s，并发 2，失败退避
  * 分页尺寸 = 服务端上限 × 0.9（铁律）
  * 记录数上限用于试跑，避免一次性拉满
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

from .arcgis import fetch_layer
from .filedl import fetch_file, iter_rows
from .merge import merge_ndjson
from .normalize import mapping_sanity, parse_mapping_from_table, to_record
from .politeness import Politeness

TABLE = "data/源总表-执行序.csv"


def load_sources(ids=None, priorities=None, limit=None):
    rows = list(csv.DictReader(open(TABLE, encoding="utf-8-sig")))
    if ids:
        want = {s.strip() for s in ids.split(",") if s.strip()}
        rows = [r for r in rows if r["source_id"] in want]
    elif priorities:
        wantp = {p.strip() for p in priorities.split(",") if p.strip()}
        rows = [r for r in rows if r["priority"] in wantp]
    return rows[:limit] if limit else rows


def run(rows, out_dir, max_records, min_interval, concurrency, log_path):
    os.makedirs(out_dir, exist_ok=True)
    pol = Politeness(min_interval=min_interval, max_concurrency=concurrency, log_path=log_path)
    raw_dir, norm_dir = os.path.join(out_dir, "raw"), os.path.join(out_dir, "normalized")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(norm_dir, exist_ok=True)

    norm_files, stats = [], []
    for i, row in enumerate(rows, 1):
        sid = row["source_id"]
        safe = sid.replace("/", "__")
        mapping = parse_mapping_from_table(row)
        st = {"source_id": sid, "channel": row["channel"], "priority": row["priority"],
              "scope": row["scope"], "state": row["state"]}
        t0 = time.time()
        try:
            raw_rows = []
            if row["channel"] == "ARCGIS":
                raw_path = os.path.join(raw_dir, safe + ".ndjson")
                info = fetch_layer(pol, row["url"], raw_path, max_records=max_records)
                st.update(info)
                with open(raw_path, encoding="utf-8") as fh:
                    raw_rows = [json.loads(l) for l in fh if l.strip()]
            else:
                info = fetch_file(pol, row["url"], os.path.join(raw_dir, safe))
                st.update({k: v for k, v in info.items() if k != "members"})
                st["members"] = len(info.get("members", []))
                raw_rows = list(iter_rows(info))

            good, bad = 0, 0
            missing = mapping_sanity(raw_rows[0], mapping) if raw_rows else []
            norm_path = os.path.join(norm_dir, safe + ".ndjson")
            with open(norm_path, "w", encoding="utf-8") as fh:
                for r in raw_rows:
                    rec = to_record(r, mapping, sid, default_state=row.get("state", ""))
                    if rec is None:
                        bad += 1
                        continue
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    good += 1
            if good:
                norm_files.append(norm_path)
            st.update({"raw_rows": len(raw_rows), "kept": good, "dropped_no_number": bad,
                       "mapping_missing_fields": missing,
                       "kept_ratio": round(good / len(raw_rows), 4) if raw_rows else 0,
                       "seconds": round(time.time() - t0, 1)})
            print(f"[{i}/{len(rows)}] {sid:28s} 原始 {len(raw_rows):>8,} → 合规则 {good:>8,}"
                  f" (丢弃 {bad:,}) {st['seconds']}s", flush=True)
        except Exception as e:
            st.update({"error": f"{type(e).__name__}: {str(e)[:160]}",
                       "seconds": round(time.time() - t0, 1)})
            print(f"[{i}/{len(rows)}] {sid:28s} ✗ {st['error'][:100]}", flush=True)
        stats.append(st)

    merged = os.path.join(out_dir, "merged.ndjson")
    report = merge_ndjson(norm_files, merged, os.path.join(out_dir, "merge_report.json"))
    summary = {"sources_tried": len(rows), "sources_ok": sum(1 for s in stats if "error" not in s),
               "requests": pol.stats, "merge": report, "per_source": stats,
               "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(os.path.join(out_dir, "stats.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources")
    ap.add_argument("--priorities")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default="out/pilot")
    ap.add_argument("--max-records", type=int, default=20000)
    ap.add_argument("--min-interval", type=float, default=0.8)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--log", default="out/request_log.jsonl")
    a = ap.parse_args(argv)
    rows = load_sources(a.sources, a.priorities, a.limit)
    if not rows:
        print("没有匹配的源", file=sys.stderr)
        return 2
    print(f"待采集源：{len(rows)} 个")
    s = run(rows, a.out, a.max_records, a.min_interval, a.concurrency, a.log)
    print(json.dumps({"sources_ok": s["sources_ok"], "requests": s["requests"],
                      "merge": s["merge"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
