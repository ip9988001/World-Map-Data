"""引擎注册表 —— 刀具架。

铁律：
  1. 一把刀只切一种肉；切不动换刀，不磨刀。
  2. 新形态 = 新引擎文件 + 在此登记；老引擎永不删除（保留对照与回滚）。
  3. 引擎名唯一且带版本（foo@v2 与 foo 并存），路由记忆按「引擎名」记账。

出场顺序（级联顺序）：
  ① 路由记忆（这个源以前哪把刀切成功过）      —— 一枪毙命，跳过所有试错
  ② 静态分 ≥ 阈值者按分排序，同分按 probe_cost 升序（便宜的刀先上）
  ③ 其余全部按静态分降序兜底（宁可多试一把，不许漏采）
"""
from __future__ import annotations

from .base import (ADDR_KEY_HINTS, Engine, EngineError, NotMyMeat, RawBundle,
                   SourceBroken, SourceCtx)

# 引擎清单：新增引擎只加一行（文件同目录，类名固定为 ENGINE）
_ENGINE_SPECS = (
    ("arcgis_rest", "ENGINE"),
    ("delimited", "ENGINE"),
    ("json_api", "ENGINE"),
    ("kml_xml", "ENGINE"),
)

ENGINES: dict[str, Engine] = {}


def _load() -> None:
    import importlib
    for mod_name, attr in _ENGINE_SPECS:
        try:
            mod = importlib.import_module(f".{mod_name}", __package__)
            attr_name = attr.split(":")[-1]        # 容忍 "mod:ATTR" 写法
            obj = mod
            for part in attr_name.split("."):
                obj = getattr(obj, part)
            eng: Engine = obj() if isinstance(obj, type) else obj
            ENGINES[eng.name] = eng
        except Exception as e:                                   # 引擎坏掉不许拖垮全批
            print(f"[engines] 加载 {mod_name} 失败（该引擎不出场）: "
                  f"{type(e).__name__}: {e}", flush=True)


_load()


def order_engines(row: dict, preferred: str = "", all_engines: bool = False) -> list[Engine]:
    """给一个源排出场的刀的顺序。"""
    scored = [(e, e.static_score(row)) for e in ENGINES.values()]
    if all_engines:
        pool = [e for e, _ in sorted(scored, key=lambda t: -t[1])]
    else:
        hot = [(e, s) for e, s in scored if s >= 0.6]
        cold = [e for e, s in sorted(scored, key=lambda t: -t[1]) if s < 0.6]
        pool = [e for e, _ in sorted(hot, key=lambda t: (-t[1], t[0].probe_cost))] + cold
    if preferred and preferred in ENGINES:
        pool = [ENGINES[preferred]] + [e for e in pool if e.name != preferred]
    return pool


def self_test_all() -> dict[str, str]:
    """全引擎自检；返回 {引擎名: 结果}。跑批前的第一道闸。"""
    out: dict[str, str] = {}
    for name, e in sorted(ENGINES.items()):
        try:
            ok, msg = e.self_test()
            out[name] = ("✓ " if ok else "✗ ") + msg
        except Exception as ex:
            out[name] = f"✗ 自检抛异常: {type(ex).__name__}: {ex}"
    return out


__all__ = ["ENGINES", "Engine", "EngineError", "NotMyMeat", "RawBundle",
           "SourceBroken", "SourceCtx", "ADDR_KEY_HINTS", "order_engines",
           "self_test_all"]
