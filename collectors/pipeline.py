"""统一采集流水线 —— 一条线走完：下载(bronze) → 规则级联(silver) → 分诊。

替代 lane.py 里原来的硬编码二分支（ARCGIS / FILE_DOWNLOAD -> 一套固定解析）。
断点语义与旧 lane 一致，但断点粒度变成**源**：每个源要么完成、要么带着错误记录，
逐源落盘 progress/state.json，中断后不重不漏（原始档已在盘上，重跑只花 CPU）。

关键守恒律（2026-10-11 拍板）：
  · 原始档只增不删；解析失败**不触发重新下载**
  · 换规则 = 本地重跑；只有「下载残缺 / 源坏取证」才允许再碰网络
  · 黑名单里的源在复检日之前直接跳过（ENGINE_GAP/RULE_GAP 除外——它们等造规则）
"""
from __future__ import annotations

import json
import os
import time
from datetime import date

from .fetch_raw import archive_dir, fetch_raw, load_manifest
from .flatten import payload_files
from .local_cascade import diagnose, run_rules
from .normalize import parse_mapping_from_table
from .politeness import Politeness
from .triage import (blacklist_add, blacklisted, preferred_engine, report_md,
                     route_update, write_gap_report)

STATE = "progress/state.json"
MIN_SOURCE_TIME = 45          # 剩余预算低于此值不再开新源
TAIL_RESERVE = 60


def load_state(path: str = STATE) -> dict:
    if path and os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"[warn] state 读取失败（按空处理）: {e}", flush=True)
    return {}


def save_state(state: dict, path: str = STATE) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _skip_reason(sid: str, bl: dict) -> str:
    """黑名单过滤：真坏的源到复检日再说；缺口类的源等造规则，不被复检日拦住。"""
    rec = bl.get(sid)
    if not rec:
        return ""
    kind = rec.get("kind", "")
    if kind in ("ENGINE_GAP", "RULE_GAP"):
        return "缺口源，等造规则（不重试）"
    retest = rec.get("retest_after") or ""
    if retest and date.today().isoformat() < retest:
        return f"黑名单({kind})，{retest} 后复检"
    return ""            # 过了复检日 → 放行，重跑一次（本地重跑极便宜）


def collect_one(pol: Politeness, sid: str, row: dict, archive: str = "raw_archive",
                out_root: str = "parsed", limit: int = 0, force_fetch: bool = False,
                max_seconds: int = 900) -> dict:
    """单源一条线：原始档 → 规则级联 → （全败才）分诊。返回状态条目。"""
    t0 = time.time()
    st: dict = {"source_id": sid, "updated": time.strftime("%Y-%m-%d %H:%M:%S")}

    local_files = payload_files(archive_dir(archive, sid))
    man = load_manifest(archive, sid)
    if force_fetch or not local_files or not man.get("done"):
        man = fetch_raw(pol, sid, row["url"], archive, force=force_fetch,
                        budget_s=max(60, max_seconds - 30))
        st["archive"] = {"protocol": man.get("protocol"), "bytes": man.get("bytes", 0),
                         "done": man.get("done"), "error": man.get("error", "")}
        if man.get("error"):
            st.update({"done": False, "stage": "fetch", "error": man["error"],
                       "attempts": 1, "seconds": round(time.time() - t0, 1)})
            return st

    pref = preferred_engine(sid)
    res = run_rules(sid, row, out_root, archive, limit=limit, preferred=pref)
    st["rule_chain"] = [{k: v for k, v in c.items() if k != "drops"} for c in res["chain"]]
    if res["ok"]:
        st.update({"done": True, "stage": "parsed", "rule": res["rule"],
                   "kept": res["kept"], "fetched": res["fetched"],
                   "ratio": res["gate"]["ratio"], "path": res["path"],
                   "seconds": round(time.time() - t0, 1), "error": ""})
        route_update(sid, res["rule"], True, res["gate"]["ratio"])
        return st

    d = diagnose(sid, row, res, archive)
    st.update({"done": False, "stage": "triage", "kind": d["kind"],
               "reason": d["reason"][:300], "report": d["report"],
               "seconds": round(time.time() - t0, 1),
               "error": "" if d["kind"] in ("RULE_GAP", "NOT_ADDRESS",
                                            "SOURCE_BROKEN", "DOWNLOAD_DEFECT")
                        else d["reason"][:200]})
    if d["kind"] in ("SOURCE_BROKEN", "NOT_ADDRESS"):
        blacklist_add(sid, d["kind"], d["reason"],
                      json.dumps(d.get("evidence", {}), ensure_ascii=False)[:500])
    elif d["kind"] == "RULE_GAP":
        write_gap_report(sid, d["chain"], res.get("sample", []), d["kind"], d["reason"])
    return st


def run_queue(items: list[dict], table: list[dict], state: dict, pol: Politeness,
              archive: str = "raw_archive", out_root: str = "parsed",
              max_seconds: int = 960, limit: int = 0, state_path: str = STATE,
              force_fetch: bool = False, per_source_max: int = 420) -> dict:
    """跑一条队列（一个泳道的一段），到点自停；逐源落盘。"""
    t0 = time.time()
    effective = max_seconds - (TAIL_RESERVE if max_seconds > 180 else 0)
    bl = blacklisted()
    stats = {"lane_sources": len(items), "done": 0, "skipped_done": 0,
             "skipped_blacklist": 0, "gap": 0, "errors": 0, "kept": 0, "fetched": 0,
             "downloaded": 0}
    for k, item in enumerate(items, 1):
        sid = item["source_id"]
        prev = state.get(sid) or {}
        if prev.get("done"):
            stats["skipped_done"] += 1
            continue
        why = _skip_reason(sid, bl)
        if why:
            stats["skipped_blacklist"] += 1
            print(f"[{k}/{len(items)}] {sid:28s} ⏭ {why}", flush=True)
            continue
        remain = effective - (time.time() - t0)
        if (stats["done"] + stats["gap"] + stats["errors"] or k > 1) and remain < MIN_SOURCE_TIME:
            print(f"[预算用尽] 已跑 {time.time() - t0:.0f}s，剩 {remain:.0f}s，"
                  f"下一个源 {sid} 交给下一段", flush=True)
            break
        row = table[item["i"]]
        if not payload_files(archive_dir(archive, sid)):
            stats["downloaded"] += 1
        try:
            st = collect_one(pol, sid, row, archive, out_root, limit=limit,
                             force_fetch=force_fetch,
                             max_seconds=int(min(remain, per_source_max)) or per_source_max)
        except Exception as e:
            st = {"source_id": sid, "done": False, "stage": "crashed",
                  "error": f"{type(e).__name__}: {str(e)[:250]}",
                  "attempts": int(prev.get("attempts") or 0) + 1}
        st["attempts"] = int(prev.get("attempts") or 0) + (0 if st.get("done") else 1)
        state[sid] = st
        save_state(state, state_path)
        if st.get("done"):
            stats["done"] += 1
            stats["kept"] += st.get("kept", 0)
            stats["fetched"] += st.get("fetched", 0)
            print(f"[{k}/{len(items)}] {sid:28s} ✓ 规则 {st['rule']} "
                  f"{st['kept']:,}/{st['fetched']:,} = {st['ratio']:.0%} "
                  f"{st['seconds']}s", flush=True)
        elif st.get("kind"):
            stats["gap"] += 1
            print(f"[{k}/{len(items)}] {sid:28s} ⚠ [{st['kind']}] "
                  f"{st['reason'][:90]}", flush=True)
        else:
            stats["errors"] += 1
            print(f"[{k}/{len(items)}] {sid:28s} ✗ {str(st.get('error'))[:110]}", flush=True)
    stats["elapsed_s"] = round(time.time() - t0, 1)
    save_state(state, state_path)
    return stats


def report(state: dict, top: int = 10) -> str:
    """给状态出一份人读小报（哪些源完成、哪些缺口、哪些等造规则）。"""
    from collections import Counter
    kinds = Counter(v.get("kind", "ok") if not v.get("done") else "ok"
                    for v in state.values())
    lines = [f"共 {len(state)} 个源：完成 {kinds.get('ok', 0)}，"
             f"{'，'.join(f'{k} {n}' for k, n in kinds.items() if k != 'ok')}"]
    gaps = [v for v in state.values() if v.get("kind") == "RULE_GAP"]
    if gaps:
        lines.append("待造规则的源（按丢弃原因聚合）：")
        for v in gaps[:top]:
            lines.append(f"  · {v['source_id']}: {v.get('reason', '')[:90]}")
    return "\n".join(lines)
