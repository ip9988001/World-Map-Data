"""规则 2：field_guess_v1 —— 不看源总表映射，靠字段名/值形态自己猜。

适用：源总表映射写错、映射为空、或字段名一眼能看懂但没登记的情况。
猜法：关键词命中（多语言）→ 无命中时挑「值几乎全是短数字」的列当门牌号。
"""
from __future__ import annotations

from . import Rule, RuleCtx
from ._common import first_numeric_key, guess_keys, mk_record


class FieldGuess(Rule):
    name = "field_guess"
    version = 1
    needs_mapping = False

    def static_score(self, row: dict, meta: dict | None = None) -> float:
        # 永远是第二候选：本地重跑极便宜，宁可多试一套
        return 0.55

    def clean(self, row: dict, ctx: RuleCtx):
        keys = _keys(row)
        g = _guess_cached(keys)
        num_k = g.get("number") or first_numeric_key([row], keys)
        street_k = g.get("street")
        if not (num_k and street_k):
            return None, "guess_no_columns"
        return mk_record(row.get(num_k), row.get(street_k),
                         row.get(g.get("city", ""), ""), row.get(g.get("zip", ""), ""),
                         row.get(g.get("state", ""), ""), ctx.source_id,
                         ctx.default_state)

    def self_test(self):
        ctx = RuleCtx("t", {}, {}, "IL")
        rec, why = self.clean({"HOUSENUM": "12", "STREET": "Main St", "CITY": "Springfield",
                               "STATE": "IL", "ZIP": "62701"}, ctx)
        assert rec and rec["number"] == "12", (rec, why)
        # 德文键名（值是仍美国地址，因为本数据集只收美国门牌点）
        rec2, why2 = self.clean({"Hausnummer": "3", "Strasse": "Bahnhofstr",
                                 "STATE": "IL", "PLZ": "10115"}, ctx)
        assert rec2 and rec2["number"] == "3", (rec2, why2)
        # 没有任何可猜字段 → 必须老实报错，不许编
        assert self.clean({"foo": "1", "bar": "x"}, ctx)[1] == "guess_no_columns"
        return True, "英文/德文键名猜测 + 无字段时拒答 OK"


_CACHE: dict[frozenset, dict] = {}


def _keys(row: dict) -> list[str]:
    return [str(k) for k in row.keys() if not str(k).startswith("_")]


def _guess_cached(keys: list[str]) -> dict:
    """同一批字段名只猜一次（本地跑百万行也不心疼 CPU）。"""
    sig = frozenset(keys)
    if sig not in _CACHE:
        _CACHE[sig] = guess_keys(keys)
    return _CACHE[sig]


RULES = [FieldGuess()]
