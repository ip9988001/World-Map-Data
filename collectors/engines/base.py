"""引擎基类 —— 一把刀只切一种肉。

设计要点（对应「多引擎级联」）：
  · 引擎 = 「怎么取数 + 怎么把取回来的东西摊成字段行」的固定组合，不做字段语义判断
    （字段语义统一交给 normalize.py，这样各引擎不必各自维护一套映射逻辑）
  · 引擎**只增不改**：逻辑语义变更一律新开一个引擎名（xxx@v2），老引擎保留做对照，
    避免"一把刀越磨越钝、改到最后谁都不敢动"
  · 引擎必须自带 self_test()：每次跑批前自检，自检不过引擎直接不出场
  · 引擎分层打分：static_score（看源总表行，零成本）→ sniff_score（看 HTTP 小样头，极廉价）
    → 实跑探针（真花钱）→ 只有胜出的引擎才跑全量

用法（级联器内部）：
    eng = ENGINES["arcgis_rest"]
    bundle = eng.fetch(pol, ctx, budget_s=20, probe=True)   # 探针：只取小样
    rows = list(islice(eng.rows(bundle), 200))
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

# ── 地址类字段的"长相"指纹：用于判断一份没见过格式的数据"看起来是不是门牌点" ──
ADDR_KEY_HINTS = re.compile(
    r"(num|nr|no|haus|huis|street|strasse|str\.|st$|st_|rue|via|avenue|ave|calle|carrer|"
    r"addr|direccion|endereco|adres|lane|road|rd|weg|straat|thoroughfare|place|plaza)",
    re.I,
)


@dataclass
class SourceCtx:
    """一个源的上下文：源总表行 + 工作目录。"""
    source_id: str
    url: str
    row: dict
    work_dir: str

    @property
    def channel(self) -> str:
        return (self.row.get("channel") or "").strip()

    @property
    def fmt(self) -> str:
        return (self.row.get("fmt") or "").strip().lower()


@dataclass
class RawBundle:
    """引擎取数的产物句柄。引擎自己往里塞什么都行，其他引擎不碰。"""
    engine: str
    work_dir: str
    payload: dict = field(default_factory=dict)
    pages: int = 0
    nbytes: int = 0
    note: str = ""

    def path(self, name: str) -> str:
        return os.path.join(self.work_dir, name)


class EngineError(RuntimeError):
    """引擎层面可分类的失败（级联器据此分诊，不当作网络抖动）。"""

    kind = "ENGINE_ERROR"

    def __init__(self, msg: str, kind: str | None = None):
        super().__init__(msg)
        if kind:
            self.kind = kind


class SourceBroken(EngineError):
    """证据表明是**源本身的问题**（404/空响应/错误页/封禁），不是我们的刀不行。"""
    kind = "SOURCE_BROKEN"


class NotMyMeat(EngineError):
    """这份数据的形态不是这把刀切的（形态识别失败，不是取数失败）。"""
    kind = "NOT_MY_MEAT"


# ── 下载前形态侦察（防「下了 266MB 才说不认识」）──────────
MAX_AUTO_BYTES = 200 * 1024 * 1024      # 超此体积且无刀确信能切 → 不下载，直接判缺口
CONFIDENT_SNIFF = 0.90


def preflight(url: str, timeout: int = 15, head_bytes: int = 4096) -> dict:
    """一次 Range 请求拿 响应头 + 前 4KB，不下载整文件。

    产出直接喂给各引擎的 sniff_score()：
      · 认得的刀 → 按分排序出场
      · 都不认得 → 不下载，直接判「形态缺口」（造新刀的依据）
    """
    import urllib.error
    import urllib.request
    ev = {"status": 0, "content_type": "", "content_length": 0, "disposition": "",
          "head": b"", "final_url": url, "error": ""}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; address-factory/1.0)",
            "Range": f"bytes=0-{head_bytes - 1}", "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ev["status"] = r.status
            ev["content_type"] = r.headers.get("Content-Type", "") or ""
            ev["disposition"] = r.headers.get("Content-Disposition", "") or ""
            cr, cl = r.headers.get("Content-Range"), r.headers.get("Content-Length")
            if cr and "/" in cr:
                try:
                    ev["content_length"] = int(cr.rsplit("/", 1)[-1])
                except Exception:
                    pass
            elif cl and r.status != 206:
                try:
                    ev["content_length"] = int(cl)
                except Exception:
                    pass
            ev["head"] = r.read(head_bytes)
            ev["final_url"] = r.geturl()
    except urllib.error.HTTPError as e:
        ev["status"] = e.code
        ev["error"] = f"HTTP {e.code} {e.reason}"
        try:
            ev["head"] = e.read(head_bytes)
        except Exception:
            pass
    except Exception as e:
        ev["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    return ev


def sniff_table(row: dict, ev: dict) -> list[tuple[Engine, float, float]]:
    """[(引擎, 静态分, 小样分)]，小样分来自 preflight 的 头/前 4KB。"""
    from . import ENGINES          # 延迟导入：注册表在包 __init__ 里，避免循环依赖
    ct, head = ev.get("content_type", ""), ev.get("head", b"") or b""
    out = []
    for e in ENGINES.values():
        try:
            sn = e.sniff_score(ct, head)
        except Exception:
            sn = 0.0
        out.append((e, e.static_score(row), sn))
    return out


def order_by_sniff(row: dict, ev: dict, preferred: str = "") -> list[Engine]:
    """嗅探后再排刀：认得的排前面，完全不认识的直接不开场（除非它是兜底）。"""
    from . import ENGINES
    tab = sniff_table(row, ev)
    known = [t for t in tab if t[2] >= 0.45]
    maybe = [t for t in tab if 0.2 <= t[2] < 0.45]
    if known:
        pool = sorted(known, key=lambda t: (-t[2], t[0].probe_cost))
        pool += [t for t in sorted(maybe, key=lambda t: (-t[2], t[0].probe_cost))][:1]
    else:
        pool = sorted(maybe, key=lambda t: (-t[2], t[0].probe_cost))
        pool += sorted(tab, key=lambda t: -t[1])[:2]        # 兜底：静态分最高的两把
    seen, out = set(), []
    for e, _s, _n in pool:
        if e.name not in seen:
            seen.add(e.name)
            out.append(e)
    if preferred and preferred in ENGINES and preferred not in seen:
        out.insert(0, ENGINES[preferred])
    return out


class Engine:
    """一把刀。子类至少实现 static_score / fetch / rows。"""

    name = "engine"
    version = 1
    probe_cost = 1.0            # 相对成本，越小越先出场（省流量的刀排前面）
    formats = ()                # 声明能吃哪些形态（fmt 字段值 / 后缀）

    # ── 第一层：静态打分（看源总表行，零成本）──────────────
    def static_score(self, row: dict) -> float:
        return 0.0

    # ── 第二层：小样嗅探（看 HTTP 头 + 前几 KB，极廉价）─────
    def sniff_score(self, content_type: str, head: bytes) -> float:
        return 0.0

    # ── 第三层：实跑（真花钱，只在探针阶段小样跑）────────────
    def fetch(self, pol, ctx: SourceCtx, budget_s: int, probe: bool) -> RawBundle:
        raise NotImplementedError

    def rows(self, bundle: RawBundle) -> Iterator[dict]:
        raise NotImplementedError

    # ── 自检：跑批前必须过 ─────────────────────────────────
    def self_test(self) -> tuple[bool, str]:
        return True, "no self-test defined"

    # ── 工具 ───────────────────────────────────────────────
    @staticmethod
    def looks_like_addresses(rows: list[dict]) -> float:
        """给出这批行「像不像门牌点」的粗判分 0~1（只看键名，不看值）。"""
        if not rows:
            return 0.0
        keys: set[str] = set()
        for r in rows[:50]:
            if isinstance(r, dict):
                keys.update(str(k) for k in r.keys())
        if not keys:
            return 0.0
        hit = sum(1 for k in keys if ADDR_KEY_HINTS.search(k))
        return min(1.0, hit / 3.0)      # 命中 3 个以上地址味字段即满分

    @staticmethod
    def is_html_error(head: bytes, content_type: str = "") -> bool:
        """错误页 / 门户页 / WAF 拦截页识别 —— 源坏的第一大特征。"""
        h = head[:4096].lower()
        if b"<!doctype html" in h or b"<html" in h:
            if not any(t in h for t in (b"<table", b"<tr", b"<td")):
                return True     # 是 HTML 但连个表格都没有 → 不是数据
        for sig in (b"cloudflare", b"captcha", b"access denied", b"403 forbidden",
                    b"page not found", b"<title>error", b"not acceptable"):
            if sig in h:
                return True
        if content_type and "html" in content_type and not head.startswith(b"{"):
            return True
        return False
