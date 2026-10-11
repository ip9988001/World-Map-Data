# 运行职责划分 —— GitHub 是生产，本机是车间

> 2026-10-11 定。起因：老板问「所有流程所有环节必须保证在 GitHub 仓库的机器上完成，
> 不然后期怎么全自动」。结论：**采集与发布的正式运行全部在 GitHub Actions 上**；
> 本机只做开发、事故排查、大规模重洗。两边不许各写各的状态。

## 1. 谁干什么

| 角色 | 在哪跑 | 干什么 |
|---|---|---|
| **生产** | GitHub Actions（`.github/workflows/factory.yml`） | 定时窗口采集、规则级联、分诊、状态回写、稳定视图发布 |
| **车间** | 本机 | 写/改规则、抽查数据、事故排查、一次性大规模重洗（用已有 bronze） |

工作流四段（每次运行）：

```
gate（窗口闸门）→ plan（16 泳道矩阵）→ collect（matrix 并行，下载→规则→分诊）
   → assemble（状态/黑名单/路由表 回写 data 分支；规则产物存 cache-parsed，留 5 份）
   → publish（合并去重 → 稳定视图同 tag --clobber；full-* 快照留 10 份）
```

## 2. 状态的唯一事实来源

- **`data` 分支**：`progress/state.json`（每个源的状态）、`data/源黑名单.csv`、`data/引擎路由表.csv`。
  采集 job 开工前拉取，收工后由 assemble 合并回写。**人可审计**（每次回写一条 commit）。
- **Release 资产**：
  - `v0.1.0-dataset`：稳定视图（按州分片 + merged），tag 永久复用，更新=删旧资产+同名重传
  - `bronze`：原始档按州缓存（`bronze-<ST>.zip`），采集前恢复、收工回存 → **支持只重洗不回源**
  - `cache-parsed`：每个窗口的规则产物快照，保留最近 5 份
  - `full-<日期>`：完整快照，保留最近 10 份
- **本机与 GH 的同步**：`bash scripts/sync_from_github.sh pull|push|status`
  跑本地采集前先 `pull`；本地跑完用 `push` 按 `updated` 字段合并回 `data` 分支（两边都不覆盖更新的一方）。

## 3. GitHub 环境的硬约束（设计就是围绕它们做的）

| 约束 | 应对 |
|---|---|
| runner 跑完即销毁 | 状态回写 git；原始档存 Release；产物存 artifact(30天) |
| 单 job 上限 6 小时；长 job 会被回收 | 按泳道分片并行（16 泳道），单泳道秒数预算 4800s，到点自停 |
| cron 只认 UTC，且高峰期会延迟 | 每小时 :05 触发一次 + `gate` 窗口闸门（非窗口时段 1 分钟内退出） |
| 公开仓库 standard runner 分钟数免费 | 采集用 matrix 并行拉满（`max-parallel: 16`） |
| 反爬/限速 | 源码级 `Politeness`：按主机各自记账、最小间隔 1.0s、并发 3 |

## 4. 发布层（三层结构）

| 层 | 资产 | 保留 | 用途 |
|---|---|---|---|
| 稳定视图 | `v0.1.0-dataset` 下按州分片 + `merged.clean.ndjson.gz` | 永久（同名覆盖） | 对外主交付 |
| full 快照 | `full-<日期>`（merged 单文件） | 10 份 | 时间点回滚 |
| batch | `cache-parsed`（规则产物 tar） | 5 份 | 中间态排查 |

## 5. 本机降级后的纪律

1. 本机不再承担例行采集；定时任务由 GH 承担。
2. 本机跑采集前**必须先 pull**，否则会把过期状态推回去。
3. 本机提交代码 → `main`；状态变更只进 `data` 分支，两不相扰。
4. 事故排查用本机（快、能随便试）；确认修好后把规则推到 `main`，由 GH 下一窗口自动生效。
