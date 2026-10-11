#!/usr/bin/env python3
"""发布日报：工厂每轮结束后，在 data 分支 progress/发布日报.md 追加一行。

为什么需要它：工厂自己会挡坏数据（verify_release 闸门 + 跨州分片重复抽样），
但「成功发布」是静默的 —— 没有 Issue、没有邮件，没人知道今天发了多少、有没有缩水。
这张表就是那个落点：每轮工厂结束都写一行（成功也写，只是不吵人）。

用法（notify 作业内）：
    RESULTS='{"plan":{"result":"success"},...}' RUN_ID=123 EVENT=schedule \
    python3 scripts/daily_note.py --ledger /tmp/ledger.csv \
        --manifest data/release-manifest.json --note /tmp/note.md --out /tmp/note_new.md

输入全部可选：缺台账 / 缺清单也能写一行，只是那几格填 '-'。
"""
import argparse
import datetime
import json
import os
import sys

ORDER = ("plan", "collect", "assemble", "brain", "publish")
SYM = {"success": "✓", "failure": "✗", "cancelled": "⊘", "skipped": "⏭"}

HEADER = """# 发布日报（工厂每轮自动追加）

发布点：每天 UTC 11 时（北京 19 时）整包发布一次；其余小时只采集、不发布。
`发布` 列：✅ 本轮真的发布了稳定视图（tag `v0.1.0-dataset`）；⏭ 没到发布点或发布作业未跑；❌ 发布了但失败。
`Δ条数`：数据集条数相对上一版的变化 —— 缩水在这里就能一眼看见。

| 时间(UTC) | run | 触发 | 环节 | 源(完成/总) | 累计条数 | 发布 | 数据集条数 | Δ条数 | sha256 |
|---|---|---|---|---|---|---|---|---|---|
"""


def jobs_cell(results: dict) -> str:
    """把各环节结果压成一行：plan✓ collect✓ assemble✓ brain⏭ publish⏭"""
    out = []
    for k in ORDER:
        r = (results.get(k) or {}).get("result")
        if r is None:
            continue
        out.append(f"{k}{SYM.get(r, '?')}")
    return " ".join(out) or "-"


def ledger_last(path: str) -> dict:
    """台账（progress/台账.csv）的最后一行：源总数/完成数/累计条数。"""
    try:
        lines = [ln.strip() for ln in open(path, encoding="utf-8-sig") if ln.strip()]
        if len(lines) < 2:
            return {}
        head = lines[0].split(",")
        vals = lines[-1].split(",")
        return dict(zip(head, vals))
    except OSError:
        return {}


def manifest_rows(path: str) -> tuple:
    """发布清单（data/release-manifest.json）：(条数, sha256, 生成时间)。"""
    try:
        d = json.load(open(path, encoding="utf-8"))
        merged = d.get("merged") or {}
        return int(d.get("rows") or 0), str(merged.get("sha256") or ""), str(d.get("generated") or "")
    except (OSError, ValueError, TypeError):
        return 0, "", ""


def prev_release_rows(note: str) -> int:
    """从日报正文里取上一行的「数据集条数」，用来算 Δ。"""
    for ln in reversed(note.splitlines()):
        if not ln.startswith("| ") or ln.startswith("| 时间"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 8 or cells[7] in ("-", ""):
            continue
        try:
            return int(cells[7].replace(",", ""))
        except ValueError:
            continue
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="给 data 分支的发布日报追加一行")
    ap.add_argument("--ledger", default="", help="progress/台账.csv（从 data 分支取回）")
    ap.add_argument("--manifest", default="", help="data/release-manifest.json（main 上，发布后才有）")
    ap.add_argument("--note", default="", help="现有日报（没有则新建，自动补表头）")
    ap.add_argument("--out", required=True, help="写到哪里")
    ap.add_argument("--run-id", default=os.environ.get("RUN_ID", "-"))
    ap.add_argument("--event", default=os.environ.get("EVENT", "-"))
    ap.add_argument("--results", default=os.environ.get("RESULTS", "{}"))
    a = ap.parse_args(argv)

    try:
        results = json.loads(a.results or "{}")
    except ValueError:
        results = {}

    note = ""
    if a.note and os.path.exists(a.note):
        note = open(a.note, encoding="utf-8").read()
    if not note.strip():
        note = HEADER
    if not note.endswith("\n"):
        note += "\n"

    led = ledger_last(a.ledger) if a.ledger else {}
    rows, sha, generated = manifest_rows(a.manifest) if a.manifest else (0, "", "")

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    pres = (results.get("publish") or {}).get("result")
    if pres == "success" and generated.startswith(today):
        pub = "✅"
    elif pres == "success":
        pub = "⏭ 未到发布点"
    elif pres == "skipped":
        pub = "⏭ 未跑"
    elif pres is None:
        pub = "⏭ 未跑"
    else:
        pub = "❌"

    if pub == "✅":
        prev = prev_release_rows(note)
        delta = f"{rows - prev:+,}" if prev else "-（首版）"
        rows_cell = f"{rows:,}"
        sha_cell = f"`{sha[:12]}`" if sha else "-"
    else:
        delta, rows_cell, sha_cell = "-", "-", "-"

    src = (f"{int(led['sources_done']):,}/{int(led['sources_total']):,}"
           if led.get("sources_done") and led.get("sources_total") else "-")
    kept = f"{int(led['kept_total']):,}" if led.get("kept_total") else "-"
    at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")

    row = (f"| {at} | {a.run_id} | {a.event} | {jobs_cell(results)} | {src} | {kept} | "
           f"{pub} | {rows_cell} | {delta} | {sha_cell} |")
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(note + row + "\n")
    print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
