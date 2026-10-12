"""规则 2：field_guess@v2 —— 不看源总表映射、也不靠字段名，**按值的形态猜**。

v1 的失败教训（实测）：ArcGIS 类源的字段名是 `SiteHouse` / `ADD_2` / `STR` 这种，
关键词表命中不了 → `guess_no_columns` → 恒为 0 条，兜底刀形同虚设。

v2 做法（两步）：
  ① 预热：跑批前拿一小批样本（默认 300 行），逐列算三类统计 ——
       数字率（`^\\d{1,6}[A-Za-z]?$`）、街名率（含字母且有空格/街名后缀）、5位ZIP率、2字母州率、
       平均长度、取值重复度；据此**定下列位**（号列/街列/市列/邮编列/州列）。
  ② 清洗：用定好的列位逐行走 mk_record（与 mapping@v1 同一套闸门）。
  优先级：关键词命中优先（名字能看懂就别猜），关键词失手才用值形态。
  注意：city 宁可不填也不乱填 —— 错的市名会污染 key 去重，宁缺毋滥。
"""
from __future__ import annotations

import re
from statistics import median

from ..normalize import norm_number, norm_street
from . import Rule, RuleCtx
from ._common import guess_keys, mk_record

NUM_RE = re.compile(r"^\d{1,6}[A-Za-z]?$")
ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
STATE_RE = re.compile(r"^[A-Z]{2}$")
STREET_SUFFIX = re.compile(r"\b(st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|way|hwy|"
                           r"highway|ct|court|pl|place|ter|cir|circle|pkwy|parkway|trail|"
                           r"tr|loop|route|rt|expy|fwy)\b\.?$", re.I)
# 踩过的坑（us/il/effingham）：把 OBJECTID 当门牌号、Grantee 当街名、County 当城市 —— 全是垃圾。
BAD_KEY = re.compile(r"^(objectid|fid|gid|oid|index|seq|no_)$|(_id|_no)$|objectid|globalid|"
                     r"parcel|pin|apn|account|taxcode|tax_code|book|page|legal|deed|"
                     r"name|owner|grantee|grantor|person|contact|taxpayer|mail", re.I)
SEQ_RE = re.compile(r"^\d{1,7}$")


SUFFIX_ANY = re.compile(r"\b(st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|way|hwy|"
                        r"highway|ct|court|pl|place|ter|cir|circle|pkwy|parkway|trail|"
                        r"loop|route|rt|expy|fwy)\b", re.I)


def _looks_street(v: str) -> bool:
    """街名得像街名：有字母 +（含街名后缀词 或「号+词」形态）。

    后缀不锚定末尾 —— 因为「N US Hwy 45」这种“后缀在中间”的街名是真实存在的。
    """
    if not re.search(r"[A-Za-z]{2,}", v):
        return False
    return bool(SUFFIX_ANY.search(v)) or bool(re.search(r"\b\d{1,6}[A-Za-z]?\s+\S", v))


def _col_stats(rows: list[dict]) -> dict[str, dict]:
    keys: set[str] = set()
    for r in rows:
        keys.update(str(k) for k in r.keys() if not str(k).startswith("_"))
    out: dict[str, dict] = {}
    for k in keys:
        vals = [str(r.get(k) or "").strip() for r in rows]
        nonblank = [v for v in vals if v]
        n = len(nonblank) or 1
        lens = sorted(len(v) for v in nonblank) or [0]
        nums: list[int] = []
        for v in nonblank:
            if NUM_RE.match(v):
                try:
                    nums.append(int(re.sub(r"\D", "", v) or 0))
                except ValueError:
                    pass
        out[k] = {
            "fill": len(nonblank) / max(len(rows), 1),
            "num": sum(1 for v in nonblank if NUM_RE.match(v)) / n,
            "street": sum(1 for v in nonblank if
                          (re.search(r"[A-Za-z]{2,}", v) and
                           ((" " in v) or STREET_SUFFIX.search(v)))) / n,
            "street_ok": sum(1 for v in nonblank if _looks_street(v)) / n,
            "suffix": sum(1 for v in nonblank if STREET_SUFFIX.search(v)) / n,
            "zip": sum(1 for v in nonblank if ZIP_RE.match(v)) / n,
            "state": sum(1 for v in nonblank if STATE_RE.match(v)) / n,
            "median_len": median(lens) if nonblank else 0,
            "distinct": len(set(nonblank)) / n,
            "badkey": bool(BAD_KEY.search(k)),
            # 值域统计（P0-2 修复用）：光看“长得像数字”拦不住年份列 / 房型列
            "min_num": min(nums) if nums else 0,
            "median_num": median(sorted(nums)) if nums else 0,
            "num_distinct": len(set(nums)),
            "num_range": (max(nums) - min(nums)) if nums else 0,
        }
    return out


def plan_columns(rows: list[dict]) -> dict[str, str]:
    """从样本里定出号/街/市/邮编/州各自用哪一列（纯值形态）。

    定序很关键（踩过的坑：先把 ZIP 列当成门牌号列 → 全部记录 number_is_zip 全丢）：
      zip → number（排除 zip 列）→ street（排除前面已定列）→ state → city
    """
    st = _col_stats(rows)
    plan: dict[str, str] = {}

    def best(score_key: str, ok, exclude: set[str]):
        cands = [(k, s) for k, s in st.items()
                 if k not in exclude and not s["badkey"] and ok(s)]
        if not cands:
            return ""
        return max(cands, key=lambda kv: (kv[1][score_key], kv[1]["fill"]))[0]

    # ⓪ 先看有没有“一整列就是完整地址”的（如 SiteAddress）→ 交给 concat_addr，本规则不乱猜
    full_cols = [k for k, s in st.items()
                 if not s["badkey"] and s["street_ok"] >= 0.5 and s["median_len"] >= 12]
    # ① 邮编列（5 位数字率）—— 必须先定，后续所有列都要排除它
    plan["zip"] = best("zip", lambda s: s["zip"] >= 0.50, set())
    # ② 州列（2 字母且取值集中）
    plan["state"] = best("state", lambda s: s["state"] >= 0.50 and s["distinct"] <= 0.2,
                         {plan["zip"]})
    # ③ 号列：数字率最高、中位长度 ≤ 6，排除已定列，**还要排除连续编号列（OBJECTID 类）**
    excl_id = {plan["zip"], plan["state"]} | set(full_cols)

    def ok_number(s):
        if not (s["num"] >= 0.30 and s["median_len"] <= 6):
            return False
        if s["distinct"] >= 0.98 and s["median_len"] <= 7:      # 每行都不同→序号列
            return False
        # —— P0-2 修复：**值域常识**。上面两条只看形态，实测放行了 YEAR_BUILT：
        #    「1998」既是数字、中位长 4、取值也不够“每行不同”（年份就那么二十几个）→ 被选成门牌号列。
        #    门牌号的真特征不是“长得像数字”，是**值域**：从 1 起、分布广、不是年份段、不是小分类。
        if s["min_num"] < 1:
            return False                                        # 含 0 的列不是门牌号
        if 1700 <= s["median_num"] <= 2200 and s["num_distinct"] <= 120:
            return False                                        # 年份列（YEAR_BUILT / 建成年份）
        if s["num_distinct"] <= 12 and s["num_range"] <= 20:
            return False                                        # 小分类列（BEDROOMS / 楼层 / 状态码）
        if s["num_range"] <= 1 and s["num_distinct"] <= 1:
            return False                                        # 常量列
        return True

    plan["number"] = best("num", ok_number, excl_id)
    # ④ 街列：必须是“像街名”的列（后缀或「号+词」），所有已定列都要排除
    excl = excl_id | {plan["number"]}
    plan["street"] = best("street_ok", lambda s: s["street_ok"] >= 0.30, excl)
    # ⑤ 市列：字母+空格、长度适中、取值不会每行都不同
    plan["city"] = best("street", lambda s: s["street"] >= 0.30 and 3 <= s["median_len"] <= 22
                        and s["num"] < 0.2 and s["distinct"] <= 0.5,
                        excl | {plan["street"]})
    return {k: v for k, v in plan.items() if v}


class FieldGuess(Rule):
    name = "field_guess"
    version = 2
    needs_mapping = False

    def __init__(self):
        self._plan: dict[str, str] = {}
        self._plan_keys: frozenset = frozenset()
        self._kw: dict[frozenset, dict] = {}

    def static_score(self, row: dict, meta: dict | None = None) -> float:
        return 0.55                      # 永远第二候选：本地重跑极便宜，宁可多试一套

    def warm(self, rows: list[dict]) -> None:
        if not rows:
            return
        keys = frozenset(str(k) for r in rows[:50] for k in r.keys()
                         if not str(k).startswith("_"))
        if keys != self._plan_keys:
            self._plan = plan_columns(rows)
            self._plan_keys = keys

    def _columns(self, row: dict) -> dict[str, str]:
        keys = [str(k) for k in row.keys() if not str(k).startswith("_")]
        sig = frozenset(keys)
        if sig not in self._kw:
            self._kw[sig] = guess_keys(keys)       # ① 名字能看懂 → 用名字
        kw = self._kw[sig]
        plan = dict(self._plan)                    # ② 名字看不懂 → 用值形态
        for slot, col in kw.items():
            if col:
                plan[slot] = col
        if not plan.get("number"):
            num_cands = [k for k in keys if NUM_RE.match(str(row.get(k) or "").strip())]
            if num_cands:
                plan["number"] = num_cands[0]
        return plan

    def clean(self, row: dict, ctx: RuleCtx):
        cols = self._columns(row)
        num_k, street_k = cols.get("number"), cols.get("street")
        if not (num_k and street_k):
            return None, "guess_no_columns"
        num_v = row.get(num_k)
        st_v = str(row.get(street_k) or "").strip()
        # 两道硬闸（踩过的坑：wheeler 的 STR 大片是 "0"，v2 一开始把无效行全“猜”成了有效记录）：
        #   ① 号必须真像号  ② 街里必须有连续两个字母（纯数字/空值不是街名）
        if not NUM_RE.match(str(num_v or "").strip()):
            return None, "guess_bad_number"
        if not _looks_street(st_v):
            return None, "guess_street_not_text"
        return mk_record(num_v, st_v,
                         row.get(cols.get("city", ""), ""), row.get(cols.get("zip", ""), ""),
                         row.get(cols.get("state", ""), ""), ctx.source_id,
                         ctx.default_state)

    def self_test(self):
        ctx = RuleCtx("t", {}, {}, "IL")
        rows = [{"HOUSENUM": "12", "STREET": "Main St", "CITY": "Springfield",
                 "STATE": "IL", "ZIP": "62701"}]
        self.warm(rows)
        rec, why = self.clean(rows[0], ctx)
        assert rec and rec["number"] == "12", (rec, why)
        # 字段名完全看不懂（ArcGIS 风格）→ 靠值形态
        weird = [{"F1": "1514", "F2": "N US Hwy 45", "F3": "Mason", "F4": "62443", "F5": "IL"},
                 {"F1": "18", "F2": "E Main St", "F3": "Mason", "F4": "62443", "F5": "IL"},
                 {"F1": "9", "F2": "S Oak Ave", "F3": "Mason", "F4": "62443", "F5": "IL"}]
        self.warm(weird)
        rec2, why2 = self.clean(weird[0], ctx)
        assert rec2 and rec2["number"] == "1514" and rec2["street"].startswith("N US"), (rec2, why2)
        return True, "键名猜测 + 值形态猜列（未知字段名）OK"


RULES = [FieldGuess()]
