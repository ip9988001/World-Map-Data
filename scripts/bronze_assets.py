"""原始档（bronze）持久化 —— 让 GitHub runner 这种一次性环境也能「下载一次、长期重洗」。

为什么需要：GH runner 跑完即销毁，`raw_archive/` 不留。没有持久化，
「同一份原始数据反复换规则重洗」这条核心价值在 GH 上就只剩「同一次 job 内」。
本脚本把原始档按州打包成 Release 资产（tag `bronze`，每州一个 zip，--clobber 更新），
采集 job 开工前自动恢复、收工后自动回存。

用法：
  python scripts/bronze_assets.py states --plan out/plan.json --lane 3     # 该泳道涉及的州
  python scripts/bronze_assets.py pack   --states OH,TX --out /tmp/bronze  # 打包（跳过大文件）
  python scripts/bronze_assets.py restore --dir /tmp/bronze --archive raw_archive
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import zipfile

MAX_ZIP_MB = 400          # 单州 zip 超过这个大小就跳过（Release 资产虽支持 2GB，但上传耗时）
ARCHIVE = "raw_archive"


def states_from_plan(plan_path: str, lane: int | None) -> list[str]:
    plan = json.load(open(plan_path, encoding="utf-8"))
    lanes = plan.get("lanes") or []
    if lane is not None:
        lanes = [L for L in lanes if L.get("lane") == lane]
    out = sorted({(it.get("state") or it["source_id"].split("/")[1]).upper()
                  for L in lanes for it in L["queue"]})
    return out


def bronze_states(archive: str = ARCHIVE) -> list[str]:
    root = os.path.join(archive, "us")
    return sorted(d.upper() for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d))) if os.path.isdir(root) else []


def pack(states: list[str], out_dir: str, archive: str = ARCHIVE) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for st in states:
        src = os.path.join(archive, "us", st.lower())
        if not os.path.isdir(src):
            continue
        dst = os.path.join(out_dir, f"bronze-{st}.zip")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for base, _dirs, files in os.walk(src):
                for f in files:
                    p = os.path.join(base, f)
                    z.write(p, os.path.relpath(p, archive))
        mb = os.path.getsize(dst) / 1048576
        if mb > MAX_ZIP_MB:
            print(f"  {st}: {mb:.0f}MB 超过 {MAX_ZIP_MB}MB 上限 → 跳过（避免拖慢 job）")
            os.remove(dst)
            continue
        print(f"  {st}: 打包 {mb:.1f}MB")
        made.append(dst)
    return made


def restore(src_dir: str, archive: str = ARCHIVE) -> int:
    n = 0
    os.makedirs(archive, exist_ok=True)
    for f in sorted(os.listdir(src_dir)) if os.path.isdir(src_dir) else []:
        if not f.startswith("bronze-") or not f.endswith(".zip"):
            continue
        path = os.path.join(src_dir, f)
        try:
            with zipfile.ZipFile(path) as z:
                z.extractall(archive)
            n += 1
            print(f"  恢复 {f} → {archive}/")
        except Exception as e:
            print(f"  {f} 解包失败（忽略）: {type(e).__name__}: {e}")
    return n


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("states")
    p1.add_argument("--plan", required=True)
    p1.add_argument("--lane", type=int, default=-1)

    p2 = sub.add_parser("pack")
    p2.add_argument("--states", default="")
    p2.add_argument("--all", action="store_true", help="打包本地已有的全部州")
    p2.add_argument("--out", default="/tmp/bronze")
    p2.add_argument("--archive", default=ARCHIVE)

    p3 = sub.add_parser("restore")
    p3.add_argument("--dir", default="/tmp/bronze")
    p3.add_argument("--archive", default=ARCHIVE)

    a = ap.parse_args(argv)
    if a.cmd == "states":
        sts = states_from_plan(a.plan, None if a.lane < 0 else a.lane)
        print(" ".join(sts))
    elif a.cmd == "pack":
        sts = bronze_states(a.archive) if a.all else \
            [s.strip().upper() for s in a.states.split(",") if s.strip()]
        made = pack(sts, a.out, a.archive)
        print(f"打包完成 {len(made)} 个州 → {a.out}")
    else:
        n = restore(a.dir, a.archive)
        print(f"恢复 {n} 个州的原始档")


if __name__ == "__main__":
    main()
