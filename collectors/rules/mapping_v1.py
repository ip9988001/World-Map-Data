"""规则 1：mapping_v1 —— 就是原来的流程（按源总表的字段映射走）。

依赖源总表的 number/street/city_f/region/postcode 五列；
这是存量 19.7M 条一直用的那套映射，作为级联第一候选（有映射的源优先用它）。
"""
from __future__ import annotations

from ..normalize import parse_mapping_from_table, to_record_ex
from . import Rule, RuleCtx


class MappingV1(Rule):
    name = "mapping"
    version = 1
    needs_mapping = True

    def static_score(self, row: dict, meta: dict | None = None) -> float:
        m = parse_mapping_from_table(row)
        has = sum(1 for k in ("number", "street") if m.get(k))
        if has == 2:
            return 1.0
        if has == 1:
            return 0.45
        return 0.15

    def clean(self, row: dict, ctx: RuleCtx):
        rec, reason = to_record_ex(row, ctx.mapping, ctx.source_id,
                                   default_state=ctx.default_state)
        return rec, (reason or "ok")

    def self_test(self):
        m = {"number": "NUM", "street": "ST", "city": "CITY",
             "region": "STATE", "postcode": "ZIP"}
        rec, why = to_record_ex({"NUM": "7", "ST": "Elm St", "CITY": "Springfield",
                                 "STATE": "IL", "ZIP": "62701"}, m, "t")
        assert rec and rec["number"] == "7" and rec["zip"] == "62701", (rec, why)
        assert to_record_ex({"NUM": "", "ST": "x"}, m, "t")[1] == "no_number"
        return True, "映射式清洗 + 无号丢弃 OK"


RULES = [MappingV1()]
