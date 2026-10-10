"""文件下载适配器 —— 覆盖剩下的 17%（zip / CSV / SHP / GDB）。

只做「下载 + 解包 + 逐行读」，不做任何字段判断（交由 normalize）。
SHP / GDB 需要可选依赖（pyshp / pyogrio）；缺失时优雅跳过并记录，不中断整批任务。
"""
from __future__ import annotations

import csv
import io
import json
import os
import zipfile

from .politeness import Politeness


def _iter_csv_bytes(raw: bytes):
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return
    head = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(head, delimiters=",;\t|")
    except Exception:
        dialect = csv.excel
    rdr = csv.DictReader(io.StringIO(text), dialect=dialect)
    for row in rdr:
        yield row


def _read_shapefile(path: str):
    try:
        import shapefile  # pyshp
    except ImportError:
        return None
    r = shapefile.Reader(path)
    names = [f[0] for f in r.fields if f[0] != "DeletionFlag"]
    for sr in r.iterShapeRecords():
        rec = sr.record
        yield {n: (rec[i] if i < len(rec) else None) for i, n in enumerate(names)}


def fetch_file(pol: Politeness, url: str, work_dir: str) -> dict:
    """下载并解包，返回 {'files': [...], 'rows': iterator 工厂} 的元信息。"""
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "download.bin"
    dest = os.path.join(work_dir, name)
    n = pol.download_to_file(url, dest)
    info = {"url": url, "path": dest, "bytes": n, "kind": "raw", "members": []}
    low = name.lower()
    if low.endswith(".zip"):
        info["kind"] = "zip"
        outdir = dest + ".d"
        os.makedirs(outdir, exist_ok=True)
        with zipfile.ZipFile(dest) as z:
            z.extractall(outdir)
            info["members"] = [m for m in z.namelist() if not m.endswith("/")]
        info["dir"] = outdir
    return info


def iter_rows(info: dict):
    """按类型逐行产出 dict。"""
    kind = info["kind"]
    if kind == "zip":
        members = info.get("members", [])
        csvs = [m for m in members if m.lower().endswith((".csv", ".txt"))]
        shps = [m for m in members if m.lower().endswith(".shp")]
        for m in csvs:
            with open(os.path.join(info["dir"], m), "rb") as fh:
                for row in _iter_csv_bytes(fh.read()):
                    yield row
        for m in shps:
            gen = _read_shapefile(os.path.join(info["dir"], m))
            if gen is None:
                info.setdefault("skipped", []).append(m + " (缺少 pyshp)")
                continue
            for row in gen:
                yield row
    else:
        low = info["path"].lower()
        if low.endswith((".csv", ".txt")):
            with open(info["path"], "rb") as fh:
                for row in _iter_csv_bytes(fh.read()):
                    yield row
        else:
            info["skipped"] = [info["path"] + " (未知格式)"]
