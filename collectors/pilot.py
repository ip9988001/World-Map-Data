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


def load_job_file(path, index):
    """按 job 索引取一条既定的源。

    关键：必须回到「源总表」取**带字段映射的完整行**（任务文件里只有 id/url，
    若直接用任务文件的精简行，字段映射为空 → 整源会被误判为「无门牌号」）。
    同一 source_id 可能有多个图层，因此按 (source_id, url) 双重匹配。
    """
    jobs = json.load(open(path, encoding="utf-8"))
    j = jobs[int(index)]
    table = list(csv.DictReader(open(TABLE, encoding="utf-8-sig")))
    for r in table:
        if r["source_id"] == j["source_id"] and r["url"].rstrip("/") == j["url"].rstrip("/"):
            return [r]
    # 兜底：同名源
    for r in table:
        if r["source_id"] == j["source_id"]:
            return [r]
    raise SystemExit(f"未在源总表中找到 {j['source_id']} / {j['url'][:60]}")


def load_sources(ids=None, priorities=None, limit=None):
    rows = list(csv.DictReader(open(TABLE, encoding="utf-8-sig")))
    if ids:
        want = {s.strip() for s in ids.split(",") if s.strip()}
        rows = [r for r in rows if r["source_id"] in want]
    elif priorities:
        wantp = {p.strip() for p in priorities.split(",") if p.strip()}
        rows = [r for r in rows if r["priority"] in wantp]
    return rows[:limit] if limit else rows


def run(rows, out_dir, max_records, min_interval, concurrency, log_path, offset_start=0, oid_hi=None, oid_lo=None):
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
                info = fetch_layer(pol, row["url"], raw_path, max_records=max_records,
                                   offset_start=offset_start, oid_hi=oid_hi, oid_lo=oid_lo)
                st.update(info)

                def _raw_iter(path=raw_path):
                    with open(path, encoding="utf-8") as fh:
                        for l in fh:
                            if l.strip():
                                yield json.loads(l)
                raw_stream = _raw_iter()
            else:
                info = fetch_file(pol, row["url"], os.path.join(raw_dir, safe))
                st.update({k: v for k, v in info.items() if k != "members"})
                st["members"] = len(info.get("members", []))
                raw_stream = iter_rows(info)

            # 流式清洗：边读边写，不在内存里囤全量（大源数百万条会撑爆 + 被回收即全丢）
            good, bad, n_raw, missing = 0, 0, 0, []
            norm_path = os.path.join(norm_dir, safe + ".ndjson")
            with open(norm_path, "w", encoding="utf-8") as fh:
                for r in raw_stream:
                    n_raw += 1
                    if n_raw == 1:
                        missing = mapping_sanity(r, mapping)
                    if n_raw % 200000 == 0:
                        print(f"    ... {sid} 已清洗 {n_raw:,}", flush=True)
                    rec = to_record(r, mapping, sid, default_state=row.get("state", ""))
                    if rec is None:
                        bad += 1
                        continue
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    good += 1
            if good:
                norm_files.append(norm_path)
            st.update({"raw_rows": n_raw, "kept": good, "dropped_no_number": bad,
                       "mapping_missing_fields": missing,
                       "kept_ratio": round(good / n_raw, 4) if n_raw else 0,
                       "seconds": round(time.time() - t0, 1)})
            print(f"[{i}/{len(rows)}] {sid:28s} 原始 {n_raw:>8,} → 合规则 {good:>8,}"
                  f" (丢弃 {bad:,}) {st['seconds']}s", flush=True)
        except Exception as e:
            st.update({"error": f"{type(e).__name__}: {str(e)[:600]}",
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
    ap.add_argument("--job-file")
    ap.add_argument("--job-index")
    ap.add_argument("--sources")
    ap.add_argument("--priorities")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default="out/pilot")
    ap.add_argument("--max-records", type=int, default=20000)
    ap.add_argument("--oid-hi", type=int, default=None)
    ap.add_argument("--oid-lo", type=int, default=None)
    ap.add_argument("--offset-start", type=int, default=0,
                    help="从服务端第 N 条开始取（大源并行分块用）")
    ap.add_argument("--min-interval", type=float, default=0.8)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--log", default="out/request_log.jsonl")
    a = ap.parse_args(argv)
    if a.job_file and a.job_index is not None:
        rows = load_job_file(a.job_file, a.job_index)
    else:
        rows = load_sources(a.sources, a.priorities, a.limit)
    if not rows:
        print("没有匹配的源", file=sys.stderr)
        return 2
    print(f"待采集源：{len(rows)} 个")
    s = run(rows, a.out, a.max_records, a.min_interval, a.concurrency, a.log, a.offset_start, a.oid_hi, a.oid_lo)
    print(json.dumps({"sources_ok": s["sources_ok"], "requests": s["requests"],
                      "merge": s["merge"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
