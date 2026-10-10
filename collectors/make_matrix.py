"""把待采源按 OID 切成固定条数的块，产出 collect-p0 的 matrix。

为什么切块：GitHub 托管 runner 会在 20~50 分钟随机回收长 job（日志
"The runner has received a shutdown signal"），单 job 跑 300 万条必丢产物。
切块后单块 ≤50 万条（实测 ≈4~8 分钟），远短于回收窗口；同源各块用
concurrency group 串行，保证对目标主机仍满足 ≥0.8s/请求 的礼貌预算。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

CHUNK = int(os.environ.get("CHUNK", "500000"))


def _get(url: str, params: dict | None = None, timeout: int = 120):
    u = url.rstrip("/")
    if params:
        u += "/query?" + urllib.parse.urlencode(params)
    else:
        u += "?f=json"
    with urllib.request.urlopen(u, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _ids(url: str) -> list[int]:
    d = _get(url, {"where": "1=1", "returnIdsOnly": "true", "f": "json"})
    if "error" in d:
        raise RuntimeError(str(d["error"])[:200])
    return sorted(int(x) for x in (d.get("objectIds") or []))


def _minmax(url: str, oid: str) -> tuple[int, int]:
    stats = json.dumps([
        {"statisticType": "min", "onStatisticField": oid, "outStatisticFieldName": "mn"},
        {"statisticType": "max", "onStatisticField": oid, "outStatisticFieldName": "mx"},
    ])
    d = _get(url, {"where": "1=1", "outStatistics": stats, "returnGeometry": "false", "f": "json"})
    if "error" in d:
        raise RuntimeError(str(d["error"])[:200])
    a = (d.get("features") or [{}])[0].get("attributes", {})
    return int(a["mn"]), int(a["mx"])


def chunks_for(url: str, total: int) -> list[tuple[int, int, int]]:
    """返回 [(lo, hi, 预估条数), ...]。"""
    meta = _get(url)
    oid = meta.get("objectIdField") or meta.get("objectIdFieldName") or "OBJECTID"
    try:
        ids = _ids(url)
    except Exception:
        ids = []
    if ids and len(ids) >= max(1, int(total * 0.999)):
        out = []
        for k in range(0, len(ids), CHUNK):
            part = ids[k:k + CHUNK]
            out.append((part[0], part[-1], len(part)))
        return out
    # 兜底：returnIdsOnly 被服务端截断 → 用 min/max 等宽切块
    lo, hi = _minmax(url, oid)
    n = max(1, -(-total // CHUNK))
    step = max(1, -(-(hi - lo + 1) // n))
    out = []
    cur = lo
    while cur <= hi:
        end = min(hi, cur + step - 1)
        out.append((cur, end, CHUNK))
        cur = end + 1
    return out


def main() -> None:
    job_file = sys.argv[1]
    src = json.load(open(job_file, encoding="utf-8"))
    items = []
    for x in src:
        url, total = x["url"], int(x.get("count") or 0)
        try:
            ch = chunks_for(url, total)
        except Exception as e:                                   # 单源失败不影响全局
            print(f"# 切块失败 {x['source_id']}: {type(e).__name__} {str(e)[:120]}", file=sys.stderr)
            ch = [(0, 0, total)]
        for k, (lo, hi, n) in enumerate(ch):
            items.append({"i": x["i"], "k": k, "id": x["source_id"],
                          "lo": lo, "hi": hi, "n": n})
        print(f"# {x['source_id']}: {total:,} 条 → {len(ch)} 块", file=sys.stderr)
    print("jobs=" + json.dumps(items, separators=(",", ":")))
    print("n=" + str(len(items)))


if __name__ == "__main__":
    main()
