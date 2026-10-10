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


def first_numeric_key(rows: list[dict], keys: list[str]) -> str:
    """没有字段名可猜时，挑一个「值几乎全是短数字」的列当门牌号列。"""
    best, best_hit = "", 0
    need = 2 if len(rows) <= 4 else max(3, len(rows) // 2)
    for k in keys:
        hit = sum(1 for r in rows[:60] if NUMERIC_RE.match(str(r.get(k, ""))))
        if hit > best_hit and hit >= need:
            best, best_hit = k, hit
    return best
