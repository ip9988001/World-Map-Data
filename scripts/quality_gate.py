"""地址形态质量闸门 —— **单一事实来源**（数据工厂分级 + 站点静态池生成共用）。

为什么存在：2026-10-13 对发布包全量 2861 万行逐条核验，发现 **9.13%（261 万条）是解析错位产物**：
  · 门牌号重复进街道字段：number=1406 / street="1406 ELM ST"
  · 城市被截成残片：city="RO"（Roseville 被切）
  · 门牌/街道是纯数字噪声：street="1 2"
  · 邮编非法或缺失后被字典补
按州不合格率 0%（NM/NC/AR/MD）~ 100%（NH 57 万行全废）、AK 93.9%、WA 40.2%。

用法：
  from quality_gate import check
  ok, why = check(rec)      # ok=False 时 why 是稳定英文标识，可直接做统计键
"""
from __future__ import annotations

import re

NUM_RE = re.compile(r"^\d{1,6}$")
ZIP_RE = re.compile(r"^\d{5}")
STREET_BAD_HEAD = re.compile(r"^\d+\s")          # 以「数字+空格」开头 = 门牌串进街道
STREET_LONG_DIGITS = re.compile(r"\d{4,}")        # 街道里出现 4 位以上数字 = 门牌区间残留
CITY_BAD = {"RO", "ST", "AVE", "RD", "BLVD", "N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def check(rec: dict) -> tuple[bool, str]:
    """返回 (是否可信, 不合格原因标识)。原因标识稳定，可直接当统计键用。"""
    num = str(rec.get("number") or "").strip()
    st = str(rec.get("street") or "").strip()
    city = str(rec.get("city") or "").strip()
    zp = str(rec.get("zip") or "").strip()

    if not num:
        return False, "no_number"
    if not NUM_RE.match(num):
        return False, "number_not_plain_digits"
    if not st:
        return False, "no_street"
    if len(st) < 3:
        return False, "street_too_short"
    if STREET_BAD_HEAD.match(st):
        return False, "street_starts_with_number"      # 门牌重复进街道（最常见）
    if STREET_LONG_DIGITS.search(st):
        return False, "street_has_long_digits"
    if "  " in st:
        return False, "street_double_space"
    if not city:
        return False, "no_city"
    if len(city) < 3:
        return False, "city_too_short"
    if any(ch.isdigit() for ch in city):
        return False, "city_has_digit"
    if city.upper() in CITY_BAD:
        return False, "city_is_fragment"
    if not zp:
        return False, "no_zip"
    if not ZIP_RE.match(zp):
        return False, "zip_not_5_digits"
    if not str(rec.get("state") or "").strip():
        return False, "no_state"
    return True, ""


def describe(why: str) -> str:
    return {
        "no_number": "门牌缺失", "number_not_plain_digits": "门牌非纯数字",
        "no_street": "街道缺失", "street_too_short": "街道过短",
        "street_starts_with_number": "街道以数字开头（门牌重复）",
        "street_has_long_digits": "街道含长数字串",
        "street_double_space": "街道含双空格",
        "no_city": "城市缺失", "city_too_short": "城市过短",
        "city_has_digit": "城市含数字", "city_is_fragment": "城市是残片",
        "no_zip": "邮编缺失", "zip_not_5_digits": "邮编非 5 位数字",
        "no_state": "州缺失",
    }.get(why, why)
