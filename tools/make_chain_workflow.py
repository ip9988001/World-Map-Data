"""生成「主动接续」采集流水线 collect-chain.yml。

设计（对标 GitHub 免费托管机的回收行为）：
  * 单个 job 只跑 960 秒（16 分钟）**主动停**，把断点(last_oid)写进 progress.json
  * 下一个 job 读断点从 last_oid+1 继续 —— 被动被回收 → 主动续跑
  * 每个源开 2 条泳道（A: 前半段 / B: 后半段），每泳道 min-interval=1.6s，
    两泳道合起来对同一主机仍是 ≥0.8s/请求，礼貌预算不破
  * 泳道内各段严格串行（needs 链），任何一段失败/被回收都不影响后续段接续

用法：STAGES=5 BUDGET=960 python tools/make_chain_workflow.py
"""
from __future__ import annotations

import os

STAGES = int(os.environ.get("STAGES", "5"))
BUDGET = int(os.environ.get("BUDGET", "960"))
INTERVAL = float(os.environ.get("INTERVAL", "1.6"))
CONC = int(os.environ.get("CONC", "3"))

# (前缀, job_file 里的索引, 源 id, 起点, 终点)
# (泳道, job 索引, 起点, 终点, 段数上限)
LANES = [
    ("inA", 0, 1, 1663345, 4),
    ("inB", 0, 1663346, 3326690, 4),
    ("ohA", 1, 1, 2799877, 8),
    ("ohB", 1, 2799878, 5599754, 8),
]

HEAD = """name: collect-chain

# 主动接续采集：单段最多 __BUDGET__ 秒，到点自己停并把断点写进 artifact，
# 下一段读断点续采。不依赖 GitHub 托管机赏脸（它会在 20 分钟上下回收长 job）。
on:
  workflow_dispatch:
    inputs:
      max_seconds:
        description: "每段最长秒数（默认 960 = 16 分钟）"
        required: false
        default: "__BUDGET__"

permissions:
  contents: read

jobs:"""

STAGE = """  __NAME__:
    runs-on: ubuntu-latest
    timeout-minutes: 45
__NEEDS__    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: 取回已有断点
        uses: actions/download-artifact@v4
        continue-on-error: true
        with:
          path: state
          pattern: state-__LANE__-*
          merge-multiple: true

      - name: 定起点（读断点，若该段已完成则跳过）
        id: pos
        run: |
          ls -la state 2>/dev/null || true
          python3 -c "
__POSCODE__
          " >> "$GITHUB_OUTPUT"

      - name: 采集 __LANE__ 第 __K__ 段（最多 16 分钟，到点主动停）
        if: steps.pos.outputs.skip != '1'
        run: |
          echo "起点 OID = ${{ steps.pos.outputs.lo }}  上限 = __HI__"
          python -m collectors.pilot \\
            --job-file data/retry_jobs.json \\
            --job-index __IDX__ \\
            --oid-lo ${{ steps.pos.outputs.lo }} \\
            --oid-hi __HI__ \\
            --max-records 0 \\
            --min-interval __INTERVAL__ \\
            --concurrency __CONC__ \\
            --max-seconds ${{ github.event.inputs.max_seconds }} \\
            --out out/seg \\
            --log out/seg/request_log.jsonl
          echo "--- 断点 ---"; cat out/seg/progress.json

      - name: 打包（丢原始层）
        if: always() && steps.pos.outputs.skip != '1'
        run: |
          cd out/seg || exit 0
          rm -rf raw
          gzip -9 -f normalized/*.ndjson 2>/dev/null || true
          du -sh .

      - name: 上传本段数据
        if: always() && steps.pos.outputs.skip != '1'
        uses: actions/upload-artifact@v4
        with:
          name: seg-__LANE__-__K__
          path: out/seg/normalized/
          retention-days: 30
          compression-level: 0
          if-no-files-found: warn

      - name: 备好断点文件
        if: always() && steps.pos.outputs.skip != '1'
        run: |
          mkdir -p state
          cp out/seg/progress.json state/progress-__LANE__-__K__.json || true

      - name: 上传断点（下一段靠它接续）
        if: always() && steps.pos.outputs.skip != '1'
        uses: actions/upload-artifact@v4
        with:
          name: state-__LANE__-__K__
          path: state/progress-__LANE__-__K__.json
          retention-days: 30
          if-no-files-found: warn
"""

MERGE = """  merge:
    runs-on: ubuntu-latest
    timeout-minutes: 300
    needs: [__ALL__]
    if: always()
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: 下载全部段数据
        uses: actions/download-artifact@v4
        continue-on-error: true
        with:
          path: in
          pattern: seg-*

      - name: 合并去重（外部排序，内存恒定）+ 按州切片
        run: |
          set -o pipefail
          df -h / | tail -1
          mkdir -p out/merged
          : > /tmp/all.ndjson
          n=0
          for f in $(find in -name '*.ndjson.gz' | sort); do gzip -dc "$f" >> /tmp/all.ndjson; n=$((n+1)); done
          for f in $(find in -name '*.ndjson' | sort); do cat "$f" >> /tmp/all.ndjson; n=$((n+1)); done
          echo "分片数: $n   输入行数: $(wc -l < /tmp/all.ndjson)"
          LC_ALL=C sort -S 3G -T /tmp --parallel=2 /tmp/all.ndjson \\
            | python -m collectors.merge_stream - \\
                --out out/merged/merged.ndjson.gz \\
                --split-dir out/merged/by_state \\
                --report out/merged/merge_report.json
          rm -f /tmp/all.ndjson
          du -sh out/merged; ls out/merged/by_state | head -20

      - name: 上传合并结果
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: chain-merged
          path: out/merged/
          retention-days: 30
          compression-level: 0

      - name: 摘要
        if: always()
        run: |
          python - <<'PY'
          import json, os
          p = "out/merged/merge_report.json"
          if not os.path.exists(p):
              print("没有 merge_report.json"); raise SystemExit(0)
          d = json.load(open(p, encoding="utf-8"))
          print("唯一地址:", f"{d['去重后唯一记录']:,}")
          print("输入行数:", f"{d['输入行数']:,}", "| 重复:", f"{d['重复条数']:,}",
                "| 非法:", f"{d['非法行数']:,}", "| 多源交叉:", f"{d['多源交叉命中']:,}")
          print("覆盖州:", d["覆盖州数"])
          PY
"""


POSCODE = "\n".join("          " + l for l in """import glob, json
lo, done = 1, False
for p in glob.glob('state/progress-__LANE__-*.json'):
    try:
        d = json.load(open(p, encoding='utf-8'))
    except Exception:
        continue
    for v in (d.values() if isinstance(d, dict) else [d]):
        if not isinstance(v, dict):
            continue
        done = done or bool(v.get('done'))
        lo = max(lo, int(v.get('last_oid') or 0) + 1)
print('lo=' + str(lo))
print('skip=' + ('1' if (done or lo > __HI__) else '0'))""".splitlines())


def main() -> None:
    out = [HEAD.replace("__BUDGET__", str(BUDGET))]
    names = []
    for lane, idx, lo, hi, nseg in LANES:
        prev = None
        for k in range(nseg):
            name = "chain-%s-%d" % (lane, k)
            names.append(name)
            needs = "    needs: [%s]\n    if: always()\n" % prev if prev else ""
            out.append(STAGE
                       .replace("__NAME__", name)
                       .replace("__NEEDS__", needs)
                       .replace("__LANE__", lane)
                       .replace("__K__", str(k))
                       .replace("__IDX__", str(idx))
                       .replace("__HI__", str(hi))
                       .replace("__POSCODE__", POSCODE.replace("__LANE__", lane).replace("__HI__", str(hi)))
                       .replace("__INTERVAL__", str(INTERVAL))
                       .replace("__CONC__", str(CONC)))
            prev = name
    out.append(MERGE.replace("__ALL__", ", ".join(names)))
    txt = "\n".join(out)
    path = ".github/workflows/collect-chain.yml"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(txt)
    print("写出", path, len(names), "段 + merge")


if __name__ == "__main__":
    main()
