# 可信研究闭环：当前实现、数据流与边界

> 本文描述当前 main 分支已经接入的实现，不再把原始设计方案中的目标接口、伪代码或未来能力写成现成功能。代码是事实来源；未实现或仅由 Agent 遵循提示词执行的部分会明确标注。

## 1. 实现概览

当前研究闭环由四个模块组成，并由 ResearchLifecycleCoordinator 在 Executor 的研究阶段边界和最终交付阶段进行协调。

| 模块 | 代码位置 | 当前职责 |
|---|---|---|
| Research Controller | `app/runtime/research_controller.py` | 规范化研究要求、根据 Finding 与来源快照计算覆盖率、检测停滞、输出受限行动建议 |
| Research Evaluator | `app/runtime/research_evaluator.py` | 规则评分、独立上下文 LLM 评审、五维加权评分、硬阻断和修复缺陷 |
| Recovery Manager | `app/runtime/recovery_manager.py` | 工具调用生命周期、未知调用核验、Checkpoint 检查、过期任务恢复协调 |
| Lifecycle Coordinator | `app/runtime/lifecycle_coordinator.py` | 串联要求初始化、阶段覆盖评估、最终报告质量评估和控制决策 |

其他关键组件：
- `app/runtime/executor.py`：构建 Agent、绑定 Checkpoint 和回调、处理人工决策、执行最终交付校验与有限修复。
- `app/runtime/worker.py`：单 Worker 租约、心跳、任务领取、错误分类、重试和终态收尾。
- `app/runtime/plan_progress.py`：根据工具和研究阶段推断计划步骤进度。
- `app/infrastructure/database.py`：持久化任务、研究控制状态、质量评估、工具调用、恢复决策和人工干预。
- `app/infrastructure/evidence_registry.py`：保存来源元数据、来源快照和 Finding。

## 2. 两类状态不要混为一谈

### 2.1 任务运行状态

SQLite 中的任务状态包括：

- `queued`、`running`、`retry_wait`
- `paused`、`waiting_for_human`
- `completed`、`failed`、`cancelled`
- `archived`、`deleting`

Worker 只领取 `runtime_generation=2` 且状态为 `queued` 或到期的 `retry_wait` 任务。`waiting_for_human` 不会被 Worker 自动领取。人工决策完成后，任务按动作重新进入队列、完成或取消。

### 2.2 研究阶段与计划步骤

研究阶段不是一套完全独立、由状态机强制驱动的工作流。PlanProgressTracker 根据工具名和工具参数将执行活动粗略映射到：

`scope → discovery → reading → analysis → report → validation`

这些阶段用于更新计划进度和触发阶段性评估，不代表 Agent 必须按固定顺序调用工具。Agent 可以根据新信息重规划。计划策略版本由 `research_plan_updated` 记录；步骤进度由 `research_plan_step_status` 独立记录。自动推断的步骤完成仍需存在、非空并可读回的成果；否则状态为 `awaiting_verification`。

## 3. Research Controller：实际控制逻辑

### 3.1 研究要求矩阵

Agent 通过 `define_research_requirements` 提交 1–20 个研究维度。每个维度包含：

- `id`、`title`
- `questions`
- `expected_evidence`
- `priority`：critical / high / normal
- `min_sources`：1–10

还可配置 `quality_policy.min_total_score` 和 `quality_policy.min_dimension_score)。这些值只能提高研究岗位的默认质量门槛，不能降低岗位基线。

如果 Agent 没有定义矩阵，Controller 会尝试从最新研究计划步骤派生维度；没有可用步骤时创建一个 `research_scope` 兜底维度。要求矩阵及版本保存在 `research_control_state`，并通过 `research_requirements_updated` 事件留痕。

### 3.2 覆盖率计算

覆盖率不按搜索结果条数计算，而是沿以下关系计算：

`dimension → Finding(dimension_id) → source_ids → registered source snapshot`

只有 Finding 关联的来源 ID 已登记且来源快照通过当前完整性检查，才计入该维度的有效来源数。有效来源数除以该维度的 `min_sources` 得到维度覆盖比例，最高为 1。

优先级权重为 critical=3、high=2、normal=1。全局覆盖率是按优先级加权后的维度覆盖率。每个维度输出 covered、partial 或 uncovered 状态；未覆盖的 critical 维度形成 critical gap。

### 3.3 停滞检测

进度向量包括：
- coverage_ratio
- valid_source_count
- finding_count
- critical_gap_count
- quality_total

当覆盖率提升至少 0.03、新增有效来源、新增 Finding、关键缺口减少或质量总分提升至少 2 分之一发生时，视为有实质进展。仅在 phase_boundary、final_delivery、scheduled_checkpoint 等周期触发器上累积停滞计数；连续两个周期没有改善，且仍有关键缺口时，判为 stagnated。

### 3.4 决策与强制程度

Controller 依据质量评审状态、预算阶段、停滞状态和覆盖缺口返回：
- `FILL_EVIDENCE_GAP`
- `REPLAN`
- `CONTINUE_RESEARCH`
- `ENTER_CONVERGENCE`
- `REQUEST_HUMAN`

这是确定性建议和允许动作列表，不是一个自动执行所有动作的规划器。Agent 通过 `get_research_status` 获取状态和决策提示，并依据提示选择研究行动。运行时有两类直接强制机制：
1. 预算进入 convergence 后，工具层拒绝新的搜索和来源读取，只保留有限的综合、成果、记忆和证据操作。
2. 阶段边界评估返回 `REQUEST_HUMAN` 时，TaskEventCallback 将任务原子地置为 `waiting_for_human`，释放 Worker 所有权并创建人工待办。

其他建议（例如补证据或重规划）主要由 Agent 按提示词执行，当前尚未全部转化为运行时硬约束。

## 4. Research Evaluator：实际质量门禁

### 4.1 五维评分

| 维度 | 默认权重 | 当前规则侧主要依据 |
|---|---:|---|
| 目标覆盖度 | 20% | Controller 的加权维度覆盖率 |
| 证据充分性 | 25% | 每个维度的有效来源数与 min_sources |
| 来源可靠性 | 20% | 通过快照存在性/标记检查的来源占比 |
| 结论一致性 | 20% | Finding 文本中正反向词汇与共同关键词的启发式冲突检查 |
| 报告完整性 | 15% | 范围/方法、发现/分析、局限、结论、来源章节及报告长度 |

规则评分与 LLM 评分按 60% / 40% 合成。默认总分至少 80 分、每个维度至少 60 分；岗位 Profile 是基线，任务要求可以提高门槛。

### 4.2 LLM 评审边界

评审通过单独的 ChatOpenAI 调用执行，不继承 Research Agent 的历史消息，温度为 0，使用相同的 `OPENAI_MODEL`、`OPENAI_BASE_URL` 和凭据配置。评审最多重试 2 次，输出 JSON 格式的五维分数、unsupported_claims、contradictions、missing_dimensions 和 actionable_repairs。

当前传入的评审材料有明确上限：
- 报告最多 16,000 字符；
- 最多 8 个来源，每个来源取快照末尾最多 1,000 字符；
- 最多 30 条 Finding；
- 评审输入 JSON 最多 42,000 字符。

因此，“独立”指独立调用和上下文，不代表独立模型供应商或第二套模型。来源摘录也不等于完整来源全文。

### 4.3 硬阻断、修复与人工决策

规则层的非空报告、来源登记和来源快照校验先执行。报告引用不存在的 source ID 会形成不可豁免硬阻断；其他报告结构或引用问题会形成可修复缺陷。存在硬阻断时跳过 LLM 评审。

最终交付通过条件包括：
- 没有不可豁免硬阻断；
- 没有 critical defects；
- 加权总分及各维度分数达到阈值；
- Reviewer 状态为 completed 或显式的 offline_rules_only。

当前研究注册项在 `app/agents/registry.py` 中设置 `max_revisions=3`，即初次验证后最多进行三轮交付修复。评审不可用时不会以规则评分冒充通过，而是创建人工待办；修复轮次耗尽且没有硬阻断时，仍未达标的报告也会进入人工决策。人工可继续修复、批准降级交付或终止，具体动作受待办 choices 限制。

配置注意：`app/agents/research/profile.toml` 中的 `quality_max_revisions=2` 会被 Evaluator 读取到 profile，但当前 Executor 的实际修订循环使用 AgentRegistration 的 `max_revisions=3`。这两个配置尚未统一；当前生效值是 3。

### 4.4 ReportValidator 实际检查项

ReportValidator 检查：
- Markdown 一级标题；
- 独立 Sources/References/参考资料章节；
- 不含来源列表的正文最低长度；
- 正文及来源章节中存在 URL；
- 正文引用的 source ID 已登记；
- 正文引用来源的 URL 出现在 Sources 章节；
- 至少 40% 的合格正文段落（长度至少 80 字符）包含有效来源引用；
- 若报告使用 Finding ID，则 ID 必须存在于登记簿。

该门禁验证格式、来源登记和引用链，不判断来源内容是否真正蕴含报告结论。语义支持由有限上下文的 LLM Reviewer 辅助检查。

## 5. Recovery Manager：实际工具恢复策略

### 5.1 工具调用生命周期

TaskEventCallback 在工具开始、结束或报错时调用 RecoveryManager。数据库中的工具调用记录包含 call_id、幂等键、工具名、参数 JSON/哈希、worker_generation、状态、尝试次数、结果摘要/哈希和错误信息。

当前状态包括 running、succeeded、failed、unknown。幂等键使用 task_id、LangGraph node/step（可用时）、工具名和规范化参数哈希生成；没有图节点元数据时退化为 run_id 作用域，避免错误合并两个不同调用。

### 5.2 未知调用核验规则

Worker 心跳过期后，RecoveryManager 将未结束的调用标记为 unknown，并按工具类别核验：

| 工具类型 | 当前处理 |
|---|---|
| web_search、fetch_url、fetch_pdf、GitHub、arXiv | 只读请求无法从本地证明远端响应，允许重新请求并保留原调用记录 |
| save_artifact | 读取目标文件；内容与原始输入完全一致时标记成功，否则允许重试 |
| record_finding | 查找完全匹配的 claim、source_ids 和 dimension_id；找到则标记成功 |
| update_research_plan | 比较规范化后的最新计划与本次输入；完全一致才标记成功 |
| update_plan_step_status | 比较步骤 ID、状态、备注、计划版本；完成状态还要求对应产物路径验证事件 |
| define_research_requirements | 比较规范化后的当前要求与本次输入；完全一致才标记成功 |
| 未知工具 | 不盲目重放，要求人工处理 |

这不是外部服务的 exactly-once 保证。只读请求在恢复后可能重复发送；已知写入只有在本地持久化事实能够证明时才自动确认。

### 5.3 Checkpoint 与任务恢复

RecoveryManager 检查 Checkpoint SQLite 数据库、必要表结构以及对应 task_id 是否存在 checkpoint 行。发现数据库/表结构损坏，或存在无法自动核验的工具类型时，创建 `recovery_conflict` 人工待办；该类冲突默认只允许终止，避免在状态不明时继续执行。

没有歧义时，Worker 执行 stale-task requeue：结算未完成 Token reservation、将任务重新置为 queued、设置 resume_mode，并由 Executor 使用原 task_id/thread_id 恢复 LangGraph Checkpoint。该流程先进行工具调用核验，再进行任务重新入队。

Checkpoint 检查是数据库结构和 thread 记录级检查，不等同于对图状态语义正确性的完整证明。

## 6. SQLite 数据模型与迁移

当前关键表：

| 表 | 作用 |
|---|---|
| tasks | 任务状态、预算、Worker 所有权、generation、错误历史 |
| events | 按 event_id 排序的执行审计日志 |
| token_reservations | 模型调用 Token 预留和结算 |
| worker_lease | 全局单 Worker 租约 |
| schema_migrations | 迁移版本标记 |
| research_control_state | 研究要求、覆盖快照、进度向量、停滞计数 |
| quality_assessments | 五维分数、硬阻断、缺陷、Reviewer 结果 |
| tool_calls | 工具调用生命周期和幂等键 |
| recovery_decisions | 恢复核验与决策记录 |
| human_interventions | 待处理人工请求和决策 |

当前迁移采用增量列检查，并使用 schema_migrations 的版本 1 标记一次性归档逻辑。首次执行时，generation 1 的 queued/running/retry_wait/paused/waiting_for_human 任务转为 archived，并记录 archived_from_status；新建任务显式设置 generation 2。Worker 只领取 generation 2。迁移不会自动接管旧任务的历史 Checkpoint。当前代码没有实现原设计稿所述的“所有迁移统一在一个 BEGIN IMMEDIATE 事务内完成”，因此不应将迁移描述为全流程原子事务。

## 7. 实际 API

当前已实现的研究相关接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/tasks/{task_id}/research` | 研究控制状态、最近质量评估、恢复决策、待处理人工请求和决策历史 |
| GET | `/tasks/{task_id}/evidence` | 证据登记簿摘要 |
| GET | `/tasks/{task_id}/memory` | 当前持久化研究记忆 |
| GET | `/tasks/{task_id}/budget` | 预算用量、收敛预留和当前阶段 |
| POST | `/tasks/{task_id}/budget/approve` | 批准预算扩展 |
| POST | `/tasks/{task_id}/human-intervention/resolve` | 解析指定人工待办 |

其他任务、事件、SSE、成果和控制接口见 README。当前没有独立的 `/research-state`、`/quality`、`/recovery` 或 `/continue` 路由；这些是早期设计草案中的接口名，不是当前 API。

## 8. 可观测事件

执行事件保存在 SQLite，再通过 SSE 推送。关键事件包括：
- 任务生命周期、Worker heartbeat/recovery、retry 和错误分类；
- 模型调用开始/完成/失败、Token reservation/settlement；
- 工具调用开始/完成/失败及工具调用恢复；
- 研究计划更新、步骤进度、研究要求更新；
- 研究覆盖评估、控制决策、阶段边界观察；
- 质量评估、交付校验、修复耗尽、报告交付；
- 人工干预请求、决策解析和决策消费。

SSE 使用 event_id 作为恢复游标。执行轨迹记录模型和工具的可观察输入/输出摘要，不采集隐藏思维链。

## 9. 预算策略

研究岗位默认值来自 `app/agents/research/profile.toml`：
- max_steps = 30
- max_runtime_seconds = 3600
- max_total_tokens = 100000

主 Agent 模型每次调用前预留：启发式输入 Token 估算 + 4096 输出 Token + 256 固定开销。输入估算结合 CJK 字符比例和 UTF-8 字节长度；Provider usage 是可用时的结算依据。主模型 max_tokens 设置为 4096；Evaluator 评审调用 max_tokens 为 1400。

收敛保护预留比例为 Token 30%、工具调用 25%、运行时间 20%，并应用最小预留和最多预留一半的规则。达到某一维度的探索额度上限后，进入 convergence，工具层阻止新的搜索/读取，保留有限的成果、记忆、证据和进度操作。预算硬上限耗尽会导致终态失败；批准扩额后可以恢复已有 Checkpoint。

## 10. 当前验证覆盖与尚未证明的事项

离线测试重点覆盖：
- Controller 研究要求、覆盖率、停滞和人工停车；
- Evaluator 五维规则评分、评审失败和交付修复；
- ReportValidator 来源 ID、URL、段落引用和快照检查；
- RecoveryManager 未知工具调用分类、Artifact/Finding/计划/进度核验；
- Worker 租约、过期任务、Checkpoint 恢复和预算闸门；
- 人工决策注入、降级交付读回校验；
- API、SSE、事件脱敏和前端轨迹更新。

主要测试文件：`tests/test_research_lifecycle.py`、`tests/test_executor_recovery.py`、`tests/test_report_validator.py`、`tests/test_budget_gates.py`、`tests/test_worker.py`、`tests/test_api.py`。

GitHub Actions 的离线测试使用测试替身，不代表真实模型已自主完成一项研究，也不代表真实外部调用期间的进程崩溃恢复已经完成端到端验证。真实模型、Tavily、GitHub MCP 和 arXiv 联合运行仍需单独冒烟测试与故障注入。

## 11. 明确的实现边界

1. **快照完整性不是防篡改校验。** EvidenceRegistry 保存内容 SHA-256 元数据；Controller、Evaluator 和 ReportValidator 当前主要检查快照文件存在及其中包含 hash/URL 标记，没有重新计算提取正文的 SHA-256。Artifact 工具也没有对 evidence 命名空间设置只读保护。
2. **控制器不是自主执行调度器。** 除预算工具闸门和 REQUEST_HUMAN 停车外，大部分研究控制建议由 Agent 遵循提示词执行。
3. **评审不是独立模型投票。** Reviewer 使用相同模型配置，只隔离调用上下文；评审材料经过截断，规则冲突检查也属于启发式。
4. **恢复不是 exactly-once。** 只读外部请求允许重试，可能重复发送；Checkpoint 检查不证明完整图状态语义。
5. **岗位抽象尚未彻底解耦。** 只有 research 岗位注册，Executor 仍包含 research-specific 分支；多岗位并行调度尚未实现。
6. **迁移原子性有限。** 当前增量迁移有版本标记和旧任务归档逻辑，但不应宣称所有迁移操作被单一事务整体包裹。


## 条件决策、原子恢复与离线验证

`tests/test_autonomous_research_e2e.py` 使用 `ConditionalResearchModel` 读取实际对话和工具结果，保留生产 Deep Agents Builder、Profile、System Prompt、工具注册、Worker、Executor 和真实 SQLite Checkpoint。矛盾证据触发战略重规划及完整审计；支持性证据保持 V1 计划；反事实测试仅替换证据内容，验证后续行动变化。质量反馈触发定向修订，无效评审和修复耗尽均阻止自动交付。自动化结果证明模拟决策下的工程链路，不证明真实模型研究判断。

Database 通过 `BEGIN IMMEDIATE` 在同一事务中检查任务状态、`worker_id`、`worker_generation`，再更新工具终态和对应事件。开始记录与开始事件也原子提交。任务完成、失败、暂停、取消、重试的状态、预留预算释放和对应事件在同一事务中执行。相同终态的重复回调不增加事件，矛盾终态被拒绝；生产工具重放保护复用已成功调用的持久化结果，不确定写操作先核验或转人工。已确认的 Artifact 写入通过精确读回避免重复改写。

`tests/test_worker_fencing_races.py` 使用独立线程和真实 SQLite 连接验证接管竞态、终态竞争和事件插入失败回滚。`tests/test_process_crash_recovery.py` 使用跨平台 spawn 子进程和 Pipe 屏障，在 Artifact 写入后、工具开始后、Checkpoint 首节点保存后强制终止进程，再验证恢复、generation 增加、消息保留和副作用不重复。损坏 Checkpoint 保留现场并转人工。`tests/test_recovery_consistency.py` 保留原有跨存储和副作用核验测试。

`tests/test_role_extensibility.py` 的分析岗位只用于测试，不是生产默认岗位。它复用 API、公共 Worker/Executor、预算和 Checkpoint，拥有自己的 JSON 产物校验和 Finalizer，覆盖暂停后恢复、取消和暂时错误重试，不触发研究专属质量评估。系统仍是单机单 Worker，不承诺多岗位并行。

`tests/test_documentation.py` 检查 UTF-8、非空文档、连续问号/替换字符、中文段落和文档引用路径。所有自动化模型、Reviewer、HTTP 数据均为模拟响应，不调用真实模型 API、不产生真实 Token 费用。测试阻断外网连接，DNS 使用确定性替身；Windows asyncio 所需本地唤醒套接字仍允许。CI 运行全量 pytest 和前端 JavaScript 语法检查。

数据库事务只覆盖任务库。Checkpoint、工作区文件与任务库之间通过核验协调，不是分布式原子事务。真实 Provider 行为、真实模型质量、多主机文件并发和外部服务恰好执行一次仍需真实环境验证；冲突不得以覆盖文件或台账消除。
