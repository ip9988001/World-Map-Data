"""规则 4：llm_spec —— AI 造的**声明式**规则（LLM 只写 JSON，不写代码）。

为什么不让 LLM 直接生成 Python 规则：规则跑在清洗主路上，一段生成代码里的任何 bug
都会污染整套产出（凭空造数 / 错位解析）。而绝大多数缺口其实只是「这个源的字段名或
地址形态没被现有规则覆盖」—— 那是**数据**（列名 + 模板 + 拆分模式），不是代码。

链路：LLM 输出 JSON 规格 → 本文件（人写的通用解释器）执行 → collectors/rule_author.py
的验证闸门在入库前，用同一套 mk_record 铁律 + 反造数检查把规格跑一遍，
不过关的进 collectors/rules/specs/rejected/。规格只增不改：改语义 = 新 name/version。

规格字段（最小可用：{"name":"x","version":1,"mode":"single","single":"SiteAddress"}）：
  name             规则名（自动加 llm_ 前缀；账本按 name@vN 记账）
  version          语义变更 +1
  match.columns_any / columns_all / columns_none   字段名门槛（决定出场）
  match.engine     仅备注（哪个引擎的源）
  mode             "columns"（号/街分列） | "single"（一整列完整地址）
  columns          {"number","street","city","zip","state"} → 源里的真实列名
  number_template  号码由多列拼（"{NUM}{SUFFIX}"）
  street_template  街名由多列拼（"{DIR} {NAME} {TYPE}"，US 地块数据极常见）
  full             备用完整地址列（columns 模式下的兜底拆分源）
  single           mode=single 时的完整地址列名
  unit_drop        是否丢掉 Apt/Unit（默认丢）
  examples         [{row:{...}, expect:{number,street}}] —— 自检样例，必须来自真实样本
"""
from __future__ import annotations

import json
import os
import re

from . import Rule, RuleCtx
from ._common import mk_record, parse_addr_v2

SPEC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "specs")
UNIT_RE = re.compile(r"\b(apt|unit|ste|suite|bldg|#)\s*[\w-]+\.?,?", re.I)


def load_specs(spec_dir: str = SPEC_DIR) -> list[dict]:
    out: list[dict] = []
    if not os.path.isdir(spec_dir):
        return out
    for fn in sorted(os.listdir(spec_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(spec_dir, fn), encoding="utf-8") as fh:
                spec = json.load(fh)
            spec["_file"] = fn
            out.append(spec)
        except Exception as e:            # 坏规格不许拖垮整条流水线
            print(f"[rules] 规格 {fn} 读不动（跳过）: {type(e).__name__}: {e}", flush=True)
    return out


def spec_name(spec: dict) -> str:
    raw = str(spec.get("name") or spec.get("_file", "spec")).lower()
    return "llm_" + re.sub(r"[^a-z0-9]+", "_", raw).strip("_")


def _s(v) -> str:
    return str(v).strip() if isinstance(v, (str, int, float)) and v is not None else ""


def _fmt(tpl: str, row: dict) -> str:
    """"{DIR} {NAME} {TYPE}" → 用行里的值填（缺的当空，不许报错）。"""
    def sub(m: re.Match) -> str:
        return _s(row.get(m.group(1).strip()))
    return re.sub(r"\{([^{}]+)\}", sub, str(tpl))


class SpecRule(Rule):
    needs_mapping = False

    def __init__(self, spec: dict):
        self.spec = spec
        self.name = spec_name(spec)
        self.version = int(spec.get("version") or 1)
        self.file = spec.get("_file", "")

    # ── 出场判定：字段名对得上的源才出场（零成本）──
    def static_score(self, row: dict, meta: dict | None = None) -> float:
        m = self.spec.get("match") or {}
        keys = {str(k).lower() for k in row.keys()}
        if any(str(c).lower() in keys for c in (m.get("columns_none") or [])):
            return 0.0
        all_ = [str(c).lower() for c in (m.get("columns_all") or [])]
        if all_ and not all(c in keys for c in all_):
            return 0.0
        any_ = [str(c).lower() for c in (m.get("columns_any") or [])]
        if any_:
            return 1.0 if any(c in keys for c in any_) else 0.0
        # 没写任何门槛 = 通用规格，只在别人失手时垫底
        return 0.5

    def clean(self, row: dict, ctx: RuleCtx):
        if (self.spec.get("mode") or "") == "single" or (not self.spec.get("mode")
                                                         and self.spec.get("single")):
            return self._clean_single(row, ctx)
        return self._clean_columns(row, ctx)

    def _clean_single(self, row: dict, ctx: RuleCtx):
        spec = self.spec
        v = _s(row.get(str(spec.get("single") or "")))
        if not v:
            return None, "spec_empty_single"
        cols = {k: str(x) for k, x in (spec.get("columns") or {}).items() if x}
        number = street = city = zipv = state = ""
        parsed = parse_addr_v2(v)          # v2：支持前置方位（"N 123 Main St"）+ 右锚定剥尾
        if parsed:
            number, street, city, state, zipv = parsed
        number = number or _s(row.get(cols.get("number", "")))
        street = street or _s(row.get(cols.get("street", "")))
        city = city or _s(row.get(cols.get("city", "")))
        zipv = zipv or _s(row.get(cols.get("zip", "")))
        state = state or _s(row.get(cols.get("state", "")))
        if street and spec.get("unit_drop", True):
            street = UNIT_RE.sub(" ", street).strip(" ,")
        return mk_record(number, street, city, zipv, state, ctx.source_id, ctx.default_state)

    def _clean_columns(self, row: dict, ctx: RuleCtx):
        spec = self.spec
        cols = {k: str(x) for k, x in (spec.get("columns") or {}).items() if x}
        num = (_fmt(spec["number_template"], row) if spec.get("number_template")
               else _s(row.get(cols.get("number", ""))))
        street = (_fmt(spec["street_template"], row) if spec.get("street_template")
                  else _s(row.get(cols.get("street", ""))))
        if (not street or not num) and spec.get("full"):
            parsed = parse_addr_v2(_s(row.get(str(spec["full"]))))
            if parsed:
                num = num or parsed[0]
                street = street or parsed[1]
        if spec.get("unit_drop", True):
            street = UNIT_RE.sub(" ", street)
        street = re.sub(r"\s{2,}", " ", street).strip(" ,-")
        return mk_record(num, street, _s(row.get(cols.get("city", ""))),
                         _s(row.get(cols.get("zip", ""))), _s(row.get(cols.get("state", ""))),
                         ctx.source_id, ctx.default_state)

    def self_test(self):
        exs = self.spec.get("examples") or []
        if not exs:
            return True, "无 examples（跳过）"
        for i, ex in enumerate(exs, 1):
            row = ex.get("row") or {}
            rec, why = self.clean(row, RuleCtx("t", row, {}, _s(ex.get("default_state")), {}))
            if not rec:
                return False, f"example{i} 洗不出：{why}"
            exp = ex.get("expect") or {}
            if exp.get("number") and str(exp["number"]).strip() != rec["number"]:
                return False, f"example{i} 号不符：期望 {exp['number']}，实得 {rec['number']}"
            if exp.get("street"):
                want = str(exp["street"]).strip().upper()
                got = rec["street"].upper()
                if not (got.startswith(want[:10]) or want.startswith(got[:10])):
                    return False, f"example{i} 街名不符：期望 {exp['street']}，实得 {rec['street']}"
        return True, f"{len(exs)} 条样例通过（{self.file}）"


RULES = [SpecRule(s) for s in load_specs()]
