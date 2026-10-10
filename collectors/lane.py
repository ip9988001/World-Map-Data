"""泳道执行器：一段之内连续采集「一条队列里的多个源」，到点自停。

与 pilot 的区别：
  pilot  = 一次采一个源（每次启动都有固定开销，小源导致槽位利用率 ~5%）
  lane   = 一次领一整条队列，采完一个源立刻接着采下一个，直到时间预算用尽
           → 槽位利用率 ~95%

断点语义（关键，别搞错）：
  · 每个源采完即写入 state.json：{source_id: {done, last_oid, fetched, kept, drops}}
  · 正常到点停 → 本段的数据与 state 一起上传为 artifact；下一段读 state 继续
  · 段被运行器回收 → 本段 artifact 全丢 → state 没推进 → 下一段从上一段的状态重做（不丢数据）
  · 源采到一半被预算打断 → state 记 last_oid（done=false），下一段从 last_oid+1 接着采，
    已采部分在上一个成功的 artifact 里，合并时拼接即可（不重不漏）

礼貌：整个段复用同一个 Politeness 实例 → 每主机间隔按主机各自记账，跨源不会变快。

用法：
  python -m collectors.lane --plan out/plan.json --lane 3 --max-seconds 960 \
      --out out/seg --state-in state/merged.json --state-out out/seg/state.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time

from .arcgis import fetch_layer
from .filedl import fetch_file, iter_rows
from .normalize import mapping_sanity, parse_mapping_from_table, to_record_ex
from .politeness import Politeness

TABLE = "data/源总表-执行序.csv"
TAIL_RESERVE = 60      # 段尾留给打包/上传的余量（秒）
MIN_SOURCE_TIME = 60   # 剩余预算低于这个值就不再开新源


def load_table() -> list[dict]:
    return list(csv.DictReader(open(TABLE, encoding="utf-8-sig")))


def load_state(path: str) -> dict:
    if path and os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"[warn] state 读取失败（按空处理）: {e}", flush=True)
    return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _iter_raw(path: str):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def run_lane(items: list[dict], table: list[dict], state: dict, out_dir: str,
             min_interval: float, concurrency: int, max_seconds: int,
             log_path: str, http_timeout: int = 20, max_retries: int = 1,
             per_source_max: int = 420) -> dict:
    # 批模式下必须"快速失败"：默认 60s 超时 × 5 次重试能让一个死源吃掉整个段
    pol = Politeness(min_interval=min_interval, max_concurrency=concurrency,
                     max_retries=max_retries, timeout=http_timeout, log_path=log_path)
    norm_dir = os.path.join(out_dir, "normalized")
    raw_dir = os.path.join(out_dir, "raw")
    os.makedirs(norm_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)
    state_path = os.path.join(out_dir, "state.json")

    t0 = time.time()
    # 预算很小时不扣尾部余量，并且**每段至少尝试一个源**（否则永远开不了工）
    effective = max_seconds - (TAIL_RESERVE if max_seconds > 180 else 0)
    stats = {"lane_sources": len(items), "done_this_stage": 0, "skipped_done": 0,
             "skipped_dead": 0, "partial": 0, "errors": 0, "kept": 0, "fetched": 0}

    for k, item in enumerate(items, 1):
        sid = item["source_id"]
        prev = state.get(sid) or {}
        if prev.get("done"):
            stats["skipped_done"] += 1
            continue
        if int(prev.get("attempts") or 0) >= 3 and prev.get("error"):
            stats["skipped_dead"] += 1
            continue
        used = time.time() - t0
        remain = effective - used
        if (stats["done_this_stage"] + stats["partial"] + stats["errors"]) and remain < MIN_SOURCE_TIME:
            print(f"[预算用尽] 已跑 {used:.0f}s，剩 {remain:.0f}s，"
                  f"下一个源 {sid} 交给下一段", flush=True)
            break

        row = table[item["i"]]
        mapping = parse_mapping_from_table(row)
        safe = sid.replace("/", "__")
        raw_path = os.path.join(raw_dir, safe + ".ndjson")
        lo = prev.get("last_oid")
        oid_lo = (int(lo) + 1) if lo else None          # 续采：从上次断点 +1
        # 续采段单独成文件（__from<oid>），避免覆盖上一段的半成品
        norm_path = os.path.join(norm_dir, safe + (f"__from{oid_lo}" if oid_lo else "") + ".ndjson")
        st = {"source_id": sid, "host": item.get("host", ""), "channel": row.get("channel", ""),
              "count": item.get("count"), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        ts = time.time()
        try:
            if row.get("channel") == "ARCGIS":
                info = fetch_layer(pol, item["url"], raw_path, max_records=0,
                                   page_size=item.get("page_size") or None, offset_start=0,
                                   oid_lo=oid_lo,
                                   max_seconds=int(min(remain, per_source_max)))
                stream = _iter_raw(raw_path)
            else:
                info = fetch_file(pol, item["url"], os.path.join(raw_dir, safe))
                stream = iter_rows(info)
                info = {k2: v for k2, v in info.items() if k2 != "members"}

            good = bad = n_raw = 0
            drops: dict[str, int] = {}
            missing: list[str] = []
            with open(norm_path, "w", encoding="utf-8") as fh:
                for r in stream:
                    n_raw += 1
                    if n_raw == 1:
                        missing = mapping_sanity(r, mapping)
                    rec, reason = to_record_ex(r, mapping, sid, default_state=row.get("state", ""))
                    if rec is None:
                        bad += 1
                        drops[reason] = drops.get(reason, 0) + 1
                        continue
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    good += 1

            is_done = bool(info.get("done", True))
            st.update({"last_oid": info.get("last_oid") or prev.get("last_oid"),
                       "done": is_done, "fetched": n_raw, "kept": good,
                       "dropped": bad, "drops": drops,
                       "mapping_missing_fields": missing,
                       "seconds": round(time.time() - ts, 1),
                       "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
            stats["kept"] += good
            stats["fetched"] += n_raw
            if is_done:
                stats["done_this_stage"] += 1
            else:
                stats["partial"] += 1
            print(f"[{k}/{len(items)}] {sid:28s} 原始 {n_raw:>9,} → 合规 {good:>9,} "
                  f"(丢弃 {bad:,}) {st['seconds']}s {'✓完成' if is_done else '⏸中断(续采)'}",
                  flush=True)
        except Exception as e:
            stats["errors"] += 1
            st.update({"error": f"{type(e).__name__}: {str(e)[:300]}", "done": False,
                       "attempts": int(prev.get("attempts") or 0) + 1,
                       "seconds": round(time.time() - ts, 1),
                       "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
            print(f"[{k}/{len(items)}] {sid:28s} ✗ {st['error'][:110]}", flush=True)

        state[sid] = st
        save_state(state_path, state)      # 逐源落盘：段内进度一眼可见
        if not st.get("done") and remain - st.get("seconds", 0) < MIN_SOURCE_TIME:
            print("[预算用尽] 源未完成但时间不够，交给下一段续采", flush=True)
            break

    stats["elapsed_s"] = round(time.time() - t0, 1)
    save_state(state_path, state)
    with open(os.path.join(out_dir, "stage_stats.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=1)
    return stats


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--lane", type=int, required=True)
    ap.add_argument("--out", default="out/seg")
    ap.add_argument("--state-in", default="")
    ap.add_argument("--max-seconds", type=int, default=960)
    ap.add_argument("--min-interval", type=float, default=0.8)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="只处理队列前 N 个源（冒烟测试用）")
    ap.add_argument("--http-timeout", type=int, default=20)
    ap.add_argument("--max-retries", type=int, default=1)
    ap.add_argument("--per-source-max", type=int, default=420,
                    help="单个源最多占用本段的秒数（防死源吃掉整段）")
    a = ap.parse_args(argv)

    plan = json.load(open(a.plan, encoding="utf-8"))
    lane = next((L for L in plan["lanes"] if L["lane"] == a.lane), None)
    if lane is None:
        raise SystemExit(f"计划里没有 lane {a.lane}")
    items = lane["queue"]
    if a.limit:
        items = items[:a.limit]
    print(f"lane {a.lane}：队列 {len(items)} 个源，预算 {a.max_seconds}s，"
          f"预算内预计可采 {sum(i['est_s'] for i in items):.0f}s 的工作量", flush=True)

    state = load_state(a.state_in)
    stats = run_lane(items, load_table(), state, a.out, a.min_interval, a.concurrency,
                     a.max_seconds, os.path.join(a.out, "request_log.jsonl"),
                     a.http_timeout, a.max_retries, a.per_source_max)
    print(json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
