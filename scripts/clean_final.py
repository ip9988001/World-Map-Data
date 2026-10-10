"""清洗已合并的最终数据集：应用州码白名单 + 门牌号/ZIP 混淆等闸门，重出 by_state。

用法：python scripts/clean_final.py out/final2/merged.ndjson.gz
"""
from __future__ import annotations

import collections
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.normalize import STATE_CODES  # noqa: E402

NUM_OK = re.compile(r"^\d{1,6}[A-Z]?$")


def main() -> None:
    src = sys.argv[1]
    base = os.path.dirname(src)
    out = os.path.join(base, "merged.clean.ndjson.gz")
    split = os.path.join(base, "by_state_clean")
    os.makedirs(split, exist_ok=True)
    report = os.path.join(base, "clean_report.json")

    kept, drop = 0, collections.Counter()
    by_src = collections.defaultdict(collections.Counter)
    handles = {}
    try:
        with gzip.open(src, "rt", encoding="utf-8") as fh, \
             gzip.open(out, "wt", encoding="utf-8", compresslevel=6) as fw:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    drop["bad_json"] += 1
                    continue
                st = (d.get("state") or "").strip().upper()
                num = (d.get("number") or "").strip().upper()
                zp = (d.get("zip") or "").strip()
                street = (d.get("street") or "").strip().upper()
                reason = None
                if st not in STATE_CODES:
                    reason = "state_invalid"
                elif not NUM_OK.match(num):
                    reason = "number_not_house_number"
                elif zp and num == zp:
                    reason = "number_is_zip"
                elif zp and street.startswith(zp + " "):
                    reason = "street_is_address_blob"
                if reason:
                    drop[reason] += 1
                    by_src[reason][d.get("sources") and d["sources"][0] or d.get("source") or "?"] += 1
                    continue
                fw.write(json.dumps(d, ensure_ascii=False) + "\n")
                h = handles.get(st)
                if h is None:
                    h = handles[st] = gzip.open(os.path.join(split, st + ".ndjson.gz"),
                                                "wt", encoding="utf-8", compresslevel=6)
                h.write(json.dumps(d, ensure_ascii=False) + "\n")
                kept += 1
    finally:
        for h in handles.values():
            h.close()

    rep = {"输入文件": src, "保留": kept, "丢弃合计": sum(drop.values()),
           "丢弃明细": dict(drop.most_common()),
           "按源(丢弃)": {k: dict(v.most_common(5)) for k, v in by_src.items()},
           "输出": out, "按州目录": split, "州数": len(handles)}
    with open(report, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2)
    print(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
