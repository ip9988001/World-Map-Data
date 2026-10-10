"""地址标准化 —— 把各源五花八门的字段，归到同一套结构。

铁律：**不含门牌号的记录，一律丢弃。**

⚠️ OpenAddresses 的 conform 有三种形态，必须全部支持，否则会「整源 0 条」：

  1) 纯字段名（拼接语义，不是取第一个非空）：
        number = ['STRUCTNO','STRUCTSU']                → 820 + A = "820A"
        street = ['PREDIR','STREETNAME','STREETTYPE']   → S MAIN ST

  2) 函数式（从**整串地址**里解析）：
        number = {"function":"prefixed_number","field":"SitusAddress1"}   → "820 WILLOW ST" → 820
        street = {"function":"postfixed_street","field":"SitusAddress1"}  → "820 WILLOW ST" → WILLOW ST

  3) 其它函数：format / join / regexp / remove_prefix / remove_postfix / chain / constant

输出结构：
    state, city, zip, street, number, key, source
主键（用于去重）：
    (state, city_norm, zip5, street_norm, number_norm)
"""
from __future__ import annotations

import ast
import re

# ---------- USPS 标准缩写字典（后续版本化迭代） ----------
SUFFIX = {
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD", "DRIVE": "DR",
    "BOULEVARD": "BLVD", "LANE": "LN", "COURT": "CT", "PLACE": "PL",
    "TERRACE": "TER", "CIRCLE": "CIR", "HIGHWAY": "HWY", "PARKWAY": "PKWY",
    "TRAIL": "TRL", "SQUARE": "SQ", "ALLEY": "ALY", "EXPRESSWAY": "EXPY",
    "FREEWAY": "FWY", "TURNPIKE": "TPKE", "PLAZA": "PLZ", "CROSSING": "XING",
    "CENTER": "CTR", "EXTENSION": "EXT", "JUNCTION": "JCT", "MOUNT": "MT",
    "MOUNTAIN": "MTN", "RIVER": "RIV", "SCHOOL": "SCH", "STATION": "STA",
    "APARTMENT": "APT", "SUITE": "STE", "BUILDING": "BLDG",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
DIRECTIONALS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}

_PUNCT = re.compile(r"[^\w\s\-]")
_WS = re.compile(r"\s+")
_UNIT = re.compile(r"\b(APT|STE|SUITE|UNIT|BLDG|FL|FLOOR)\b.*$", re.I)
_LEAD_NUM = re.compile(r"^\s*(\d+[A-Za-z]?)\b")
_TAIL_NUM = re.compile(r"\b(\d+[A-Za-z]?)\s*$")
_EMPTYISH = {"", "-", "0", "#", "N/A", "NA", "NULL", "NONE", "NO"}


# ---------- 基础归一 ----------
def norm_text(s) -> str:
    if s is None:
        return ""
    s = str(s).upper().strip()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def norm_street(name) -> str:
    s = norm_text(name)
    if not s:
        return ""
    s = _UNIT.sub("", s).strip()
    return " ".join(SUFFIX.get(w, w) for w in s.split())


def norm_city(name) -> str:
    return norm_text(name)


def norm_zip(z) -> str:
    s = re.sub(r"\D", "", str(z or ""))
    return s[:5] if len(s) >= 5 else ""


def norm_number(n) -> str:
    s = re.sub(r"\s+", "", str(n or "").upper())
    m = re.match(r"^(\d+)([A-Z]?)$", s)
    if m:
        return m.group(1) + m.group(2)
    m = re.match(r"^(\d+)", s)
    return m.group(1) if m else ""


# ---------- 取值核心 ----------
def cell(row: dict, key: str):
    if key in row:
        return row[key]
    lk = str(key).strip().lower()
    for rk in row:
        if str(rk).strip().lower() == lk:
            return row[rk]
    return None


def _s(v) -> str:
    if v is None:
        return ""
    v = str(v).strip()
    return "" if v.upper() in _EMPTYISH else v


def _apply_function(row: dict, spec: dict) -> str:
    fn = str(spec.get("function") or "").lower()
    field = spec.get("field")
    fields = spec.get("fields") or ([field] if field else [])
    vals = [_s(cell(row, f)) for f in fields]
    src = vals[0] if vals else ""

    if fn in ("prefixed_number", "prefix_number"):
        m = _LEAD_NUM.match(src)
        return m.group(1) if m else ""
    if fn in ("postfixed_number", "suffix_number"):
        m = _TAIL_NUM.search(src)
        return m.group(1) if m else ""
    if fn == "postfixed_street":
        m = _LEAD_NUM.match(src)
        return src[m.end():].strip() if m else src
    if fn == "prefixed_street":
        m = _TAIL_NUM.search(src)
        return src[:m.start()].strip() if m else src
    if fn == "remove_prefix":
        p = str(spec.get("prefix") or "")
        return src[len(p):].strip() if src.upper().startswith(p.upper()) else src
    if fn == "remove_postfix":
        p = str(spec.get("postfix") or "")
        return src[:-len(p)].strip() if p and src.upper().endswith(p.upper()) else src
    if fn == "regexp":
        pat = spec.get("pattern") or ""
        rep = spec.get("replace") or ""
        try:
            return re.sub(pat, rep, src).strip()
        except re.error:
            return src
    if fn == "join":
        sep = str(spec.get("separator", " "))
        return sep.join(v for v in vals if v)
    if fn == "format":
        tpl = str(spec.get("format") or "")
        out = tpl
        for f in fields:
            v = _s(cell(row, f))
            out = out.replace("{" + str(f) + "}", v)
        return out.strip()
    if fn == "constant":
        return str(spec.get("value") or "")
    if fn == "chain":
        out = src
        for sub in (spec.get("chain") or []):
            if isinstance(sub, dict):
                tmp = dict(sub)
                tmp.setdefault("field", "")
                if not tmp.get("fields") and not tmp.get("field") and out:
                    out = _apply_passthrough(out, tmp)
                else:
                    out = _apply_function(row, tmp) or out
        return out
    if fn == "swap":
        return " ".join(reversed([v for v in vals if v]))
    if fn in ("lower", "upper", "truncate"):
        v = vals[0] if vals else ""
        if fn == "lower":
            return v.lower()
        if fn == "upper":
            return v.upper()
        n = spec.get("length") or spec.get("limit") or 0
        try:
            return v[:int(n)]
        except Exception:
            return v
    # 未知函数 → 退回字段值
    return " ".join(v for v in vals if v)


def _apply_passthrough(text: str, spec: dict) -> str:
    fn = str(spec.get("function") or "").lower()
    if fn in ("prefixed_number", "prefix_number"):
        m = _LEAD_NUM.match(text)
        return m.group(1) if m else ""
    if fn == "postfixed_street":
        m = _LEAD_NUM.match(text)
        return text[m.end():].strip() if m else text
    return text


def resolve(row: dict, spec, sep: str = " ") -> str:
    """按 OA 语义解析一个字段规格 → 字符串。支持 str / dict / list。"""
    if spec in (None, "", [], {}):
        return ""
    if isinstance(spec, list):
        parts = [resolve(row, s, sep) for s in spec]
        return sep.join(p for p in parts if p).strip()
    if isinstance(spec, dict):
        return _apply_function(row, spec)
    return _s(cell(row, str(spec)))


# ---------- 主入口 ----------
def to_record(row: dict, mapping: dict, source: str, default_state: str = "") -> dict | None:
    """原始行 → 标准化记录。不合法（无门牌号 / 无街道）返回 None。"""
    number = norm_number(resolve(row, mapping.get("number"), sep=""))
    street = norm_street(resolve(row, mapping.get("street")))
    if not number or not street:
        return None                       # ← 铁律：没有门牌号，整条丢弃
    state = norm_text(resolve(row, mapping.get("region")))[:2] or norm_text(default_state)[:2]
    city = norm_city(resolve(row, mapping.get("city")))
    zip5 = norm_zip(resolve(row, mapping.get("postcode")))
    key = f"{state}|{city}|{zip5}|{street}|{number}"
    return {"state": state, "city": city, "zip": zip5, "street": street,
            "number": number, "key": key, "source": source}


def parse_mapping_from_table(csv_row: dict) -> dict:
    """从「源总表」一行解析字段映射（列里存的是 OA 定义的字面量）。"""
    def _p(v):
        v = (v or "").strip()
        if not v:
            return ""
        if v.startswith("[") or v.startswith("{"):
            try:
                return ast.literal_eval(v)
            except Exception:
                return v.strip("[]{}'\"")
        return v.strip("'\"")
    return {"number": _p(csv_row.get("number")), "street": _p(csv_row.get("street")),
            "city": _p(csv_row.get("city_f")), "region": _p(csv_row.get("region")),
            "postcode": _p(csv_row.get("postcode"))}


def mapping_sanity(row: dict, mapping: dict) -> list[str]:
    """检查映射里引用的字段是否真的存在于数据中（发现过期定义）。"""
    def names(spec, acc):
        if isinstance(spec, list):
            for s in spec:
                names(s, acc)
        elif isinstance(spec, dict):
            if spec.get("field"):
                acc.append(str(spec["field"]))
            for f in (spec.get("fields") or []):
                acc.append(str(f))
        elif spec:
            acc.append(str(spec))
        return acc
    need = names(mapping.get("number"), []) + names(mapping.get("street"), []) \
        + names(mapping.get("city"), []) + names(mapping.get("postcode"), [])
    have = {str(k).strip().lower() for k in row}
    return [n for n in need if n.strip().lower() not in have]
