"""本地规则级联（silver 层）—— 下载一次，规则随便换。

流程（每个源）：
  ① 原始档：raw_archive/<state>/<sid>/ —— 没有就先抓（fetch_raw），已有且 done 就**直接用**
  ② 规则级联（全部本地，零网络）：mapping_v1 → field_guess → concat_addr → …
       每套规则：本地摊平 → 清洗 → 质检闸门
       不合格 → **删掉这套规则的产物**（原始档一个字不动）→ 下一套规则
  ③ 全规则皆败 → 分诊顺序（与旧「引擎」方案相反，先本地后回源）：
       1) 本地抽查原始档前几百行（零成本）——绝大多数情况在这里就能定案
       2) 本地压根没有地址字段 → NOT_ADDRESS：源不适用，拉黑
       3) 本地有地址字段但规则都不认 → RULE_GAP：造新规则（本地样本直接当依据）
       4) 本地档案为空/读不出 → 才回源取样核查（只有这一步会花网络）

用法：
  python -m collectors.local_cascade --source us/ca/san_francisco --fetch
  python -m collectors.local_cascade --source us/ca/san_francisco --limit 5000
  python -m collectors.local_cascade --inspect us/az/statewide     # 只看本地原始档
  python -m collectors.local_cascade --sample 5                    # 抽 5 个源跑一轮
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from itertools import islice

from .engines.base import Engine
from .fetch_raw import archive_dir, fetch_raw, load_manifest
from .flatten import iter_rows_from_archive, payload_files, peek
from .normalize import parse_mapping_from_table
from .politeness import Politeness
from .rules._common import FULL_ADDR_RE
from .rules import RuleCtx, order_rules, self_test_all
from .rules._common import SUFFIX_ANY2, parse_addr_v2
from .triage import (blacklist_add, evidence_probe, report_md, route_update,
                     write_gap_report)

TABLE = "data/源总表-执行序.csv"
ARCHIVE = "raw_archive"
PARSED = "parsed"
GATE_MIN_KEPT = 5
GATE_MIN_RATIO = 0.30          # 老分母（带地址行）用的旧阈值，仅作兼容参考
TRIAGE_DIR = "out/triage"
# 「带地址内容的行」判据（阀 2 版）：光“含字母”不够（DISTRICT="laurens"、业主姓名、LAT/LNG 都会误命中），
# 要有**地址痕迹**：一个值里同时出现「门牌号 + 后面的词」才箅（如 "1514 N US Hwy 45"、"123 MAIN ST"）。
# 为何要算分母：大量源里存在空置地块/林地/无门牌号税档（garfield 只有四分之一的行业带真地址），
# 拿全部行做分母会把正常源误判成「规则洗不出」。
NUM_ONLY = re.compile(r"^\d{1,6}[A-Za-z]?$")
ADDR_TRACE = re.compile(r"\b\d{1,6}[A-Za-z]?\s+[A-Za-z]{2,}")


def _true_addr(raw: dict) -> bool:
    """「真门牌号形态」判据 —— 源级闸门的**分母**（比 _is_contentful 严得多）。

    为什么换分母（实测 us/wa/garfield）：旧分母“带地址行”把税单/法律描述/业主邮寄地址
    全算成地址（83% 噪声），于是 30% 阈值既可能误杀真规则、也可能放过垃圾（v1 曾拿
    业主邮寄列当场地地址造出 466 条错记录）。新分母只认两种真形态：
      ① 某个值本身是完整地址（可选前置方位 + 号 + 街名），或
      ② 行里同时有「短数字」与「街名样」两列（号/街分列的老式源）
    """
    vals = [str(v).strip() for k, v in raw.items()
            if not str(k).startswith("_") and isinstance(v, str) and v.strip()]
    for v in vals:
        if parse_addr_v2(v):
            return True
    nums = {v for v in vals if NUM_ONLY.match(v)}
    if nums:
        for v in vals:
            if v in nums or len(v) > 90 or not re.search(r"[A-Za-z]{2,}", v):
                continue
            # ③ 分列式源：必须看到**街名后缀词**才算真街名。
            # （实测：只要求“≥两个词”会把 "STATE DEPT OF WILDLIFE" 这种也当街名，
            #   分母被噪声抬高 → 闸门反而变松；加上后缀词后 garfield 分母 96%→73%，才是真形态）
            if SUFFIX_ANY2.search(v):
                return True
    return False


def _is_contentful(row: dict) -> bool:
    vals = [v.strip() for k, v in row.items()
            if not str(k).startswith("_") and isinstance(v, str) and v.strip()]
    for v in vals:
        if ADDR_TRACE.search(v) or (len(v) < 130 and FULL_ADDR_RE.match(v)):
            return True
    # 分开的号/街两列：有短数字列 + 另一个带街名后缀的列
    if any(re.fullmatch(r"\d{1,6}[A-Za-z]?", v) for v in vals) and \
            any(re.search(r"[A-Za-z]{2,}\s+(st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|way|hwy|highway|ct|court|pl|ter|circle|cir)\b",
                          v, re.I) for v in vals):
        return True
    return False


def _record_ok(rec: dict, raw: dict) -> bool:
    """产物级防垃圾（规则无关的硬护栏，任何规则的产物都要过）：
      ① 街名必须以字母/数字开头 —— 拦 "- RURAL UNIMPROVED" 这类把地类码当地址的产物
         （实测 concat_addr@v1 拿 PropertyClass 造过 mass 垃圾记录）
      ② 门牌号必须在原始行的某个值里真实出现过 —— 拦凭空造数
    """
    street = str(rec.get("street") or "")
    if not street or not street[0].isalnum():
        return False
    num = str(rec.get("number") or "")
    if not num:
        return False
    blob = " ".join(str(v) for v in raw.values() if isinstance(v, (str, int, float)))
    return num in blob or num.lstrip("0") in blob


def _gate(kept: int, contentful: int, rows: int, true_n: int = 0) -> tuple[bool, str]:
    """源级闸门（两道，各管一头）：

    ① 反造数（用**严判据**的分母 true_n）：产物数量不能远超「真门牌号形态行」。
       实测价值：us/wa/garfield 有源把**业主邮寄地址**当地址造出 466 条错记录，
       旧闸门看不出来（分母是噪声、覆盖率还很高）；现在真形态行只有 29 条 → 466 > 29×1.5 → 当场拒。
    ② 覆盖率（用老分母 contentful）：真规则在相当一部分行上洗不出来是正常的
       （空置地块/无号税档，实测 us/il/effingham 只有 67% 的行能出记录）
       → 阈值仍用 30%，避免误杀真规则。
    """
    if rows == 0:
        return False, "0 行"
    if contentful == 0:
        return False, f"{rows:,} 行里没有一行带地址内容（源不含地址）"
    ref, rname = (true_n, "真门牌号行") if true_n else (contentful, "带地址行")
    if kept > ref * 1.5:
        return False, (f"疑似凭空造数据：合规 {kept:,} 条 > {rname} {ref:,} 条×150%"
                       f" → 拒绝这套产物")
    ratio = kept / contentful
    if kept >= GATE_MIN_KEPT and ratio >= GATE_MIN_RATIO:
        return True, (f"合规 {kept}/{contentful} = {ratio:.0%}（带地址行分母；"
                      f"真门牌号行 {true_n:,}）总行 {rows:,}")
    if kept >= 3 and ratio >= 0.8 and contentful <= 10:
        return True, f"小源放行 {kept}/{contentful}"
    return False, (f"合规 {kept}/{contentful} = {ratio:.0%}（阈 {GATE_MIN_RATIO:.0%}；"
                   f"总行 {rows:,}，带地址 {contentful:,}，真门牌号 {true_n:,}）")


def run_rules(sid: str, row: dict, out_root: str = PARSED, archive: str = ARCHIVE,
              limit: int = 0, preferred: str = "", keep_failed: bool = False) -> dict:
    """对一个源的原始档跑规则级联。返回结果字典（含每套规则的判决链）。"""
    arch = archive_dir(archive, sid)
    man = load_manifest(archive, sid)
    mapping = parse_mapping_from_table(row)
    default_state = row.get("state", "") or ""
    dst_dir = os.path.join(out_root, *sid.split("/"))
    os.makedirs(dst_dir, exist_ok=True)
    chain: list[dict] = []
    passing: list[tuple[int, str, dict, str]] = []      # 已通过闸门的规则（最后选量最大的那套）
    t0 = time.time()
    sample: list[dict] = []

    for r in order_rules(row, meta=man, preferred=preferred):
        rname = f"{r.name}@v{r.version}"
        try:
            r.warm(list(islice(iter_rows_from_archive(arch), 300)))   # 预热：按值形态定列
        except Exception as e:
            print(f"  [warm] {rname} 预热失败（不致命）: {type(e).__name__}", flush=True)
        stats: Counter = Counter()
        dst = os.path.join(dst_dir, rname + ".ndjson")
        ctx = RuleCtx(sid, row, mapping, default_state, dict())
        fetched = kept = contentful_n = kept_contentful = true_n = 0
        hint_seen = None
        try:
            with open(dst, "w", encoding="utf-8") as fh:
                for raw in iter_rows_from_archive(arch):
                    if "_read_error" in raw:
                        stats["read_error"] += 1
                        continue
                    if limit and fetched >= limit:
                        break
                    if hint_seen is None:
                        hint_seen = Engine.looks_like_addresses([raw])
                    fetched += 1
                    if not sample and len(sample) < 5:
                        sample.append(raw)
                    contentful = _is_contentful(raw)
                    contentful_n += 1 if contentful else 0
                    if _true_addr(raw):
                        true_n += 1
                    if not contentful:
                        # 硬不变量：一行没有地址痕迹（号+词）就不该产出门牌点记录。
                        # 这条护栏是规则无关的 —— 它拦住了「猜错列位凭空造数据」
                        # （实测 us/il/effingham：field_guess 曾把 OBJECTID 当号、Grantee 当街，
                        #  在 23,578 行空地址税档上凭空造出 23,577 条垃圾记录）
                        stats["no_address_trace"] += 1
                        continue
                    rec, why = r.clean(raw, ctx)
                    if rec is None:
                        stats[why] += 1
                        continue
                    if not _record_ok(rec, raw):
                        stats["junk_rejected"] += 1
                        continue
                    kept += 1
                    kept_contentful += 1
                    rec["_rule"] = rname
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            stats[f"rule_error:{type(e).__name__}"] = 1
        ok, why = _gate(kept_contentful, contentful_n, fetched, true_n)
        step = {"rule": rname, "rows": fetched, "kept": kept,
                "contentful": contentful_n, "kept_contentful": kept_contentful,
                "true_addr": true_n,
                "ratio": round(kept_contentful / contentful_n, 3) if contentful_n else 0.0,
                "coverage": round(contentful_n / fetched, 3) if fetched else 0.0,
                "addr_hint": hint_seen, "drops": dict(stats.most_common(6)),
                "verdict": "PASS" if ok else "GATE_FAIL", "why": why,
                "seconds": round(time.time() - t0, 1)}
        chain.append(step)
        print(f"  [{rname:16s}] 原始 {fetched:>8,} → 合规 {kept:>8,}  "
              f"（真门牌号行 {true_n:,}／带地址行 {contentful_n:,}，其中合规 {kept_contentful:,}）"
              f"{'✓通过' if ok else '✗丢弃这套产物'}  ({why})", flush=True)
        if ok:
            # 不立即收工：多套规则都可能通过，最后选「洗出最多」的那套（质量由闸门 + 产物级防垃圾保证）
            passing.append((kept, rname, step, dst))
            continue
        if keep_failed:
            continue
        try:
            os.remove(dst)                    # 只删规则产物，原始档不动
        except OSError:
            pass
    if passing:
        passing.sort(key=lambda x: (-x[0], x[1]))
        best_n, best_rule, best_step, best_dst = passing[0]
        for _n, rname2, _s2, dst2 in passing[1:]:
            try:
                os.remove(dst2)               # 落选规则的产品删掉，只留赢家
            except OSError:
                pass
        print(f"  → 选中 {best_rule}（通过者中洗出最多：{best_n:,} 条）", flush=True)
        return {"ok": True, "rule": best_rule, "chain": chain, "gate": best_step,
                "kept": best_n, "fetched": best_step["rows"], "path": best_dst,
                "seconds": round(time.time() - t0, 1)}
    return {"ok": False, "rule": "", "chain": chain, "gate": None,
            "sample": sample, "fetched": sum(c["rows"] for c in chain),
            "seconds": round(time.time() - t0, 1)}


# ── 全规则皆败 → 分诊（先本地，后回源）────────────────────
def _missing_deps(files: list[str]) -> str:
    """本地文件需要、但环境里没装的解析依赖（.gdb → fiona；.shp → pyshp）。"""
    lows = [str(f).lower() for f in files]
    miss = []
    if any(l.endswith(".gdb") or ".gdb/" in l or ".gdb\\" in l for l in lows):
        try:
            import fiona  # noqa: F401
        except ImportError:
            miss.append("fiona(.gdb/FileGDB)")
    if any(l.endswith(".shp") for l in lows):
        try:
            import shapefile  # noqa: F401
        except ImportError:
            miss.append("pyshp(.shp)")
    return "、".join(miss)


def diagnose(sid: str, row: dict, out: dict, archive: str = ARCHIVE) -> dict:
    arch = archive_dir(archive, sid)
    files = payload_files(arch)
    man = load_manifest(archive, sid)
    chain = out["chain"]
    best_hint = max((c.get("addr_hint") or 0) for c in chain) if chain else 0
    local_rows = out.get("fetched") or 0

    if not files:
        kind = "DOWNLOAD_EMPTY"
        reason = f"本地原始档为空（manifest: {man.get('error') or '未抓取'}）"
    elif local_rows == 0 and _missing_deps(files):
        # 缺解析依赖 ≠ 源坏 ≠ 下载坏：单独成类，装了依赖就能解（不拉黑，下一轮重试）
        kind = "FORMAT_GAP"
        reason = (f"本地有 {len(files)} 个文件但缺解析依赖：{_missing_deps(files)}"
                  f" → 装依赖后再跑（不当成源坏/下载缺陷）")
    elif local_rows == 0:
        kind = "DOWNLOAD_EMPTY"
        reason = f"本地有 {len(files)} 个文件但摊不出任何行（读不出/格式怪）"
    elif best_hint and best_hint < 0.34:
        kind = "NOT_ADDRESS"
        reason = f"本地抽查：原始字段无门牌点味（addr_hint={best_hint}）→ 源不适用"
    elif chain and all((c.get("contentful") or 0) == 0 for c in chain):
        kind = "NOT_ADDRESS"
        reason = (f"本地抽查：{chain[0].get('rows', 0):,} 行里没有一行带地址内容"
                  f"（全是空置地块/无门牌号税档）→ 源不含门牌点，不适用")
    else:
        kind = "RULE_GAP"
        reason = ("本地抽查：有地址味但现有规则都洗不出（" +
                  "；".join(f"{c['rule']}:{c['why']}" for c in chain[:3]) + "）→ 造新规则")

    ev = {}
    if kind == "DOWNLOAD_EMPTY":          # 只有这一步才回源
        ev = evidence_probe(row.get("url", ""))
        hint = ev.get("verdict_hint", "")
        if hint.startswith("SOURCE_BROKEN"):
            kind, reason = "SOURCE_BROKEN", f"[回源取证] {hint}"
        elif hint.startswith("DATA_PRESENT"):
            kind, reason = "DOWNLOAD_DEFECT", (f"[回源取证] 源里有数据但本地档为空 → "
                                               f"下载环节缺陷（补采，不换规则）｜{hint}")

    rec = {"source_id": sid, "kind": kind, "reason": reason, "chain": chain,
           "evidence": ev, "local_files": [os.path.basename(f) for f in files[:10]],
           "manifest": {k: man.get(k) for k in ("protocol", "bytes", "rows", "done",
                                                "error", "seconds")}}
    os.makedirs(TRIAGE_DIR, exist_ok=True)
    path = os.path.join(TRIAGE_DIR, f"分诊-{sid.replace('/', '__')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rec | {"sample_rows": out.get("sample", [])[:5]}, fh,
                  ensure_ascii=False, indent=1)
    rec["report"] = path
    return rec


# ── 规则对比模式：同一个源，所有规则都跑一遍（纯本地，零成本）──
def compare_rules(sid: str, row: dict, archive: str = ARCHIVE, limit: int = 0
                  ) -> list[dict]:
    """每套规则独立跑一遍原始档，返回成绩单（按洗出条数降序）。

    这就是「第一套洗完是废数据 → 原始不动，交给第二套」的量化版：
    规则之间先各自跑出成绩单，再决定用哪套（成本只有 CPU，不必省）。
    """
    arch = archive_dir(archive, sid)
    mapping = parse_mapping_from_table(row)
    table = []
    for r in order_rules(row):
        rname = f"{r.name}@v{r.version}"
        try:
            r.warm(list(islice(iter_rows_from_archive(arch), 300)))
        except Exception:
            pass
        stats: Counter = Counter()
        ctx = RuleCtx(sid, row, mapping, row.get("state", "") or "", dict())
        kept = fetched = 0
        contentful_n = kept_contentful = true_n = 0
        for raw in iter_rows_from_archive(arch):
            if "_read_error" in raw:
                stats["read_error"] += 1
                continue
            if limit and fetched >= limit:
                break
            fetched += 1
            cf = _is_contentful(raw)
            contentful_n += 1 if cf else 0
            if _true_addr(raw):
                true_n += 1
            if not cf:
                continue                     # 无地址痕迹的行不参与（同 run_rules 的硬不变量）
            rec, why = r.clean(raw, ctx)
            if rec is None:
                stats[why] += 1
                continue
            if not _record_ok(rec, raw):
                stats["junk_rejected"] += 1
                continue
            kept += 1
            kept_contentful += 1
        ok, why = _gate(kept_contentful, contentful_n, fetched, true_n)
        table.append({"rule": rname, "rows": fetched, "kept": kept,
                      "contentful": contentful_n, "kept_contentful": kept_contentful,
                      "true_addr": true_n,
                      "ratio": round(kept_contentful / contentful_n, 3) if contentful_n else 0.0,
                      "coverage": round(contentful_n / fetched, 3) if fetched else 0.0,
                      "verdict": "PASS" if ok else "GATE_FAIL", "why": why,
                      "drops": dict(stats.most_common(6))})
        print(f"  [{rname:16s}] 合规 {kept:>8,}/{fetched:<8,}（带地址行 {contentful_n:,}，"
              f"其中合规 {kept_contentful:,}） {(kept_contentful / contentful_n if contentful_n else 0):>6.1%}"
              f"  {'✓' if ok else '✗'} 丢弃原因 {dict(stats.most_common(3))}", flush=True)
    return sorted(table, key=lambda x: -x["kept"])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="本地规则级联（下载一次，规则随便换）")
    ap.add_argument("--source", default="")
    ap.add_argument("--sample", type=int, default=0, help="从源总表抽 N 个源")
    ap.add_argument("--fetch", action="store_true", help="先把原始档抓回本地（无规则全量）")
    ap.add_argument("--force-fetch", action="store_true", help="已有档也重抓（慎用）")
    ap.add_argument("--inspect", default="", help="只本地抽查原始档，不跑规则")
    ap.add_argument("--compare", action="store_true",
                    help="所有规则各跑一遍出成绩单（纯本地，零成本）")
    ap.add_argument("--limit", type=int, default=0, help="每套规则最多处理 N 行（试跑用）")
    ap.add_argument("--preferred", default="", help="优先用哪套规则（如 mapping@v1）")
    ap.add_argument("--archive", default=ARCHIVE)
    ap.add_argument("--out", default=PARSED)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if a.inspect:
        rows = peek(archive_dir(a.archive, a.inspect), 8)
        print(f"本地原始档 {a.inspect}: {len(rows)} 行样本")
        for r in rows[:5]:
            print("  ", json.dumps(r, ensure_ascii=False)[:220])
        if rows:
            print("  字段名:", sorted({k for r in rows for k in r.keys()})[:30])
        return

    if not a.quiet:
        st = self_test_all()
        print("[规则架] " + "、".join(f"{k}{v[0]}" for k, v in sorted(st.items())), file=sys.stderr)

    table = list(csv.DictReader(open(TABLE, encoding="utf-8-sig")))
    by_id = {r["source_id"]: r for r in table}
    picked = [a.source] if a.source else [r["source_id"] for r in table[: a.sample]]
    pol = Politeness(min_interval=0.8, max_concurrency=2, max_retries=1, timeout=20,
                     log_path=os.path.join("out", "fetch_raw_log.jsonl"))
    results, entries = [], []
    for sid in picked:
        row = by_id.get(sid)
        if row is None:
            print(f"[跳过] 源总表里没有 {sid}", file=sys.stderr)
            continue
        print(f"── {sid}", flush=True)
        if a.fetch or a.force_fetch or not payload_files(archive_dir(a.archive, sid)):
            man = fetch_raw(pol, sid, row["url"], a.archive, force=a.force_fetch)
            print(f"   原始档: {man.get('protocol')} {man.get('bytes', 0) / 1048576:.2f}MB "
                  f"{man.get('rows', '')}行 done={man.get('done')} "
                  f"{('err=' + man['error']) if man.get('error') else ''}", flush=True)
        if a.compare:
            tbl = compare_rules(sid, row, a.archive, a.limit)
            best = tbl[0] if tbl else None
            print(f"   成绩单最优: {best['rule']} 合规 {best['kept']:,} 条"
                  if best else "   无规则可用")
            results.append({"source_id": sid, "ok": bool(best and best["verdict"] == "PASS"),
                            "rule": best["rule"] if best else "",
                            "kept": best["kept"] if best else 0,
                            "fetched": best["rows"] if best else 0})
            continue
        res = run_rules(sid, row, a.out, a.archive, a.limit, a.preferred)
        if res["ok"]:
            print(f"   ✓ 由规则 {res['rule']} 洗出 {res['kept']:,} 条 → {res['path']}")
            route_update(sid, res["rule"], True, res["gate"]["ratio"])
        else:
            d = diagnose(sid, row, res, a.archive)
            print(f"   ✗ [{d['kind']}] {d['reason'][:120]}\n     详情: {d['report']}")
            if d["kind"] in ("SOURCE_BROKEN", "NOT_ADDRESS"):
                blacklist_add(sid, d["kind"], d["reason"],
                              json.dumps(d.get("evidence", {}), ensure_ascii=False)[:500])
            elif d["kind"] == "RULE_GAP":
                write_gap_report(sid, d["chain"], res.get("sample", []), d["kind"], d["reason"])
            entries.append(d)
        results.append({"source_id": sid, "ok": res["ok"], "rule": res["rule"],
                        "kept": res.get("kept", 0), "fetched": res.get("fetched", 0)})
    if entries and not a.quiet:
        print("分诊报告:", report_md(entries))
    print(json.dumps({"sources": len(results),
                      "ok": sum(1 for r in results if r["ok"]),
                      "kept": sum(r["kept"] for r in results)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
