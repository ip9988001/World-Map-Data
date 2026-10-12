"""存量污染剔除 —— 针对 normalize.py 正则两个系统性 bug 的签名清洗。

背景（2026-10-11 已修并合主干）：
  ① Python re.sub 不认 JS 风格的 `$1` → 输出字面量 "$1" → 街道归一后变成 "1"
     （受影响约 200 源 / 600 万条）
  ② 无 replace 时 `re.sub(..., "")` 把整段删空 → 街道/号变 0 条
     （受影响 17 源 / 34 万条）
  本次要剔的是**已经进了数据集的那批**（按签名扫出来是 498 条：ND 441 / WY 56 / MS 1）。

做法：
  · 流式扫 out/final2/by_state_clean/*.ndjson.gz，命中签名 → 丢弃（不进新集）
  · 写出 out/final3/（每州一个干净分片）+ 报告：丢了多少、按源分布
  · 产出 data/重采清单-正则污染.csv（受污染源 → 走 v2 链路重采：原始档已在盘上就只花 CPU）
  · 原始分片**原地不动**（只读），随时可回滚

签名（从严，避免误杀真数据）：
  street 恰好是 "1" / "$1" / 纯数字 1-3 位，或字段里出现字面量 "$1" / "$2"
用法：
  python scripts/clean_contaminated.py --dry        # 只报告
  python scripts/clean_contaminated.py             # 写干净分片 + 重采清单
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import os
import re
import sys
from collections import Counter

SRC_DIR = "out/final2/by_state_clean"
DST_DIR = "out/final3"
RETRY_CSV = "data/重采清单-正则污染.csv"
BAD_STREET = re.compile(r"^(\$\d+|1)$")      # 街名字面量 "$1" / 归一后被剥成 "1"（这才是那两个 bug 的签名）
LITERAL = re.compile(r"\$\d+\b")


def is_bad(rec: dict, mode: str) -> str:
    """返回污染签名（空字符串=干净）。mode: strict 只杀 street 签名；loose 连字段内字面量也杀。"""
    st = str(rec.get("street", ""))
    if BAD_STREET.match(st):
        return f"street={st}"
    if mode == "loose":
        for k, v in rec.items():
            if isinstance(v, str) and LITERAL.search(v):
                return f"{k}含字面量$n"
    return ""


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC_DIR)
    ap.add_argument("--dst", default=DST_DIR)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--mode", choices=("strict", "loose"), default="strict")
    a = ap.parse_args(argv)

    shards = sorted(glob.glob(os.path.join(a.src, "*.ndjson.gz")))
    if not shards:
        raise SystemExit(f"找不到分片: {a.src}/*.ndjson.gz")
    if not a.dry:
        os.makedirs(a.dst, exist_ok=True)

    tot_kept = tot_drop = 0
    by_state: Counter = Counter()
    by_source: Counter = Counter()
    samples: list[str] = []
    for shard in shards:
        name = os.path.basename(shard)
        kept = drop = 0
        out = None
        if not a.dry:
            out = gzip.open(os.path.join(a.dst, name), "wt", encoding="utf-8")
        try:
            with gzip.open(shard, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    sig = is_bad(rec, a.mode)
                    if sig:
                        drop += 1
                        by_state[rec.get("state", "?")] += 1
                        by_source[rec.get("source", "?")] += 1
                        if len(samples) < 10:
                            samples.append(f"{rec.get('source')} | {rec.get('number')} "
                                           f"{rec.get('street')} ({sig})")
                        continue
                    kept += 1
                    if out:
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        finally:
            if out:
                out.close()
        tot_kept += kept
        tot_drop += drop
        print(f"{name:20s} 保留 {kept:>9,}  丢弃 {drop:>6,}", flush=True)

    print(f"\n合计：保留 {tot_kept:,}  丢弃 {tot_drop:,}"
          f"{'（dry-run，未写文件）' if a.dry else f' → {a.dst}/'}")
    if by_state:
        print("按州：", dict(by_state.most_common(10)))
        print("按源 Top：", dict(by_source.most_common(10)))
        print("样本：")
        for s in samples:
            print("  ", s)
    if not a.dry and by_source:
        with open(RETRY_CSV, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["source_id", "dropped_rows", "note"])
            for sid, n in by_source.most_common():
                w.writerow([sid, n, "正则污染签名命中，走 v2 链路重采（原始档优先复用）"])
        print(f"重采清单：{RETRY_CSV}（{len(by_source)} 个源）")


if __name__ == "__main__":
    main()
