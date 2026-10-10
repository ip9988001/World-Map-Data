"""地址标准化 —— 把各源五花八门的字段，归到同一套结构。

铁律：**不含门牌号的记录，一律丢弃。**

注意 OpenAddresses 的 conform 语义：
  字段映射给的是一个**列表**，意思是「把这些字段拼起来」，
  而不是「取第一个非空的」。例如：
      number = ['STRUCTNO','STRUCTSU']              → 820 + A  = "820A"
      street = ['PREDIR','STREETNAME','STREETTYPE'] → S MAIN ST
  拼错这一步，街道名就会变成 "S" 这种垃圾值。

输出结构（每条一个 dict）：
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


def norm_text(s) -> str:
    if s is None:
        return ""
    s = str(s).upper().strip()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def norm_street(name) -> str:
    """街道名标准化：去单元号 → 逐词缩写 → 归一空格。"""
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
    """门牌号归一：数字 + 可选后缀字母（12A）。"""
    s = re.sub(r"\s+", "", str(n or "").upper())
    m = re.match(r"^(\d+)([A-Z]?)$", s)
    if m:
        return m.group(1) + m.group(2)
    m = re.match(r"^(\d+)", s)
    return m.group(1) if m else ""


# ---------- 取值：支持「拼接语义」 ----------
def spec_keys(spec) -> list[str]:
    if not spec:
        return []
    if isinstance(spec, str):
        return [spec]
    return [str(x) for x in spec if x]


def cell(row: dict, key: str):
    """大小写不敏感地取一个字段的值。"""
    if key in row:
        return row[key]
    lk = key.strip().lower()
    for rk in row:
        if rk.strip().lower() == lk:
            return row[rk]
    return None


def join_values(row: dict, spec, sep: str = " ") -> str:
    """按 OA 语义把映射里的字段**拼接**起来（跳过空值）。"""
    parts = []
    for k in spec_keys(spec):
        v = cell(row, k)
        if v is None:
            continue
        v = str(v).strip()
        if v and v not in ("-", "0", "#", "N/A", "NA", "NULL", "NONE"):
            parts.append(v)
    return sep.join(parts).strip()


def pick_one(row: dict, spec) -> str:
    for k in spec_keys(spec):
        v = cell(row, k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def to_record(row: dict, mapping: dict, source: str, default_state: str = "") -> dict | None:
    """原始行 → 标准化记录。不合法（无门牌号 / 无街道）返回 None。"""
    number = norm_number(join_values(row, mapping.get("number"), sep=""))
    street = norm_street(join_values(row, mapping.get("street")))
    if not number or not street:
        return None                       # ← 铁律：没有门牌号，整条丢弃
    state = norm_text(pick_one(row, mapping.get("region")))[:2] or norm_text(default_state)[:2]
    city = norm_city(pick_one(row, mapping.get("city")))
    zip5 = norm_zip(pick_one(row, mapping.get("postcode")))
    key = f"{state}|{city}|{zip5}|{street}|{number}"
    return {"state": state, "city": city, "zip": zip5, "street": street,
            "number": number, "key": key, "source": source}


def parse_mapping_from_table(csv_row: dict) -> dict:
    """从「源总表」一行里解析字段映射（列里存的是 Python 字面量列表）。"""
    def _p(v):
        v = (v or "").strip()
        if not v:
            return ""
        if v.startswith("["):
            try:
                return [str(x) for x in ast.literal_eval(v)]
            except Exception:
                return [x.strip(" '\"") for x in v.strip("[]").split(",") if x.strip()]
        return v.strip("'\"")
    return {"number": _p(csv_row.get("number")), "street": _p(csv_row.get("street")),
            "city": _p(csv_row.get("city_f")), "region": _p(csv_row.get("region")),
            "postcode": _p(csv_row.get("postcode"))}
