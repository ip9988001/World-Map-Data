"""礼貌访问器 —— 采集铁律的代码实现。

铁律：任何采集都必须在「不触发对方反爬机制」的前提下执行。
     官方单次上限 1000 条 → 我们最多取 900 条。
     宁可慢，不可封。

本模块提供：
  * 每主机最小请求间隔（串行限速）
  * 全局并发上限
  * 指数退避 + 抖动重试
  * 分页尺寸自动取「服务端上限 × 0.9」
  * 全量请求日志（JSONL）
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SAFETY_RATIO = 0.9          # 官方上限只取 90%
DEFAULT_MAX_RECORD_COUNT = 2000
DEFAULT_UA = "World-map-data/1.0 (+https://github.com/ip9988001/world-Map-Data) polite-collector"


def safe_page_size(server_max: int | None, fallback: int = DEFAULT_MAX_RECORD_COUNT) -> int:
    """官方上限 × 0.9，且不低于 100。"""
    mx = server_max if isinstance(server_max, int) and server_max > 0 else fallback
    return max(100, int(mx * SAFETY_RATIO))


class Politeness:
    def __init__(self, min_interval: float = 0.8, max_concurrency: int = 2,
                 max_retries: int = 4, timeout: int = 60,
                 log_path: str | None = None, user_agent: str = DEFAULT_UA):
        self.min_interval = float(min_interval)
        self.max_retries = int(max_retries)
        self.timeout = int(timeout)
        self.ua = user_agent
        self._sem = threading.Semaphore(max(1, int(max_concurrency)))
        self._lock = threading.Lock()
        self._last_hit: dict[str, float] = {}
        self._log_lock = threading.Lock()
        self.log_path = log_path
        self.stats = {"requests": 0, "bytes": 0, "retries": 0, "errors": 0}
        if log_path:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)

    # ---------- 内部 ----------
    def _wait_for_host(self, host: str) -> None:
        with self._lock:
            now = time.time()
            last = self._last_hit.get(host, 0.0)
            wait = self.min_interval - (now - last)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.25))
            self._last_hit[host] = time.time()

    def _log(self, rec: dict) -> None:
        if not self.log_path:
            return
        with self._log_lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _open(self, url: str):
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua,
            "Accept": "application/json,text/csv,*/*",
            "Accept-Encoding": "gzip, deflate",
        })
        resp = urllib.request.urlopen(req, timeout=self.timeout)
        # 透明解压
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            return gzip.GzipFile(fileobj=resp), resp
        return resp, resp

    def fetch(self, url: str, params: dict | None = None, binary: bool = False):
        """GET 一个 URL，带限速/并发/退避/日志。返回 (payload, http_status)。"""
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = url + ("&" if "?" in url else "?") + qs
        host = urllib.parse.urlparse(url).netloc
        last_err = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.stats["retries"] += 1
                time.sleep(min(30.0, (2 ** attempt) + random.uniform(0, 1.5)))
            try:
                with self._sem:
                    self._wait_for_host(host)
                    t0 = time.time()
                    body, resp = self._open(url)
                    data = body.read()
                    status = getattr(resp, "status", 200)
                self.stats["requests"] += 1
                self.stats["bytes"] += len(data)
                self._log({"url": url, "status": status, "bytes": len(data),
                           "ms": int((time.time() - t0) * 1000), "attempt": attempt})
                return (data if binary else data.decode("utf-8", "replace")), status
            except urllib.error.HTTPError as e:
                last_err = e
                self.stats["errors"] += 1
                self._log({"url": url, "status": e.code, "error": str(e.reason),
                           "attempt": attempt})
                if e.code in (400, 401, 403, 404, 422):
                    raise            # 客户端错误不重试
            except Exception as e:   # 超时 / 连接错误 → 重试
                last_err = e
                self.stats["errors"] += 1
                self._log({"url": url, "status": 0, "error": str(e)[:200],
                           "attempt": attempt})
        raise last_err if last_err else RuntimeError("request failed: " + url)

    def get_json(self, url: str, params: dict | None = None) -> dict:
        txt, _ = self.fetch(url, params)
        return json.loads(txt)

    def download_to_file(self, url: str, dest: str) -> int:
        data, _ = self.fetch(url, binary=True)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        return len(data)
