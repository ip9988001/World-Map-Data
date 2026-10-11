"""本地形态摊平（零网络）—— 把原始归档里的字节摊成「字段行」流。

与引擎方案的区别：这一层**纯本地**，所以可以随便多写几种形态，失败成本只是几毫秒 CPU，
不涉及任何重新下载。规则（清洗）在这一层之上跑。

支持：ndjson / csv,tsv,txt(含 BOM、分号、latin-1) / json,geojson / kml,kmz,xml / zip 递归 / shp(可选依赖)
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from typing import Iterator

from .engines.json_api import _flatten as _json_flatten, _pick_records
from .engines.kml_xml import parse_kml_text
from .filedl import _iter_csv_bytes, _read_shapefile

CSV_EXT = (".csv", ".tsv", ".txt", ".psv")
JSON_EXT = (".json", ".geojson", ".ndjson", ".jsonl")
XML_EXT = (".kml", ".xml", ".gml")
UNPACKED = set()
GDB_SEEN: set[str] = set()


def _gdb_root(path: str) -> str:
    """FileGDB 是**目录**（一堆 a00000001.gdbtable）→ 找到它的根目录路径，\n    整个目录只当一次数据源（否则每个内部文件都会被当成独立表）。"""
    low = path.lower()
    i = low.find(".gdb")
    return path[: i + 4] if i >= 0 else ""


def _read_gdb(root: str):
    """读 FileGDB（需 fiona）；没装就返回 None（上层会记为「缺解析依赖」而不是下载缺陷）。"""
    try:
        import fiona
    except ImportError:
        return None
    try:
        layers = fiona.listlayers(root)
    except Exception:
        return None
    for lyr in layers:
        try:
            with fiona.open(root, layer=lyr) as src:
                for feat in src:
                    props = dict(feat.get("properties") or {})
                    props.setdefault("__layer", lyr)
                    yield props
        except Exception:
            continue


def payload_files(archive: str) -> list[str]:
    out = []
    for base, _dirs, files in os.walk(archive):
        for f in files:
            if f.startswith("manifest.json"):
                continue
            out.append(os.path.join(base, f))
    return sorted(out)


def _unzip(path: str) -> str:
    """zip 在本地解包一次并缓存（原始 zip 本体不动）。"""
    d = path + ".unpacked"
    if d not in UNPACKED and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        try:
            with zipfile.ZipFile(path) as z:
                z.extractall(d)
        except Exception:
            pass
    UNPACKED.add(d)
    return d


def _iter_json(text: str) -> Iterator[dict]:
    got = _pick_records(json.loads(text))
    if not got:
        return
    for r in got[0]:
        if isinstance(r, dict):
            yield _json_flatten(r)


def iter_rows_from_archive(archive: str, max_bytes_per_file: int = 0) -> Iterator[dict]:
    """摊平一个源的全部原始文件 → dict 流。每条带 _file / _fmt 溯源标记。"""
    stack = payload_files(archive)
    seen: set[str] = set()
    while stack:
        path = stack.pop(0)
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        low = path.lower()
        name = os.path.basename(path)
        if low.endswith(".zip"):
            stack = payload_files(_unzip(path)) + stack
            continue
        if max_bytes_per_file and os.path.getsize(path) > max_bytes_per_file:
            continue
        root = _gdb_root(path)
        if root:
            if root in GDB_SEEN:
                continue
            GDB_SEEN.add(root)
            gen = _read_gdb(root)
            if gen is None:
                yield {"_file": os.path.basename(root), "__layer": "",
                       "_read_error": "MissingDependency: 需要 fiona 才能读 FileGDB(.gdb)"}
                continue
            try:
                for row in gen:
                    row.setdefault("_file", os.path.basename(root))
                    yield row
            except Exception as e:
                yield {"_file": os.path.basename(root),
                       "_read_error": f"{type(e).__name__}: {str(e)[:150]}"}
            continue
        try:
            if low.endswith(CSV_EXT):
                with open(path, "rb") as fh:
                    for row in _iter_csv_bytes(fh.read()):
                        row.setdefault("_file", name)
                        yield row
            elif low.endswith(".ndjson") or low.endswith(".jsonl"):
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(d, dict):
                            d.setdefault("_file", name)
                            yield d
                        elif isinstance(d, list):
                            for x in d:
                                if isinstance(x, dict):
                                    x.setdefault("_file", name)
                                    yield x
            elif low.endswith(JSON_EXT):
                with open(path, encoding="utf-8-sig", errors="replace") as fh:
                    for row in _iter_json(fh.read()):
                        row.setdefault("_file", name)
                        yield row
            elif low.endswith(XML_EXT):
                with open(path, encoding="utf-8-sig", errors="replace") as fh:
                    for row in parse_kml_text(fh.read()):
                        row.setdefault("_file", name)
                        yield row
            elif low.endswith(".shp"):
                gen = _read_shapefile(path)
                if gen is None:
                    # 缺 pyshp 不能当成「源没数据」（实测 us/ak/city_of_juneau 被误判成下载缺陷）
                    yield {"_file": name,
                           "_read_error": "MissingDependency: 需要 pyshp 才能读 shapefile(.shp)"}
                    continue
                for row in gen:
                    row.setdefault("_file", name)
                    yield row
        except Exception as e:
            yield {"_file": name, "_read_error": f"{type(e).__name__}: {str(e)[:150]}"}


def peek(archive: str, n: int = 300) -> list[dict]:
    """本地抽查：拿前 n 行看看（零成本，随时可看）。"""
    out = []
    for i, r in enumerate(iter_rows_from_archive(archive)):
        if i >= n:
            break
        out.append(r)
    return out
