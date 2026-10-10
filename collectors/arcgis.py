"""ArcGIS FeatureServer 适配器 —— 覆盖全美 83% 的数据源。

采集方式：
  1. 读服务元数据，拿 maxRecordCount（官方单次上限）
  2. 分页 requestRecordCount = maxRecordCount × 0.9
  3. 按 OBJECTID 排序，resultOffset 逐页拉取
  4. 输出原始 NDJSON（一行一条 feature 属性）

不拉几何（returnGeometry=false），只取属性 —— 我们要的是地址，不是地图。
"""
from __future__ import annotations

import json
import os

from .politeness import Politeness, safe_page_size


def normalize_layer_url(url: str) -> str:
    """确保 url 指向具体图层（以数字结尾）。"""
    u = (url or "").rstrip("/")
    tail = u.rsplit("/", 1)[-1]
    if not tail.isdigit():
        u += "/0"
    return u


def layer_meta(pol: Politeness, url: str) -> dict:
    u = normalize_layer_url(url)
    d = pol.get_json(u, {"f": "json"})
    if "error" in d:
        raise RuntimeError(f"arcgis meta error: {d['error']}")
    return {
        "url": u,
        "name": d.get("name"),
        "maxRecordCount": d.get("maxRecordCount"),
        "geometryType": d.get("geometryType"),
        "supportsPagination": d.get("advancedQueryCapabilities", {}).get("supportsPagination", True),
        "objectIdField": d.get("objectIdField") or "OBJECTID",
        "fields": [f.get("name") for f in d.get("fields", [])],
    }


def layer_count(pol: Politeness, url: str) -> int:
    u = normalize_layer_url(url)
    d = pol.get_json(f"{u}/query", {"where": "1=1", "returnCountOnly": "true", "f": "json"})
    if "error" in d:
        raise RuntimeError(f"arcgis count error: {d['error']}")
    return int(d.get("count", 0))


def fetch_layer(pol: Politeness, url: str, out_ndjson: str,
                max_records: int | None = None, page_size: int | None = None,
                out_fields: str = "*", progress=None) -> dict:
    """拉取一个图层全部属性 → NDJSON。返回统计信息。"""
    meta = layer_meta(pol, url)
    total = layer_count(pol, meta["url"])
    ps = page_size or safe_page_size(meta["maxRecordCount"])
    oid = meta["objectIdField"]
    target = min(total, max_records) if max_records else total

    os.makedirs(os.path.dirname(out_ndjson) or ".", exist_ok=True)
    kept, offset, pages = 0, 0, 0
    with open(out_ndjson, "w", encoding="utf-8") as fh:
        while kept < target:
            want = min(ps, target - kept)
            d = pol.get_json(f"{meta['url']}/query", {
                "where": "1=1",
                "outFields": out_fields,
                "returnGeometry": "false",
                "orderByFields": oid,
                "resultOffset": offset,
                "resultRecordCount": want,
                "f": "json",
            })
            if "error" in d:
                raise RuntimeError(f"arcgis query error: {d['error']}")
            feats = d.get("features") or []
            if not feats:
                break
            for f in feats:
                fh.write(json.dumps(f.get("attributes", {}), ensure_ascii=False) + "\n")
            kept += len(feats)
            offset += len(feats)
            pages += 1
            if progress:
                progress(kept, target, pages)
            if offset >= total:
                break

    return {"url": meta["url"], "layer": meta["name"], "server_total": total,
            "fetched": kept, "pages": pages, "page_size": ps,
            "server_max_record_count": meta["maxRecordCount"], "fields": meta["fields"]}
