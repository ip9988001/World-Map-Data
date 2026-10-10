"""解析清洗规则架（silver 层）—— 规则**只增不改**，一套不行换下一套，原始数据不动。

与「引擎」方案的差别（用户 2026-10-11 拍板）：
  引擎方案把「多试几把」放在**下载层**，每试一把 = 一次全量下载（下了删、删了下）；
  规则方案放在**本地层**，每试一套 = 几秒 CPU，原始档（raw_archive/）永不动。

级联顺序：路由记忆（这个源以前哪套规则洗成功过）→ 静态分（看源总表行 + 原始形态）→ 兜底。

规则接口：
    name / version               规则名带版本，账本按此记账（改语义 = 新规则名）
    needs_mapping                是否依赖源总表的字段映射（mapping_v1 依赖，猜测类规则不依赖）
    static_score(row, meta)      该不该先出场（零成本）
    clean(row, ctx) -> (record|None, reason)
    self_test() -> (ok, msg)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class Rule:
    name = "rule"
    version = 1
    needs_mapping = False

    def static_score(self, row: dict, meta: dict | None = None) -> float:
        return 0.0

    def clean(self, row: dict, ctx: "RuleCtx") -> tuple[dict | None, str]:
        raise NotImplementedError

    def self_test(self) -> tuple[bool, str]:
        return True, "no self-test"


@dataclass
class RuleCtx:
    """规则跑批时需要的公共上下文（映射、州默认值、源 id）。"""
    source_id: str
    row: dict
    mapping: dict
    default_state: str = ""
    stats: dict | None = None

    def bump(self, key: str) -> None:
        if self.stats is not None:
            self.stats[key] = self.stats.get(key, 0) + 1


_RULE_SPECS = (
    ("mapping_v1", "RULES"),
    ("field_guess", "RULES"),
    ("concat_addr", "RULES"),
)

RULES: dict[str, Rule] = {}


def _load() -> None:
    import importlib
    for mod_name, attr in _RULE_SPECS:
        try:
            mod = importlib.import_module(f".{mod_name}", __package__)
            obj = getattr(mod, attr.split(":")[-1])
            for r in (obj if isinstance(obj, (list, tuple)) else [obj]):
                RULES[f"{r.name}@v{r.version}"] = r
        except Exception as e:
            print(f"[rules] 加载 {mod_name} 失败（该规则不出场）: "
                  f"{type(e).__name__}: {e}", flush=True)


_load()


def order_rules(row: dict, meta: dict | None = None, preferred: str = "") -> list[Rule]:
    pool = sorted(RULES.values(), key=lambda r: -r.static_score(row, meta))
    if preferred and preferred in RULES:
        pool = [RULES[preferred]] + [r for r in pool if r.name != RULES[preferred].name]
    return pool


def self_test_all() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, r in sorted(RULES.items()):
        try:
            ok, msg = r.self_test()
            out[name] = ("✓ " if ok else "✗ ") + msg
        except Exception as e:
            out[name] = f"✗ 自检抛异常: {type(e).__name__}: {e}"
    return out


__all__ = ["RULES", "Rule", "RuleCtx", "order_rules", "self_test_all"]
