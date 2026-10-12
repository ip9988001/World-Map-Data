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
MAX_ATTEMPTS = 8             # 连续**无进展**的下载尝试上限（P1-6）


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
        if man.get("error") or man.get("defect"):
            # 取数失败也要分诊（否则死源每次跑批都要再撞一次）：
            #   上游自报 not found / Invalid URL / 4xx → 源坏，拉黑
            #   DNS/SSL/超时 → TRANSIENT，下批重试（不拉黑）
            #   EMPTY/SHORT（第 6 步完整性对账）→ DOWNLOAD_DEFECT，补采
            from .triage import evidence_probe
            err = str(man.get("error") or "")
            low = err.lower()
            kind = "TRANSIENT"
            if man.get("defect"):
                kind = "DOWNLOAD_DEFECT"
                err = err or f"{man['defect']}_DOWNLOAD: 下载不完整（defect={man['defect']}）"
            elif low.startswith("empty_download") or low.startswith("short_download"):
                # 第 6 步下载完整性对账标记的缺陷：补采即可，不换规则、不拉黑
                kind = "DOWNLOAD_DEFECT"
            elif any(t in low for t in ("not found", "invalid url", "http error 404",
                                        "http error 410", "http error 403", "http error 401")):
                kind = "SOURCE_BROKEN"
            st.update({"done": False, "stage": "fetch", "kind": kind, "error": err,
                       "attempts": 1, "seconds": round(time.time() - t0, 1)})
            if kind == "SOURCE_BROKEN":
                ev = evidence_probe(row["url"])
                st["evidence"] = ev
                blacklist_add(sid, kind, f"取数失败：{err[:200]}",
                              json.dumps(ev, ensure_ascii=False)[:500])
            return st

    pref = preferred_engine(sid)
    # 级联也设墙钟上限：单源整体（下载 + 级联）不得无界膨胀，否则会吃穿泳道预算
    res = run_rules(sid, row, out_root, archive, limit=limit, preferred=pref,
                    cascade_seconds=max(120, int(max_seconds) - 60))
    st["rule_chain"] = [{k: v for k, v in c.items() if k != "drops"} for c in res["chain"]]
    if res["ok"]:
        st.update({"done": True, "stage": "parsed", "rule": res["rule"],
                   "kept": res["kept"], "fetched": res["fetched"],
                   "ratio": res["gate"]["ratio"], "path": res["path"],
                   # 第 9 步质量底数（源质量档案/分档的输入）
                   "verdict": res["gate"].get("verdict", "PASS"),
                   "grade_hint": res["gate"].get("grade_hint", "A"),
                   "quality": res["gate"].get("quality") or {},
                   "seconds": round(time.time() - t0, 1), "error": ""})
        # 完整性不达标且源级契约未声明豁免 → 进待造刀（退回第 3 步重造 spec），
        # 不算失败、也不删产物，但绝不允许当 A 级交付
        if res["gate"].get("kind") == "INCOMPLETE" and not res["gate"].get("exempt"):
            st["needs_rule"] = True
            st["reason"] = res["gate"].get("why", "")[:300]
        route_update(sid, res["rule"], True, res["gate"]["ratio"])
        return st

    if res.get("budget_exhausted"):
        # 级联被时间上限掐断（余下规则没试完）→ 不是「规则缺口」，下一窗口重试
        st.update({"done": False, "stage": "parsed", "kind": "TRANSIENT",
                   "reason": "规则级联触到单源时间上限，余下规则下一窗口再试",
                   "seconds": round(time.time() - t0, 1), "error": ""})
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
             "skipped_blacklist": 0, "skipped_exhausted": 0, "gap": 0, "errors": 0,
             "kept": 0, "fetched": 0, "downloaded": 0, "artifact_missing": 0}
    for k, item in enumerate(items, 1):
        sid = item["source_id"]
        prev = state.get(sid) or {}
        if prev.get("done"):
            # ponytail: 当前上限＝「账本 done + 产物 ndjson 在盘上」才算完成。
            # 只看账本会在产物被清理/换机器后永久卡死：采集全跳过、发布又拒发空包。
            art = prev.get("path")
            if art and os.path.exists(art):
                stats["skipped_done"] += 1
                continue
            stats["artifact_missing"] += 1
            print(f"[{k}/{len(items)}] {sid:28s} ♻ 账本 done 但产物缺失"
                  f"（{art or '无记录'}）→ 重采", flush=True)
        why = _skip_reason(sid, bl)
        if why:
            stats["skipped_blacklist"] += 1
            print(f"[{k}/{len(items)}] {sid:28s} ⏭ {why}", flush=True)
            continue
        # 重试上限（P1-6）：只惩罚「连续无进展的**下载**失败」——
        # 一个永远 0 字节/永远 DNS 失败的源，不许每窗口都来烧一遍预算。
        # 只对「本地档案也没下完」的源生效：解析阶段的重试（RULE_GAP 等）不受影响，
        # 慢但一直在长的源也不算（有进展就会重置计数，见下面的 grew 分支）。
        if (int(prev.get("attempts") or 0) >= MAX_ATTEMPTS and not prev.get("done")
                and not (prev.get("archive") or {}).get("done")):
            stats["skipped_exhausted"] += 1
            print(f"[{k}/{len(items)}] {sid:28s} ⏭ 已连续 {prev['attempts']} 次没进展"
                  f"（{prev.get('kind') or '?'}），挂起等人工：清掉 attempts 或被拉黑后才会再采",
                  flush=True)
            continue
        remain = effective - (time.time() - t0)
        if (stats["done"] + stats["gap"] + stats["errors"] or k > 1) and remain < MIN_SOURCE_TIME:
            print(f"[预算用尽] 已跑 {time.time() - t0:.0f}s，剩 {remain:.0f}s，"
                  f"下一个源 {sid} 交给下一段", flush=True)
            break
        row = table[item["i"]]
        # 磁盘护栏（运维事实，不是安全劝阻）：原始档只增不删，快满时必须停手并报告
        import shutil as _shutil
        free_gb = _shutil.disk_usage(".").free / 1024 ** 3
        if free_gb < 2.0:
            print(f"[磁盘护栏] 剩余 {free_gb:.1f}GB < 2GB，停止开新源（已完成的都已落盘）",
                  flush=True)
            stats["stopped_by_disk"] = True
            break
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
        # 有进展（原始档字节数变多）就不算「连续失败」：慢但一直在长的源不能被上限误杀
        if (int((st.get("archive") or {}).get("bytes") or 0)
                > int((prev.get("archive") or {}).get("bytes") or 0)):
            st["attempts"] = 1
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
            msg = st.get("reason") or st.get("error") or ""
            print(f"[{k}/{len(items)}] {sid:28s} ⚠ [{st['kind']}] {msg[:90]}", flush=True)
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


def main(argv=None) -> None:
    """批式跑一组源：python -m collectors.pipeline --list-file data/xxx.csv --max-seconds 5400"""
    import argparse
    import csv as _csv

    ap = argparse.ArgumentParser(description="统一采集流水线（下载→规则→分诊）")
    ap.add_argument("--list-file", default="", help="CSV，含 source_id 列（风险清单/重采清单）")
    ap.add_argument("--plan", default="", help="plan_batch 产出的泳道计划（配合 --lane）")
    ap.add_argument("--lane", type=int, default=-1, help="只跑该泳道（与 --plan 合用）")
    ap.add_argument("--sources", default="", help="逗号分隔的 source_id")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-seconds", type=int, default=3600)
    ap.add_argument("--per-source-max", type=int, default=420)
    ap.add_argument("--archive", default="raw_archive")
    ap.add_argument("--parsed", default="parsed")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--min-interval", type=float, default=0.8)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--http-timeout", type=int, default=20)
    ap.add_argument("--rules-limit", type=int, default=0, help="每源最多洗 N 行（试跑用）")
    ap.add_argument("--force-fetch", action="store_true")
    ap.add_argument("--dry", action="store_true", help="只看结果，不写账本/状态")
    ap.add_argument("--reclassify", action="store_true",
                    help="不联网抓数据，只把 state 里取数失败的源复核一遍"
                         "（DNS → DoH 判定：域名已死→拉黑；能解析→保留重试）")
    ap.add_argument("--recheck-blacklist", action="store_true",
                    help="把黑名单里的「域名已不存在」条目重新复核一遍，误拉的放回来")
    ap.add_argument("--order", choices=("list", "size", "size_desc"), default="list",
                    help="跑批顺序：list=按清单原序；size=小源先跑（预算内尽量多完成）；size_desc=大源先跑")
    a = ap.parse_args(argv)

    sids: list[str] = []
    sizes: dict[str, int] = {}
    plan_items: list[dict] = []
    if a.plan:
        plan = json.load(open(a.plan, encoding="utf-8"))
        lanes = plan.get("lanes") or []
        if a.lane >= 0:
            lanes = [L for L in lanes if L.get("lane") == a.lane]
        for L in lanes:
            plan_items += [{"source_id": it["source_id"], "i": it["i"]} for it in L["queue"]]
    if a.list_file:
        with open(a.list_file, encoding="utf-8-sig") as fh:
            for r in _csv.DictReader(fh):
                sid = (r.get("source_id") or r.get("source") or "").strip()
                if sid:
                    sids.append(sid)
                    try:
                        sizes[sid] = int(r.get("count") or 0)
                    except ValueError:
                        sizes[sid] = 0
    if a.sources:
        sids += [s.strip() for s in a.sources.split(",") if s.strip()]

    table = list(_csv.DictReader(open("data/源总表-执行序.csv", encoding="utf-8-sig")))
    idx = {r["source_id"]: i for i, r in enumerate(table)}
    if plan_items:
        items = [it for it in plan_items if it["source_id"] in idx]
        for it in items:
            it["i"] = idx[it["source_id"]]      # 以当前源总表为准重算行号
    else:
        items = []

    if a.recheck_blacklist:
        from urllib.parse import urlparse

        from .triage import BLACKLIST, BLACKLIST as _BL, dns_check
        rows = list(_csv.DictReader(open(_BL, encoding="utf-8-sig")))
        table = list(_csv.DictReader(open("data/源总表-执行序.csv", encoding="utf-8-sig")))
        url_by = {r["source_id"]: r.get("url", "") for r in table}
        freed, kept = [], []
        for r in rows:
            sid = r.get("source_id", "")
            reason = r.get("reason", "") + r.get("evidence", "")
            if "DoH" not in reason and "NXDOMAIN" not in reason:
                kept.append(r)
                continue
            host = urlparse(url_by.get(sid, "")).netloc
            ev = dns_check(host)
            if ev["verdict"] == "DEAD":
                kept.append(r)
            else:
                freed.append((sid, ev))
                print(f"  ↻ 放回 {sid:28s} {host} → {ev['verdict']}")
        if not a.dry:
            with open(_BL, "w", encoding="utf-8", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(kept)
            st = load_state(a.state)
            for sid, ev in freed:
                if sid in st:
                    st[sid]["kind"] = "TRANSIENT"
                    st[sid]["reason"] = f"黑名单复核放回（{ev['verdict']}）"
            save_state(st, a.state)
        print(f"黑名单复核：放回 {len(freed)}｜维持拉黑 {len([r for r in rows]) - len(freed)}")
        return

    if a.reclassify:
        from urllib.parse import urlparse

        from .triage import blacklist_add, dns_check
        state = load_state(a.state)
        dead = alive = unknown = 0
        for sid, st in state.items():
            if st.get("done") or st.get("kind") in ("SOURCE_BROKEN", "NOT_ADDRESS"):
                continue
            err = str(st.get("error") or "")
            if not any(t in err for t in ("Name or service", "No address associated",
                                          "Errno -2", "Errno -5", "Resolution")):
                continue
            row = table[idx[sid]] if sid in idx else {}
            host = urlparse(row.get("url", "")).netloc
            ev = dns_check(host)
            st["dns"] = ev
            if ev["verdict"] == "DEAD":
                st["kind"] = "SOURCE_BROKEN"
                st["reason"] = f"域名已不存在（DoH 判定 NXDOMAIN: {host}）"
                blacklist_add(sid, "SOURCE_BROKEN", st["reason"],
                              json.dumps(ev, ensure_ascii=False)[:400])
                dead += 1
                print(f"  ☠ {sid:28s} {host} → NXDOMAIN，已拉黑")
            elif ev["verdict"] == "ALIVE":
                st["kind"] = "TRANSIENT"
                st["reason"] = f"域名可解析（{ev['answers'][:1]}）但取数失败 → 下批重试"
                alive += 1
                print(f"  ↻ {sid:28s} {host} → 域名活着，保留重试")
            else:
                unknown += 1
        save_state(state, a.state)
        print(f"重判结果：死域名拉黑 {dead}｜域名活着待重试 {alive}｜无法判定 {unknown}")
        return

    if not sids and not plan_items:
        raise SystemExit("给 --list-file / --sources / --plan+--lane 其一")
    if sids:
        items = [{"source_id": s, "i": idx[s]} for s in sids if s in idx]
    missing = [s for s in sids if s not in idx]
    if missing:
        print(f"[警告] {len(missing)} 个源不在源总表里，已跳过（前 3: {missing[:3]}）", flush=True)
    if a.limit:
        items = items[: a.limit]
    elif not items and not plan_items:
        raise SystemExit("给 --list-file / --sources / --plan+--lane 其一")
    if a.order != "list" and sizes:
        items.sort(key=lambda it: sizes.get(it["source_id"], 0),
                   reverse=(a.order == "size_desc"))
    print(f"待跑 {len(items)} 个源，预算 {a.max_seconds}s，单源上限 {a.per_source_max}s，"
          f"顺序 {a.order}", flush=True)

    state = load_state(a.state)
    pol = Politeness(min_interval=a.min_interval, max_concurrency=a.concurrency,
                     max_retries=1, timeout=a.http_timeout,
                     log_path=os.path.join("out", "pipeline_log.jsonl"))
    stats = run_queue(items, table, state, pol, archive=a.archive, out_root=a.parsed,
                      max_seconds=a.max_seconds, limit=a.rules_limit,
                      state_path=a.state, force_fetch=a.force_fetch,
                      per_source_max=a.per_source_max)
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    print(report(state))


if __name__ == "__main__":
    main()
