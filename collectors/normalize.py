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


# ---------- 州码校验（脏数据闸门）----------
# 老逻辑 `norm_text(raw)[:2]` 会把 "1" / "55" / "ST" / "TEXAS"(→TE) 之类当成州码，
# 也会把 ZIP、街道后缀截进来 —— 这就是脏州码的来源。
STATE_CODES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
                  "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
                  "DC PR VI GU AS MP".split())
STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI",
    "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC", "WASHINGTON DC": "DC",
}


def norm_state(raw, default: str = "") -> str:
    """只认合法州码；认不出就退回 default（源表里的州），都没有则返回空串 → 该条丢弃。"""
    t = norm_text(raw)
    if t:
        if t in STATE_NAMES:
            return STATE_NAMES[t]
        if t in STATE_CODES:
            return t
        head = t.split()[0] if t.split() else ""
        if head in STATE_NAMES:
            return STATE_NAMES[head]
        if head in STATE_CODES:
            return head
    d = norm_text(default)
    if d in STATE_CODES:
        return d
    if d in STATE_NAMES:
        return STATE_NAMES[d]
    return ""


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


def _apply_function(row: dict, spec: dict, text: str | None = None) -> str:
    """执行一个映射函数。text 非 None 时以它作为输入（用于 chain 中间值/变量引用）。"""
    fn = str(spec.get("function") or "").lower()
    field = spec.get("field")
    fields = spec.get("fields") or ([field] if field else [])
    vals = [_s(cell(row, f)) for f in fields]
    src = text if text is not None else (vals[0] if vals else "")

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
        if not p and spec.get("field_to_remove"):
            p = _s(cell(row, spec["field_to_remove"]))
        i = src.upper().find(p.upper()) if p else -1
        return (src[:i] + src[i + len(p):]).strip(" ,;") if i == 0 else src
    if fn == "remove_postfix":
        # OA 语义两种写法：postfix 直接给字符串；field_to_remove 给**另一个字段名**，
        # 取该字段的值当要删的后缀（如 SiteAddress 去掉 SiteCSZ="Mason IL 62443"）。
        p = str(spec.get("postfix") or "")
        if not p and spec.get("field_to_remove"):
            p = _s(cell(row, spec["field_to_remove"]))
        if not p:
            return src
        i = src.upper().rfind(p.upper())
        return (src[:i] + src[i + len(p):]).strip(" ,;") if i >= 0 else src
    if fn == "regexp":
        pat = spec.get("pattern") or ""
        rep = spec.get("replace")
        try:
            if rep is None:
                # 没有 replace → **提取语义**：有捕获组就取第 1 组。
                # （老实现用 re.sub(pat, "", src)：当 pattern 能匹配整串时会把街道名整条删空 → 整源 0 条）
                m = re.search(pat, src)
                if not m:
                    return src
                return (m.group(1) if m.groups() else m.group(0)).strip()
            # 有 replace → 替换语义，但必须把 JS/PHP 风格的 $1 转成 Python 的 \g<1>。
            # （老实现直接 re.sub(pat, "$1", src)：Python 不认 $1，会输出**字面量 "$1"** → 街道名变成 "$1"）
            return re.sub(pat, _js_repl_to_py(str(rep)), src).strip()
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
        # ⚠ 2026-10-11 修：OA 规范里子步骤在 "functions" 键下，老实现只读 "chain" 键
        #   → 子步骤一个都不执行 → 街道整段为空 → 整源 0 条（us/il/effingham 就是这样丢的 2.3 万条）。
        # 变量引用：子步骤里 field 写 "oa:<variable>" 或直接写 variable 名时，输入取**中间值**。
        steps = spec.get("functions") or spec.get("chain") or []
        var = str(spec.get("variable") or "")
        cur = src
        for sub in steps:
            if not isinstance(sub, dict):
                continue
            t = dict(sub)
            f = str(t.get("field") or "")
            is_var = bool(f) and (f.startswith("oa:") or (var and f == var))
            t2 = dict(t)
            if is_var:
                t2.pop("field", None)
                t2.pop("fields", None)
            if is_var or (not f and not t2.get("fields")):
                cur = _apply_function(row, t2, text=cur)
            else:
                cur = _apply_function(row, t2) or cur
        return cur
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


def _js_repl_to_py(rep: str) -> str:
    """把 JS/PHP 风格的 $1 / $2 替换语法规整成 Python re.sub 认的 \\g<1>。"""
    return re.sub(r"\$(\d+)", r"\\g<\1>", rep)


def _apply_passthrough(text: str, spec: dict) -> str:
    """纯文本版本（无字段引用）—— 现已被 _apply_function(text=...) 取代，保留兼容旧调用。"""
    return _apply_function({}, dict(spec, field=None, fields=None), text=text)


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
def to_record_ex(row: dict, mapping: dict, source: str, default_state: str = ""):
    """原始行 → (标准化记录, 丢弃原因)。合格记录的原因位为 None。"""
    number = norm_number(resolve(row, mapping.get("number"), sep=""))
    street = norm_street(resolve(row, mapping.get("street")))
    if not number:
        return None, "no_number"          # ← 铁律：没有门牌号，整条丢弃
    if not street:
        return None, "no_street"
    state = norm_state(resolve(row, mapping.get("region")), default_state)
    if not state:
        return None, "state_invalid"
    city = norm_city(resolve(row, mapping.get("city")))
    zip5 = norm_zip(resolve(row, mapping.get("postcode")))
    if zip5 and number == zip5:
        return None, "number_is_zip"      # 整串地址兜底残留：ZIP 被当成门牌号
    if zip5 and street.startswith(zip5 + " "):
        return None, "street_is_address_blob"
    key = f"{state}|{city}|{zip5}|{street}|{number}"
    return ({"state": state, "city": city, "zip": zip5, "street": street,
             "number": number, "key": key, "source": source}, None)


def to_record(row: dict, mapping: dict, source: str, default_state: str = "") -> dict | None:
    """兼容旧调用：只要记录，丢弃原因走 to_record_ex。"""
    return to_record_ex(row, mapping, source, default_state)[0]


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
