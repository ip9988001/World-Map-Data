"""源体检：亲自抓几百条原始数据看清楚到底长什么样。

遇到「整源 0 条」「丢弃率异常高」时，先跑这个，不要靠猜。
每个源都是真人做的，数据通常没错 —— 先看是**我们的映射错了**，还是**它本来就不是门牌点数据**。

用法：
  python scripts/inspect_source.py us/ms/lauderdale --sample 500
  python scripts/inspect_source.py us/wa/yakima --sample 300 --show 3
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.arcgis import layer_meta, normalize_layer_url            # noqa: E402
from collectors.normalize import (mapping_sanity, norm_number, norm_street,  # noqa: E402
                                  norm_state, norm_zip, parse_mapping_from_table,
                                  resolve, to_record_ex)
from collectors.politeness import Politeness                             # noqa: E402

TABLE = "data/源总表-执行序.csv"
COUNT_FIELDS = re.compile(r"^\d{1,6}[A-Z]?$")


def find_row(source_id: str) -> dict:
    for r in csv.DictReader(open(TABLE, encoding="utf-8-sig")):
        if r["source_id"] == source_id:
            return r
    raise SystemExit(f"源总表里没有 {source_id}")


def sample_features(pol: Politeness, url: str, n: int) -> list[dict]:
    u = normalize_layer_url(url)
    params = {"where": "1=1", "outFields": "*", "returnGeometry": "false",
              "resultRecordCount": str(n), "f": "json"}
    d = pol.get_json(f"{u}/query", params)
    if "error" in d:
        raise SystemExit(f"取样失败: {d['error']}")
    return [f.get("attributes", {}) for f in (d.get("features") or [])]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source_id")
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--show", type=int, default=3, help="打印几条完整原始记录")
    a = ap.parse_args(argv)

    row = find_row(a.source_id)
    mapping = parse_mapping_from_table(row)
    pol = Politeness(min_interval=0.8, max_concurrency=1, max_retries=1, timeout=25)

    print(f"=== {a.source_id} ===")
    print(f"url      : {row['url']}")
    print(f"channel  : {row['channel']}   priority: {row['priority']}   state: {row['state']}")
    print(f"mapping  : number={row.get('number')}")
    print(f"           street={row.get('street')}")
    print(f"           city={row.get('city_f')}  region={row.get('region')}  zip={row.get('postcode')}")

    try:
        meta = layer_meta(pol, row["url"])
        print(f"layer    : {meta['name']}  maxRecordCount={meta['maxRecordCount']}  "
              f"pagination={meta['supportsPagination']}  oid={meta['objectIdField']}")
        print(f"字段数   : {len(meta['fields'])}")
    except Exception as e:
        print(f"[warn] 元数据失败: {e}")

    rows = sample_features(pol, row["url"], a.sample)
    print(f"\n取样 {len(rows)} 条。空行 {sum(1 for r in rows if not r)} 条。")
    if not rows:
        return

    print(f"映射缺失字段: {mapping_sanity(rows[0], mapping) or '无'}")

    # 1) 各字段填充率 + 是否像门牌号
    keys = list(rows[0].keys())
    fill = {k: sum(1 for r in rows if str(r.get(k) or "").strip() not in ("", "None", "null")) for k in keys}
    print("\n--- 各字段填充率 / 像门牌号的比例 ---")
    for k in keys:
        vals = [str(r.get(k) or "").strip() for r in rows]
        numish = sum(1 for v in vals if COUNT_FIELDS.match(v))
        print(f"  {k:34s} 填充 {fill[k]/len(rows)*100:5.1f}%   像门牌号 {numish/len(rows)*100:5.1f}%")

    # 2) 用现有映射跑一遍，看丢弃原因
    kept = 0
    drops: dict[str, int] = {}
    for r in rows:
        rec, reason = to_record_ex(r, mapping, a.source_id, default_state=row.get("state", ""))
        if rec:
            kept += 1
        else:
            drops[reason] = drops.get(reason, 0) + 1
    print(f"\n--- 现有映射结果：合规 {kept}/{len(rows)}  "
          f"丢弃 {drops if drops else '无'} ---")

    # 3) 逐字段试探：哪个字段当门牌号/街道更合适
    print("\n--- 逐字段试探（把该字段当门牌号时，能救回多少条）---")
    for k in keys:
        if fill[k] / len(rows) < 0.3:
            continue
        ok = 0
        for r in rows:
            if norm_number(r.get(k)):
                ok += 1
        if ok / len(rows) >= 0.3:
            print(f"  {k:34s} → 可当门牌号 {ok}/{len(rows)} ({ok/len(rows)*100:.0f}%)")

    print("\n--- 原始记录样例 ---")
    for r in rows[:a.show]:
        print(json.dumps(r, ensure_ascii=False)[:600])
        n = resolve(r, mapping.get("number"), sep="")
        s = resolve(r, mapping.get("street"))
        print(f"   → number={norm_number(n)!r}  street={norm_street(s)!r}  "
              f"state={norm_state(resolve(r, mapping.get('region')), row.get('state',''))!r}  "
              f"zip={norm_zip(resolve(r, mapping.get('postcode')))!r}")

    print("\n--- 判定提示 ---")
    any_num = False
    for k in keys:
        vals = [str(r.get(k) or "").strip() for r in rows]
        if sum(1 for v in vals if COUNT_FIELDS.match(v)) / len(rows) >= 0.5:
            any_num = True
            print(f"  · 字段 {k} 里大量是门牌号形态 → 映射可能指错了字段")
    if not any_num:
        print("  · 所有字段里都找不到门牌号形态的值 → 很可能是「非门牌点源」（地块/中心点/路径），"
              "按铁律应剔除，不是映射问题")


if __name__ == "__main__":
    main()
