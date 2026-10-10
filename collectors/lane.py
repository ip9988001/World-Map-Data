"""泳道执行器（v2）—— 一条队列跑到底：下载(bronze) → 本地规则级联(silver) → 分诊。

与 v1 的区别（2026-10-11 拍板）：
  v1：按 channel 硬编码二分支（ARCGIS 走 arcgis、其余走文件下载）+ 固定一套映射解析。
  v2：下载只剩一条主路（无规则全量落 raw_archive/，只增不删），解析清洗全在本地，
      多套规则级联、失败换规则而不是重下载。全部实现在 pipeline.run_queue。

断点语义（与 v1 一致）：
  · 每个源跑完即写 progress/state.json：{done, stage, rule, kept, ratio, kind, ...}
  · 到点自停 → 下一段读 state 继续；被回收 → state 没推进 → 下一段重做（不丢数据）
  · 原始档已在盘上，重跑一个源只花 CPU（几秒），不再重新下载

礼貌：整段复用同一个 Politeness 实例 → 每主机间隔按主机各自记账，跨源不会变快。

用法：
  python -m collectors.lane --plan out/plan.json --lane 3 --max-seconds 960 \
      --out out/seg --state-in progress/state.json --state-out progress/state.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os

from .pipeline import load_state, run_queue, save_state
from .politeness import Politeness

TABLE = "data/源总表-执行序.csv"


def load_table() -> list[dict]:
    return list(csv.DictReader(open(TABLE, encoding="utf-8-sig")))


def run_lane(items: list[dict], table: list[dict], state: dict, out_dir: str,
             min_interval: float, concurrency: int, max_seconds: int,
             log_path: str, http_timeout: int = 20, max_retries: int = 1,
             per_source_max: int = 420, limit: int = 0,
             archive: str = "raw_archive", out_root: str = "parsed",
             state_path: str = "", force_fetch: bool = False) -> dict:
    """泳道执行：委托给 pipeline.run_queue（下载→规则→分诊一条线）。"""
    pol = Politeness(min_interval=min_interval, max_concurrency=concurrency,
                     max_retries=max_retries, timeout=http_timeout, log_path=log_path)
    return run_queue(items, table, state, pol, archive=archive, out_root=out_root,
                     max_seconds=max_seconds, limit=limit,
                     per_source_max=per_source_max,
                     state_path=state_path or os.path.join(out_dir, "state.json"),
                     force_fetch=force_fetch)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--lane", type=int, required=True)
    ap.add_argument("--out", default="out/seg")
    ap.add_argument("--state-in", default="")
    ap.add_argument("--state-out", default="progress/state.json")
    ap.add_argument("--max-seconds", type=int, default=960)
    ap.add_argument("--min-interval", type=float, default=0.8)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="只处理队列前 N 个源（冒烟测试用）")
    ap.add_argument("--http-timeout", type=int, default=20)
    ap.add_argument("--max-retries", type=int, default=1)
    ap.add_argument("--per-source-max", type=int, default=420,
                    help="单个源最多占用本段的秒数（防死源吃掉整段）")
    ap.add_argument("--archive", default="raw_archive", help="原始档根目录（只增不删）")
    ap.add_argument("--parsed", default="parsed", help="规则产物根目录")
    ap.add_argument("--force-fetch", action="store_true", help="已有原始档也重抓（慎用）")
    a = ap.parse_args(argv)

    plan = json.load(open(a.plan, encoding="utf-8"))
    lane = next((L for L in plan["lanes"] if L["lane"] == a.lane), None)
    if lane is None:
        raise SystemExit(f"计划里没有 lane {a.lane}")
    items = lane["queue"]
    if a.limit:
        items = items[:a.limit]
    print(f"lane {a.lane}：队列 {len(items)} 个源，预算 {a.max_seconds}s，"
          f"预算内预计工作量 {sum(i['est_s'] for i in items):.0f}s", flush=True)

    state = load_state(a.state_in or a.state_out)
    stats = run_lane(items, load_table(), state, a.out, a.min_interval, a.concurrency,
                     a.max_seconds, os.path.join(a.out, "request_log.jsonl"),
                     a.http_timeout, a.max_retries, a.per_source_max, a.limit,
                     a.archive, a.parsed, a.state_out, a.force_fetch)
    save_state(state, a.state_out)
    print(json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
