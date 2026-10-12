"""构建发布包 v2 —— 13 环节设计 · 第 11/12/13 步（合并去重 / 字典补齐 / 分级发布与出厂自检）。

v2 契约（与 v1 的差别）：
  1. **不再吃旧数据集**：v1 默认把 `out/final3`（上一版稳定视图）当合并底稿，
     导致「旧垃圾 + 新数据」混着走。v2 只聚合 `parsed/**`（全部规则产物），
     `--legacy` 保留但默认关闭，只作应急回放。
  2. **全局排序 + 按 key 的州分片**：key 形如 `ST|city|zip|street|number`，
     先全局按 key 排序，再按 key 的州边读边落分片。这样
        · 同一个 key 不可能同时出现在两个州分片里（跨州记录不再产生跨分片重复）
        · 合并去重是**全局**的，不是各州各去各的
        · 每个分片内部依然按 key 有序，下游可以流式处理
  3. **真合并（第 11 步）**：同 key 多来源并集进 `sources`（v1 只留优先级最高的一条，
     实测 6303 万条里 sources 恒为 1）；字段冲突＝优先级新者优先 × 非空优先。
  4. **字典补齐（第 12 步）**：`--zip-dict`（ZIP↔city↔state↔county↔fips）。
     补出来的值一律标 `city_src=zip_map` / `zip_src=zip_map` / `county_src=zip_map`，
     **绝不覆盖源里真实采到的值**；`county` 先由源总表兜底（source_meta）。
  5. **交付分级（第 13 步）**：A（五要素齐）→ `out/release/site/<ST>.ndjson.gz`；
     B（缺 city/zip）／C（补不齐）留在全量分片并带 `_grade` 标注。**无 `_grade` 即为 A**。
  6. **出厂硬门 + 写完回读**：每个分片写完重新读一遍统计行数/唯一 key/空行/字段完整率；
     空行率必须 0、行数必须 == 唯一 key 数、不许有无 sources/无 key/坏 JSON；
     有任一不过 → 不写清单、退出 2。manifest 的数字**全部由回读得到**，禁用写前计数。
  7. **溯源引用表**：`sources.json` + `sources-<ST>.json`（记录只留短 id，元信息查表）；
     不在源总表的孤儿来源直接挡发布；license 空必须显式标 `unknown`。
  8. **交付 schema 是白名单**：只有 number/street/city/state/zip/county/county_fips/key/
     sources/`*_src`/`_grade` 会进交付物，内部字段（`_rule` 等）一律不出厂。

用法：
  python scripts/build_release.py --dry
  python scripts/build_release.py --zip-dict dict/zip_city.tsv
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

NEW_DIR = "parsed"
OUT_DIR = "out/release"
MANIFEST = "data/release-manifest.json"
SOURCE_TABLE = "data/源总表.csv"
STATE_FILE = "progress/state.json"
ZIP_DICT = "dict/zip_city.tsv"
CORE_FIELDS = ("number", "street", "city", "state", "zip")
NEW_PRIO = 2          # 新采集（规则产物）
LEGACY_PRIO = 1       # 应急回放的老分片
STRICT_A = False      # A 级是否要求零推断字段（--strict-a 打开）


# ────────────────────────── 小工具 ──────────────────────────

def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def key_state(key: str) -> str:
    """key 的第一段就是州码；不合法则归 ZZ（可见地暴露出来，而不是悄悄丢）。"""
    st = (key or "").split("|", 1)[0].strip().upper()
    return st if len(st) == 2 and st.isalpha() else "ZZ"


# ────────────────────────── 输入采集 ──────────────────────────

def collect_new(root: str = NEW_DIR) -> dict[str, list[str]]:
    """{州目录: [规则产物 ndjson 路径]}（州目录只用于遍历，落分片看 key）"""
    out: dict[str, list[str]] = {}
    for path in glob.glob(os.path.join(root, "**", "*.ndjson"), recursive=True):
        rel = os.path.relpath(path, root).replace("\\", "/")
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] != "us":
            continue
        out.setdefault(parts[1].upper(), []).append(path)
    return out


def load_source_meta(path: str) -> dict[str, dict]:
    """源总表 → 溯源元信息表（记录里只留 source_id，元信息查这张表）。"""
    meta: dict[str, dict] = {}
    if not os.path.exists(path):
        print(f"⚠ 找不到源总表 {path}，溯源表将为空")
        return meta
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            sid = (row.get("source_id") or "").strip()
            if not sid:
                continue
            lic = (row.get("license") or "").strip()
            geo = (row.get("geoid") or "").strip()
            meta[sid] = {
                "attribution": (row.get("attribution") or "").strip(),
                "url": (row.get("url") or "").strip(),
                "website": (row.get("website") or "").strip(),
                "license": lic,
                "license_status": "ok" if lic else "unknown",   # 空许可不许假装有
                "state": (row.get("state") or "").strip(),
                "county": (row.get("county") or "").strip(),
                "geoid": geo,
                # P1-3：全州性源的 geoid 存的是**州** FIPS（实测 us/ak/statewide geoid="02"），
                # 直接当县 FIPS 写进交付物 → 5 位县码字段里出现 2 位州码，下游按 FIPS 关联必错
                "county_fips": geo if len(geo) == 5 else "",
                "scope": (row.get("scope") or "").strip(),
                "layer": (row.get("layer") or "").strip(),
                "protocol": (row.get("protocol") or "").strip(),
            }
    return meta


def load_state_meta(path: str) -> dict[str, str]:
    """state.json → {source_id: 最后采集时间}。"""
    ts: dict[str, str] = {}
    if not os.path.exists(path):
        return ts
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return ts
    for sid, v in d.items():
        if not isinstance(v, dict):
            continue
        val = (v.get("updated") or v.get("fetched_at") or v.get("ts")
               or v.get("last_updated") or v.get("collect_time") or "")
        ts[sid] = str(val)
    return ts


def load_zip_dict(path: str):
    """ZIP 字典（第 12 步）→ (zip→{city,state,county,fips}, (state,city)→zip)。

    文件不存在时返回空表：此时不做任何推断，全部按「真实采集值」处理。
    """
    z2c: dict[str, dict] = {}
    c2z: dict[tuple[str, str], str] = {}
    if not path or not os.path.exists(path):
        return z2c, c2z
    with open(path, encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = "\t" if sample.count("\t") >= sample.count(",") else ","
        rd = csv.DictReader(fh, delimiter=delim)
        low = {(k or "").strip().lower(): k for k in (rd.fieldnames or [])}

        def col(*names):
            for n in names:
                if n in low:
                    return low[n]
            return None

        c_zip, c_city = col("zip", "zip5", "postal"), col("city", "city_name", "place")
        c_st, c_co = col("state", "state_abbr", "st"), col("county", "county_name")
        c_fips = col("county_fips", "fips", "geoid")
        for row in rd:
            z = re.sub(r"\D", "", (row.get(c_zip) or ""))[:5] if c_zip else ""
            if len(z) != 5:
                continue
            city = (row.get(c_city) or "").strip().upper() if c_city else ""   # 与 norm_city 同口径（全大写）
            st = (row.get(c_st) or "").strip().upper() if c_st else ""
            co = (row.get(c_co) or "").strip() if c_co else ""
            fips = (row.get(c_fips) or "").strip() if c_fips else ""
            if z not in z2c or (city and not z2c[z].get("city")):
                z2c[z] = {"city": city, "state": st, "county": co, "county_fips": fips}
            if st and city:
                c2z.setdefault((st, city.upper()), z)
    return z2c, c2z


# ────────────────────────── 合并 + 分级（第 11/12 步） ──────────────────────────

def merge_group(key: str, recs: list[tuple[int, dict]], meta: dict[str, dict]) -> dict:
    """同 key 的多来源记录 → 一条合并记录（字段取非空、来源并集、冲突按优先级）。"""
    ordered = sorted(recs, key=lambda x: -x[0])       # 高优先级（新数据）在前
    rec: dict = {"key": key}
    for f in CORE_FIELDS:
        for _prio, r in ordered:
            v = r.get(f) or ""
            v = v.strip() if isinstance(v, str) else str(v)
            if v:
                rec[f] = v
                m = r.get(f + "_src")      # 字段级溯源跟着值一起走（derived/scope_default…）
                if m:
                    rec[f + "_src"] = m
                break
        rec.setdefault(f, "")
    # 来源并集（第 11 步的核心修复：不许只留第一条）
    srcs: list[str] = []
    for _prio, r in ordered:
        s = r.get("sources") or r.get("source") or []
        if isinstance(s, str):
            s = [s]
        for x in s:
            if x and x not in srcs:
                srcs.append(x)
    rec["sources"] = sorted(srcs)
    # county：源总表兜底（决策：county 必须留在发布 schema 里）
    # ⚠ 只认**同州**的源元信息：跨源合并后 sources 是排序过的，第一个可能属于别州，
    #    拿它的县名当本记录的县会造出「县名与 county_fips 对不上」的错值。
    st_now = rec.get("state") or ""
    # ① 采集值优先（P1-3）：产物本来就带 county/county_fips 的，绝不被推断值顶替
    for _prio, r in ordered:
        if not rec.get("county") and (r.get("county") or "").strip():
            rec["county"] = str(r["county"]).strip()
            if r.get("county_src"):
                rec["county_src"] = r["county_src"]
        if not rec.get("county_fips") and (r.get("county_fips") or "").strip():
            rec["county_fips"] = str(r["county_fips"]).strip()
    # ② 源总表兜底：只在本记录还没有 county 时才填（否则会覆盖采集值）。
    #    注：故意让「源自己的 county」优先于 ZIP 字典 —— 县级源是它自己那片地的官方数据，
    #    比 ZIP 反查更权威；全州性源 county 为空，自然会落到字典（z2c）去补。
    if not rec.get("county"):
        for sid in rec["sources"]:
            m = meta.get(sid) or {}
            if st_now and (m.get("state") or "").upper() not in ("", st_now):
                continue
            if m.get("county"):
                rec["county"] = m["county"]
                rec["county_src"] = "source_meta"
                if m.get("county_fips") and not rec.get("county_fips"):
                    rec["county_fips"] = m["county_fips"]
                break
    if not rec.get("county_fips"):
        for sid in rec["sources"]:
            m = meta.get(sid) or {}
            if st_now and (m.get("state") or "").upper() not in ("", st_now):
                continue
            if m.get("county_fips"):
                rec["county_fips"] = m["county_fips"]
                break
    return rec


def fill_and_grade(rec: dict, z2c: dict, c2z: dict) -> str:
    """字典补齐（第 12 步）+ 分级（第 13 步）。就地改 rec，返回 "A"/"B"/"C"。

    A：number+street+city+state+zip 五要素齐
    B：缺 city 或 zip，但 number+street+state 齐（本次补不齐，留待下一版字典）
    C：连 street/number/state 都不齐 → 补不齐
    **字典只填空缺，绝不覆盖源里采到的值。**
    """
    st = rec.get("state") or ""
    if not rec.get("zip") and rec.get("city") and st:
        z = c2z.get((st, rec["city"].upper()))
        if z:
            rec["zip"] = z
            rec["zip_src"] = "zip_map"
    if rec.get("zip"):
        d = z2c.get(rec["zip"])
        if d:
            if not rec.get("city") and d.get("city") and (not d.get("state") or d["state"] == st):
                rec["city"] = d["city"]
                rec["city_src"] = "zip_map"
            if not rec.get("county") and d.get("county"):
                rec["county"] = d["county"]
                rec["county_src"] = "zip_map"
            if not rec.get("county_fips") and d.get("county_fips"):
                rec["county_fips"] = d["county_fips"]
    inferred = sum(1 for f in ("city", "zip") if rec.get(f + "_src") == "zip_map")
    if all(rec.get(f) for f in CORE_FIELDS) and (inferred == 0 if STRICT_A else inferred <= 1):
        return "A"
    if rec.get("number") and rec.get("street") and rec.get("state"):
        return "B"
    return "C"


# ────────────────────────── 全局排序 + 分片（第 11/13 步） ──────────────────────────

def canon_key(rec: dict, z2c: dict, c2z: dict) -> str:
    """入全局表**之前**先用字典把 city/zip 补上，再用补齐后的值重算 key。

    ⚠ 自审发现的一条（外部审查也没抓到）：key 是用「原始 city/zip」拼的，而字典补齐发生在
    分组**之后**。于是同一条街同一个门牌会有两个 key —— A 源带 city（`CA|BISHOP|93514|OAK ST|9`）、
    B 源不带（`CA||93514|OAK ST|9`）；补完 city 后两条五要素都齐、都判 A，
    **site/ 站用子集里就出现同址两条记录**，而现有所有检查（唯一 key、跨州重复）都看不见它。
    在入口就补齐 + 重算 key，这两条会合并成一条（sources 并集）。
    只动 city/zip（county 在合并阶段按同州源元信息处理）。
    """
    st = str(rec.get("state") or "").strip().upper()
    # 城市统一大写（与 norm_city 同口径）：字典地名与源里地名的大小写不同会让**同一个门牌**
    # 落成两个 key，反而把「同址重复」又变出来。分区大小写 = 去重失效，不能容忍。
    city0 = str(rec.get("city") or "").strip()
    if city0 and city0 != city0.upper():
        rec["city"] = city0.upper()
    if not str(rec.get("zip") or "").strip() and city0 and st:
        z = c2z.get((st, city0.upper()))
        if z:
            rec["zip"] = z
            rec["zip_src"] = "zip_map"
    z = str(rec.get("zip") or "").strip()
    if z and not str(rec.get("city") or "").strip():
        d = z2c.get(z)
        if d and d.get("city") and (not d.get("state") or d["state"] == st):
            rec["city"] = str(d["city"]).upper()
            rec["city_src"] = "zip_map"
    return (f"{st}|{rec.get('city') or ''}|{rec.get('zip') or ''}"
            f"|{rec.get('street') or ''}|{rec.get('number') or ''}")


def build_global_tsv(new: dict[str, list[str]], legacy_shards: dict[str, str],
                     tmp_dir: str, z2c: dict, c2z: dict) -> tuple[str, dict]:
    """所有记录写成一张全局表（key / prio / json），供全局排序。

    key 以「州|城市|邮编|街|号」开头 → 全局按 key 排序后，同一个州的记录天然连成一段，
    于是可以边读边落分片，既做到全局唯一，又不需要把 key 集合塞进内存。
    """
    raw = os.path.join(tmp_dir, "all.tsv")
    st_cnt = {"in": 0, "no_key": 0, "bad_json": 0}

    def emit(fh, rec: dict, prio: int) -> None:
        if not (rec.get("number") and rec.get("street") and rec.get("state")):
            st_cnt["no_key"] += 1        # 三要素都不齐 → 无法作为地址去重，宁可挡下来
            return
        k = canon_key(rec, z2c, c2z)
        fh.write(f"{k}\t{prio}\t"
                 f"{json.dumps(rec, ensure_ascii=False, separators=(',', ':'))}\n")
        st_cnt["in"] += 1

    with open(raw, "w", encoding="utf-8") as fh:
        for _st, files in sorted(new.items()):
            for p in sorted(files):          # 文件名也排序：输入顺序确定，输出才可复现
                with open(p, encoding="utf-8") as src:
                    for line in src:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            emit(fh, json.loads(line), NEW_PRIO)
                        except Exception:
                            st_cnt["bad_json"] += 1
        for _st, p in sorted(legacy_shards.items()):
            if not os.path.exists(p):
                continue
            with gzip.open(p, "rt", encoding="utf-8") as src:
                for line in src:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        emit(fh, json.loads(line), LEGACY_PRIO)
                    except Exception:
                        st_cnt["bad_json"] += 1
    return raw, st_cnt


def write_shards(raw: str, out_dir: str, site_dir: str, meta: dict,
                 z2c: dict, c2z: dict) -> dict:
    """全局排序 → 按 key 的州边读边落分片（全量 + 站用子集）。返回每州统计。"""
    srt = raw + ".sorted"
    env = dict(os.environ, LC_ALL="C")     # 按字节序排：与 Python 比较一致，可复现
    # `-s`（stable）必不可少：默认的 sort **不保证稳定**，同一个 key 下多条记录的相对顺序
    # 会变 → 合并时「取第一条非空值」跟着变 → 同输入两次构建的 sha256 不一致（实测踩到）。
    # 加上 -s 之后：输入顺序（目录名排序 + 文件名排序）决定一切 → 完全可复现。
    subprocess.run(["sort", "-s", "-t\t", "-k1,1", "-S", "192M", "-T", os.path.dirname(raw),
                    "-o", srt, raw], check=True, env=env)

    stats: dict[str, dict] = {}
    writers: dict[str, list] = {}
    cur_key: str | None = None
    cur_state = ""
    group: list[tuple[int, dict]] = []

    def flush():
        nonlocal cur_key, cur_state, group
        if cur_key is None:
            return
        st = cur_state
        s = stats.setdefault(st, {"keys": 0, "input_records": 0, "merged_away": 0,
                                  "grades": {"A": 0, "B": 0, "C": 0}, "multi_source": 0,
                                  "inferred": {"city": 0, "zip": 0}})
        if st not in writers:
            writers[st] = [
                gzip.open(os.path.join(out_dir, f"{st}.ndjson.gz"), "wt", encoding="utf-8"),
                gzip.open(os.path.join(site_dir, f"{st}.ndjson.gz"), "wt", encoding="utf-8"),
            ]
        full, site = writers[st]
        rec = merge_group(cur_key, group, meta)
        g = fill_and_grade(rec, z2c, c2z)
        s["keys"] += 1
        s["input_records"] += len(group)
        s["merged_away"] += len(group) - 1
        s["grades"][g] += 1
        for f in ("city", "zip"):       # 可审计事实：这个州有多少条是字典推出来的（口径不是嘴上说的）
            if rec.get(f + "_src") == "zip_map":
                s["inferred"][f] += 1
        if len(rec["sources"]) > 1:
            s["multi_source"] += 1
        if g != "A":
            rec["_grade"] = g          # 非 A 级带标注；没有该字段即 A 级
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        full.write(line)
        if g == "A":
            site.write(line)
        cur_key, cur_state, group = None, "", []

    with open(srt, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line:
                continue
            key, prio, payload = line.split("\t", 2)
            if key != cur_key:
                flush()
                cur_key, cur_state, group = key, key_state(key), []
            group.append((int(prio), json.loads(payload)))
        flush()
    for pair in writers.values():
        for w in pair:
            w.close()
    os.remove(raw)
    os.remove(srt)
    return stats


def audit_shard(path: str) -> dict:
    """**写完回读**：行数 / 唯一 key / 空行 / 字段完整率，全部从文件里重新数。

    分片按 key 有序写出，所以唯一 key 只需靠「相邻 key 变化」流式统计，不吃内存。
    """
    st = {"rows": 0, "unique": 0, "blank": 0, "no_source": 0, "bad_json": 0,
          "missing": {f: 0 for f in CORE_FIELDS}, "multi_source": 0}
    prev = None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                st["blank"] += 1
                continue
            st["rows"] += 1
            try:
                rec = json.loads(line)
            except Exception:
                st["bad_json"] += 1
                continue
            k = rec.get("key") or f"#nokey-{st['rows']}"
            if k != prev:
                st["unique"] += 1
                prev = k
            for f in CORE_FIELDS:
                if not rec.get(f):
                    st["missing"][f] += 1
            srcs = rec.get("sources") or []
            if not srcs:
                st["no_source"] += 1
            elif len(srcs) > 1:
                st["multi_source"] += 1
    return st


def build_sources_tables(meta: dict, used_by_state: dict[str, set], out_dir: str,
                         state_meta: dict) -> dict:
    """溯源引用表：记录里只留短 id，元信息查表（不内联，省体积）。"""
    def entry(sid):
        m = dict(meta.get(sid) or {})
        m["source_id"] = sid
        m["fetched_at"] = state_meta.get(sid) or "unknown"   # 与 license 同口径：没有就如实写 unknown
        return m

    all_used = set().union(*used_by_state.values()) if used_by_state else set()
    glob_tbl = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "source_id → 官方机构 / 原始接口 / 许可 / 采集时间；记录里只存短 id",
                "count": len(all_used),
                "sources": {sid: entry(sid) for sid in sorted(all_used)}}
    gp = os.path.join(out_dir, "sources.json")
    with open(gp, "w", encoding="utf-8") as fh:
        json.dump(glob_tbl, fh, ensure_ascii=False, separators=(",", ":"))
    per_state: dict[str, dict] = {}
    for st, ids in sorted(used_by_state.items()):
        p = os.path.join(out_dir, f"sources-{st}.json")
        tbl = {"state": st, "count": len(ids),
               "sources": {sid: entry(sid) for sid in sorted(ids)}}
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(tbl, fh, ensure_ascii=False, separators=(",", ":"))
        per_state[st] = {"count": len(ids), "sha256": sha256(p)}
    return {"sources_json": {"count": len(all_used), "sha256": sha256(gp)},
            "per_state": per_state, "orphans": sorted(s for s in all_used if s not in meta)}


# ────────────────────────── 主流程 ──────────────────────────

def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parsed", default=NEW_DIR)
    ap.add_argument("--legacy", default="", help="应急回放：老分片目录（默认空＝不吃旧数据）")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--sources-csv", default=SOURCE_TABLE)
    ap.add_argument("--state-file", default=STATE_FILE)
    ap.add_argument("--zip-dict", default=ZIP_DICT)
    ap.add_argument("--strict-a", action="store_true",
                    help="A 级要求零推断字段（默认允许最多 1 个 zip_map）")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-merged", action="store_true")
    a = ap.parse_args(argv)
    global STRICT_A
    STRICT_A = bool(a.strict_a)

    legacy_shards = {os.path.basename(p).split(".")[0]: p
                     for p in glob.glob(os.path.join(a.legacy, "*.ndjson.gz"))} if a.legacy else {}
    new = collect_new(a.parsed)
    meta = load_source_meta(a.sources_csv)
    state_meta = load_state_meta(a.state_file)
    z2c, c2z = load_zip_dict(a.zip_dict)
    print(f"规则产物目录数 {len(new)}｜源总表 {len(meta)} 个｜ZIP 字典 {len(z2c):,} 条"
          f"{'（未加载：不做任何推断）' if not z2c else ''}"
          + (f"｜[应急]老分片 {a.legacy}" if a.legacy else ""), flush=True)

    if not new:
        print("✗ 没有任何规则产物（parsed/ 为空）—— 不发布，也不许降级成「发个空包」")
        sys.exit(2)

    if a.dry:
        tot = 0
        for st, files in sorted(new.items()):
            n = sum(sum(1 for _ in open(p, encoding="utf-8")) for p in files)
            tot += n
            print(f"  {st}: 规则产物 {n:,} 条", flush=True)
        print(f"[dry] 合计 {tot:,} 条输入（未做全局排序/合并）")
        return

    tmp_dir = os.path.join(a.out, "_tmp")
    site_dir = os.path.join(a.out, "site")
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(site_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(os.path.dirname(a.manifest) or ".", exist_ok=True)

    manifest = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "pipeline": "v2/13-环节", "parsed_source": a.parsed,
                "legacy_used": bool(legacy_shards),
                "zip_dict": os.path.basename(a.zip_dict) if z2c else None,
                "states": {}, "rows": 0, "site_rows": 0,
                "dedup_removed": 0, "multi_source_records": 0,
                "a_policy": ("A=五要素齐且推断字段为 0（--strict-a）" if STRICT_A
                             else "A=五要素齐且推断字段≤1（默认；字典只补空缺、标记为 zip_map）"),
                "inferred_totals": {"city": 0, "zip": 0},
                "grades": {"A": 0, "B": 0, "C": 0}, "sources": {}}
    hard: list[str] = []

    raw, in_cnt = build_global_tsv(new, legacy_shards, tmp_dir, z2c, c2z)
    if in_cnt["bad_json"]:
        hard.append(f"[输入] 坏 JSON {in_cnt['bad_json']:,} 条")
    if in_cnt["no_key"]:
        hard.append(f"[输入] 无 key 记录 {in_cnt['no_key']:,} 条（无法去重，已剔除）")
    print(f"输入 {in_cnt['in']:,} 条 → 全局按 key 排序…", flush=True)
    stats = write_shards(raw, a.out, site_dir, meta, z2c, c2z)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    used_by_state: dict[str, set] = {}
    for st in sorted(stats):
        out_path = os.path.join(a.out, f"{st}.ndjson.gz")
        stt = stats[st]
        chk = audit_shard(out_path)                 # ← 写完回读，清单数字只看这里
        if chk["blank"]:
            hard.append(f"[出厂]{st} 空行 {chk['blank']:,} 条（必须为 0）")
        if chk["rows"] != chk["unique"]:
            hard.append(f"[出厂]{st} 行数 {chk['rows']:,} != 唯一 key {chk['unique']:,}")
        if chk["bad_json"]:
            hard.append(f"[出厂]{st} 坏 JSON {chk['bad_json']:,} 条")
        if chk["no_source"]:
            hard.append(f"[出厂]{st} 无 sources 记录 {chk['no_source']:,} 条（溯源断裂）")
        if chk["rows"] == 0:
            hard.append(f"[出厂]{st} 分片为空")
        manifest["states"][st] = {
            "rows": chk["rows"], "unique_keys": chk["unique"], "blank_lines": chk["blank"],
            "bytes": os.path.getsize(out_path), "sha256": sha256(out_path),
            "grades": stt["grades"], "multi_source": stt["multi_source"],
            "inferred": stt["inferred"],
            "field_missing": chk["missing"],
        }
        for f in ("city", "zip"):
            manifest["inferred_totals"][f] += stt["inferred"][f]
        manifest["rows"] += chk["rows"]
        manifest["site_rows"] += stt["grades"]["A"]
        manifest["dedup_removed"] += stt["merged_away"]
        manifest["multi_source_records"] += stt["multi_source"]
        for g, n in stt["grades"].items():
            manifest["grades"][g] += n
        used_by_state[st] = set()
        with gzip.open(out_path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    for s in (json.loads(line).get("sources") or []):
                        used_by_state[st].add(s)
        print(f"  {st}: {chk['rows']:,} 行（唯一 key {chk['unique']:,}｜空行 {chk['blank']}）"
              f"  A {stt['grades']['A']:,} / B {stt['grades']['B']:,} / C {stt['grades']['C']:,}"
              f"｜多来源 {stt['multi_source']:,}｜并入去重 {stt['merged_away']:,}", flush=True)

    # 溯源引用表（含孤儿检测：不在源总表里的 source_id）
    manifest["sources"] = build_sources_tables(meta, used_by_state, a.out, state_meta)
    orph = manifest["sources"]["orphans"]
    if orph:
        hard.append(f"[溯源] 孤儿来源 {len(orph)} 个（不在源总表）：{orph[:5]}")

    if hard:
        print("\n✗ 出厂硬门不放行（不写清单、不发布）：")
        for e in hard[:20]:
            print("   " + e)
        sys.exit(2)

    # 合并版放在硬门**之后**：不过门就不应该浪费磁盘和时间去拼全量单文件（P1-5 附带）
    if not a.no_merged:
        merged = os.path.join(a.out, "merged.clean.ndjson.gz")
        with gzip.open(merged, "wt", encoding="utf-8") as out:
            for st in sorted(stats):
                p = os.path.join(a.out, f"{st}.ndjson.gz")
                if os.path.exists(p):
                    with gzip.open(p, "rt", encoding="utf-8") as src:
                        shutil.copyfileobj(src, out)
        manifest["merged"] = {"bytes": os.path.getsize(merged), "sha256": sha256(merged),
                             "rows": manifest["rows"]}
        print(f"合并版 merged.clean.ndjson.gz：{os.path.getsize(merged) / 1048576:.1f}MB")

    with open(a.manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1, sort_keys=True)
    inf = manifest["inferred_totals"]
    print(f"发布清单：{a.manifest}｜合计 {manifest['rows']:,} 行"
          f"｜站用子集(A) {manifest['site_rows']:,} 行"
          f"（{len(stats)} 个州分片）"
          f"｜多来源记录 {manifest['multi_source_records']:,}"
          f"｜推断字段 city {inf['city']:,} / zip {inf['zip']:,}")


if __name__ == "__main__":
    main()
