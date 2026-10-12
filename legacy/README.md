# legacy/ —— 旧实现分叉归档（不删，只移）

**为什么在这里**：13 环节 v2 之前，同一概念存在多份实现，改一处不会改另一处，缺陷随之漂移
（设计稿 §0 的结构性问题）。这里放的是**在 v2 链路上已经没有任何引用**的那几份。

| 文件 | 原用途 | 被谁取代 |
|---|---|---|
| `scripts/verify_parsed.py` | 校验单个源的规则产物 | `scripts/verify_release.py`（出厂闸门，含对账/溯源/空行/跨州重复） |
| `scripts/clean_contaminated.py` | 清洗正则污染分片 | `build_release.py` 的合并 + 出厂硬门 |
| `scripts/clean_final.py` | 清洗 final2 合并档 | 同上（v2 不再吃旧数据集） |
| `collectors/merge.py` | 多源并集合并（内存版） | `build_release.py`（全局外部排序 + 并集，内存恒定） |
| `collectors/pilot.py` | 单源试跑工具 | `collectors/pipeline.py` + `scripts/source_intake.py` |

**故意没移的两份（有活引用，移了会留悬空）**
- `collectors/cascade.py`：`collectors/triage.py` 从它取判决常量。
- `collectors/merge_stream.py`：仍被 3 个**已禁用**的旧链路工作流
  （`collect-chain.yml` / `collect-p0.yml` / `merge-run.yml`）和 `tools/make_chain_workflow.py` 引用。
  这 3 个工作流已被 v2 的 `factory.yml` 取代且处于 disabled 状态；等它们被删除时，这里一并归档。

**注意**：本目录里的代码**不在任何运行路径上**，也不要再往里加东西。要复活某一份，先说明为什么 v2 那份不够用。
