"""级联采集器 —— 一个源，多把刀轮流上；全败才交人。

流程（每个源）：
  1. 排序出刀：路由记忆 → 静态分（贵刀后置）→ 其余兜底
  2. 每把刀先跑**探针**（小样 ≤200 条 / ≤25s），拿到数据立刻过**质检闸门**
  3. 探针不合格 → 结果**当场丢弃**，换下一把刀；合格 → 这把刀跑全量，源完成
  4. 所有刀都不合格 → 源进**分诊队列**（triage.py），由人判定：
       源坏 → 拉黑不管 ／ 形态没覆盖 → 造新刀（新引擎文件，不改老刀）

为什么先探针后全量（我的加固，比"整源重采三次"省 2/3 流量）：
  字面执行「引擎1采全量→扔掉→引擎2采全量」会让同一份大文件被下载最多 5 次；
  改为小样筛选后，只有胜出的那把刀跑全量。

断点/预算语义与 lane.py 一致：state 逐源落盘，probe 阶段不写正式数据。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from itertools import islice
from typing import Iterator

from .engines import (ENGINES, Engine, EngineError, NotMyMeat, RawBundle,
                      SourceBroken, SourceCtx, order_engines, self_test_all)
from .engines.base import (CONFIDENT_SNIFF, MAX_AUTO_BYTES, order_by_sniff, preflight,
                           sniff_table)
from .normalize import mapping_sanity, parse_mapping_from_table, to_record_ex
from .politeness import Politeness

TABLE = "data/源总表-执行序.csv"
PROBE_N = 200            # 探针取样条数
PROBE_BUDGET = 25        # 单把刀探针秒数上限
GATE_MIN_KEPT = 5        # 合格下限：至少产出这么多合规记录
GATE_MIN_RATIO = 0.30    # 合格下限：合规/原始 ≥ 30%

VERDICT_PASS = "PASS"
VERDICT_EMPTY = "EMPTY"          # 取到了但 0 行
VERDICT_GATE = "GATE_FAIL"       # 有行但不合规
VERDICT_FETCH = "FETCH_FAIL"     # 取数异常
VERDICT_BROKEN = "SOURCE_BROKEN"  # 有证据表明是源的问题
VERDICT_NOTMEAT = "NOT_MY_MEAT"  # 形态不匹配这把刀


# ── 质检闸门 ───────────────────────────────────────────────
def gate_eval(rows: list[dict], mapping: dict, sid: str, default_state: str = "") -> dict:
    """合格判据，全部量化。返回 {'ok','ratio','kept','raw','drops','addr_hint','why'}"""
    n = len(rows)
    kept, drops = 0, Counter()
    for r in rows:
        rec, reason = to_record_ex(r, mapping, sid, default_state=default_state)
        if rec is None:
            drops[reason] += 1
        else:
            kept += 1
    ratio = (kept / n) if n else 0.0
    addr_hint = Engine.looks_like_addresses(rows) if rows else 0.0
    missing = mapping_sanity(rows[0], mapping) if rows else []
    ok = False
    why = ""
    if n == 0:
        why = "0 行"
    elif kept >= GATE_MIN_KEPT and ratio >= GATE_MIN_RATIO:
        ok, why = True, f"合规 {kept}/{n} = {ratio:.0%}"
    elif kept >= 3 and ratio >= 0.8 and n <= 10:
        ok, why = True, f"小源放行 {kept}/{n}"
    else:
        why = f"合规 {kept}/{n} = {ratio:.0%}（阈 {GATE_MIN_RATIO:.0%}）"
    return {"ok": ok, "ratio": round(ratio, 3), "kept": kept, "raw": n,
            "drops": dict(drops.most_common(6)), "addr_hint": round(addr_hint, 2),
            "mapping_missing": missing, "why": why}


# ── 单源级联 ───────────────────────────────────────────────
def run_source(pol: Politeness, ctx: SourceCtx, mapping: dict, out_dir: str,
               prev: dict | None = None, full: bool = True,
               probe_n: int = PROBE_N, probe_budget: int = PROBE_BUDGET,
               full_budget: int = 420) -> dict:
    prev = prev or {}
    os.makedirs(out_dir, exist_ok=True)
    norm_dir = os.path.join(out_dir, "normalized")
    os.makedirs(norm_dir, exist_ok=True)
    safe = ctx.source_id.replace("/", "__")
    norm_path = os.path.join(norm_dir, safe + ".ndjson")
    default_state = ctx.row.get("state", "") or ""
    order = order_engines(ctx.row, preferred=prev.get("engine", ""))
    chain: list[dict] = []
    t_all = time.time()

    # ── 下载前侦察：一次 Range 请求定形态，把不认识的刀拦在下载之前 ──
    pf = preflight(ctx.url)
    if pf["error"] and pf["status"] == 0:
        return {"ok": False, "engine": "", "gate": None, "preflight": pf,
                "chain": [{"engine": "__preflight__", "verdict": VERDICT_FETCH,
                           "detail": pf["error"]}],
                "seconds": round(time.time() - t_all, 1)}
    sniffed = order_by_sniff(ctx.row, pf, preferred=prev.get("engine", ""))
    if sniffed:
        order = sniffed
    size = int(pf.get("content_length") or 0)
    trust = [e for e, _s, sn in sniff_table(ctx.row, pf) if sn >= CONFIDENT_SNIFF]
    if size > MAX_AUTO_BYTES and not trust:
        return {"ok": False, "engine": "", "gate": None, "preflight": pf,
                "chain": [{"engine": "__preflight__", "verdict": VERDICT_GATE,
                           "detail": f"体积 {size / 1048576:.0f}MB 且无刀确信能切"
                                     f"（{pf.get('content_type', '')[:40]}）→ 判形态缺口，不下载（省 "
                                     f"{size / 1048576:.0f}MB × 刀数）"}],
                "seconds": round(time.time() - t_all, 1)}

    for eng in order:
        t0 = time.time()
        probe_dir = os.path.join(out_dir, "probe", safe, eng.name)
        os.makedirs(probe_dir, exist_ok=True)
        pctx = SourceCtx(ctx.source_id, ctx.url, ctx.row, probe_dir)
        step = {"engine": eng.name, "version": eng.version}
        try:
            bundle = eng.fetch(pol, pctx, budget_s=probe_budget, probe=True)
            rows = list(islice(eng.rows(bundle), probe_n))
            gate = gate_eval(rows, mapping, ctx.source_id, default_state)
            step.update({"rows": len(rows), "gate": gate, "verdict":
                         VERDICT_PASS if gate["ok"] else (VERDICT_EMPTY if not rows else VERDICT_GATE),
                         "seconds": round(time.time() - t0, 1)})
        except SourceBroken as e:
            step.update({"verdict": VERDICT_BROKEN, "detail": str(e)[:200],
                         "seconds": round(time.time() - t0, 1)})
            chain.append(step)
            continue
        except NotMyMeat as e:
            step.update({"verdict": VERDICT_NOTMEAT, "detail": str(e)[:200],
                         "seconds": round(time.time() - t0, 1)})
            chain.append(step)
            continue
        except Exception as e:
            step.update({"verdict": VERDICT_FETCH, "detail": f"{type(e).__name__}: {str(e)[:200]}",
                         "seconds": round(time.time() - t0, 1)})
            chain.append(step)
            continue

        chain.append(step)
        # ── 探针通过 → 这把刀跑全量 ──
        if not gate["ok"]:
            _discard(probe_dir)          # 不合格的结果直接扔掉，不留痕
            continue
        if not full:
            return {"ok": True, "engine": eng.name, "chain": chain,
                    "probe_only": True, "gate": gate, "fetched": len(rows),
                    "kept": gate["kept"],
                    "seconds": round(time.time() - t_all, 1)}

        try:
            if getattr(eng, "probe_reusable", False):
                fbundle, frows = bundle, rows          # 文件已落盘，直接续读
            else:
                fbundle = eng.fetch(pol, SourceCtx(ctx.source_id, ctx.url, ctx.row, ctx.work_dir),
                                    budget_s=full_budget, probe=False)
                frows = None
            kept = fetched = 0
            drops = Counter()
            mode = "w"
            with open(norm_path, mode, encoding="utf-8") as fh:
                src = frows if frows is not None else eng.rows(fbundle)
                for r in src:
                    fetched += 1
                    rec, reason = to_record_ex(r, mapping, ctx.source_id, default_state=default_state)
                    if rec is None:
                        drops[reason] += 1
                        continue
                    rec["_engine"] = eng.name
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    kept += 1
            done = bool(fbundle.payload.get("info", {}).get("done", True))
            _discard(probe_dir)
            return {"ok": True, "engine": eng.name, "chain": chain, "gate": gate,
                    "fetched": fetched, "kept": kept, "dropped": sum(drops.values()),
                    "drops": dict(drops.most_common(8)), "done": done,
                    "norm_path": norm_path,
                    "last_oid": fbundle.payload.get("info", {}).get("last_oid"),
                    "seconds": round(time.time() - t_all, 1)}
        except Exception as e:
            step = {"engine": eng.name, "version": eng.version, "verdict": VERDICT_FETCH,
                    "detail": f"全量阶段 {type(e).__name__}: {str(e)[:200]}",
                    "seconds": round(time.time() - t0, 1)}
            chain.append(step)
            _discard(probe_dir)
            continue

    # ── 全败：交分诊 ──
    return {"ok": False, "engine": "", "chain": chain, "gate": None,
            "seconds": round(time.time() - t_all, 1)}


def _discard(path: str) -> None:
    """探针产物直接扔掉（省盘 + 防止半成品混进数据集）。"""
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# ── CLI：单源体检 / 小样试跑 ───────────────────────────────
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="级联采集器（单源试跑 / 全败分诊）")
    ap.add_argument("--source", default="", help="source_id，如 us/ms/lauderdale")
    ap.add_argument("--sources", default="", help="逗号分隔的多个 source_id（批试探针）")
    ap.add_argument("--sample", type=int, default=0, help="从源总表按 channel 轮抽 N 个源试针")
    ap.add_argument("--out", default="out/cascade")
    ap.add_argument("--probe-n", type=int, default=PROBE_N)
    ap.add_argument("--full", action="store_true", help="探针通过后跑全量")
    ap.add_argument("--only", default="", help="只跑指定引擎（逗号分隔），用于单刀验证")
    ap.add_argument("--min-interval", type=float, default=0.8)
    a = ap.parse_args(argv)

    rows = list(csv.DictReader(open(TABLE, encoding="utf-8-sig")))
    by_id = {r["source_id"]: r for r in rows}
    # 跑批前的第一道闸：自检不过的刀不出场
    st = self_test_all()
    bad = {k: v for k, v in st.items() if v.startswith("✗")}
    print(f"[刀具架] {len(st)} 把刀自检 | " +
          "、".join(f"{k}{v[0]}" for k, v in sorted(st.items())), file=sys.stderr)
    if bad:
        print(f"[警告] 自检不过的刀（已跳过）: {list(bad)}", file=sys.stderr)
    picked: list[str] = []
    if a.source:
        picked = [a.source]
    elif a.sources:
        picked = [s.strip() for s in a.sources.split(",") if s.strip()]
    elif a.sample:
        buckets: dict[str, list[str]] = {}
        for r in rows:
            buckets.setdefault(r.get("channel", "?"), []).append(r["source_id"])
        while len(picked) < a.sample:
            added = False
            for ch in sorted(buckets):
                while buckets[ch] and len(picked) < a.sample:
                    sid = buckets[ch].pop(0)
                    picked.append(sid)
                    added = True
                    break
            if not added:
                break
    else:
        raise SystemExit("给 --source / --sources / --sample 其一")

    pol = Politeness(min_interval=a.min_interval, max_concurrency=2, max_retries=1,
                     timeout=20, log_path=os.path.join(a.out, "request_log.jsonl"))
    if a.only:
        import collectors.engines as E
        E.ENGINES.clear()
        for n in a.only.split(","):
            E.ENGINES[n] = __import__(f"collectors.engines.{n}", fromlist=["ENGINE"]).ENGINE()

    state: dict = {}
    state_path = os.path.join(a.out, "state.json")
    os.makedirs(a.out, exist_ok=True)

    def _flush():
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, state_path)

    for sid in picked:
        row = by_id.get(sid)
        if row is None:
            print(f"[跳过] 源总表里没有 {sid}", file=sys.stderr)
            continue
        ctx = SourceCtx(sid, row["url"], row, os.path.join(a.out, "work"))
        out = run_source(pol, ctx, parse_mapping_from_table(row), a.out,
                         full=a.full, probe_n=a.probe_n)
        chain = " → ".join(f"{c['engine']}:{c['verdict']}" for c in out["chain"])
        g = out.get("gate") or {}
        tag = "✓" if out["ok"] else "✗"
        knife = ("刀=" + out["engine"]) if out["ok"] else "全败"
        q = (f" 合规 {g.get('kept')}/{g.get('raw')}={g.get('ratio')}") if g else ""
        print(f"[{tag}] {sid:30s} {knife}{q}  {chain}", file=sys.stderr)
        state[sid] = {"engine": out["engine"], "engine_chain": out["chain"],
                      "done": bool(out["ok"] and out.get("done", True)),
                      "kept": out.get("kept", 0), "fetched": out.get("fetched", 0),
                      "ratios": g.get("ratio") if g else None,
                      "seconds": out["seconds"]}
        _flush()          # 逐源落盘：中断也不丢已得结论（状态文件本身就是断点）
    os.makedirs(a.out, exist_ok=True)
    _flush()
    ok = sum(1 for v in state.values() if v["done"])
    print(json.dumps({"probed": len(state), "ok": ok, "failed": len(state) - ok,
                      "engines_used": sorted({v["engine"] for v in state.values() if v["engine"]}),
                      "state": state_path},
                     ensure_ascii=False, indent=1))
    if len(state) - ok:
        print(f"有 {len(state) - ok} 个源全败 → python -m collectors.triage --from-state "
              f"{state_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
