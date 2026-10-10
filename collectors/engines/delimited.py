"""刀 B：分隔符文本文件（CSV / TSV / TXT / ZIP 打包 / SHP）。

形态特征：直链下载，首行是表头，字符集不定（utf-8-sig / utf-8 / latin-1）
切法：整文件下载 → 嗅探分隔符（, ; \\t |）→ DictReader 逐行
      zip 自动解包，内部 CSV/TXT 全读；SHP 走可选依赖 pyshp（缺了就记 skipped，不炸）
"""
from __future__ import annotations

import os
from typing import Iterator

from ..filedl import fetch_file, iter_rows
from .base import Engine, EngineError, NotMyMeat, RawBundle, SourceBroken, SourceCtx

DELIM_EXT = (".csv", ".tsv", ".txt", ".psv", ".zip", ".shp", ".dbf")


class ENGINE(Engine):
    name = "delimited"
    version = 1
    probe_cost = 0.8                # 本地解析，最便宜，排前面
    probe_reusable = True           # 文件已落盘，全量复用，不必二次下载
    formats = ("csv", "tsv", "txt", "zip", "shp", "dbf", "text/csv")

    def static_score(self, row: dict) -> float:
        u = (row.get("url") or "").lower().split("?")[0]
        ch = (row.get("channel") or "").upper()
        fmt = (row.get("fmt") or "").lower()
        s = 0.0
        if u.endswith(DELIM_EXT):
            s = 0.85
        if any(f in fmt for f in ("csv", "tsv", "txt", "shp", "zip", "dbf", "shapefile")):
            s = max(s, 0.85)
        if ch == "CSV_DIRECT":
            s = max(s, 0.90)
        elif ch == "FILE_DOWNLOAD":
            s = max(s, 0.70)
        return s

    def sniff_score(self, content_type: str, head: bytes) -> float:
        ct = (content_type or "").lower()
        if "zip" in ct or head[:2] == b"PK":
            return 0.80
        if "csv" in ct or "tab-separated" in ct:
            return 0.90
        first = head.split(b"\n", 1)[0]
        if first.count(b",") >= 2 or first.count(b";") >= 2 or first.count(b"\t") >= 2:
            return 0.70
        if first.count(b",") == 0 and first.count(b"\t") == 0 and head[:1] in (b"{", b"["):
            return 0.0
        return 0.0

    def fetch(self, pol, ctx: SourceCtx, budget_s: int, probe: bool) -> RawBundle:
        info = fetch_file(pol, ctx.url, ctx.work_dir)
        if not info.get("bytes"):
            raise SourceBroken("下载 0 字节")
        low = info["path"].lower()
        if info["kind"] == "zip":
            members = [m for m in info.get("members", [])
                       if m.lower().endswith((".csv", ".txt", ".shp", ".dbf"))]
            if not members:
                raise NotMyMeat(f"zip 内无可用成员: {info.get('members', [])[:5]}")
        elif not low.endswith(DELIM_EXT + (".tsv", ".psv")):
            raise NotMyMeat(f"后缀不在本刀能力内: {os.path.basename(low)}")
        bundle = RawBundle(engine=self.name, work_dir=ctx.work_dir,
                           payload={"info": info}, nbytes=int(info.get("bytes") or 0))
        # 探针也必须整文件下载（格式所限，无法只取前 N 行）—— 记进 note，级联器据此选刀次序
        bundle.note = "探针=整文件下载" if probe else ""
        return bundle

    def rows(self, bundle: RawBundle) -> Iterator[dict]:
        yield from iter_rows(bundle.payload["info"])

    def self_test(self):
        from ..filedl import _iter_csv_bytes
        rows = list(_iter_csv_bytes("num,street\n1,A St\n2,B St\n".encode()))
        assert len(rows) == 2 and rows[0]["num"] == "1", rows
        rows2 = list(_iter_csv_bytes("num;street\n3;C St\n".encode("latin-1")))
        assert rows2 and rows2[0]["street"] == "C St"
        assert self.sniff_score("text/csv", b"a,b,c") >= 0.9
        return True, "CSV/分号/latin-1 解析 OK"
