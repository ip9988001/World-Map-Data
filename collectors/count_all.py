"""全量源计数扫描 —— 只取「记录数」，不拉数据。

用途：一次扫描全部源的可用性与真实规模，产出覆盖率地图。
礼貌性：每个源只发 1~2 个请求（元数据 + returnCountOnly），
        并在源总表里标注服务端上限，供正式采集时按 0.9 计算分页。

用法：
  python -m collectors.count_all --out out/counts.csv \
      --min-interval 0.8 --concurrency 3 [--priorities P0,P1]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import sys
import time

from .arcgis import layer_count, layer_meta, normalize_layer_url
from .politeness import Politeness


def load_rows(priorities=None, ids=None, limit=None):
    rows = list(csv.DictReader(open("data/源总表-执行序.csv", encoding="utf-8-sig")))
    if ids:
        want = {s.strip() for s in ids.split(",") if s.strip()}
        rows = [r for r in rows if r["source_id"] in want]
    elif priorities:
        wantp = {p.strip() for p in priorities.split(",")}
        rows = [r for r in rows if r["priority"] in wantp]
    return rows[:limit] if limit else rows


def probe(pol: Politeness, row: dict) -> dict:
    sid = row["source_id"]
    out = {"source_id": sid, "state": row["state"], "scope": row["scope"],
           "priority": row["priority"], "channel": row["channel"],
           "layer": row["layer"], "url": row["url"][:200]}
    t0 = time.time()
    try:
        if row["channel"] == "ARCGIS":
            u = normalize_layer_url(row["url"])
            meta = layer_meta(pol, u)
            n = layer_count(pol, u)
            out.update(status="OK", count=n,
                       server_max_record_count=meta.get("maxRecordCount"),
                       page_size_90pct=max(100, int((meta.get("maxRecordCount") or 2000) * 0.9)),
                       fields=len(meta.get("fields") or []), error="")
        else:
            # 文件型：只探可达性，不下载
            _, status = pol.fetch(row["url"], binary=True)
            out.update(status="OK" if status < 400 else "HTTP%d" % status, count="",
                       server_max_record_count="", page_size_90pct="", fields="", error="")
    except Exception as e:
        msg = str(e)[:120]
        st = "TIMEOUT" if "timed out" in msg.lower() else (
            "AUTH" if ("401" in msg or "403" in msg or "Token" in msg) else "FAIL")
        out.update(status=st, count="", server_max_record_count="", page_size_90pct="",
                   fields="", error=msg)
    out["ms"] = int((time.time() - t0) * 1000)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--priorities")
    ap.add_argument("--sources")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default="out/counts.csv")
    ap.add_argument("--min-interval", type=float, default=0.8)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--log", default="out/count_log.jsonl")
    a = ap.parse_args(argv)

    rows = load_rows(a.priorities, a.sources, a.limit)
    if not rows:
        print("没有匹配的源", file=sys.stderr)
        return 2
    print(f"待扫描源：{len(rows)} 个（每个源仅 1~2 个只读请求）", flush=True)

    pol = Politeness(min_interval=a.min_interval, max_concurrency=a.concurrency,
                     log_path=a.log)
    results = []
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {ex.submit(probe, pol, r): r for r in rows}
        for i, f in enumerate(concurrent.futures.as_completed(futs), 1):
            res = f.result()
            results.append(res)
            if i % 25 == 0 or i == len(rows):
                print(f"  {i}/{len(rows)}  已用 {int(time.time()-t0)}s  "
                      f"最近: {res['source_id']} {res['status']} {res['count']}", flush=True)

    order = {r["source_id"] + "|" + r["url"]: i for i, r in enumerate(rows)}
    results.sort(key=lambda r: order.get(r["source_id"] + "|" + r["url"], 0))

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    ok = [r for r in results if r["status"] == "OK"]
    withnum = [r for r in ok if str(r["count"]).isdigit()]
    total = sum(int(r["count"]) for r in withnum)
    summary = {
        "扫描源数": len(results),
        "可用": len(ok),
        "可用率": round(len(ok) / len(results), 4),
        "有记录数": len(withnum),
        "记录总数": total,
        "请求统计": pol.stats,
        "耗时秒": int(time.time() - t0),
    }
    with open(a.out.replace(".csv", "_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
