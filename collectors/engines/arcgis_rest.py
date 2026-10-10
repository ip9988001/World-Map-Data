"""刀 A：ArcGIS REST 图层（MapServer / FeatureServer）。

形态特征：/rest/services/.../MapServer/N 或 ..../FeatureServer/N，JSON 响应
          {"features":[{"attributes":{...}}], "objectIdField":...}
切法：键集分页（OID 递增），深度翻页不会被服务端 400 拒绝。
"""
from __future__ import annotations

import json
import os

from ..arcgis import fetch_layer, layer_count, layer_meta, normalize_layer_url
from .base import Engine, EngineError, RawBundle, SourceBroken, SourceCtx


class ENGINE(Engine):
    name = "arcgis_rest"
    version = 1
    probe_cost = 1.0
    probe_reusable = False          # 探针只取 200 条，全量必须重取
    formats = ("arcgis", "feature-server", "map-server")

    def static_score(self, row: dict) -> float:
        u = (row.get("url") or "").lower()
        ch = (row.get("channel") or "").upper()
        proto = (row.get("protocol") or "").lower()
        s = 0.0
        if "mapserver" in u or "featureserver" in u:
            s = 0.90
        if "/rest/services/" in u:
            s = max(s, 0.88)
        if "arcgis" in u or "arcgis" in proto:
            s = max(s, 0.80)
        if ch == "ARCGIS":
            s = max(s, 0.95)
        return s

    def sniff_score(self, content_type: str, head: bytes) -> float:
        h = head[:4096]
        if any(k in h for k in (b'"objectIdField"', b'"geometryType"', b'"maxRecordCount"')):
            return 0.95
        if b'"features"' in h and b'"attributes"' in h:
            return 0.90
        if b"arcgis" in h.lower():
            return 0.60
        return 0.0

    def fetch(self, pol, ctx: SourceCtx, budget_s: int, probe: bool) -> RawBundle:
        url = normalize_layer_url(ctx.url)
        out = os.path.join(ctx.work_dir, "arcgis.ndjson")
        try:
            meta = layer_meta(pol, url)
            total = layer_count(pol, meta["url"])
        except Exception as e:
            msg = str(e)
            if any(k in msg for k in ("404", "400", "403", "410", "Not Found")):
                raise SourceBroken(f"图层元信息不可用: {msg[:160]}") from e
            raise EngineError(f"元信息请求失败: {type(e).__name__}: {msg[:160]}") from e
        if not total:
            raise SourceBroken("图层要素数为 0（源本身空图层）")
        info = fetch_layer(pol, meta["url"], out, max_records=(200 if probe else 0),
                           page_size=None, oid_lo=None, max_seconds=budget_s)
        bundle = RawBundle(engine=self.name, work_dir=ctx.work_dir,
                           payload={"path": out, "info": info, "total": total,
                                    "layer_url": meta["url"], "oid_field": meta["objectIdField"]},
                           pages=info.get("pages", 0))
        bundle.note = f"图层共 {total:,} 条"
        return bundle

    def rows(self, bundle: RawBundle):
        path = bundle.payload["path"]
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def self_test(self):
        u = "https://x.gov/arcgis/rest/services/y/MapServer/3"
        assert normalize_layer_url(u) == u, "已是图层 URL 应当原样保留"
        assert normalize_layer_url(u.rsplit("/", 1)[0]).endswith("MapServer/0")
        row = {"FULLADDR": "1", "STREET": "x", "HOUSENUM": "2"}
        assert self.looks_like_addresses([row]) > 0.3
        assert self.static_score({"channel": "ARCGIS"}) >= 0.9
        return True, "URL 规范化 + 打分 + 地址味判定 OK"
