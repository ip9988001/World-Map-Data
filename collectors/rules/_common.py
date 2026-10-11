"""规则共用件：记录构造闸门（与 normalize.to_record_ex 同一套铁律）+ 字段名猜测打分。"""
from __future__ import annotations

import re

from ..normalize import (norm_city, norm_number, norm_state, norm_street, norm_zip)

# ── 字段名猜测（多语言关键词，只用于「不依赖源总表映射」的规则）──
PAT_NUM = re.compile(r"(house)?[_ ]?(num|nr|nbr|number|no)$|hou?senum|housenr|hausnr|nummer|huisnummer|"
                     r"adr?num|primary_number|street_number|n_house|house_num", re.I)
PAT_STREET = re.compile(r"street|strasse|straat|str$|_str|thoroughfare|streetname|"
                        r"rue$|_rue|via$|calle|carrer|road|_rd$|avenue|_ave$|lane|weg$|"
                        r"addr.*(street|name)|st_name|str_name", re.I)
PAT_CITY = re.compile(r"city|town|municipal|gemeente|commune|localit|place|ciudad|"
                      r"ville|ort$|_ort", re.I)
PAT_ZIP = re.compile(r"zip|postal|postcode|plz|post_code|codigo_postal|cp$|_cp", re.I)
PAT_STATE = re.compile(r"^state|state$|statecode|state_code|region|province|prov$|"
                       r"prefecture|bundesland|comunidad", re.I)
PAT_UNIT = re.compile(r"unit|apt|apartment|suite|ste$|floor|fl$|bldg|building|"
                      r"wohnung|appartement", re.I)

NUMERIC_RE = re.compile(r"^\s*\d{1,6}\s*[A-Za-z]?\s*$")
# 完整地址：「123 N Main St」/「123A Main St」—— 号与街之间必须有空格，否则会把街名的
# 首字母吞进门牌号（"123 N Main" 曾解析成 number=123N）
FULL_ADDR_RE = re.compile(r"^\s*(\d{1,6}(?:[-/]\d+)?[A-Za-z]?)\s+(.{3,120}?)\s*$")
# 完整地址末尾的城市/州/ZIP：「..., San Francisco, CA 94110」
TAIL_RE = re.compile(r",?\s*([^,]{2,40}?)[,\s]+([A-Z]{2}|[A-Za-z ]{3,25})[,\s]+(\d{5}(?:-\d{4})?)\s*$")


def mk_record(number, street, city, zipv, statev, source: str, default_state: str = ""):
    """五件套 → 记录（与 normalize.to_record_ex 完全同一套闸门）。"""
    number = norm_number(number)
    street = norm_street(street)
    if not number:
        return None, "no_number"
    if not street:
        return None, "no_street"
    state = norm_state(statev, default_state)
    if not state:
        return None, "state_invalid"
    city = norm_city(city)
    zip5 = norm_zip(zipv)
    if zip5 and number == zip5:
        return None, "number_is_zip"
    if zip5 and street.startswith(zip5 + " "):
        return None, "street_is_address_blob"
    key = f"{state}|{city}|{zip5}|{street}|{number}"
    return ({"state": state, "city": city, "zip": zip5, "street": street,
             "number": number, "key": key, "source": source}, None)


def guess_keys(keys: list[str]) -> dict[str, str]:
    """从一批字段名里猜出 number/street/city/zip/state/unit 各自最可能的键。"""
    out: dict[str, str] = {}
    prio = {"number": (PAT_NUM, 3), "street": (PAT_STREET, 3), "city": (PAT_CITY, 2),
            "zip": (PAT_ZIP, 2), "state": (PAT_STATE, 2), "unit": (PAT_UNIT, 1)}
    for slot, (pat, need) in prio.items():
        best, best_len = "", 0
        for k in keys:
            if pat.search(k):
                if len(k) > best_len:          # 关键词更长 = 更具体（housenum 优于 no）
                    best, best_len = k, len(k)
        if best:
            out[slot] = best
    return out


SUFFIX_ANY2 = re.compile(r"\b(st|street|ave|av|avenue|rd|road|dr|drive|ln|lane|blvd|way|hwy|highway|"
                        r"ct|court|pl|place|ter|cir|circle|pkwy|parkway|trl|trail|loop|rt|route|"
                        r"fwy|expy|tpke|pike|bend|holw|hollow|spur|xing|cv|cove|pt|pass|run|"
                        r"walk|sq|row|alley|aly)\b\.?", re.I)
_BARE_SUFFIX = re.compile(r"^(st|street|ave|av|avenue|rd|road|dr|drive|ln|lane|blvd|way|hwy|highway|"
                          r"ct|court|pl|place|ter|cir|pkwy|trl|loop|rt|route|fwy|expy)$", re.I)
# 非「场地地址」列：业主邮寄地址 / 法律描述 / 地类码 —— 误用会拉起另一拨人的地址（污染 key）
NON_SITE_KEY = re.compile(r"mail|bill|owner|taxpayer|grantee|grantor|legal|comment|descr|"
                          r"class|zoning|use_?code|name|contact|deed|note", re.I)
SITE_KEY = re.compile(r"site|situs|physical|property|parcel_?addr|location|addr|address|street", re.I)
# 整条地址 v2（新能力，不动 v1 语义）：可选前置方位 + 号 + 街名（街名不许以非字母数字开头）
ADDR_RE2 = re.compile(r"^\s*(?:(?P<dir>[NSEW]{1,2})[.\s]+)?"
                      r"(?P<num>\d{1,6}[A-Za-z]?(?:[-/]\d{1,5})?)\s+(?P<street>[A-Za-z0-9].{1,90})$")


def _street_ok(s: str) -> bool:
    """街名得像街名：≥3 字、≥2 字母，且不是光一个后缀词（"St"/"Av" 不是街名）。"""
    if len(s) < 3:
        return False
    if len(re.sub(r"[^A-Za-z]", "", s)) < 2:
        return False
    words = re.sub(r"[^A-Za-z ]", " ", s).split()
    return not (len(words) == 1 and _BARE_SUFFIX.match(words[0]))


def split_tail(text: str) -> tuple[str, str, str, str]:
    """从整条地址尾部剥出 city / state / ZIP —— **右锚定**。

    v1 的 TAIL_RE 会把城市吞进州（"1514 N US Hwy 45 Mason IL 62443" 里
    city="N US Hwy 45"、state="Mason IL"），剩下的街名为空 → 整条洗不出来。
    两种形态：
      · 逗号式「街, 市, 州 ZIP」→ 市从逗号节直接拿
      · 无逗号式「1514 N US Hwy 45 Mason IL 62443」→ 剥掉州+ZIP 后按词判：
        最后一个词是市（前提：前面部分像街 = 含门牌号或街名后缀词），且该词本身不是后缀词。
    拿不准就给空 —— 错的市名会污染去重 key。
    """
    t = str(text or "").strip().strip(",")
    zip5 = state = city = ""
    parts = [p for p in (x.strip() for x in t.split(",")) if p]
    if len(parts) >= 3:                      # 街, 市, 州 ZIP
        city = parts[-2]
        head = ", ".join(parts[:-2])
        tail = parts[-1]
    elif len(parts) == 2:                    # 街, 州 ZIP
        head, tail = parts[0], parts[1]
    else:
        head, tail = "", (parts[0] if parts else "")
    m = re.search(r"(?:^|[,\s])(\d{5})(?:-\d{4})?$", tail)
    if m:
        zip5 = m.group(1)
        tail = tail[: m.start()].strip().strip(",")
    m = re.search(r"(?:^|[,\s])([A-Z]{2}|[A-Za-z]{4,20})$", tail)
    if m:
        state = m.group(1)
        tail = tail[: m.start()].strip().strip(",")
    if not city and tail:
        toks = tail.split()
        if len(toks) >= 3:
            cand, pre = toks[-1], " ".join(toks[:-1])
            looks_street = bool(re.search(r"\d", pre)) or bool(SUFFIX_ANY2.search(pre))
            if looks_street and re.match(r"^[A-Za-z][A-Za-z'\-]{1,20}$", cand):
                city, tail = cand, pre
    street = " ".join(x for x in (head, tail) if x).strip()
    return street, city, state, zip5


def parse_addr_v2(text: str):
    """整条地址 → (number, street, city, state, zip)；不成地址就给 None。

    比 v1 强的两处：① 认前置方位（"N 123 Main St"）② 右锚定剥尾（不吞街名）。
    """
    m = ADDR_RE2.match(str(text or "").strip())
    if not m:
        return None
    num, rest = m.group("num"), m.group("street")
    street, city, state, zip5 = split_tail(rest)
    street = street.strip(" ,-").strip()
    if not _street_ok(street):
        return None
    # 既没剖出城市、街名也不以街名后缀词结尾 → 多半是「街名 + 地块名」的假地址，丢弃
    if not city and not SUFFIX_ANY2.search(street.split()[-1] if street else ""):
        return None
    return num, street, city, state, zip5


def site_addr_key(row: dict) -> str:
    """挑「场地地址」列：优先名字像地址的，排除业主邮寄/法律描述/地类码列。"""
    scored = []
    for k in row.keys():
        if str(k).startswith("_") or NON_SITE_KEY.search(str(k)):
            continue
        scored.append((1 if SITE_KEY.search(str(k)) else 0, len(str(k)), str(k)))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    for _s, _l, k in scored:
        if parse_addr_v2(row.get(k)):
            return k
    return ""


def first_numeric_key(rows: list[dict], keys: list[str]) -> str:
    """没有字段名可猜时，挑一个「值几乎全是短数字」的列当门牌号列。"""
    best, best_hit = "", 0
    need = 2 if len(rows) <= 4 else max(3, len(rows) // 2)
    for k in keys:
        hit = sum(1 for r in rows[:60] if NUMERIC_RE.match(str(r.get(k, ""))))
        if hit > best_hit and hit >= need:
            best, best_hit = k, hit
    return best
