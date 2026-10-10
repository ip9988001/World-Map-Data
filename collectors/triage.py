"""全败分诊 —— 人只看这一份队列，只做两种判决。

判决口径（三选一，不猜）：
  · SOURCE_BROKEN  源本身坏：4xx/5xx/空响应/错误页/封禁/空图层  → 拉黑，不再管
  · NOT_ADDRESS    源能取到数据，但原始字段里根本没有门牌点味（addr_hint 低）
                   → 它本来就不是门牌点源 → 拉黑（标注原因不同，留着以防误杀）
  · ENGINE_GAP     源能取到数据、字段有门牌点味，但没有一把刀能把它摊平/映射
                   → **造新刀**（新引擎文件 + 注册表加一行），绝不改老刀
  · TRANSIENT      全部失败都是超时/连接错误 → 不算判决，下次重试

产出：
  data/源黑名单.csv             黑洞名单（含证据、首见时间、复检日期）
  data/引擎路由表.csv           源→引擎 成功/失败账本（下次直接用成功的刀）
  out/triage/待造刀-<源>.json   缺口源的原始样本 + 失败链（造刀依据）
  out/triage/报告.md            人读汇总

用法：
  python -m collectors.triage --source us/ms/lauderdale     # 现场跑一次并分诊
  python -m collectors.triage --from-state out/seg/state.json  # 扫全段状态里的全败源
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import datetime, timedelta

from .cascade import (TABLE, VERDICT_BROKEN, VERDICT_EMPTY, VERDICT_FETCH,
                      VERDICT_GATE, VERDICT_NOTMEAT, VERDICT_PASS, run_source)
from .normalize import parse_mapping_from_table
from .politeness import Politeness

BLACKLIST = "data/源黑名单.csv"
ROUTES = "data/引擎路由表.csv"
TRIAGE_DIR = "out/triage"
RETEST_DAYS = 30

BL_COLS = ["source_id", "kind", "reason", "evidence", "first_seen", "retest_after", "hits"]
RT_COLS = ["source_id", "engine", "wins", "losses", "last_ok", "last_ratio", "note"]


# ── 判决 ───────────────────────────────────────────────────
def classify(chain: list[dict], gate: dict | None = None) -> tuple[str, str]:
    """(kind, reason)。kind ∈ SOURCE_BROKEN / NOT_ADDRESS / ENGINE_GAP / TRANSIENT / PASS"""
    if not chain:
        return "TRANSIENT", "没有引擎出场（注册表空？）"
    pf_gate = next((c for c in chain if c.get("engine") == "__preflight__"
                    and c.get("verdict") == VERDICT_GATE), None)
    if pf_gate:
        return "ENGINE_GAP", "下载前侦察判定形态未覆盖：" + str(pf_gate.get("detail", ""))[:200]
    pf_fail = next((c for c in chain if c.get("engine") == "__preflight__"
                    and c.get("verdict") == VERDICT_FETCH), None)
    if pf_fail:
        return "TRANSIENT", str(pf_fail.get("detail", ""))[:200]
    verdicts = [c.get("verdict") for c in chain]
    if VERDICT_PASS in verdicts:
        return "PASS", "有刀切开"
    # 取到过数据吗？
    got_rows = [c for c in chain if c.get("rows")]
    if not got_rows:
        if all(v in (VERDICT_BROKEN,) for v in verdicts):
            return "SOURCE_BROKEN", chain[-1].get("detail", "全引擎判定源坏")
        if any(v == VERDICT_BROKEN for v in verdicts):
            return "SOURCE_BROKEN", next(c.get("detail", "") for c in chain
                                         if c.get("verdict") == VERDICT_BROKEN)
        if any(v == VERDICT_NOTMEAT for v in verdicts):
            # 有数据但所有刀都不认形态 —— 也是缺口，但没有样本，只能给理由
            return "ENGINE_GAP", "有响应但没有任何刀能识别形态：" + \
                "; ".join(c.get("detail", "")[:80] for c in chain
                          if c.get("verdict") == VERDICT_NOTMEAT)[:300]
        if all(v == VERDICT_FETCH for v in verdicts):
            return "TRANSIENT", chain[-1].get("detail", "网络失败")
        return "TRANSIENT", "失败链无定论"
    hints = [c["gate"]["addr_hint"] for c in got_rows if c.get("gate")]
    hint = max(hints) if hints else 0.0
    best = max(got_rows, key=lambda c: (c["gate"]["kept"] if c.get("gate") else 0))
    g = best.get("gate") or {}
    if hint < 0.34 and (g.get("kept") or 0) == 0:
        return "NOT_ADDRESS", f"能取到数据但无门牌点字段（addr_hint={hint}，键：{list((g.get('drops') or {}).keys())[:3]}）"
    return "ENGINE_GAP", (f"有数据、有门牌点味（addr_hint={hint}）但映射不上：{g.get('why', '')}；"
                          f"缺字段 {g.get('mapping_missing', [])}")


# ── 独立复核（关键：不信引擎自述）────────────────────────
def evidence_probe(url: str, timeout: int = 20) -> dict:
    """分诊自己抓一次原始响应当证据。

    为什么要这一步：引擎说「源坏」是**它的自述**——它自己可能就不会切这种形态。
    拉黑一个源的代价是不可逆的（30 天内不再采），所以拉黑前必须有一次独立取证；
    只要原始响应里有数据（json/csv/zip/xml 且 >0 字节），就不准判源坏，
    一律归「我们的刀不够」→ ENGINE_GAP。
    """
    import urllib.error
    import urllib.request
    ev = {"url": url, "status": 0, "bytes": 0, "content_type": "", "head": "",
          "verdict_hint": ""}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; address-factory/1.0)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read(8192)
            ev.update(status=r.status, bytes=len(b),
                      content_type=r.headers.get("Content-Type", ""),
                      head=b[:200].decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        ev.update(status=e.code, head=str(e.reason)[:200])
    except Exception as e:
        ev.update(status=0, head=f"{type(e).__name__}: {str(e)[:150]}")
    st, ct, h = ev["status"], ev["content_type"].lower(), ev["head"]
    dataish = st == 200 and ev["bytes"] > 0 and any(
        t in ct for t in ("json", "csv", "zip", "xml", "octet", "text/plain"))
    if "Invalid URL" in h:
        ev["verdict_hint"] = "SOURCE_BROKEN: 上游自报 Invalid URL（服务已下线/改名）"
    elif st in (404, 410):
        ev["verdict_hint"] = f"SOURCE_BROKEN: {st} 资源不存在"
    elif st in (401, 403):
        ev["verdict_hint"] = f"SOURCE_BROKEN: {st} 需鉴权/被封"
    elif dataish:
        ev["verdict_hint"] = "DATA_PRESENT: 源有数据 → 是我们的刀不够（ENGINE_GAP），不许拉黑"
    elif st == 0:
        ev["verdict_hint"] = "TRANSIENT: 网络层没通"
    else:
        ev["verdict_hint"] = f"UNSURE: {st} {ev['content_type']} {ev['bytes']}B"
    return ev


def apply_evidence(kind: str, reason: str, ev: dict) -> tuple[str, str]:
    """用独立证据纠正引擎自述的判决。"""
    hint = ev.get("verdict_hint", "")
    if kind == "SOURCE_BROKEN" and hint.startswith("DATA_PRESENT"):
        return "ENGINE_GAP", f"[复核推翻源坏] {hint}｜引擎自述：{reason}"
    if kind in ("ENGINE_GAP", "NOT_ADDRESS") and hint.startswith("SOURCE_BROKEN"):
        return "SOURCE_BROKEN", f"[复核] {hint}｜引擎自述：{reason}"
    return kind, reason


# ── 账本 ───────────────────────────────────────────────────
def _upsert_csv(path: str, cols: list[str], key: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows: list[dict] = []
    if os.path.exists(path):
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    hit = False
    for r in rows:
        if r.get(key) == row.get(key):
            r.update({k: str(v) for k, v in row.items() if v is not None})
            hit = True
            break
    if not hit:
        rows.append({c: str(row.get(c, "")) for c in cols})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def blacklist_add(sid: str, kind: str, reason: str, evidence: str) -> None:
    now = datetime.now()
    rows = []
    if os.path.exists(BLACKLIST):
        rows = list(csv.DictReader(open(BLACKLIST, encoding="utf-8-sig")))
    hits = 1
    for r in rows:
        if r.get("source_id") == sid:
            hits = int(r.get("hits") or 0) + 1
    _upsert_csv(BLACKLIST, BL_COLS, "source_id", {
        "source_id": sid, "kind": kind, "reason": reason[:400], "evidence": evidence[:600],
        "first_seen": next((r.get("first_seen") for r in rows if r.get("source_id") == sid),
                           now.strftime("%Y-%m-%d %H:%M")),
        "retest_after": (now + timedelta(days=RETEST_DAYS)).strftime("%Y-%m-%d"),
        "hits": hits})


def route_update(sid: str, engine: str, ok: bool, ratio: float = 0.0, note: str = "") -> None:
    if not engine:
        return
    rows = []
    if os.path.exists(ROUTES):
        rows = list(csv.DictReader(open(ROUTES, encoding="utf-8-sig")))
    wins = losses = 0
    for r in rows:
        if r.get("source_id") == sid and r.get("engine") == engine:
            wins, losses = int(r.get("wins") or 0), int(r.get("losses") or 0)
    if ok:
        wins += 1
    else:
        losses += 1
    _upsert_csv(ROUTES, RT_COLS, "source_id", {
        "source_id": sid, "engine": engine, "wins": wins, "losses": losses,
        "last_ok": time.strftime("%Y-%m-%d %H:%M") if ok else "",
        "last_ratio": round(ratio, 3), "note": note[:200]})


def blacklisted() -> dict[str, dict]:
    if not os.path.exists(BLACKLIST):
        return {}
    return {r["source_id"]: r for r in csv.DictReader(open(BLACKLIST, encoding="utf-8-sig"))}


def preferred_engine(sid: str) -> str:
    if not os.path.exists(ROUTES):
        return ""
    best, score = "", 0
    for r in csv.DictReader(open(ROUTES, encoding="utf-8-sig")):
        if r.get("source_id") == sid:
            s = int(r.get("wins") or 0) - int(r.get("losses") or 0)
            if s > score:
                best, score = r.get("engine", ""), s
    return best


# ── 待造刀报告 ─────────────────────────────────────────────
def write_gap_report(sid: str, chain: list[dict], sample: list[dict], kind: str,
                     reason: str, evidence: dict | None = None) -> str:
    os.makedirs(TRIAGE_DIR, exist_ok=True)
    path = os.path.join(TRIAGE_DIR, f"待造刀-{sid.replace('/', '__')}.json")
    doc = {"source_id": sid, "kind": kind, "reason": reason,
           "decided_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "chain": [{k: v for k, v in c.items() if k != "gate"} | ({"gate_why": c["gate"]["why"]} if c.get("gate") else {})
                     for c in chain],
           "evidence": evidence or {},
           "sample_rows": sample[:5],
           "sample_keys": sorted({str(k) for r in sample[:50] if isinstance(r, dict) for k in r.keys()}),
           "next_action": "造一把新刀（collectors/engines/ 新文件 + 注册表加一行）；不要改老引擎"}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    return path


def report_md(entries: list[dict]) -> str:
    os.makedirs(TRIAGE_DIR, exist_ok=True)
    path = os.path.join(TRIAGE_DIR, "报告.md")
    lines = [f"# 全败源分诊报告 · {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"共 {len(entries)} 个源全败。", ""]
    by: dict[str, list[dict]] = {}
    for e in entries:
        by.setdefault(e["kind"], []).append(e)
    for kind, items in sorted(by.items(), key=lambda kv: -len(kv[1])):
        lines += [f"## {kind} · {len(items)} 个", ""]
        for e in items[:40]:
            lines.append(f"- `{e['source_id']}` — {e['reason'][:160]}")
        if len(items) > 40:
            lines.append(f"- …另有 {len(items) - 40} 个")
        lines.append("")
    lines += ["## 处置口径", "",
              "- SOURCE_BROKEN / NOT_ADDRESS → 拉黑，30 天后自动复检一次",
              "- ENGINE_GAP → 见 out/triage/待造刀-*.json，造新刀（只增不改）", ""]
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    return path


# ── CLI ────────────────────────────────────────────────────
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="全败源分诊")
    ap.add_argument("--source", default="", help="现场跑一个源再分诊")
    ap.add_argument("--from-state", default="", help="扫状态文件里的全败源（不重新抓）")
    ap.add_argument("--out", default="out/cascade")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true", help="只看判决，不写账本")
    a = ap.parse_args(argv)

    entries: list[dict] = []
    table = list(csv.DictReader(open(TABLE, encoding="utf-8-sig")))
    if a.source:
        row = next((r for r in table if r["source_id"] == a.source), None)
        if row is None:
            raise SystemExit(f"源总表里没有 {a.source}")
        pol = Politeness(min_interval=0.8, max_concurrency=2, max_retries=1, timeout=20,
                         log_path=os.path.join(a.out, "request_log.jsonl"))
        from .engines import SourceCtx
        ctx = SourceCtx(a.source, row["url"], row, os.path.join(a.out, "work"))
        out = run_source(pol, ctx, parse_mapping_from_table(row), a.out)
        if out["ok"]:
            print(f"✓ {a.source} 由刀 {out['engine']} 切开")
            if not a.dry:
                route_update(a.source, out["engine"], True,
                             (out.get("gate") or {}).get("ratio", 0.0))
            return
        kind, reason = classify(out["chain"])
        ev = evidence_probe(row["url"])
        kind, reason = apply_evidence(kind, reason, ev)
        entries.append({"source_id": a.source, "kind": kind, "reason": reason,
                        "chain": out["chain"], "evidence": ev})
    elif a.from_state:
        st = json.load(open(a.from_state, encoding="utf-8"))
        items = list(st.items())[: a.limit or None]
        for sid, s in items:
            if s.get("done") or s.get("engine"):
                continue
            chain = s.get("engine_chain") or []
            if not chain:
                continue
            kind, reason = classify(chain)
            row = next((r for r in table if r["source_id"] == sid), None)
            ev = evidence_probe((row or {}).get("url", "")) if row else {}
            if ev:
                kind, reason = apply_evidence(kind, reason, ev)
            entries.append({"source_id": sid, "kind": kind, "reason": reason,
                            "chain": chain, "evidence": ev})
    else:
        raise SystemExit("给 --source 或 --from-state")

    for e in entries:
        ev = e.get("evidence") or {}
        print(f"[{e['kind']:13s}] {e['source_id']:32s} {e['reason'][:110]}")
        if ev.get("verdict_hint"):
            print(f"{'':15s} 复核证据: {ev['verdict_hint'][:120]}｜HTTP {ev.get('status')} "
                  f"{ev.get('bytes')}B {ev.get('content_type', '')[:40]}")
        if a.dry:
            continue
        if e["kind"] in ("SOURCE_BROKEN", "NOT_ADDRESS"):
            blacklist_add(e["source_id"], e["kind"], e["reason"],
                          json.dumps({"chain": e["chain"], "evidence": ev},
                                     ensure_ascii=False)[:600])
        elif e["kind"] == "ENGINE_GAP":
            write_gap_report(e["source_id"], e["chain"], [], e["kind"], e["reason"], ev)
            blacklist_add(e["source_id"], e["kind"], e["reason"], "待造刀")
    if entries and not a.dry:
        print("报告:", report_md(entries))


if __name__ == "__main__":
    main()
