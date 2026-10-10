"""规则 3：concat_addr_v1 —— 只有一整列「完整地址」，把它拆成 号 + 街（+ 市/州/ZIP）。

适用：源只给 FULLADDR / ADDRESS / 完整地址串（美国市政源常见），
     映射规则拿不到独立的号/街两列 → 全丢；这套规则把它切开。
"""
from __future__ import annotations

import re

from . import Rule, RuleCtx
from ._common import FULL_ADDR_RE, TAIL_RE, mk_record

FULL_KEYS = re.compile(r"full|complete|display|address|addr|adresse|direccion|"
                       r"endereco|location|site_addr|situs|composite", re.I)


class ConcatAddr(Rule):
    name = "concat_addr"
    version = 1
    needs_mapping = False

    def static_score(self, row: dict, meta: dict | None = None) -> float:
        return 0.5

    def _full_key(self, row: dict, keys: list[str]) -> str:
        best = ""
        for k in keys:
            v = row.get(k)
            if isinstance(v, str) and FULL_ADDR_RE.match(v) and FULL_KEYS.search(k):
                return k
        for k in keys:
            v = row.get(k)
            if isinstance(v, str) and FULL_ADDR_RE.match(v) and len(v) > 8:
                best = best or k
        return best

    def clean(self, row: dict, ctx: RuleCtx):
        keys = list(row.keys())
        fk = self._full_key(row, keys) or (ctx.mapping.get("number") or "")
        full = str(row.get(fk, "") or "").strip()
        m = FULL_ADDR_RE.match(full)
        if not m:
            return None, "no_full_address"
        number, rest = m.group(1), m.group(2)
        city = zip5 = state = ""
        t = TAIL_RE.search(rest)
        if t:
            city, state, zip5 = t.group(1).strip(), t.group(2).strip(), t.group(3)
            rest = rest[: t.start()].strip().rstrip(",")
        unit = ""
        um = re.search(r"\b(apt|unit|ste|suite|#)\s*([\w-]+)", rest, re.I)
        if um:
            unit = um.group(2)
            rest = rest[: um.start()].strip().rstrip(",")
        if not rest:
            return None, "split_no_street"
        return mk_record(number, rest, city, zip5, state, ctx.source_id, ctx.default_state)

    def self_test(self):
        ctx = RuleCtx("t", {}, {}, "CA")
        rec, why = self.clean({"FULLADDR": "123 N Main St, Springfield, CA 94110"}, ctx)
        assert rec and rec["number"] == "123", (rec, why)
        assert rec["street"].upper().startswith("N MAIN"), rec
        assert rec["zip"] == "94110" and rec["city"].upper() == "SPRINGFIELD", rec
        rec2, why2 = self.clean({"ADDRESS": "123 Main St Apt 4"}, RuleCtx("t", {}, {}, "NY"))
        assert rec2 and rec2["number"] == "123", (rec2, why2)
        rec3, _ = self.clean({"FULLADDR": "45B Oak Ave"}, RuleCtx("t", {}, {}, "NY"))
        assert rec3 and rec3["number"] == "45B", rec3
        return True, "完整地址拆分（含尾部市/州/ZIP + 45B 带字母号）OK"


RULES = [ConcatAddr()]
