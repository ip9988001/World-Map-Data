# 标准化字典层（13 环节 · 第 12 步）

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

**来源**：GeoNames Postal Codes · US — <https://download.geonames.org/export/zip/US.zip>
许可 **CC BY 4.0**，署名 **GeoNames**（<https://www.geonames.org/>）。
生成时间：2026-10-12 10:42:39（UTC 由运行环境决定）
原始表行数：41,490｜聚合后邮编：41,488｜涉及州：53
`zip_city.tsv` sha256 前 16 位：`6a5da9b1b74c5e0e`

**已知局限（必须如实告知，不许假装权威）**
- 地名来自 GeoNames 的 **place name**，**不是 USPS 官方 preferred city name**；
  两者在部分小城镇写法不一致 → 因此补出的 city 一律带 `_src=zip_map` 标记。
- 一个邮编跨县是真实存在的（本表 0 个邮编跨县）→ 取出现最多的那个县。
- 同一邮编对应多个地名（2 个）→ 取出现最多的那个。
- 缺地名 0 个邮编、缺 FIPS 509 个邮编 → 落空即不补（绝不编）。
- 该表只用于**补齐空缺**，永远不会覆盖源里真实采到的值。

**刷新**：`python scripts/build_zip_dict.py`（联网重生成）；`.github/workflows/dict-refresh.yml`
每月自动跑一次并回写本目录。上游漂移时，重跑即得新版，表里 sha256 跟着变。
