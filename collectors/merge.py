"""多源合并 —— 并集 + 去重 + 来源保留。

规则（已与需求方确认）：
  1. 主键 = (state, city, zip, 标准街道名, 门牌号)
  2. 多源命中同一主键 → 保留一条，但记录全部来源（sources 数组）
  3. 不做任何"覆盖式"取舍：是并集，不是取最新/取最大
  4. 来源权重仅用于字段补全，不用于删除数据
"""
from __future__ import annotations

import json
import os
from collections import Counter


def merge_ndjson(inputs: list[str], out_path: str, report_path: str | None = None) -> dict:
    seen: dict[str, dict] = {}
    total_in, bad = 0, 0
    per_source = Counter()

    for path in inputs:
        if not os.path.exists(path):
            continue
        src = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total_in += 1
                try:
                    r = json.loads(line)
                except Exception:
                    bad += 1
                    continue
                k = r.get("key")
                if not k:
                    bad += 1
                    continue
                cur = seen.get(k)
                if cur is None:
                    r["sources"] = [r.get("source", src)]
                    seen[k] = r
                    per_source[r["sources"][0]] += 1
                else:
                    s = r.get("source", src)
                    if s not in cur["sources"]:
                        cur["sources"].append(s)
                    cur["dup"] = cur.get("dup", 1) + 1

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for r in seen.values():
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    report = {
        "输入行数": total_in,
        "非法行数": bad,
        "去重后唯一记录": len(seen),
        "重复条数": total_in - bad - len(seen),
        "多源交叉命中的记录": sum(1 for r in seen.values() if len(r.get("sources", [])) > 1),
        "按来源": dict(per_source.most_common(30)),
        "按州": dict(Counter(r.get("state") or "?" for r in seen.values()).most_common(60)),
    }
    if report_path:
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return report
