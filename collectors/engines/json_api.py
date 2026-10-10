"""刀 C：JSON / GeoJSON / Socrata SODA / 通用 JSON API。

形态特征：
  · 文件形态：.json / .geojson，FeatureCollection 或裸数组或 {data:[...]} / {results:[...]}
  · API 形态：Socrata /resource/xxxx.json?$limit=&$offset= （美国市政数据平台最常用）
  · 通用形态：任意 GET 返回 JSON，里面某个键下挂着对象数组
切法：递归找"第一个像记录集的对象数组"，键名启发式 + 深度上限；
      分页只用 Socrata 的 $limit/$offset，其他一律单请求（多页靠 link 头或断点由上层重试）
"""
from __future__ import annotations

import json
import os
import urllib.parse

from .base import (ADDR_KEY_HINTS, Engine, EngineError, NotMyMeat, RawBundle,
                   SourceBroken, SourceCtx)

LIST_KEYS = ("features", "results", "data", "rows", "records", "value",
             "@graph", "items", "entries", "objects", "content")
MAX_DEPTH = 6


def _pick_records(doc, depth: int = 0):
    """在任意 JSON 里找出「记录数组」：list[dict]，且字典键里带地址味或至少 3 个键。"""
    if depth > MAX_DEPTH:
        return None
    if isinstance(doc, list):
        if doc and all(isinstance(x, dict) for x in doc[:10]):
            keys = set()
            for x in doc[:20]:
                keys.update(x.keys())
            score = (1 if any(ADDR_KEY_HINTS.search(str(k)) for k in keys) else 0) \
                + (1 if len(keys) >= 3 else 0)
            return (doc, score) if score else (doc, 0)
        for x in doc[:5]:
            got = _pick_records(x, depth + 1)
            if got:
                return got
        return None
    if isinstance(doc, dict):
        # GeoJSON：geometry 里没字段，属性在 properties
        for k in LIST_KEYS:
            if k in doc and isinstance(doc[k], list):
                got = _pick_records(doc[k], depth + 1)
                if got:
                    return got
        best = None
        for k, v in doc.items():
            if isinstance(v, (list, dict)):
                got = _pick_records(v, depth + 1)
                if got and (best is None or got[1] > best[1]):
                    best = got
        return best
    return None


def _flatten(rec: dict) -> dict:
    """GeoJSON Feature → 把 properties 摊平（几何不要）。"""
    if isinstance(rec, dict) and "properties" in rec and isinstance(rec["properties"], dict):
        out = dict(rec["properties"])
        for k in ("geometry", "type", "id"):
            if k in rec and k not in out:
                out.setdefault("_geo_" + k, rec[k] if k != "geometry" else None)
        return out
    return rec


class ENGINE(Engine):
    name = "json_api"
    version = 1
    probe_cost = 1.2
    probe_reusable = True
    formats = ("json", "geojson", "socrata", "api")

    def static_score(self, row: dict) -> float:
        u = (row.get("url") or "").lower()
        fmt = (row.get("fmt") or "").lower()
        s = 0.0
        if ".geojson" in u:
            s = 0.90
        elif "/resource/" in u:
            s = 0.90                                   # Socrata
        elif u.endswith(".json") or "format=json" in u or "/api/" in u:
            s = 0.72
        if "json" in fmt or "geojson" in fmt:
            s = max(s, 0.80)
        return s

    def sniff_score(self, content_type: str, head: bytes) -> float:
        ct = (content_type or "").lower()
        h = head.lstrip()[:4096]
        if not h.startswith((b"{", b"[")):
            return 0.0
        if b'"type": "FeatureCollection"' in h or b'"type":"FeatureCollection"' in h:
            return 0.92
        if b'"features"' in h:
            return 0.85
        if b'"results"' in h or b'"rows"' in h or b'"data"' in h:
            return 0.55
        if "json" in ct:
            return 0.35
        return 0.2

    # ── 取数 ───────────────────────────────────────────────
    def _is_socrata(self, url: str) -> bool:
        return "/resource/" in url.lower() or "/api/views/" in url.lower()

    def fetch(self, pol, ctx: SourceCtx, budget_s: int, probe: bool) -> RawBundle:
        url = ctx.url
        out = os.path.join(ctx.work_dir, "json.ndjson")
        want = 200 if probe else 0
        n = 0
        pages = 0
        doc0 = None
        with open(out, "w", encoding="utf-8") as fh:
            if self._is_socrata(url):
                limit = 1000 if not want else min(want, 1000)
                offset = 0
                while True:
                    q = urllib.parse.urlparse(url)
                    params = dict(urllib.parse.parse_qsl(q.query))
                    params["$limit"] = str(limit)
                    params["$offset"] = str(offset)
                    full = urllib.parse.urlunparse(q._replace(query=urllib.parse.urlencode(params)))
                    d = pol.get_json(full)
                    got = _pick_records(d)
                    if not got:
                        if pages == 0:
                            raise NotMyMeat("Socrata 响应里没有记录数组")
                        break
                    recs = [_flatten(r) for r in got[0]]
                    for r in recs:
                        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    n += len(recs)
                    pages += 1
                    if len(recs) < limit or (want and n >= want):
                        break
                    offset += limit
            else:
                raw = pol.fetch(url, binary=True)      # → (bytes, status)
                body = raw[0] if isinstance(raw, tuple) else raw
                if isinstance(body, (bytes, bytearray)):
                    if self.is_html_error(bytes(body)):
                        raise SourceBroken("JSON 端点返回 HTML 错误页")
                    try:
                        d = json.loads(body.decode("utf-8-sig", "replace"))
                    except Exception as e:
                        raise NotMyMeat(f"响应不是合法 JSON: {str(e)[:120]}") from e
                else:
                    d = body          # get_json 风格的假想返回（实际不会走到）
                doc0 = d
                got = _pick_records(d)
                if not got:
                    raise NotMyMeat("JSON 里找不到记录数组")
                recs = [_flatten(r) for r in got[0]]
                if want:
                    recs = recs[:want]
                for r in recs:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                n = len(recs)
                pages = 1
        if n == 0:
            raise SourceBroken("JSON 端点返回 0 条记录")
        bundle = RawBundle(engine=self.name, work_dir=ctx.work_dir,
                           payload={"path": out, "root_keys": list(doc0.keys())[:20]
                                    if isinstance(doc0, dict) else [],
                                    "socrata": self._is_socrata(url)},
                           pages=pages)
        bundle.note = f"{n:,} 条 / {pages} 页"
        return bundle

    def rows(self, bundle: RawBundle):
        with open(bundle.payload["path"], encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def self_test(self):
        fc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]},
             "properties": {"HOUSENUM": "12", "STREET": "Main St"}}]}
        got = _pick_records(fc)
        assert got and got[1] >= 1
        flat = _flatten(got[0][0])
        assert flat["STREET"] == "Main St" and "geometry" not in flat
        got2 = _pick_records({"data": {"rows": [{"num": 1, "str": "A"}]}})
        assert got2 and got2[0][0]["num"] == 1
        return True, "FeatureCollection 摊平 + 嵌套记录集定位 OK"
