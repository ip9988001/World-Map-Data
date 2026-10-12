"""原始层抓取（bronze）—— 只负责把源的全量数据搬回本地，**不看内容、不挑数据、不解析**。

铁律（2026-10-11 用户拍板，替代原「多引擎下载」方案）：
  1. 下载只有一条主路：无规则、全量、原样落盘。判形态、挑字段、清洗全部搬到本地。
  2. 原始数据**永不删除**：解析失败、清洗失败、规则全败 —— 都不许动 payload/。
     换规则 = 本地重跑，零网络成本；绝不能「下了删、删了下」。
  3. 唯一允许的例外是**取数协议**：ArcGIS REST / Socrata 这类服务端分页的源，
     「全量」必须按它自己的分页协议翻页拿完（这不是挑数据，仍是全量）。
     分页只在「拿不完/被截断/报错」时重试，**不因解析失败而重下**。

归档结构：
  raw_archive/<state>/<source_id>/
      manifest.json          # 协议/URL/状态/字节/sha256/页数/时间/done
      payload/               # 原始字节，永不改动（zip 保持 zip 不解包）
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse

from .arcgis import fetch_layer, layer_count, layer_meta, normalize_layer_url
from .politeness import Politeness

UA_TAG = "address-factory/2.0"


def archive_dir(root: str, sid: str) -> str:
    return os.path.join(root, *sid.split("/"))


def manifest_path(root: str, sid: str) -> str:
    return os.path.join(archive_dir(root, sid), "manifest.json")


def load_manifest(root: str, sid: str) -> dict:
    p = manifest_path(root, sid)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_manifest(root: str, sid: str, man: dict) -> None:
    d = archive_dir(root, sid)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "manifest.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, os.path.join(d, "manifest.json"))


def pick_protocol(url: str) -> str:
    """选择**取数协议**（不是数据规则）：直链 / ArcGIS 分页 / Socrata 分页。

    注意顺序：先判「直链文件」，再判分页 API。因为 Socrata 的
    `/api/views/<id>/rows.csv?accessType=DOWNLOAD` 明明是直链 CSV，
    早先按路径含 /api/views/ 判成了分页 API → 抓回空档。
    """
    u = (url or "").lower().split("?#")[0]
    q = (url or "").lower()
    if u.endswith((".csv", ".tsv", ".txt", ".psv", ".zip", ".shp", ".dbf",
                   ".geojson", ".kml", ".kmz", ".gml", ".xml", ".json")) \
            or "accesstype=download" in q or "/files/" in u or "/download" in u:
        return "direct"
    if "/mapserver/" in u or "/featureserver/" in u:
        return "arcgis_paged"
    if "/resource/" in u or "/api/views/" in u:
        return "socrata_paged"
    return "direct"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 三种取数协议：都是「全量拉取」，只是拿法不同 ────────────
def _count_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, "rb") as fh:
        return sum(1 for _ in fh)


def _integrity(man: dict, actual: int, declared: int = 0) -> dict:
    """第 6 步 下载完整性对账：本地拿到的行数 vs 服务端声明的总数。

    铁律：**不许静默 0 行**。空档 / 明显缺量必须显式标缺陷，交给分诊去补采，
    而不是当成「采完了、只是没数据」让下游白跑一轮（还可能把好源误判成无地址）。
    预算到点没采完（done=False）不算缺陷 —— 那是正常的续拿。
    """
    man["rows"] = actual
    if actual == 0:
        man.update({"error": (f"EMPTY_DOWNLOAD: 服务端自报 {declared:,} 条，本地拿到 0 行"
                              if declared else "EMPTY_DOWNLOAD: 本地拿到 0 行"),
                    "done": False, "defect": "EMPTY"})
    elif declared and man.get("done") and actual < declared * 0.9:
        man.update({"error": f"SHORT_DOWNLOAD: 服务端 {declared:,} 条，本地只拿到 {actual:,} 条"
                             f"（{actual / declared:.0%}）",
                    "done": False, "defect": "SHORT"})
    return man


def _direct(pol: Politeness, url: str, payload_dir: str, man: dict) -> dict:
    name = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1] or "download.bin"
    if "." not in name:
        cd = ""
        name = name or "download.bin"
    dest = os.path.join(payload_dir, name)
    n = pol.download_to_file(url, dest)
    man.update({"files": [{"name": name, "bytes": n, "sha256": _sha256_file(dest),
                           "url": url}], "bytes": n, "done": True})
    if n == 0:                                   # 0 字节也算下载缺陷，不许当「采完了」
        man.update({"error": "EMPTY_DOWNLOAD: 下载得到 0 字节", "done": False, "defect": "EMPTY"})
    return man


def _arcgis(pol: Politeness, url: str, payload_dir: str, man: dict,
            budget_s: int = 3600) -> dict:
    """键集分页全量拉取 → payload/features.ndjson（记录流即原始档）。"""
    u = normalize_layer_url(url)
    meta = layer_meta(pol, u)
    total = layer_count(pol, meta["url"])
    out = os.path.join(payload_dir, "features.ndjson")
    info = fetch_layer(pol, meta["url"], out, max_records=0, page_size=None,
                       oid_lo=None, max_seconds=budget_s)
    man.update({"files": [{"name": "features.ndjson",
                           "bytes": os.path.getsize(out) if os.path.exists(out) else 0,
                           "sha256": _sha256_file(out) if os.path.exists(out) else "",
                           "url": f"{meta['url']}/query"}],
                "bytes": os.path.getsize(out) if os.path.exists(out) else 0,
                "server_total": total, "last_oid": info.get("last_oid"),
                "pages": info.get("pages"), "done": bool(info.get("done", True)),
                "layer_url": meta["url"], "oid_field": meta["objectIdField"]})
    return _integrity(man, _count_lines(out), int(total or 0))


def _socrata(pol: Politeness, url: str, payload_dir: str, man: dict,
             page: int = 1000, budget_s: int = 3600) -> dict:
    """Socrata $limit/$offset 翻页全量 → payload/records.ndjson。

    若这个端点其实不是 JSON API（例如 id 拼错、或响应不是 JSON），
    回退成直链下载 —— 永远是「全量拉取」，只是拿法不同。
    """
    try:
        return _socrata_paged(pol, url, payload_dir, man, page, budget_s)
    except Exception as e:
        man["socrata_fallback"] = f"{type(e).__name__}: {str(e)[:120]}"
        return _direct(pol, url, payload_dir, man)


def _socrata_paged(pol: Politeness, url: str, payload_dir: str, man: dict,
                   page: int = 1000, budget_s: int = 3600) -> dict:
    out = os.path.join(payload_dir, "records.ndjson")
    q = urllib.parse.urlparse(url)
    base_params = dict(urllib.parse.parse_qsl(q.query))
    # P1-6 修复：真正支持续拿。旧写法固定 `offset=0` + `open(out, "w")`，
    # 预算到点后下一窗口会从 0 重下已得部分 —— manifest 里写着 next_offset 但没人读。
    offset = int(man.get("next_offset") or 0)
    n = int(man.get("rows") or 0)
    pages = 0
    deadline = time.monotonic() + budget_s
    done = True
    with open(out, "a" if offset else "w", encoding="utf-8") as fh:
        while time.monotonic() < deadline:
            p = dict(base_params)
            p["$limit"] = str(page)
            p["$offset"] = str(offset)
            full = urllib.parse.urlunparse(q._replace(query=urllib.parse.urlencode(p)))
            d = pol.get_json(full)
            recs = d if isinstance(d, list) else (d.get("data") or d.get("results") or [])
            if not recs:
                break
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += len(recs)
            pages += 1
            if len(recs) < page:
                break
            offset += page
        else:
            done = False        # 预算到点，下次续拿（offset 记在 manifest）
    man.update({"files": [{"name": "records.ndjson", "bytes": os.path.getsize(out),
                           "sha256": _sha256_file(out), "url": url}],
                "bytes": os.path.getsize(out), "rows": n, "pages": pages,
                "next_offset": offset, "done": done})
    return _integrity(man, n)


def fetch_raw(pol: Politeness, sid: str, url: str, root: str = "raw_archive",
              force: bool = False, budget_s: int = 3600) -> dict:
    """把某个源的全量数据搬回本地。已有且 done 的归档直接返回（不重下）。"""
    man = load_manifest(root, sid)
    if man.get("done") and not force:
        return man
    payload_dir = os.path.join(archive_dir(root, sid), "payload")
    os.makedirs(payload_dir, exist_ok=True)
    proto = pick_protocol(url)
    man.update({"source_id": sid, "url": url, "protocol": proto,
                "started": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": UA_TAG})
    t0 = time.time()
    try:
        if proto == "arcgis_paged":
            man = _arcgis(pol, url, payload_dir, man, budget_s)
        elif proto == "socrata_paged":
            man = _socrata(pol, url, payload_dir, man, budget_s=budget_s)
        else:
            man = _direct(pol, url, payload_dir, man)
        if not man.get("defect"):
            # P0-1 修复：_integrity 标的下载缺陷（EMPTY/SHORT）绝不允许被成功路径抹掉，
            # 否则 pipeline 只看 error → 漏判缺陷 → 90% 的部分档案照常过闸、
            # state 记 done → 下一窗口跳过 → **缺的那部分永远不补**。
            man["error"] = ""
    except Exception as e:
        man.update({"error": f"{type(e).__name__}: {str(e)[:300]}", "done": False})
    man["seconds"] = round(time.time() - t0, 1)
    man["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_manifest(root, sid, man)          # 每源一份 manifest，中断也留痕
    return man
