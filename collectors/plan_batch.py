"""按「主机桶」把待采源分配给泳道，产出打包式分派计划（plan JSON）。

为什么要这样分派（对比现状）：
  现状 = 每个 16 分钟 stage 只采 1 个源。而全表 88% 的源规模 < 20 万条，
  小源在 0.8s/请求下 45 秒就采完 → 槽位利用率约 5%，算力几乎全在空转。
  本模块改为：**一个泳道领一条「主机 → 源」队列**，段内连采多个源直到时间预算用尽。

礼貌约束（硬性）：
  · **每个主机（hostname）只允许归属一个泳道** → 同一主机永远只有一条请求流，
    天然满足「单主机 ≥0.8s/请求」；跨泳道不会打到同一个主机。
  · 泳道在主机之间切换时复用同一个 Politeness 实例，间隔按主机各自记账。

调度算法：按主机总耗时做 LPT（最长优先）贪心分配到 N 条泳道，
使最慢泳道的总时长尽量小（makespan 最小化）。

用法：
  python collectors/plan_batch.py --lanes 16 --out out/plan.json
  python collectors/plan_batch.py --lanes 16 --state state/state.json --out out/plan.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
from collections import defaultdict

MIN_INTERVAL = 0.8          # 单主机最小请求间隔（秒）
META_REQUESTS = 3           # 元数据 + 计数 + 余量的固定请求数
DEFAULT_PAGE = 1800         # 兜底分页大小（服务端上限 × 0.9 的实测常见值）


def load_sources(table: str, counts: str, exclude: str = "", blacklist: str = "") -> list[dict]:
    """合并「源总表」与「源计数」，只保留可用且未被剔除的源。

    blacklist（★ 2026-10-11 新增）：把「真坏 / 不适用」的源在**排兵阶段**就剔掉，别让队列虚胖。
    缺口类（ENGINE_GAP / RULE_GAP）**不剔** —— 它们需要重采/重打包，要留在队列里。
    """
    cnt: dict[str, dict] = {}
    with open(counts, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            cnt[r["source_id"]] = r
    skip: set[str] = set()
    if exclude and os.path.exists(exclude):
        with open(exclude, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r.get("source_id"):
                    skip.add(r["source_id"])
    today = time.strftime("%Y-%m-%d")
    n_bl = 0
    if blacklist and os.path.exists(blacklist):
        with open(blacklist, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                sid = r.get("source_id") or ""
                if not sid or sid in skip:
                    continue
                kind = r.get("kind") or ""
                retest = (r.get("retest_after") or "").strip()
                if kind in ("ENGINE_GAP", "RULE_GAP"):
                    continue                      # 缺口源留着等造规则 / 重采
                if retest and today < retest:
                    skip.add(sid)
                    n_bl += 1
    if n_bl:
        print(f"黑名单提前剔除 {n_bl} 个源（真坏/不适用，未到复检日）")
    out = []
    with open(table, encoding="utf-8-sig") as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            sid = r["source_id"]
            if sid in skip:
                continue
            c = cnt.get(sid)
            if not c or c.get("status") != "OK":
                continue
            try:
                n = int(float(c.get("count") or 0))
            except ValueError:
                n = 0
            if n <= 0:
                continue
            ps = 0
            try:
                ps = int(float(c.get("page_size_90pct") or 0))
            except ValueError:
                ps = 0
            out.append({"i": i, "source_id": sid, "state": r.get("state", ""),
                        "priority": r.get("priority", ""), "channel": r.get("channel", ""),
                        "url": r.get("url", ""), "count": n, "page_size": ps or DEFAULT_PAGE})
    return out


def bucket_of(url: str) -> str:
    """主机桶 = hostname（小写）。这是礼貌预算的记账单位。"""
    try:
        return urllib.parse.urlsplit(url).netloc.lower() or "(no-url)"
    except Exception:
        return "(no-url)"


def est_seconds(src: dict, interval: float = MIN_INTERVAL) -> float:
    """估算一个源采完需要的秒数（固定开销 + 分页数 × 间隔）。"""
    pages = math.ceil(src["count"] / max(100, src["page_size"]))
    return META_REQUESTS * interval + pages * interval


def build_lanes(sources: list[dict], lanes: int, interval: float = MIN_INTERVAL) -> list[dict]:
    """LPT：按主机总耗时从大到小，依次丢给当前最闲的泳道。"""
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for s in sources:
        by_bucket[bucket_of(s["url"])].append(s)
    buckets = []
    for h, items in by_bucket.items():
        # 同主机内：优先级在前，其次小源在前（小源先清掉，队列尾部留大源）
        items.sort(key=lambda x: (x["priority"], x["count"], x["source_id"]))
        buckets.append({"host": h, "sources": items,
                        "est_s": sum(est_seconds(x, interval) for x in items),
                        "rows": sum(x["count"] for x in items)})
    buckets.sort(key=lambda b: -b["est_s"])
    out = [{"lane": k, "hosts": [], "sources": 0, "rows": 0, "est_s": 0.0} for k in range(lanes)]
    for b in buckets:
        tgt = min(out, key=lambda x: x["est_s"])
        tgt["hosts"].append(b["host"])
        tgt["sources"] += len(b["sources"])
        tgt["rows"] += b["rows"]
        tgt["est_s"] += b["est_s"]
        tgt.setdefault("_queue", []).extend(
            [{"i": s["i"], "source_id": s["source_id"], "state": s["state"],
              "channel": s["channel"], "url": s["url"], "count": s["count"],
              "page_size": s["page_size"], "host": b["host"],
              "est_s": round(est_seconds(s, interval), 1)} for s in b["sources"]])
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="data/源总表-执行序.csv")
    ap.add_argument("--counts", default="data/源计数全量.csv")
    ap.add_argument("--exclude", default="data/剔除源-非门牌点.csv")
    ap.add_argument("--blacklist", default="data/源黑名单.csv",
                    help="真坏/不适用源提前剔除（缺口类保留在队列里）")
    ap.add_argument("--lanes", type=int, default=16)
    ap.add_argument("--interval", type=float, default=MIN_INTERVAL)
    ap.add_argument("--state", default="", help="已完成状态（{source_id: {...}}），用于跳过")
    ap.add_argument("--out", default="out/plan.json")
    a = ap.parse_args(argv)

    src = load_sources(a.table, a.counts, a.exclude, a.blacklist)
    done: set[str] = set()
    if a.state and os.path.exists(a.state):
        st = json.load(open(a.state, encoding="utf-8"))
        done = {k for k, v in st.items() if isinstance(v, dict) and v.get("done")}
    todo = [s for s in src if s["source_id"] not in done]

    lanes = build_lanes(todo, a.lanes, a.interval)
    plan = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lanes_n": a.lanes, "interval": a.interval,
            "sources_n": len(todo), "rows_n": sum(s["count"] for s in todo),
            "skipped_done": len(src) - len(todo),
            "lanes": [{k: v for k, v in L.items() if not k.startswith("_")} | {"queue": L["_queue"]}
                      for L in lanes]}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False)

    print(f"可采源 {len(todo)}（跳过已完成 {len(src) - len(todo)}）  总条数 {plan['rows_n']:,}")
    hs = defaultdict(lambda: [0, 0.0, 0])
    for L in lanes:
        for s in L["_queue"]:
            hs[s["host"]][0] += 1
            hs[s["host"]][1] += s["est_s"]
            hs[s["host"]][2] += s["count"]
    print(f"主机桶 {len(hs)} 个（最大 5 个占源数 {sum(sorted((v[0] for v in hs.values()), reverse=True)[:5])} 个）")
    print("最重 6 个主机：")
    for h, (n, sec, rows) in sorted(hs.items(), key=lambda x: -x[1][1])[:6]:
        print(f"  {h:34s} 源 {n:4d}  行 {rows:>12,}  估时 {sec/3600:6.2f} h")
    print("各泳道预估（makespan 看最大值）：")
    for L in sorted(lanes, key=lambda x: -x["est_s"])[:6]:
        print(f"  lane {L['lane']:2d}  主机 {len(L['hosts']):3d}  源 {L['sources']:4d}  "
              f"行 {L['rows']:>12,}  估时 {L['est_s']/3600:6.2f} h")
    mk = max(L["est_s"] for L in lanes)
    print(f"\nmakespan ≈ {mk/3600:.2f} h（16 泳道并行）；"
          f"按每天 10 小时窗口 → 约 {math.ceil(mk/3600/10)} 天")
    print(f"计划已写入 {a.out}")


if __name__ == "__main__":
    main()
