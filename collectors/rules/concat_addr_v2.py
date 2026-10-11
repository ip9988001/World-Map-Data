"""规则 3@v2：concat_addr —— 整条地址拆成 号 + 街（+ 市/州/ZIP）。

v1 两个真 bug（2026-10-11 实测 us/il/effingham 抓出）：
  ① 尾部剥离用的 TAIL_RE 是**左锚定 + 懒惰**，对 "1514 N US Hwy 45 Mason IL 62443"
     把 city 吞成 "N US Hwy 45"、state 吞成 "Mason IL" → 剩下的街名为空
     → 整条洗不出来（该源 mapping 0% / concat 只有 5%，被误判成「要造新规则」）。
  ② 选列不设防：PropertyClass（"0021 - Rural Unimproved"）这种值也能被 FULL_ADDR_RE 匹配，
     于是造出 number=0021 / street="- RURAL UNIMPROVED" 的垃圾记录（v1 实测确实在造这种脏数据）。

v2 做法：
  · 用 _common.parse_addr_v2（可选前置方位 + 右锚定剥尾）
  · 选列先按「像地址的名字」（site/situs/physical/address）挑，再排除业主邮寄/法律描述/地类码
  · 街名必须像街名（≥2 字母、不是光一个后缀词），且不以街名后缀词结尾又没剖出城市 → 丢弃
语义变了 → 新版本（v1 保留，历史产物可复现）。
"""
from __future__ import annotations

from . import Rule, RuleCtx
from ._common import mk_record, parse_addr_v2, site_addr_key


class ConcatAddrV2(Rule):
    name = "concat_addr"
    version = 2
    needs_mapping = False

    def static_score(self, row: dict, meta: dict | None = None) -> float:
        return 0.55

    def clean(self, row: dict, ctx: RuleCtx):
        key = site_addr_key(row)
        if not key:
            return None, "v2_no_site_column"
        parsed = parse_addr_v2(row.get(key))
        if not parsed:
            return None, "v2_not_a_full_address"
        number, street, city, state, zip5 = parsed
        return mk_record(number, street, city, zip5, state, ctx.source_id, ctx.default_state)

    def self_test(self):
        cases = [
            ("1514 N US Hwy 45 Mason IL 62443", "1514", "N US HWY 45", "MASON", "62443"),
            ("123 N Main St, Springfield, CA 94110", "123", "N MAIN ST", "SPRINGFIELD", "94110"),
            ("23320 E 200th Av Dieterich IL 62424", "23320", "E 200TH AVE", "DIETERICH", "62424"),
            ("45B Oak Ave", "45B", "OAK AVE", "", ""),
        ]
        for text, num, street, city, zip5 in cases:
            rec, why = self_test_one(text)
            assert rec, f"{text} 洗不出：{why}"
            assert rec["number"] == num and rec["street"] == street, (text, rec)
            assert rec["city"] == city and rec["zip"] == zip5, (text, rec)
        # 不该出记录的：无门牌号 / 地类码
        for bad in ("N 1525th St Mason IL 62443", "0021 - Rural Unimproved", "E 150th Av Mason IL 62443"):
            rec, _why = self_test_one(bad)
            assert rec is None, f"{bad} 不该出记录，却出了 {rec}"
        return True, "整条地址拆分 v2（前置方位 + 右锚定剥尾 + 垃圾值拒绝）OK"


def self_test_one(text: str):
    r = ConcatAddrV2()
    row = {"SiteAddress": text}
    return r.clean(row, RuleCtx("t", row, {}, "IL"))


RULES = [ConcatAddrV2()]
