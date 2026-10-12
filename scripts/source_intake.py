"""源准入四段（13 环节 · 第 1~4 步）—— 前置门，判「采不采」。

铁律（写在墙上的一条）：**前置门（1~4 步）判采不采，后置门（第 13 步）判发不发，两套不许混用。**
误杀可以由复检救回；漏放不可逆。所以这里宁可拒，也不放。

  第 1 步 P0 静态预检  1~2 个请求拿元数据 + 字段清单；判「字段里有没有能映射出
                       number/street/city/state/zip 的候选列」（MUNICIPALITY / MAILCITY /
                       POST_OFFICE_CITY / PLACE 也都算城市）。零请求判不了的文件型源 → NEEDS_DOWNLOAD。
  第 2 步 P1 分层抽检  分页首/中/尾 + 主样本，算值域极值（最短/最长/空值/特殊字符），
                       出 **sample_hash 样本指纹**（上游一漂移指纹就变）。
  第 3 步（由 collectors/rule_author.py 承担：样本 + 字段形态 → 声明式 spec）
  第 4 步 P2 契约体检  用服务端统计接口（returnCountOnly / outStatistics）拿**每字段非空计数**，
                       与质量契约逐项比对 → CONTRACTED / CONTRACTED_DOWNGRADED / REJECTED_P2。
                       不合格的源在这里就挡掉，省下后面全量采集的时间。

用法：
  python scripts/source_intake.py --source us/ak/anchorage --all
  python scripts/source_intake.py --todo 20            # 挑 20 个还没采过的源批量过闸
  python scripts/source_intake.py --source us/xx/yy --stage p0
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.arcgis import layer_meta, normalize_layer_url      # noqa: E402
from collectors.politeness import Politeness                      # noqa: E402
from collectors.rules._common import FULL_ADDR_RE, SITE_KEY, guess_keys   # noqa: E402
from collectors.normalize import parse_mapping_from_table         # noqa: E402

TABLE = "data/源总表-执行序.csv"
STATE = "progress/state.json"
BLACKLIST = "data/源黑名单.csv"
CARD_DIR = "data/预检卡"
SUMMARY = "data/预检汇总.csv"
SAMPLE_DIR = "out/samples"
LICENSE_TODO = "data/许可待补.csv"

CORE = ("number", "street", "city", "zip", "state")
MAX_MISS = {"city": 0.20, "zip": 0.20}      # 与第 9 步的全局硬门同一口径
MIN_KEEP = 0.80                            # number/street 必须有这么多行洗得出来
SPECIAL = re.compile(r"[^A-Za-z0-9 .#\-/]")
SUMMARY_COLS = ["source_id", "state", "county", "channel", "protocol", "p0", "p1", "p2",
                "verdict", "fields_n", "cols", "total", "city_miss", "zip_miss",
                "sample_hash", "license_status", "reason", "checked_at"]


def load_table(path: str = TABLE) -> list[dict]:
    if not os.path.exists(path):
        raise SystemExit(f"找不到源总表 {path}")
    return [r for r in csv.DictReader(open(path, encoding="utf-8-sig")) if r.get("source_id")]


def load_done(path: str = STATE) -> set:
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return set()
    return {k for k, v in d.items() if isinstance(v, dict) and v.get("done")}


def load_blacklist(path: str = BLACKLIST) -> set:
    if not os.path.exists(path):
        return set()
    return {r["source_id"] for r in csv.DictReader(open(path, encoding="utf-8-sig"))
            if r.get("source_id")}


def is_arcgis(row: dict) -> bool:
    u = (row.get("url") or "").lower()
    return ("/featureserver/" in u or "/mapserver/" in u
            or (row.get("channel") or "").upper() == "ARCGIS")


def pick_todo(rows: list[dict], limit: int, only: str = "") -> list[dict]:
    """挑「还没采过的源」：不在 state.done、不在黑名单、已有预检卡的不重复判。"""
    if only:
        return [r for r in rows if r["source_id"] == only]
    done, bl = load_done(), load_blacklist()
    out = []
    for r in rows:
        sid = r["source_id"]
        if sid in done or sid in bl:
            continue
        if os.path.exists(os.path.join(CARD_DIR, sid.replace("/", "__") + ".json")):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _mapped_name(v, names: list[str]) -> str:
    """把源总表里的映射（可能是 'HOUSE_NUMBER'，也可能是 "['HOUSE_NUMBER','HALF_ADDRESS']"）
    解析成**图层里真实存在的**单个字段名。猜出来的列不可信，源总表的映射优先。"""
    if not v:
        return ""
    for c in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(v)):
        if c in names:
            return c
    return ""


# ─────────────────────── 第 1 步 · P0 静态预检 ───────────────────────

def stage_p0(row: dict, pol: Politeness) -> dict:
    """1~2 个请求：元数据 + 字段清单 → 判有没有能映射出五要素的候选列。"""
    sid = row["source_id"]
    card: dict = {"source_id": sid, "stage": "p0", "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "url": row.get("url", ""), "channel": row.get("channel", ""),
                  "protocol": row.get("protocol", ""), "state": row.get("state", ""),
                  "county": row.get("county", ""), "attribution": row.get("attribution", ""),
                  "license": row.get("license", ""),
                  "license_status": "ok" if (row.get("license") or "").strip() else "unknown"}

    # 许可缺失：如实记「待补」。是否因此拒收由 --require-license 决定（默认只记不拒）。
    if card["license_status"] == "unknown":
        os.makedirs(os.path.dirname(LICENSE_TODO) or ".", exist_ok=True)
        new = not os.path.exists(LICENSE_TODO)
        with open(LICENSE_TODO, "a", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["source_id", "state", "attribution", "url", "website", "note"])
            w.writerow([sid, row.get("state", ""), row.get("attribution", ""),
                        row.get("url", ""), row.get("website", ""), "以官方页面为准，待补"])

    if not is_arcgis(row):
        # 文件型（csv/zip/shp/gdb…）：零请求判不出字段名 → 交给 P2 的「下载后统计体检」
        card.update({"p0": "NEEDS_DOWNLOAD",
                     "reason": "文件型源，字段清单需下载后统计（走 P2 体检）",
                     "verdict": "NEEDS_DOWNLOAD"})
        return card

    try:
        meta = layer_meta(pol, row["url"])
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:120]}"
        low = msg.lower()
        # 网络/DNS/超时是「暂时」，不是源本身坏 —— 不许当永久拒收（否则一次抖网就误杀好源）
        if any(t in low for t in ("name resolution", "urlopen error", "timed out", "timeout",
                                  "connection reset", "temporarily")):
            card.update({"p0": "TRANSIENT", "verdict": "TRANSIENT",
                         "reason": f"暂时取不到元数据（网络类）：{msg} → 下轮重试"})
        else:
            card.update({"p0": "REJECTED_P0", "verdict": "REJECTED_P0",
                         "reason": f"元数据取不到（源本身）：{msg}"})
        return card

    raw_fields = meta.get("fields") or []
    names = [(f.get("name", "") if isinstance(f, dict) else str(f))
             for f in raw_fields]
    names = [n for n in names if n]
    site_cols = [n for n in names if SITE_KEY.search(n)][:5]

    # ① 源总表里**已经写好的**映射最可信（那是当初逐个源看过的）
    mapping = parse_mapping_from_table(row)
    mkeys = {"number": "number", "street": "street", "city": "city",
             "zip": "postcode", "state": "region", "unit": "unit"}
    curated = {f: _mapped_name(mapping.get(mk), names) for f, mk in mkeys.items()}
    curated = {k: v for k, v in curated.items() if v}
    # ② 猜出来的只能当补充（实测过误报：EMERGENCY_SERVICE_NUMBER 被当成门牌号列）
    guessed = {k: v for k, v in guess_keys(names).items() if v and k not in curated}
    cols = {**guessed, **curated}

    card["fields_n"] = len(names)
    card["fields"] = names[:80]
    card["candidate_cols"] = cols
    card["curated_cols"] = curated
    card["guessed_cols"] = guessed
    card["site_cols"] = site_cols
    card["layer"] = {"name": meta.get("name"), "maxRecordCount": meta.get("maxRecordCount"),
                     "supportsPagination": meta.get("supportsPagination"),
                     "objectIdField": meta.get("objectIdField")}

    cur_ns = bool(curated.get("number") and curated.get("street"))
    gue_ns = bool(cols.get("number") and cols.get("street"))
    has_zip_city = bool(cols.get("zip") or cols.get("city"))
    if cur_ns:
        card.update({"p0": "PASS_P0", "verdict": "PASS_P0",
                     "reason": f"字段够用（源总表已映射）：number={curated['number']} "
                               f"street={curated['street']}"
                               + (f" city={curated['city']}" if curated.get("city") else "")
                               + (f" zip={curated['zip']}" if curated.get("zip") else "")
                               + ("" if has_zip_city else "（无 city/zip 列 → 交付会降级，靠字典补）")})
        return card
    if gue_ns:
        card.update({"p0": "PASS_P0_WEAK", "verdict": "PASS_P0",
                     "reason": f"字段可能是够的（**猜的**）：number={cols['number']} "
                               f"street={cols['street']} —— 需造刀时用样本确认"})
        return card
    if site_cols:
        card.update({"p0": "PASS_P0_SITE", "verdict": "PASS_P0",
                     "reason": f"无独立号/街列，但有整条地址列候选 {site_cols[:2]} → 交给 concat_addr 拆"})
        return card
    card.update({"p0": "REJECTED_P0", "verdict": "REJECTED_P0",
                 "reason": f"{len(names)} 个字段里找不到门牌号/街名候选列，也找不到整条地址列"
                           f"（号候选 {cols.get('number')!r}／街候选 {cols.get('street')!r}）"})
    return card


# ─────────────────────── 第 2 步 · P1 分层抽检 ───────────────────────

def _q(pol: Politeness, u: str, params: dict) -> dict:
    d = pol.get_json(f"{u}/query", params)
    if isinstance(d, dict) and "error" in d:
        raise RuntimeError(str(d["error"])[:150])
    return d


def stage_p1(row: dict, pol: Politeness, card: dict, main_n: int = 300,
             edge_n: int = 15) -> dict:
    """分页首/中/尾 + 主样本；算极值；出样本指纹。"""
    sid = row["source_id"]
    if not is_arcgis(row):
        card["p1"] = {"verdict": "NEEDS_DOWNLOAD", "reason": "文件型源：样本要等下载后取"}
        return card
    u = normalize_layer_url(row["url"])
    try:
        total = int(_q(pol, u, {"where": "1=1", "returnCountOnly": "true", "f": "json"})
                    .get("count", 0))
    except Exception as e:
        card["p1"] = {"verdict": "FAIL_P1", "reason": f"计数失败：{str(e)[:120]}"}
        card["verdict"] = "FAIL_P1"
        return card

    def grab(offset: int, n: int) -> list[dict]:
        try:
            d = _q(pol, u, {"where": "1=1", "outFields": "*", "returnGeometry": "false",
                            "resultOffset": str(max(0, offset)), "resultRecordCount": str(n),
                            "f": "json"})
            return [f.get("attributes", {}) for f in (d.get("features") or [])]
        except Exception:
            return []

    main = grab(0, main_n)
    parts = {"head": grab(0, edge_n),
             "mid": grab(max(0, total // 2), edge_n),
             "tail": grab(max(0, total - edge_n), edge_n)}
    parts = {k: v for k, v in parts.items() if v}
    edges = [r for v in parts.values() for r in v]

    if not main:
        card["p1"] = {"verdict": "FAIL_P1",
                      "reason": f"服务端自报 {total:,} 条，但主样本一条都取不到"}
        card["verdict"] = "FAIL_P1"
        return card

    # 值域极值（在字段数上算，控成本）
    ex: dict[str, dict] = {}
    keys = [k for k in (main[0] or {})][:40]
    for k in keys:
        vals = [str(r.get(k) or "").strip() for r in main]
        empt = sum(1 for v in vals if not v)
        nonempty = [v for v in vals if v]
        ex[k] = {"空": empt, "空率": round(empt / len(vals), 3),
                 "最短": min(nonempty, key=len)[:40] if nonempty else "",
                 "最长": max(nonempty, key=len)[:80] if nonempty else "",
                 "带特殊字符": sum(1 for v in nonempty if SPECIAL.search(v))}

    os.makedirs(os.path.join(SAMPLE_DIR, sid), exist_ok=True)
    mp = os.path.join(SAMPLE_DIR, sid, "main.jsonl")
    ep = os.path.join(SAMPLE_DIR, sid, "edges.jsonl")
    with open(mp, "w", encoding="utf-8") as fh:
        for r in main:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    with open(ep, "w", encoding="utf-8") as fh:
        for k, v in parts.items():
            for r in v:
                fh.write(json.dumps({"_part": k, **r}, ensure_ascii=False, sort_keys=True) + "\n")
    h = hashlib.sha256()
    for p in (mp, ep):
        with open(p, "rb") as fh:
            h.update(fh.read())
    sample_hash = h.hexdigest()[:16]

    ok = len(main) >= min(main_n, 100) and len(parts) >= 2
    card["p1"] = {"verdict": "PASS_P1" if ok else "FAIL_P1",
                  "total": total, "main_n": len(main),
                  "partitions": {k: len(v) for k, v in parts.items()},
                  "sample_hash": sample_hash, "extremes": ex,
                  "reason": ("样本量达标、分页首/中/尾都取到"
                             if ok else
                             f"样本不足或分区没取全（主样本 {len(main)}／分区 {list(parts)}）→ 重抽")}
    card["sample_hash"] = sample_hash
    card["verdict"] = card["p1"]["verdict"]
    return card


# ─────────────────────── 第 4 步 · P2 契约体检 ───────────────────────

def stage_p2(row: dict, pol: Politeness, card: dict) -> dict:
    """服务端统计接口一次拿回各字段非空计数 → 与质量契约逐项比对。

    这是省时间省钱的主战场：不合格的源在这里就挡掉，别等全量采集跑完才发现洗不出东西。
    """
    sid = row["source_id"]
    p1 = card.get("p1") or {}
    total = int(p1.get("total") or 0)
    if not total or not is_arcgis(row):
        card["p2"] = {"verdict": "DEFERRED",
                      "reason": "文件型源/无总数：等下载后做统计体检（同一套契约口径）"}
        return card
    u = normalize_layer_url(row["url"])
    cols = card.get("candidate_cols") or {}
    # 源总表映射优先，猜的列只兜底
    mapping = parse_mapping_from_table(row)
    names = list(card.get("fields") or [])
    curated = {f: _mapped_name(mapping.get(mk), names)
               for f, mk in {"number": "number", "street": "street", "city": "city",
                             "zip": "postcode", "state": "region"}.items()}
    probe = {}
    for f in CORE:
        probe[f] = curated.get(f) or cols.get(f) or ""
    # 整条地址列的兜底：P0 判的是 PASS_P0_SITE 时，用 site 列统计
    site = (card.get("site_cols") or [""])[0]
    if site:
        probe.setdefault("full", site)
    probe = {k: v for k, v in probe.items() if v}
    if not probe:
        card["p2"] = {"verdict": "REJECTED_P2", "reason": "没有可比对的字段"}
        card["verdict"] = "REJECTED_P2"
        return card

    stats = [{"statisticType": "count", "onStatisticField": f, "outStatisticFieldName": f"n_{k}"}
             for k, f in probe.items()]
    try:
        d = _q(pol, u, {"where": "1=1", "outStatistics": json.dumps(stats), "f": "json"})
        feats = d.get("features") or []
        rowd = (feats[0].get("attributes") or {}) if feats else {}
    except Exception as e:
        card["p2"] = {"verdict": "DEFERRED",
                      "reason": f"统计接口不可用（{str(e)[:80]}）→ 退回下载后统计"}
        return card
    # ArcGIS 会把 outStatisticFieldName **自动转大写**（n_city → N_CITY）→ 比较必须不分大小写
    rowd_ci = {(k or "").lower(): v for k, v in (rowd or {}).items()}
    # 返回空 / 字段名对不上 ≠ 字段全空！绝不能因此把好源判死
    if not rowd_ci or not any(f"n_{k}".lower() in rowd_ci for k in probe):
        card["p2"] = {"verdict": "DEFERRED",
                      "reason": f"统计接口返回空或字段口径对不上（请求 {sorted(probe)}，拿到 {sorted(rowd)[:6]}）"
                                f" → 退回下载后统计，不当拒收理由"}
        return card

    nonnull = {k: int(rowd_ci.get(f"n_{k}".lower()) or 0) for k in probe}
    miss = {k: round(1 - nonnull[k] / total, 4) for k in nonnull}
    card["p2"] = {"verdict": "CONTRACTED", "total": total, "nonnull": nonnull,
                  "miss_rate": miss, "contract": {"city_max_miss": MAX_MISS["city"],
                                                  "zip_max_miss": MAX_MISS["zip"],
                                                  "min_keep": MIN_KEEP}}
    ns_ok = (miss.get("number", 1.0) <= 1 - MIN_KEEP) and (miss.get("street", 1.0) <= 1 - MIN_KEEP)
    via = ""
    if not ns_ok and "full" in miss:
        if miss.get("full", 1.0) <= 1 - MIN_KEEP:
            ns_ok, via = True, "full_addr"      # 整条地址列兜底（造刀时走 concat_addr）
    bad = [f for f in ("city", "zip") if miss.get(f, 0) > MAX_MISS[f]]
    if not ns_ok:
        card["p2"].update({"verdict": "REJECTED_P2",
                           "reason": f"服务端统计显示 number/street 非空率不到 {MIN_KEEP:.0%}"
                                     f"（缺失率 {miss}）→ 全量数据证伪，拒收"})
    elif bad:
        card["p2"].update({"verdict": "CONTRACTED_DOWNGRADED",
                           "reason": f"number/street 达标"
                                     + (f"（经整条地址列 {probe.get('full')} 兜底：号/街独立列缺失）" if via else "")
                                     + f"，但 {'/'.join(bad)} 缺失率超上限"
                                       f"（{miss}）→ 签约但降级：只进全量、不当 A 级交付"})
    else:
        card["p2"].update({"reason": "契约全项通过（缺失率 " + str(miss) + "）→ 可全量采集"
                                     + (f"；**必须用整条地址列 {probe.get('full')} 拆**（concat_addr）"
                                        if via else "")})
    if via:
        card["p2"]["via"] = via          # 告诉造刀环节：这源要用 concat_addr，不是 mapping
    card["verdict"] = card["p2"]["verdict"]
    return card


# ─────────────────────── 主流程 ───────────────────────

def save_card(card: dict) -> str:
    os.makedirs(CARD_DIR, exist_ok=True)
    p = os.path.join(CARD_DIR, card["source_id"].replace("/", "__") + ".json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(card, fh, ensure_ascii=False, indent=1, sort_keys=True)
    return p


def write_summary(cards: list[dict]) -> None:
    """汇总表：一行一个源，看板/排队都用它。"""
    old: dict[str, dict] = {}
    if os.path.exists(SUMMARY):
        for r in csv.DictReader(open(SUMMARY, encoding="utf-8-sig")):
            if r.get("source_id"):
                old[r["source_id"]] = r
    for c in cards:
        p1 = c.get("p1") or {}
        p2 = c.get("p2") or {}
        miss = p2.get("miss_rate") or {}
        cols = c.get("candidate_cols") or {}
        old[c["source_id"]] = {
            "source_id": c["source_id"], "state": c.get("state", ""),
            "county": c.get("county", ""), "channel": c.get("channel", ""),
            "protocol": c.get("protocol", ""), "p0": c.get("p0", ""),
            "p1": (p1.get("verdict") if isinstance(p1, dict) else p1) or c.get("p1", ""),
            "p2": (p2.get("verdict") if isinstance(p2, dict) else p2) or c.get("p2", ""),
            "verdict": c.get("verdict", ""), "fields_n": c.get("fields_n", ""),
            "cols": ",".join(f"{k}={v}" for k, v in sorted(cols.items()) if v),
            "total": p1.get("total", ""), "city_miss": miss.get("city", ""),
            "zip_miss": miss.get("zip", ""), "sample_hash": c.get("sample_hash", ""),
            "license_status": c.get("license_status", ""),
            "reason": (p2.get("reason") or p1.get("reason") or c.get("reason") or "")[:160],
            "checked_at": c.get("checked_at", ""),
        }
    with open(SUMMARY, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_COLS, extrasaction="ignore")
        w.writeheader()
        for sid in sorted(old):
            w.writerow(old[sid])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="", help="单个源 id")
    ap.add_argument("--todo", type=int, default=0, help="批量：挑 N 个还没采过的源过闸")
    ap.add_argument("--stage", default="", choices=["", "p0", "p1", "p2"])
    ap.add_argument("--all", action="store_true", help="p0→p1→p2 顺序跑（前置不过就停）")
    ap.add_argument("--main-sample", type=int, default=300)
    ap.add_argument("--interval", type=float, default=0.8, help="请求间隔（礼貌）")
    ap.add_argument("--require-license", action="store_true",
                    help="缺许可直接拒（默认只记「待补」，不拦采集）")
    a = ap.parse_args(argv)

    rows = load_table()
    if a.source:
        todo = pick_todo(rows, 1, only=a.source)
    elif a.todo:
        todo = pick_todo(rows, a.todo)
    else:
        raise SystemExit("要么 --source，要么 --todo N")

    if not todo:
        print("没有需要过闸的源（都已采集 / 已拉黑 / 已有预检卡）")
        return

    pol = Politeness(min_interval=a.interval, max_concurrency=1, max_retries=1, timeout=30)
    cards: list[dict] = []
    for row in todo:
        sid = row["source_id"]
        t0 = time.time()
        card = stage_p0(row, pol)
        if a.require_license and card.get("license_status") == "unknown":
            card.update({"verdict": "REJECTED_P0", "reason": "缺许可（--require-license）"})
        if a.all or a.stage == "p1":
            if card.get("verdict") in ("PASS_P0", "PASS_P1", "NEEDS_DOWNLOAD"):
                card = stage_p1(row, pol, card, main_n=a.main_sample)
        if a.all or a.stage == "p2":
            if (card.get("verdict") or "").startswith("PASS"):
                card = stage_p2(row, pol, card)
        save_card(card)
        cards.append(card)
        print(f"  {sid:44s} P0={card.get('p0','-'):16s} P1={(card.get('p1') or {}).get('verdict','-') if isinstance(card.get('p1'), dict) else '-'}"
              f"  P2={(card.get('p2') or {}).get('verdict','-') if isinstance(card.get('p2'), dict) else '-'}"
              f"  → {card.get('verdict','-')}  ({time.time() - t0:.0f}s)", flush=True)
        print(f"      {(card.get('p2') or {}).get('reason') or (card.get('p1') or {}).get('reason') or card.get('reason', '')}",
              flush=True)
    write_summary(cards)
    cnt: dict[str, int] = {}
    for c in cards:
        k = str(c.get("verdict") or "?")
        cnt[k] = cnt.get(k, 0) + 1
    print(f"\n预检完成 {len(cards)} 个源 → {SUMMARY}｜" +
          " ".join(f"{k}={v}" for k, v in sorted(cnt.items())))
    print(f"预检卡：{CARD_DIR}/｜样本：{SAMPLE_DIR}/（只提交汇总表，样本走 Release 不占仓库）")


if __name__ == "__main__":
    main()
