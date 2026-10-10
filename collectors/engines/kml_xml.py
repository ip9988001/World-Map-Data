"""刀 D：KML / KMZ / GML / 一般 XML（含 zipped 的 kmz）。

形态特征：<?xml 开头；KML 的 <Placemark> + <ExtendedData>/<SimpleData>；GML 的 <gml:featureMember>
切法：轻量正则抽取（不引 XML 库的命名空间地狱），把 Placemark/featureMember 块摊成 dict；
      kmz = zip 解包取 .kml。
"""
from __future__ import annotations

import os
import re
import zipfile

from .base import Engine, NotMyMeat, RawBundle, SourceBroken, SourceCtx

BLOCK = re.compile(r"<(?:[A-Za-z0-9_]+:)?(Placemark|featureMember|row|record)\b.*?</(?:[A-Za-z0-9_]+:)?\1>",
                   re.S | re.I)
TAG = re.compile(r"<([A-Za-z0-9_:\-]+)(?:\s[^>]*)?>(.*?)</\1>", re.S)
SIMPLE = re.compile(r'<(?:\w+:)?SimpleData[^>]*name="([^"]+)"[^>]*>(.*?)</(?:\w+:)?SimpleData>',
                    re.S | re.I)
DATA_EL = re.compile(r'<(?:\w+:)?Data[^>]*name="([^"]+)"[^>]*>\s*<(?:\w+:)?value>(.*?)</(?:\w+:)?value>',
                     re.S | re.I)


def _clean(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


def parse_kml_text(text: str):
    blocks = BLOCK.findall(text) if BLOCK.search(text) else []
    blocks = BLOCK.finditer(text)
    for m in blocks:
        body = m.group(0)
        rec: dict = {}
        for name, val in SIMPLE.findall(body):
            rec[name] = _clean(val)
        for name, val in DATA_EL.findall(body):
            rec[name] = _clean(val)
        if not rec:                       # 无扩展数据 → 用简单标签当字段
            for tag, val in TAG.findall(body):
                t = tag.split(":")[-1]
                if t in ("Point", "coordinates", "LineString", "Polygon", "MultiGeometry",
                         "outerBoundaryIs", "innerBoundaryIs", "linearRing", "Style", "name"):
                    continue
                v = _clean(val)
                if v and len(v) < 200:
                    rec[t] = v
        if rec:
            yield rec


class ENGINE(Engine):
    name = "kml_xml"
    version = 1
    probe_cost = 1.0
    probe_reusable = True
    formats = ("kml", "kmz", "gml", "xml")

    def static_score(self, row: dict) -> float:
        u = (row.get("url") or "").lower().split("?")[0]
        fmt = (row.get("fmt") or "").lower()
        s = 0.0
        if u.endswith((".kml", ".kmz")):
            s = 0.90
        elif u.endswith((".gml", ".xml")):
            s = 0.75
        if any(f in fmt for f in ("kml", "kmz", "gml", "xml")):
            s = max(s, 0.85)
        return s

    def sniff_score(self, content_type: str, head: bytes) -> float:
        ct = (content_type or "").lower()
        h = head.lstrip()[:4096].lower()
        if h.startswith(b"pk"):
            return 0.5
        if not h.startswith(b"<?xml") and b"<kml" not in h and b"<gml" not in h:
            return 0.0
        if b"<placemark" in h:
            return 0.95
        if b"<gml" in h or b"featuremember" in h:
            return 0.7
        return 0.4

    def fetch(self, pol, ctx: SourceCtx, budget_s: int, probe: bool) -> RawBundle:
        out = os.path.join(ctx.work_dir, "xml.ndjson")
        low = ctx.url.lower().split("?")[0]
        text = ""
        if low.endswith(".kmz"):
            zip_path = os.path.join(ctx.work_dir, "dl.kmz")
            pol.download_to_file(ctx.url, zip_path)
            with zipfile.ZipFile(zip_path) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".kml")]
                if not names:
                    raise NotMyMeat("kmz 内没有 kml")
                text = z.read(names[0]).decode("utf-8", "replace")
        else:
            raw = pol.fetch(ctx.url, binary=True)     # → (bytes, status)
            body = raw[0] if isinstance(raw, tuple) else raw
            if isinstance(body, (bytes, bytearray)):
                if self.is_html_error(bytes(body)):
                    raise SourceBroken("端点返回 HTML 错误页")
                text = bytes(body).decode("utf-8-sig", "replace")
            else:
                text = str(body)
        if "<" not in text[:200]:
            raise NotMyMeat("响应不像 XML")
        n = 0
        first = None
        with open(out, "w", encoding="utf-8") as fh:
            import json
            for rec in parse_kml_text(text):
                first = first or rec
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
                if probe and n >= 200:
                    break
        if n == 0:
            raise NotMyMeat("XML 里找不到 Placemark/featureMember 记录块")
        bundle = RawBundle(engine=self.name, work_dir=ctx.work_dir,
                           payload={"path": out, "first": first}, pages=1)
        bundle.note = f"{n:,} 条"
        return bundle

    def rows(self, bundle: RawBundle):
        import json
        with open(bundle.payload["path"], encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def self_test(self):
        kml = """<?xml version="1.0"?><kml><Document><Placemark>
        <ExtendedData><Data name="HOUSENUM"><value>7</value></Data>
        <Data name="STREET"><value>Elm St</value></Data></ExtendedData>
        </Placemark></Document></kml>"""
        recs = list(parse_kml_text(kml))
        assert recs and recs[0]["HOUSENUM"] == "7" and recs[0]["STREET"] == "Elm St", recs
        return True, "KML ExtendedData 抽取 OK"
