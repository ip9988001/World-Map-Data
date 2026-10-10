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
                out_fields: str = "*", progress=None,
                offset_start: int = 0, oid_hi: int | None = None) -> dict:
    """拉取一个图层全部属性 → NDJSON。

    ⚠️ 关键实现：使用**键集分页（keyset pagination）**而不是 resultOffset。
       ArcGIS 服务在 resultOffset 变深时会直接报 400
       （'Cannot perform query. Invalid query parameters'），
       表现为「任务成功但产出 0 条」。改为每页用
           where = "<oidField> > <上一页最大 OID>"
       即可无限深度翻页，且不依赖 offset。

    offset_start 保留仅为兼容旧调用（键集模式下会被忽略后丢弃前 N 条）。
    """
    meta = layer_meta(pol, url)
    total = layer_count(pol, meta["url"])
    ps = page_size or safe_page_size(meta["maxRecordCount"])
    oid = meta["objectIdField"]
    target = total if not max_records else min(total, max_records)

    os.makedirs(os.path.dirname(out_ndjson) or ".", exist_ok=True)
    kept, pages, skipped = 0, 0, 0
    last = None
    use_keyset = True
    with open(out_ndjson, "w", encoding="utf-8") as fh:
        while kept < target:
            want = min(ps, target - kept)
            if last is None:
                where = "1=1" if oid_hi is None else f"{oid} <= {oid_hi}"
            else:
                where = f"{oid} > {last}" if oid_hi is None else f"{oid} > {last} AND {oid} <= {oid_hi}"
            params = {
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "false",
                "resultRecordCount": want,
                "f": "json",
            }
            if use_keyset:
                params["orderByFields"] = oid
            try:
                d = pol.get_json(f"{meta['url']}/query", params)
            except Exception as e:
                if use_keyset:
                    # 个别服务不支持 orderByFields → 退回 offset 模式
                    use_keyset = False
                    continue
                raise
            if isinstance(d, dict) and "error" in d:
                if use_keyset:
                    use_keyset = False
                    continue
                raise RuntimeError(f"arcgis query error: {d['error']}")
            feats = d.get("features") or []
            if not feats:
                break
            short_page = len(feats) < want   # 服务端已到本块末尾
            max_oid = None
            for f in feats:
                a = f.get("attributes", {})
                v = a.get(oid)
                try:
                    vi = int(float(v))
                    max_oid = vi if max_oid is None else max(max_oid, vi)
                except Exception:
                    pass
                if skipped < offset_start:
                    skipped += 1
                    continue
                fh.write(json.dumps(a, ensure_ascii=False) + "\n")
                kept += 1
            pages += 1
            if progress:
                progress(kept, target, pages)
            if max_oid is None or (last is not None and max_oid <= last):
                break                     # 无法推进 → 结束，避免死循环
            last = max_oid
            if short_page:
                break
            if kept >= target:
                break

    return {"url": meta["url"], "layer": meta["name"], "server_total": total,
            "offset_start": offset_start, "fetched": kept, "pages": pages, "page_size": ps,
            "server_max_record_count": meta["maxRecordCount"], "fields": meta["fields"]}
