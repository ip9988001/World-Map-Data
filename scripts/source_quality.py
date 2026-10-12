"""源质量档案 + 分档（13 环节 · 第 9 步的账本侧 + 第 2 档「让好源自己浮出来」）。

输入：`progress/state.json`（第 9 步已经在每个源上落了 quality 底数）
输出：`data/源质量档案.csv`（每源一行：完整率 / 每街门牌号密度 / 脏值率 / 产量 / 评分 / 档位）

为什么要这张表：
  实测同一批源的质量差 10 倍 —— AK 每街 1.48 个门牌号（96% 的街道只有 1 个号）、
  CA 3.77、OR 15.12。好源差源一视同仁地排队采集，等于把产能浪费在刷不出地址的源上。
  这张表就是「采集优先级」和「退场」的唯一依据。

评分（0~100，四项加权，口径全部来自实测字段）：
  完整性 40｜每街门牌号密度 25｜产量 20｜干净度 15
档位：S ≥85 ｜ A ≥70 ｜ B ≥50 ｜ C <50 ｜ X = 未完成/失败（不进评分）

用法：
  python scripts/source_quality.py                 # 写 data/源质量档案.csv
  python scripts/source_quality.py --top 20        # 顺带打印最好/最差的源
"""
from __future__ import annotations

import argparse
import csv
import json
import os

STATE = "progress/state.json"
TABLE = "data/源总表-执行序.csv"
OUT = "data/源质量档案.csv"

FIELDS = ["source_id", "state", "county", "geoid", "done", "rule", "kept", "ratio",
          "city_miss", "zip_miss", "junk_rate", "per_street", "verdict", "grade_hint",
          "score", "tier", "note"]


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def load_table(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    out: dict = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            sid = (r.get("source_id") or "").strip()
            if sid:
                out[sid] = r
    return out


def score_of(st: dict):
    """四项加权打分。

    ⚠ 没有质量底数（老状态，第 9 步之前采的）→ **不评分**（返回 None），
    宁可直接标「未评测」也不给假分数：一个假 15 分会把好源误判成 C 档退场。
    下一轮采集回填 quality 后自动补上。
    """
    q = st.get("quality")
    if not q:
        return None, {}
    mr = q.get("miss_rate") or {}
    city_m = float(mr.get("city") if mr.get("city") is not None else 1.0)
    zip_m = float(mr.get("zip") if mr.get("zip") is not None else 1.0)
    per_street = float(q.get("per_street") or 0.0)
    junk = float(q.get("junk_rate") or 0.0)
    kept = int(st.get("kept") or 0)

    s_complete = max(0.0, 1.0 - (city_m + zip_m) / 2) * 40        # 完整性
    s_density = min(per_street / 8.0, 1.0) * 25                   # 每街门牌号密度
    s_yield = min(kept / 5000.0, 1.0) * 20                        # 产量
    s_clean = max(0.0, 1.0 - junk) * 15                           # 干净度
    total = s_complete + s_density + s_yield + s_clean
    return round(total, 1), {"完整性": round(s_complete, 1), "密度": round(s_density, 1),
                             "产量": round(s_yield, 1), "干净度": round(s_clean, 1)}


def tier_of(st: dict, score: float) -> str:
    if not st.get("done"):
        return "X"
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 50:
        return "B"
    return "C"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--table", default=TABLE)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args(argv)

    state = load_state(a.state)
    table = load_table(a.table)
    rows: list[dict] = []
    for sid, st in sorted(state.items()):
        if not isinstance(st, dict):
            continue
        t = table.get(sid) or {}
        q = st.get("quality") or {}
        mr = q.get("miss_rate") or {}
        score, parts = score_of(st)
        if score is None:
            tier = "N" if st.get("done") else "X"     # N=未评测（等下一轮采集回填质量底数）
            score = ""
        else:
            tier = tier_of(st, score)
        why = st.get("reason") or ""
        if tier == "X" and not why:
            why = st.get("kind") or st.get("error") or "未完成"
        rows.append({
            "source_id": sid, "state": t.get("state") or "", "county": t.get("county") or "",
            "geoid": t.get("geoid") or "", "done": int(bool(st.get("done"))),
            "rule": st.get("rule") or "", "kept": int(st.get("kept") or 0),
            "ratio": st.get("ratio") or "",
            "city_miss": mr.get("city", ""), "zip_miss": mr.get("zip", ""),
            "junk_rate": q.get("junk_rate", ""), "per_street": q.get("per_street", ""),
            "verdict": st.get("verdict") or "", "grade_hint": st.get("grade_hint") or "",
            "score": score, "tier": tier, "note": str(why)[:120],
        })

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    cnt: dict[str, int] = {}
    for r in rows:
        cnt[r["tier"]] = cnt.get(r["tier"], 0) + 1
    print(f"源质量档案：{a.out}｜{len(rows)} 个源｜档位 " +
          " ".join(f"{k}={cnt.get(k, 0)}" for k in ("S", "A", "B", "C", "N", "X")))
    good = [r for r in rows if r["tier"] in ("S", "A")]
    bad = [r for r in rows if r["tier"] == "C"]
    pr = lambda r: (r["score"] if r["score"] == "" else f"{r['score']:>5}")
    if a.top:
        print(f"最好的源（{a.top}）：" + ("（无）" if not good else ""))
        for r in sorted(good, key=lambda x: -float(x["score"]))[:a.top]:
            print(f"   {pr(r)} [{r['tier']}] {r['source_id']:42s} "
                  f"kept={r['kept']:>7,} 每街 {r['per_street']:>6} city缺 {r['city_miss']}")
        if bad:
            print(f"最差的源（{a.top}，C 档 → 应降权/退场）：")
            for r in sorted(bad, key=lambda x: float(x["score"]))[:a.top]:
                print(f"   {pr(r)} [C] {r['source_id']:42s} kept={r['kept']:>7,} "
                      f"每街 {r['per_street']:>6} city缺 {r['city_miss']} {r['note'][:30]}")
        if any(r["tier"] in ("N", "X") for r in rows):
            print("   ️N=未评测（老状态没有质量底数，下一轮采集自动回填）；X=未完成/失败")
    print("（档位只作采集优先级依据；A 级交付与否由发布闸门按字段实际填齐情况判定）")


if __name__ == "__main__":
    main()
