# specs/ —— AI 造的声明式规则（llm_spec）

本目录下每个 `*.json` = 一套规则，由 `collectors/rules/llm_spec.py` 解释执行。
文件由 `collectors/rule_author.py` 自动生成并通过验证闸门；也可以手写（同 schema）。

## 规则
- **只增不改**：改语义 → 新 `name` 或 `version + 1`，旧规格保留（历史可回溯）。
- 不合格的规格进 `rejected/`（保留在库里当证据，避免反复烧 token 试同一条死路）。
- 验证闸门用的是与主流水线**同一套** `mk_record` 铁律 + 反造数检查（见 rule_author.validate）。

## 最小可用规格
```json
{"name": "effingham_site_address", "version": 1, "mode": "single", "single": "SiteAddress"}
```

## 字段
| 字段 | 说明 |
|---|---|
| `name` | 规则名（自动加 `llm_` 前缀；账本按 `name@vN` 记账） |
| `version` | 语义变更 +1 |
| `match.columns_any` | 这些列里**任一**存在才出场（推荐必写） |
| `match.columns_all` | 必须**全部**存在才出场 |
| `match.columns_none` | 出现任一即**不出场**（排除同名污染源） |
| `match.engine` | 仅备注：这套规格是给哪种协议的源用的 |
| `mode` | `columns`（号/街分列） 或 `single`（一整列完整地址） |
| `columns` | `{"number","street","city","zip","state"}` → **源里真实存在**的列名 |
| `number_template` | 号码由多列拼，如 `"{NUM}{SUFFIX}"` |
| `street_template` | 街名由多列拼，如 `"{DIR} {NAME} {TYPE}"` |
| `full` | 备用完整地址列（columns 模式下兜底拆分） |
| `single` | `mode=single` 时的完整地址列名 |
| `unit_drop` | 是否丢掉 Apt/Unit（默认 true） |
| `examples` | `[{"row": {…真实样本行…}, "expect": {"number":"123","street":"MAIN ST"}}]`，进 `self_test()` |

## 硬规矩（写规格时必须遵守）
1. 只能引用样本里真实出现过的列名 —— 不许发明列名。
2. 不许造数据：`number` 必须是原始行里真实存在的门牌号。
3. `street` 只放街名（可含方位/类型后缀），不许把 city/state/zip 混进去。
4. 拿不准的字段留空（`""`）—— 尤其是 city：错的市名会污染 key 去重，宁缺毋滥。
