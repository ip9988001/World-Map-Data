#!/usr/bin/env bash
# 本地 ⇄ GitHub 状态同步 —— 采集流水线的唯一事实来源是 GitHub 的 data 分支。
#
# 为什么需要：GH Actions 是生产环境（定时、并行、发布）；
# 本机只做「开发 / 事故排查 / 大规模重洗」。两边都写状态会打架，所以规定：
#   · 本机跑任何采集前，先 `sync_from_github.sh pull`（把 GH 的状态/账本拉下来）
#   · 本机跑完，用 `sync_from_github.sh push`（把本地新状态并回 data 分支，取 updated 最新者）
#
# 用法：
#   bash scripts/sync_from_github.sh pull     # GH → 本地
#   bash scripts/sync_from_github.sh push     # 本地 → GH（合并，不覆盖更新的一方）
#   bash scripts/sync_from_github.sh status   # 看一眼两边各有多少源、完成多少
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="ip9988001/World-Map-Data"
BRANCH="data"
FILES=("progress/state.json" "data/源黑名单.csv" "data/引擎路由表.csv")

pull_state() {
  git fetch origin "$BRANCH" --depth=1 -q || true
  mkdir -p progress data
  for f in "${FILES[@]}"; do
    if git show "origin/$BRANCH:$f" > "$f.tmp" 2>/dev/null; then
      mv "$f.tmp" "$f"
      echo "  ↓ 拉回 $f"
    else
      rm -f "$f.tmp"
      echo "  · $f 远端还没有"
    fi
  done
}

push_state() {
  python3 - <<'PY'
import base64, json, subprocess, urllib.request

REPO = "ip9988001/World-Map-Data"


def api(path, method="GET", data=None):
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/{path}",
                                 method=method, data=json.dumps(data).encode() if data else None)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# 合并：远端为底，本地按 updated 更新者胜
local = json.load(open("progress/state.json", encoding="utf-8"))
try:
    cur = api("contents/progress/state.json?ref=data")
    import base64 as b64
    remote = json.loads(b64.b64decode(cur["content"]).decode("utf-8"))
    sha = cur["sha"]
except Exception as e:
    print(f"  远端 state 读取失败（按空处理）: {type(e).__name__}")
    remote, sha = {}, None
merged = dict(remote)
for sid, v in local.items():
    p = merged.get(sid)
    if p is None or (v.get("updated") or "") >= (p.get("updated") or ""):
        merged[sid] = v
payload = {"message": "state: 本地重洗/排查结果并回（sync_from_github.sh）",
           "content": base64.b64encode(json.dumps(merged, ensure_ascii=False, indent=1,
                                                  sort_keys=True).encode()).decode(),
           "branch": "data"}
if sha:
    payload["sha"] = sha
api("contents/progress/state.json", "PUT", payload)
done = [v for v in merged.values() if v.get("done")]
print(f"  ↑ 已并回 data 分支：{len(merged)} 源｜完成 {len(done)}｜"
      f"洗出 {sum(v.get('kept') or 0 for v in done):,} 条")
PY
}

status() {
  python3 - <<'PY'
import json, os, subprocess
local = json.load(open("progress/state.json", encoding="utf-8")) if os.path.exists("progress/state.json") else {}
try:
    out = subprocess.run(["git", "show", "origin/data:progress/state.json"],
                         capture_output=True, text=True).stdout
    remote = json.loads(out) if out.strip() else {}
except Exception:
    remote = {}
for tag, d in (("本地", local), ("GH data 分支", remote)):
    done = [v for v in d.values() if v.get("done")]
    print(f"{tag}: {len(d)} 源｜完成 {len(done)}｜洗出 {sum(v.get('kept') or 0 for v in done):,} 条")
PY
}

case "${1:-status}" in
  pull)   pull_state ;;
  push)   push_state ;;
  status) status ;;
  *) echo "用法: $0 {pull|push|status}" >&2; exit 2 ;;
esac
