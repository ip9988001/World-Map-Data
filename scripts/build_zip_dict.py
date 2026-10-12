"""生成标准化字典层（13 环节 · 第 12 步）：ZIP ↔ city ↔ state ↔ county ↔ county_fips。

来源（全部公开、免费、可溯源）：
  GeoNames Postal Codes · US  https://download.geonames.org/export/zip/US.zip
  许可 CC BY 4.0（地名数据，需署名 GeoNames）—— **不是** USPS 官方 preferred city
  该表自带：postal_code / place_name / state / county_name / county FIPS(3位) → 正合我们的需要

为什么要这一层：
  实测发布分片里 city 空 34.7%、zip 空（CA 48.5%），county 基本没有。
  有了版本化字典，发布时就能把「可推导的」字段补出来并标 *_src=zip_map，
  采集值与推断值分开记（这是对外可复核的前提），同时 county/county_fips 一并补上。

输出：
  dict/zip_city.tsv    zip \t city \t state \t county \t county_fips
  dict/README.md       来源 / 许可 / 生成时间 / 行数 / sha256 / 已知局限

用法：
  python scripts/build_zip_dict.py                     # 联网下载后生成
  python scripts/build_zip_dict.py --src /path/US.txt  # 用本地已有的原始表
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict

URL = "https://download.geonames.org/export/zip/US.zip"
OUT_TSV = "dict/zip_city.tsv"
OUT_README = "dict/README.md"
UA = "WorldMapData-address-factory/1.0 (+data pipeline; contact: repo owner)"

# 州 FIPS 前缀（GeoNames 只给 3 位县码，补上前缀才是 5 位 GEOID）
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "AS": "60", "GU": "66", "MP": "69",
    "PR": "72", "UM": "74", "VI": "78",
}


def fetch_raw(dst_dir: str) -> str:
    """下载 GeoNames US.zip → 解出 US.txt，返回路径。"""
    os.makedirs(dst_dir, exist_ok=True)
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    print(f"下载 {URL} …", flush=True)
    with urllib.request.urlopen(req, timeout=120) as resp:
        blob = resp.read()
    print(f"  得到 {len(blob) / 1024:.0f}KB", flush=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".txt")), None)
        if not name:
            raise SystemExit("压缩包里找不到 .txt")
        z.extract(name, dst_dir)
        return os.path.join(dst_dir, name)


def parse(src: str):
    """ GeoNames US.txt（制表符分隔，无表头）：
    country, postal_code, place, state_name, state_abbr, county, county_code3, ... , lat, lon, acc
    按 zip 聚合：city 取出现最多的地名（并列取字典序），county 取出现最多的县；
    **同一个 zip 跨县是真实存在的**，取最常见那个，并把跨县情况计数如实报出来。
    """
    zips: dict[str, Counter] = defaultdict(Counter)
    counties: dict[str, Counter] = defaultdict(Counter)
    states: dict[str, Counter] = defaultdict(Counter)
    total = 0
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) < 7:
                continue
            zc = re.sub(r"\D", "", p[1])
            if len(zc) != 5:
                continue
            total += 1
            city = (p[2] or "").strip()
            st = (p[4] or "").strip().upper()
            county = (p[5] or "").strip()
            ccode = re.sub(r"\D", "", p[6] or "")
            if city:
                zips[zc][city] += 1
            if st:
                states[zc][st] += 1
            if county:
                key = f"{county}|{ccode}"
                counties[zc][key] += 1
    return zips, counties, states, total


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="", help="本地 US.txt（不填则联网下载）")
    ap.add_argument("--out", default=OUT_TSV)
    ap.add_argument("--readme", default=OUT_README)
    a = ap.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="zipdict-")
    try:
        src = a.src or fetch_raw(tmp)
        zips, counties, states, total = parse(src)
        rows: list[tuple[str, str, str, str, str]] = []
        for zc in sorted(zips):
            city = zips[zc].most_common(1)[0][0] if zips[zc] else ""
            st = states[zc].most_common(1)[0][0] if states[zc] else ""
            cty, ccode = "", ""
            if counties[zc]:
                cty, ccode = counties[zc].most_common(1)[0][0].split("|", 1)
            fips = (STATE_FIPS.get(st, "") + ccode.zfill(3)) if (st and ccode) else ""
            rows.append((zc, city, st, cty, fips))

        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, "w", encoding="utf-8", newline="") as fh:
            fh.write("zip\tcity\tstate\tcounty\tcounty_fips\n")
            for r in rows:
                fh.write("\t".join(r) + "\n")
        h = hashlib.sha256(open(a.out, "rb").read()).hexdigest()[:16]

        multi_cty = sum(1 for z in counties if len(counties[z]) > 1)
        multi_city = sum(1 for z in zips if len(zips[z]) > 1)
        no_city = sum(1 for r in rows if not r[1])
        no_fips = sum(1 for r in rows if not r[4])
        st_cnt = len({r[2] for r in rows})
        with open(a.readme, "w", encoding="utf-8") as fh:
            fh.write(f"""# 标准化字典层（13 环节 · 第 12 步）

**用途**：发布时把「可推导」的字段补出来（city / zip / county / county_fips），
补出来的值一律标 `city_src=zip_map`、`zip_src=zip_map`、`county_src=zip_map`，
与真实采集值分开记 —— 采集值 vs 推断值必须能区分，这是对外可复核的前提。

**文件**：`zip_city.tsv`（制表符分隔）
| 列 | 含义 |
|---|---|
| zip | 5 位邮编（ZIP/ZCTA） |
| city | 该邮编下出现最多的地名 |
| state | 州两位码 |
| county | 该邮编下出现最多的县名 |
| county_fips | 5 位县 FIPS（州码 2 位 + 县码 3 位） |

**来源**：GeoNames Postal Codes · US — <{URL}>
许可 **CC BY 4.0**，署名 **GeoNames**（<https://www.geonames.org/>）。
生成时间：{time.strftime("%Y-%m-%d %H:%M:%S")}（UTC 由运行环境决定）
原始表行数：{total:,}｜聚合后邮编：{len(rows):,}｜涉及州：{st_cnt}
`zip_city.tsv` sha256 前 16 位：`{h}`

**已知局限（必须如实告知，不许假装权威）**
- 地名来自 GeoNames 的 **place name**，**不是 USPS 官方 preferred city name**；
  两者在部分小城镇写法不一致 → 因此补出的 city 一律带 `_src=zip_map` 标记。
- 一个邮编跨县是真实存在的（本表 {multi_cty:,} 个邮编跨县）→ 取出现最多的那个县。
- 同一邮编对应多个地名（{multi_city:,} 个）→ 取出现最多的那个。
- 缺地名 {no_city:,} 个邮编、缺 FIPS {no_fips:,} 个邮编 → 落空即不补（绝不编）。
- 该表只用于**补齐空缺**，永远不会覆盖源里真实采到的值。

**刷新**：`python scripts/build_zip_dict.py`（联网重生成）；`.github/workflows/dict-refresh.yml`
每月自动跑一次并回写本目录。上游漂移时，重跑即得新版，表里 sha256 跟着变。
""")
        print(f"字典已生成：{a.out}｜{len(rows):,} 个邮编｜sha256 {h}")
        print(f"  跨县邮编 {multi_cty:,}｜多地名邮编 {multi_city:,}｜缺地名 {no_city:,}｜"
              f"缺 FIPS {no_fips:,}｜州数 {st_cnt}")
        json.dump({"zip": len(rows), "sha256_16": h, "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "multi_county": multi_cty, "no_city": no_city, "no_fips": no_fips},
                  open(os.path.join(os.path.dirname(a.out) or ".", "zip_city.stats.json"), "w",
                       encoding="utf-8"), ensure_ascii=False, indent=1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
