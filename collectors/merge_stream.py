"""流式合并去重 —— 内存恒定，可处理上亿条。

输入：**已按整行排序**的 NDJSON 流（stdin 或文件）。
     因为每行都以 {"state": ... 开头，按整行排序即等于按
     (state, city, zip, street, number) 排序，去重键相同的行必然相邻。

输出：
    --out        合并去重后的 NDJSON（可选 .gz）
    --split-dir  按州切片，每州一个 .ndjson.gz
    --report     统计报告 JSON

去重键 = state|city|zip|street|number
同一键的多条记录合并为一条，并保留 sources 数组（来源可追溯）。
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
from collections import Counter


def _open_any(path, mode="rt"):
    if path in ("-", None):
        return io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    if path.endswith(".gz"):
        return gzip.open(path, mode, encoding=None if "b" in mode else "utf-8")
    return open(path, mode, encoding=None if "b" in mode else "utf-8")


def _writer(path):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "wb", compresslevel=6), encoding="utf-8")
    return open(path, "w", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("--out", required=True)
    ap.add_argument("--split-dir")
    ap.add_argument("--report")
    a = ap.parse_args(argv)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    out = _writer(a.out)
    split_dir = a.split_dir
    if split_dir:
        os.makedirs(split_dir, exist_ok=True)

    per_state = Counter()
    per_source = Counter()
    multi_source = 0
    total_in = bad = 0

    cur_key = None
    cur_rec = None
    state_fh = None
    state_name = None

    def flush():
        nonlocal multi_source
        if cur_rec is None:
            return
        rec = dict(cur_rec)
        if len(rec["sources"]) > 1:
            rec["sources"].sort()
            multi_source += 1
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        out.write(line)
        per_state[rec["state"] or "?"] += 1
        if state_fh is not None:
            state_fh.write(line)

    for raw in _open_any(a.infile):
        raw = raw.strip()
        if not raw:
            continue
        total_in += 1
        try:
            r = json.loads(raw)
        except Exception:
            bad += 1
            continue
        key = r.get("key")
        if not key:
            bad += 1
            continue
        if key != cur_key:
            flush()
            cur_key = key
            src = r.get("source") or r.get("sources") or "unknown"
            if isinstance(src, list):
                srcs = list(src)
            else:
                srcs = [src]
            cur_rec = {"state": r.get("state", ""), "city": r.get("city", ""),
                       "zip": r.get("zip", ""), "street": r.get("street", ""),
                       "number": r.get("number", ""), "key": key, "sources": srcs}
            for s in srcs:
                per_source[s] += 1
            if split_dir:
                st = cur_rec["state"] or "XX"
                if st != state_name:
                    if state_fh is not None:
                        state_fh.close()
                    state_name = st
                    state_fh = _writer(os.path.join(split_dir, f"{st}.ndjson.gz"))
        else:
            for s in (r.get("sources") or ([r["source"]] if r.get("source") else [])):
                if s not in cur_rec["sources"]:
                    cur_rec["sources"].append(s)
                    per_source[s] += 1
    flush()
    out.close()
    if state_fh is not None:
        state_fh.close()

    report = {"输入行数": total_in, "非法行数": bad, "去重后唯一记录": sum(per_state.values()),
              "重复条数": total_in - bad - sum(per_state.values()),
              "多源交叉命中": multi_source, "覆盖州数": len(per_state),
              "按州": dict(per_state.most_common()), "按来源": dict(per_source.most_common(60))}
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k not in ("按州", "按来源")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
