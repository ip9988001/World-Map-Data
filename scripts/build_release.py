"""构建发布包 —— 把「旧数据集清洗后」与「本轮新采集」合并去重成稳定视图。

输入：
  out/final3/<ST>.ndjson.gz          旧数据集按州分片（已剔除 498 条正则污染）
  parsed/us/<st>/<src>/<rule>@vN.ndjson   本轮重采的规则产物
输出：
  out/release/<ST>.ndjson.gz         稳定视图（按州，每州一个文件，tag 复用）
  out/release/merged.clean.ndjson.gz 全量合并（单文件）
  data/release-manifest.json         行数 + sha256 + 生成时间（发布对账用）

去重口径：**新数据优先**（新采集记录同 key 覆盖旧记录），
实现用「外部排序 + 每 key 取最高优先级」，避免 2000 万条 key 全塞内存。
用法：
  python scripts/build_release.py --dry          # 只统计
  python scripts/build_release.py                # 写 out/release/
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys

OLD_DIR = "out/final3"
NEW_DIR = "parsed"
OUT_DIR = "out/release"
MANIFEST = "data/release-manifest.json"
NEW_PRIO = 2          # 新数据（本地重采，规则更新）
OLD_PRIO = 1          # 旧数据（已清洗）


def _source_state(sid: str) -> str:
    parts = sid.split("/")
    return parts[1].upper() if len(parts) >= 2 else "ZZ"


def collect_new() -> dict[str, list[str]]:
    """{州: [ndjson 路径]}"""
    out: dict[str, list[str]] = {}
    for path in glob.glob(os.path.join(NEW_DIR, "**", "*.ndjson"), recursive=True):
        rel = os.path.relpath(path, NEW_DIR).replace("\\", "/")
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] != "us":
            continue
        st = parts[1].upper()
        out.setdefault(st, []).append(path)
    return out


def _norm_payload(payload: str) -> str:
    """统一发布包的 schema（历史遗留格式混着很脏）：
      · 来源统一成 `sources` 列表（老分片是单值 `source`）
      · 剔掉内部字段 `_rule`（那是流水线自用的，不该出现在交付物里）
    """
    if '"_rule"' not in payload and '"sources"' in payload:
        return payload
    try:
        rec = json.loads(payload)
    except Exception:
        return payload
    rec.pop("_rule", None)
    src = rec.pop("source", None)
    if src and not rec.get("sources"):
        rec["sources"] = [src] if isinstance(src, str) else list(src)
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":"))


def _write_sorted_state(state: str, old_shard: str | None, new_files: list[str],
                        tmp_dir: str, out_path: str) -> tuple[int, int]:
    """写一个州的稳定分片：外部排序 + 每 key 取最高优先级。返回 (行数, 去重掉的数量)。"""
    raw = os.path.join(tmp_dir, f"{state}.tsv")
    n_old = n_new = 0
    with open(raw, "w", encoding="utf-8") as fh:
        if old_shard and os.path.exists(old_shard):
            with gzip.open(old_shard, "rt", encoding="utf-8") as src:
                for line in src:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    fh.write(f"{rec.get('key')}\t{OLD_PRIO}\t{line}\n")
                    n_old += 1
        for p in new_files:
            with open(p, encoding="utf-8") as src:
                for line in src:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    fh.write(f"{rec.get('key')}\t{NEW_PRIO}\t{line}\n")
                    n_new += 1
    srt = raw + ".sorted"
    subprocess.run(["sort", "-t\t", "-k1,1", "-k2,2r", "-S", "192M", "-o", srt, raw],
                   check=True)
    kept = 0
    prev_key = None
    with open(srt, encoding="utf-8") as fh, gzip.open(out_path, "wt", encoding="utf-8") as out:
        for line in fh:
            key, _prio, payload = line.split("\t", 2)
            if key == prev_key:
                continue
            prev_key = key
            out.write(_norm_payload(payload) + "\n")
            kept += 1
    os.remove(raw)
    os.remove(srt)
    return kept, (n_old + n_new - kept)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default=OLD_DIR)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-merged", action="store_true", help="不生成单文件合并版")
    a = ap.parse_args(argv)

    old_shards = {os.path.basename(p).split(".")[0]: p
                  for p in glob.glob(os.path.join(a.old, "*.ndjson.gz"))}
    new = collect_new()
    states = sorted(set(old_shards) | set(new))
    print(f"州数 {len(states)}｜旧分片 {len(old_shards)}｜本轮有新数据的州 {len(new)}", flush=True)

    tmp_dir = os.path.join(a.out, "_tmp")
    if not a.dry:
        os.makedirs(a.out, exist_ok=True)
        os.makedirs(tmp_dir, exist_ok=True)
    manifest = {"generated": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                "states": {}, "dedup_removed": 0, "rows": 0}
    for st in states:
        out_path = os.path.join(a.out, f"{st}.ndjson.gz")
        new_files = new.get(st, [])
        if a.dry:
            n_old = 0
            if old_shards.get(st):
                with gzip.open(old_shards[st], "rt", encoding="utf-8") as src:
                    n_old = sum(1 for _ in src)
            n_new = sum(sum(1 for _ in open(p, encoding="utf-8")) for p in new_files)
            print(f"  {st}: 旧 {n_old:,} + 新 {n_new:,}", flush=True)
            manifest["rows"] += n_old + n_new
            continue
        kept, dedup = _write_sorted_state(st, old_shards.get(st), new_files, tmp_dir, out_path)
        manifest["states"][st] = {"rows": kept, "dedup_removed": dedup,
                                  "sha256": sha256(out_path),
                                  "bytes": os.path.getsize(out_path)}
        manifest["rows"] += kept
        manifest["dedup_removed"] += dedup
        print(f"  {st}: 保留 {kept:,}（去重掉 {dedup:,}）", flush=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if not a.dry and not a.no_merged:
        merged = os.path.join(a.out, "merged.clean.ndjson.gz")
        with gzip.open(merged, "wt", encoding="utf-8") as out:
            for st in states:
                p = os.path.join(a.out, f"{st}.ndjson.gz")
                if os.path.exists(p):
                    with gzip.open(p, "rt", encoding="utf-8") as src:
                        shutil.copyfileobj(src, out)
        manifest["merged"] = {"bytes": os.path.getsize(merged), "sha256": sha256(merged),
                             "rows": manifest["rows"]}
        print(f"合并版 merged.clean.ndjson.gz：{os.path.getsize(merged) / 1048576:.1f}MB")
    if not a.dry:
        with open(MANIFEST, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"发布清单：{MANIFEST}｜合计 {manifest['rows']:,} 行")
    else:
        print(f"[dry] 合计 {manifest['rows']:,} 行")


if __name__ == "__main__":
    main()
