"""AI 造规则机器人 —— 缺口源（RULE_GAP）自动造刀，人不再介入。

全自动链路（全程在 GitHub 机器上跑，本机无需开机）：
  collect（现有规则全败 → 落 out/triage/待造刀-<源>.json）
    → assemble（把待造刀清单写进 data 分支 progress/待造刀/）
    → brain 作业（本文件）：从 bronze 原始档取**真实样本** → 叫 LLM 写 JSON 规格
      → 验证闸门（同一套 mk_record 铁律 + 反造数 + examples 自检 + 提升幅度）
        过关：写 collectors/rules/specs/<源>.json → 推 main → 下一个窗口自动生效（重洗不回源）
        不过关：进 specs/rejected/（留证据，避免反复为同一条死路烧 token）
    → 仍无解只有两种情况：源根本不含地址（拉黑）/ 下载残缺（补采），都由分诊定案

为什么 LLM 只准写 JSON 不许写代码：见 collectors/rules/llm_spec.py 顶部。

用法：
  python -m collectors.rule_author --gap-dir progress/待造刀 --archive raw_archive \
      --out collectors/rules/specs --max-sources 8 --budget-seconds 2400
  python -m collectors.rule_author --source us/il/effingham --dry-run   # 只看提示词，不烧钱

环境变量：DEEPSEEK_API_KEY（或 BRAIN_API_KEY）；BRAIN_MODEL 默认 deepseek-flash。
退出码恒为 0：造规则失败只记档，不让整条采集线红叉（缺口源下一轮还会被带出来）。
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from itertools import islice

from .fetch_raw import archive_dir
from .flatten import iter_rows_from_archive
from .local_cascade import GATE_MIN_KEPT, GATE_MIN_RATIO, _is_contentful
from .normalize import norm_number
from .rules import RuleCtx
from .rules._common import NON_SITE_KEY, parse_addr_v2
from .rules.llm_spec import SpecRule, spec_name
from .triage import blacklist_add

API = "https://api.deepseek.com/chat/completions"
TABLE = "data/源总表-执行序.csv"
LEDGER = "data/造规则账本.csv"
PROMPT_ROWS = 30                 # 喂给 LLM 的样本行数
VALIDATE_ROWS = 200              # 验证闸门用的样本行数
NUM_RE = re.compile(r"^\d{1,6}[A-Za-z]?\d?$")

SYSTEM = ("你是美国地址数据（门牌点）解析规则工程师。"
          "你只输出一个严格 JSON 对象，不许有任何解释文字、不许用 markdown 代码围栏。")

USER = """为目标采集源写一套**声明式**解析规则（JSON），把原始字段变成门牌号 + 街名（可选 城市/邮编/州）。

【源】{sid}（州 {state}）  URL: {url}
【现状】现有规则全失手：{chain}
【可用模式】
  A. mode="columns"：号码与街名是分开的列（或可用模板把几列拼出来）
  B. mode="single" ：整条地址塞在一列里（支持**可选前置方位**，尾部市/州/ZIP 自动剥离）。
                     例："1514 N US Hwy 45 Mason IL 62443"、"123 N Main St, Springfield, CA 94110"
【如果这个源根本没有门牌号】不要硬凑，直接输出（花括号按原样写）：
  {infeasible_example}
  （只给街名没有号、那列是地类码/地块号/LABEL、地址全是业主邮寄地址 —— 都属于这种。
   我们会用证据复核，确认后把该源标为「不含地址」不再浪费算力。）
【输出 JSON（严格照这个骨架，用不上的键写 ""，不许省略键）】
{schema}
【硬规矩】
1) 只能引用下面样本里**真实出现过**的列名，不许发明列名。
2) 不许造数据：number 必须是样本行里真实存在的门牌号。
3) `examples` 至少 2 条，每条的 row 必须从样本里**原样复制**（一个字都别改）。
4) street 只放街名（可含方位/类型后缀），绝不许把 city/state/zip 混进 street。
5) 拿不准的字段写 "" —— 尤其是 city：错的市名会污染去重 key，宁缺毋滥。
6) `match.columns_any` 必须写，用最独特的那个列名（决定这套规则只对本源类出场）。
【样本（{n} 行，原样，值超长已截断）】
【本地证据】抽检 {ev_rows} 行里只有 {ev_hit} 行真含「门牌号 + 街名」形态（场地地址列 {ev_ratio:.0%}；
若把业主邮寄/账单列也算上是 {ev_any} 行 {ev_any_ratio:.0%} —— 邮寄地址**不是**门牌点，不要用）
{rows}
"""

SCHEMA = json.dumps({
    "name": "短英文名（小写+下划线，如 effingham_site_address）",
    "version": 1,
    "note": "一句话说明为什么这么写",
    "match": {"columns_any": ["列名"], "columns_all": [""], "columns_none": [""],
              "engine": "delimited / arcgis_rest / ..."},
    "mode": "columns 或 single",
    "columns": {"number": "", "street": "", "city": "", "zip": "", "state": ""},
    "number_template": "", "street_template": "", "full": "", "single": "",
    "unit_drop": True,
    "examples": [{"row": {"列名": "值"}, "expect": {"number": "123", "street": "MAIN ST"}}],
}, ensure_ascii=False, indent=1)
# 这个字面量必须在模板外拼好：直接写进 USER 会被 str.format 当成占位符
# （实战踩过：KeyError: '"infeasible"' 直接把 brain 作业打挂）
INFEASIBLE_EXAMPLE = '{"infeasible": true, "reason": "一句话", "evidence_column": "列名"}'


# ─────────────────────────────────────────────────────────── 取样本

def source_table() -> dict[str, dict]:
    if not os.path.exists(TABLE):
        return {}
    with open(TABLE, encoding="utf-8-sig") as fh:
        return {r["source_id"]: r for r in csv.DictReader(fh) if r.get("source_id")}


def sample_rows(sid: str, archive: str, limit: int = VALIDATE_ROWS) -> list[dict]:
    """从 bronze 原始档里取真实样本（永不回源、零网络）。"""
    d = archive_dir(archive, sid)
    if not os.path.isdir(d):
        return []
    out: list[dict] = []
    for r in islice(iter_rows_from_archive(d), limit):
        if "_read_error" in r:
            continue
        out.append({k: v for k, v in r.items() if k != "_file"})
    return out


def collect_gaps(gap_dir: str, only: str = "") -> list[dict]:
    gaps: list[dict] = []
    if not os.path.isdir(gap_dir):
        return gaps
    for fn in sorted(os.listdir(gap_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            g = json.load(open(os.path.join(gap_dir, fn), encoding="utf-8"))
        except Exception:
            continue
        if (g.get("kind") or "") not in ("RULE_GAP", "ENGINE_GAP"):
            continue
        sid = g.get("source_id") or ""
        if only and sid != only:
            continue
        gaps.append(g)
    return gaps


# ─────────────────────────────────────────────────────────── LLM

def _models() -> list[str]:
    env = os.environ.get("BRAIN_MODEL", "").strip()
    out = [env] if env else ["deepseek-flash"]
    for m in ("deepseek-chat",):
        if m not in out:
            out.append(m)
    return out


def call_llm(messages: list[dict], key: str, timeout: int = 300) -> tuple[str, str]:
    """返回 (内容, 用上的模型)。带 response_format，400 时自动降级重试。"""
    last = ""
    for model in _models():
        for use_json in (True, False):
            body = {"model": model, "messages": messages, "temperature": 0.2,
                    "max_tokens": 8000}
            if use_json:
                body["response_format"] = {"type": "json_object"}
            req = urllib.request.Request(
                API, data=json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                return (d["choices"][0]["message"].get("content") or ""), model
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
                if e.code in (400, 422):
                    continue        # 换一种请求姿势再试
                break
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                continue
    raise RuntimeError(f"LLM 调用全败：{last}")


def parse_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("响应里没有 JSON 对象")
    return json.loads(t[i:j + 1])


# ─────────────────────────────────────────────────────────── 验证闸门

def _row_text(row: dict) -> str:
    return " | ".join(str(v) for v in row.values() if isinstance(v, (str, int, float)))


def validate(spec: dict, rows: list[dict], sid: str, best_ratio: float = 0.0
             ) -> tuple[bool, dict, list[str]]:
    """不过关的规格绝不许进流水线（这一层是全部安全性的来源）。"""
    errs: list[str] = []
    keys = set()
    for r in rows:
        keys |= {str(k).lower() for k in r.keys()}
    cols = spec.get("columns") or {}
    for slot, col in cols.items():
        if col and str(col).lower() not in keys:
            errs.append(f"列不存在：{slot}={col}")
    for k in ("number_template", "street_template"):
        for c in re.findall(r"\{([^}]+)\}", str(spec.get(k) or "")):
            if c.strip().lower() not in keys:
                errs.append(f"{k} 引用了不存在的列：{c}")
    for k in ("single", "full"):
        if spec.get(k) and str(spec[k]).lower() not in keys:
            errs.append(f"{k} 列不存在：{spec[k]}")

    rule = SpecRule(dict(spec))
    st = (sid.split("/")[1].upper() if "/" in sid else "")
    exs = spec.get("examples") or []
    if len(exs) < 2:
        errs.append(f"examples 少于 2 条（{len(exs)}）")
    for i, ex in enumerate(exs, 1):
        row = {k: v for k, v in (ex.get("row") or {}).items() if not str(k).startswith("_")}
        rec, why = rule.clean(row, RuleCtx(sid, row, {}, st, {}))
        exp = ex.get("expect") or {}
        if not rec:
            errs.append(f"example{i} 洗不出（{why}）")
            continue
        if exp.get("number") and norm_number(exp["number"]) != rec["number"]:
            errs.append(f"example{i} 号不符：期望 {exp['number']} 实得 {rec['number']}")
        if exp.get("street"):
            want, got = str(exp["street"]).upper(), rec["street"].upper()
            if not (got.startswith(want[:10]) or want.startswith(got[:10])):
                errs.append(f"example{i} 街名不符：期望 {exp['street']} 实得 {rec['street']}")

    rows_n = contentful = kept = kept_contentful = fab = 0
    for r in rows:
        rows_n += 1
        c = _is_contentful(r)
        contentful += 1 if c else 0
        rec, _why = rule.clean(r, RuleCtx(sid, r, {}, st, {}))
        if not rec:
            continue
        kept += 1
        kept_contentful += 1 if c else 0
        # 反造数：号码必须在该行真实出现过
        if not re.search(r"\b" + re.escape(rec["number"]) + r"\b", _row_text(r)):
            fab += 1
    ratio = (kept_contentful / contentful) if contentful else (kept / rows_n if rows_n else 0.0)
    stats = {"rows": rows_n, "contentful": contentful, "kept": kept,
             "kept_contentful": kept_contentful, "ratio": round(ratio, 3),
             "fabricated": fab, "best_ratio": round(best_ratio, 3)}
    if fab:
        errs.append(f"凭空造数 {fab} 条（号码不在原始行里）")
    if kept < GATE_MIN_KEPT:
        errs.append(f"洗出太少：{kept} 条（阈 {GATE_MIN_KEPT}）")
    if ratio < GATE_MIN_RATIO:
        errs.append(f"合规率 {ratio:.0%} 低于阈 {GATE_MIN_RATIO:.0%}")
    if best_ratio >= 0.05 and ratio < best_ratio:
        errs.append(f"不如现有规则（{ratio:.0%} < {best_ratio:.0%}）")
    if not NUM_RE.match(str(spec_set_first_number(rule, rows) or "1")):
        errs.append("号码形态异常")
    return (not errs), stats, errs


def spec_set_first_number(rule: SpecRule, rows: list[dict]) -> str:
    for r in rows[:5]:
        rec, _ = rule.clean(r, RuleCtx("t", r, {}, "", {}))
        if rec:
            return str(rec["number"])
    return "1"


def best_existing_ratio(gap: dict) -> float:
    best = 0.0
    for c in gap.get("chain") or []:
        try:
            best = max(best, float(c.get("ratio") or 0.0))
        except Exception:
            pass
    return best


# ─────────────────────────────────────────────────────────── 落盘

def slug(sid: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", sid.lower()).strip("_")


def true_addr_ratio(rows: list[dict], site_only: bool = True) -> tuple[int, float]:
    """有多少行真的含「门牌号 + 街名」形态的地址（造规则 / 判「不含地址」的共同证据）。

    site_only=True 只看「场地地址」类列（排除业主邮寄/账单/法律描述列）——
    否则一个只有业主邮寄地址的源会被误判成「有门牌号」（us/wa/garfield 实测：
    任意列 9.8%，只看场地列 0%）。
    """
    hit = 0
    for r in rows:
        for k, v in r.items():
            if str(k).startswith("_") or not isinstance(v, str):
                continue
            if site_only and NON_SITE_KEY.search(str(k)):
                continue
            if parse_addr_v2(v):
                hit += 1
                break
    return hit, (hit / len(rows) if rows else 0.0)


def blacklisted_kinds(path: str = "data/源黑名单.csv") -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if row.get("source_id"):
                out[row["source_id"]] = row.get("kind", "")
    return out


def already_handled(spec_dir: str, sid: str, bl: dict[str, str] | None = None) -> str:
    """已经造过/否决过的源不再重复烧 token（除非 BRAIN_RETRY_REJECTED=1）。"""
    pfx = slug(sid) + "__"
    if (bl or {}).get(sid):
        return f"已在黑名单（{(bl or {}).get(sid)}）"
    if glob.glob(os.path.join(spec_dir, pfx + "*.json")):
        return "已有对应规则（specs/）"
    if not os.environ.get("BRAIN_RETRY_REJECTED") and \
            glob.glob(os.path.join(spec_dir, "rejected", pfx + "*.json")):
        return "已试过并否决（rejected/）"
    return ""


def write_spec(spec: dict, out_dir: str, sid: str, stats: dict, model: str) -> str:
    spec = {k: v for k, v in spec.items() if not str(k).startswith("_")}
    spec["name"] = f"{slug(sid)}__{re.sub(r'[^a-z0-9_]+', '_', str(spec.get('name','rule')).lower())}"
    spec["version"] = int(spec.get("version") or 1)
    spec["_provenance"] = {"source_id": sid, "generated_by": model,
                           "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "validated": stats}
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{spec['name']}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=1)
    return path


def write_rejected(spec: dict, out_dir: str, sid: str, stats: dict, errs: list[str],
                   model: str) -> str:
    d = os.path.join(out_dir, "rejected")
    os.makedirs(d, exist_ok=True)
    doc = {"_rejected": {"source_id": sid, "model": model, "errs": errs[:8],
                         "stats": stats, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
           "spec": spec}
    path = os.path.join(d, f"{slug(sid)}__{spec_name(spec)}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    return path


def append_ledger(sid: str, rule: str, action: str, stats: dict, model: str, note: str) -> None:
    cols = ["at", "source_id", "rule", "action", "rows", "contentful", "kept",
            "kept_contentful", "ratio", "model", "note"]
    new = not os.path.exists(LEDGER)
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "source_id": sid,
                    "rule": rule, "action": action, "rows": stats.get("rows", 0),
                    "contentful": stats.get("contentful", 0), "kept": stats.get("kept", 0),
                    "kept_contentful": stats.get("kept_contentful", 0),
                    "ratio": stats.get("ratio", 0), "model": model, "note": note[:160]})


def build_prompt(sid: str, row: dict, gap: dict, rows: list[dict]) -> str:
    st = (row or {}).get("state") or (sid.split("/")[1].upper() if "/" in sid else "")
    chain = "；".join(
        f"{c.get('rule')}: 洗出 {c.get('kept', 0)} 条 / 合规 {c.get('kept_contentful', 0)} / "
        f"共 {c.get('rows', 0)} 行 ({c.get('ratio', 0):.0%})"
        for c in (gap.get("chain") or [])[:6]) or "（无链信息）"
    slim: list[dict] = []
    for r in rows[:PROMPT_ROWS]:
        slim.append({k: (v[:120] if isinstance(v, str) else v) for k, v in list(r.items())[:60]})
    ev_hit, ev_ratio = true_addr_ratio(rows)
    ev_any, ev_any_ratio = true_addr_ratio(rows, site_only=False)
    return USER.format(sid=sid, state=st, url=(row or {}).get("url", ""), chain=chain,
                       schema=SCHEMA, n=len(slim), ev_rows=len(rows), ev_hit=ev_hit, ev_ratio=ev_ratio,
                       ev_any=ev_any, ev_any_ratio=ev_any_ratio,
                       infeasible_example=INFEASIBLE_EXAMPLE,
                       rows=json.dumps(slim, ensure_ascii=False, indent=0)[:40000])


# ─────────────────────────────────────────────────────────── 主流程

def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap-dir", default="progress/待造刀", help="待造刀清单目录（data 分支取回）")
    ap.add_argument("--source", default="", help="只处理某一个源")
    ap.add_argument("--archive", default="raw_archive")
    ap.add_argument("--out", default="collectors/rules/specs")
    ap.add_argument("--max-sources", type=int, default=8)
    ap.add_argument("--budget-seconds", type=int, default=2400)
    ap.add_argument("--dry-run", action="store_true", help="只打印提示词，不调 LLM")
    a = ap.parse_args(argv)

    key = (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("BRAIN_API_KEY") or "").strip()
    if not key and not a.dry_run:
        print("[brain] 未配置 DEEPSEEK_API_KEY → 本轮不造规则（缺口源留待配置后自动处理）")
        return

    table = source_table()
    bl = blacklisted_kinds()
    gaps = collect_gaps(a.gap_dir, a.source)
    if a.source and not gaps:
        gaps = [{"source_id": a.source, "kind": "RULE_GAP", "chain": []}]
    if not gaps:
        print(f"[brain] 没有待造刀清单（{a.gap_dir}）→ 无需造规则")
        return
    print(f"[brain] 待处理缺口源 {len(gaps)} 个；本轮上限 {a.max_sources}")

    t0 = time.time()
    done = ok_n = 0
    lines: list[str] = []
    for gap in gaps:
        if done >= a.max_sources or time.time() - t0 > a.budget_seconds:
            print("[brain] 预算用尽，余下缺口下一窗口继续")
            break
        sid = gap["source_id"]
        why = already_handled(a.out, sid, bl)
        if why:
            print(f"[brain] {sid} 跳过：{why}")
            continue
        row = table.get(sid) or {}
        rows = sample_rows(sid, a.archive)
        if len(rows) < 10:
            print(f"[brain] {sid} bronze 里只有 {len(rows)} 行 → 跳过（等补采）")
            append_ledger(sid, "", "SKIP_NO_BRONZE", {"rows": len(rows)}, "", "原始档样本不足")
            continue
        done += 1
        ev_hit, ev_ratio = true_addr_ratio(rows)
        try:
            prompt = build_prompt(sid, row, gap, rows)
        except Exception as e:
            # 单个源出意外不许把整个作业打挂（实战：模板占位符 KeyError → brain 直接 exit 1）
            print(f"[brain] {sid} 提示词构造失败（跳过，不影响其它源）: {type(e).__name__}: {e}")
            append_ledger(sid, "", "PROMPT_FAIL", {}, "", f"{type(e).__name__}: {e}")
            continue
        prompt = build_prompt(sid, row, gap, rows)
        if a.dry_run:
            print("=" * 20, sid, "=" * 20)
            print(prompt)
            continue
        try:
            content, model = call_llm([{"role": "system", "content": SYSTEM},
                                       {"role": "user", "content": prompt}], key)
            spec = parse_json(content)
        except Exception as e:
            print(f"[brain] {sid} LLM 失败：{type(e).__name__}: {e}")
            append_ledger(sid, "", "LLM_FAIL", {}, "", f"{type(e).__name__}: {e}")
            continue
        best = best_existing_ratio(gap)
        if spec.get("infeasible"):
            # LLM 自认「这源不含门牌号」——必须过**证据闸门**才准拉黑（否则一律驳回）
            ev = (f"样本 {len(rows)} 行，真门牌号形态 {ev_hit} 行（{ev_ratio:.1%}）"
                  f"｜LLM 自述：{str(spec.get('reason'))[:200]}"
                  f"｜证据列：{spec.get('evidence_column', '')}")
            if ev_ratio < 0.03:
                blacklist_add(sid, "NOT_ADDRESS", "本地抽查：源不含「门牌号+街名」形态的地址", ev)
                msg = f"⊘ {sid} 判为「源不含地址」（证据支持：{ev_hit}/{len(rows)} 行）→ 已拉黑 NOT_ADDRESS"
                print(f"[brain] {msg}")
                lines.append(msg)
                append_ledger(sid, "", "NOT_ADDRESS", {"rows": len(rows)}, model, ev[:160])
                bl[sid] = "NOT_ADDRESS"
            else:
                stats = {"rows": len(rows)}
                p = write_rejected(spec, a.out, sid, stats, [f"infeasible 被证据推翻：{ev_ratio:.0%} 行真含门牌号"], model)
                msg = f"✗ {sid} LLM 说「无门牌号」被证据推翻（{ev_hit}/{len(rows)} 行含真门牌号）→ 驳回"
                print(f"[brain] {msg}\n        → {p}")
                lines.append(msg)
                append_ledger(sid, "", "REJECTED_INFEASIBLE", stats, model, ev[:160])
            continue
        try:
            okv, stats, errs = validate(spec, rows, sid, best)
        except Exception as e:
            print(f"[brain] {sid} 验证闸门抛异常（按不通过处理）: {type(e).__name__}: {e}")
            okv, stats, errs = False, {"rows": len(rows)}, [f"validate 异常: {type(e).__name__}: {e}"]
        if okv:
            p = write_spec(spec, a.out, sid, stats, model)
            ok_n += 1
            msg = (f"✓ {sid} 造出规则 {spec_name(spec)}@{spec.get('version', 1)}｜"
                   f"合规 {stats['kept_contentful']}/{stats['contentful']} = {stats['ratio']:.0%}")
            print(f"[brain] {msg}\n        → {p}")
            lines.append(msg)
            append_ledger(sid, spec_name(spec), "ACCEPTED", stats, model,
                          str(spec.get("note") or "")[:160])
        else:
            p = write_rejected(spec, a.out, sid, stats, errs, model)
            msg = f"✗ {sid} 规则不过闸：{errs[0] if errs else '未知'}"
            print(f"[brain] {msg}\n        → {p}")
            lines.append(msg)
            append_ledger(sid, spec_name(spec), "REJECTED", stats, model, "｜".join(errs)[:160])

    os.makedirs("out/brain", exist_ok=True)
    with open("out/brain/报告.md", "w", encoding="utf-8") as fh:
        fh.write(f"# AI 造规则报告 {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        fh.write(f"- 缺口源 {len(gaps)} 个，本轮处理 {done} 个，出新规则 {ok_n} 套\n")
        for l in lines:
            fh.write(f"- {l}\n")
    print(f"[brain] 本轮：处理 {done}，出新规则 {ok_n}，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
