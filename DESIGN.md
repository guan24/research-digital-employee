# 研究数字员工设计说明

本文描述仓库中已经实现的系统，目标是单机环境中的持久化、可恢复、可审计研究任务，不是未来架构承诺。

## 1. 运行模型与职责

### 研究输入与截断边界

`app/tools/research_schemas.py` 是模型工具与计划持久化共用的契约。需求包含 3–12 个有类型约束的研究维度，质量策略禁止未知字段；计划包含 3–8 个 ID 唯一的完整步骤。计划写入前先完成 JSON、字段、数量、唯一性校验和结构规范化，失败不生成计划更新事件、不增加版本，也不覆盖旧计划。执行状态字段仍从战略结构中剥离。

需求与计划工具通过 LangChain `handle_validation_error` 和业务校验错误处理返回结构化反馈。任务事件持久记录每个工具的失败次数；首次失败允许一次模型修正，再次失败使用明确终止错误。错误反馈不修改研究状态。

步骤工具 `set_active_plan_step` 与 `update_plan_step_status` 的参数校验错误（含 Schema 校验）返回 `plan_step_validation`，附当前计划版本和有效步骤 ID。模型自行选择修正参数、通过完整计划修订改变步骤或采取其他行动；运行时不自动映射 ID 或重试。失败调用保留在审计账本，但不据此把计划步骤改为阻塞。后续调用仍受原工具次数、Token、运行时和任务控制限制；暂停、取消、失去租约及预算耗尽继续中止执行。

`app/runtime/research_safety.py` 的同步与异步模型中间件先检查 `finish_reason`，截断响应不会到达工具节点。首次截断通过同一个 handler 请求一次精简完整响应，保留原输出上限、模型回调和预算预留；再次截断失败。原始响应和 usage 分别保留在模型完成与截断事件中，回调独立标记 `truncated` 异常。

研究控制器的停滞计数不会自动放宽。仅当人工决策明确继续研究、改变方向或补充需求时，数据库在同一决策事务中重置停滞窗口并记录 `research_stagnation_reset`，已有证据缺口、质量门槛、预算和研究版本保持不变。新窗口仍受两个无进展周期约束。

Research Agent 的 Checkpoint 停在 `tools` 节点时，新的人工指令优先于旧工具批次：Executor 为尚无结果的调用补记被人工决策取代的 ToolMessage，保留已有工具结果及历史副作用，再通过 `update_state(as_node="tools")` 将流程转回模型节点。工具结果排在人工指令之前，保持模型消息协议合法；替代的调用 ID 记录在 `checkpoint_tools_superseded_by_human` 事件中。普通 Checkpoint 恢复保持原行为。该机制只保证模型先处理新指令，不替代后续工具权限或研究收尾策略。

浏览器是操作控制台，FastAPI 负责创建和控制持久化任务；独立 Worker 获取全局 SQLite 租约后领取任务，Executor 打开任务工作区和 LangGraph Checkpoint，构建岗位 Agent。Agent 自行调用工具、收集证据、修改计划和生成报告，运行时没有固定的搜索、阅读、写作流水线。

| 组件 | 当前职责 |
|---|---|
| FastAPI | 任务、岗位、事件、产物、预算、研究状态和人工干预接口 |
| Agent Registry | Builder、Profile Loader、产物路径、Validator、Postprocessor、修复上限、Finalizer 和默认预算 |
| Research Agent | 岗位 Prompt、计划、工具选择、研究记忆和报告 |
| TaskToolService | 公共产物读写、持久化记忆、预算查询和扩额申请 |
| ResearchToolService | 搜索、网页/PDF、GitHub/arXiv、证据、Finding、计划和研究状态工具 |
| Research Controller | 研究要求、证据覆盖率、停滞检测和受限行动建议 |
| Research Evaluator | 规则评分、独立上下文评审、质量缺陷和硬阻断 |
| RecoveryManager | 工具生命周期、实际副作用核验、Checkpoint 检查和过期任务协调 |
| Worker / Executor | 任务领取、回调、预算、重试、交付校验、人工停车和岗位收尾 |

默认只注册 `research`。第二岗位只存在于 `tests/test_role_extensibility.py`，用来验证公共运行时、独立预算、JSON 产物校验和 Finalizer。Executor 仍有研究专属生命周期接线，系统不是多岗位并行调度器。

## 2. 生命周期

1. API 校验岗位，按岗位默认预算创建运行版本为 2 的任务。
2. Worker 获取全局租约并启动心跳，领取任务时增加 `worker_generation`。
3. Executor 使用任务 ID 作为 `thread_id`，复用或创建 SQLite Checkpoint，调用生产 Deep Agents Builder。
4. 模型调用前预留 Token；工具调用前消耗工具预算，登记开始记录。任务控制信号在执行边界检查。
5. 研究工具活动映射为范围、检索、阅读、分析、报告、校验阶段。PlanProgressTracker 根据显式步骤或阶段推断进度；完成步骤必须有非空、可读回的产物，否则进入 `awaiting_verification`。
6. Controller 在阶段边界或状态查询时评估覆盖与停滞；阶段边界的 `REQUEST_HUMAN` 会停车，其余大部分建议由 Agent 执行。
7. Executor 读回成果、执行岗位校验；研究岗位额外运行质量评估，允许有限修复，必要时转人工。
8. Worker 原子提交任务终态及其事件，然后调用岗位 Finalizer。研究岗位归档计划和实质性修订。

人工决策注入使用稳定消息 ID，恢复时避免重复注入。暂停和重试保留同一 `thread_id`；取消是协作式控制，不能终止已经发送的外部请求。

## 3. 数据所有权与数据库事务

| 存储 | 权威数据 |
|---|---|
| `tasks.sqlite` | 任务状态、Worker owner/generation、租约、预算、重试、事件、工具台账、研究要求、质量评估、人工干预和恢复决策 |
| `checkpoints.sqlite` | LangGraph 消息、节点状态和待执行节点 |
| `workspaces/{task_id}/` | 计划及归档、记忆、来源快照、证据登记簿、中间成果和最终产物 |

主要表为 `tasks`、`events`、`token_reservations`、`worker_lease`、`schema_migrations`、`research_control_state`、`quality_assessments`、`tool_calls`、`recovery_decisions`、`human_interventions`。

Database 使用 `BEGIN IMMEDIATE` 串行化写事务。Worker 发起的关键更新在实际写入所属事务中重新核对任务状态、`worker_id` 和 `worker_generation`，不依赖回调开始前的 Python 检查。所有权失效抛出 `WorkerLeaseLost`。任务完成、失败、暂停、取消、重试，以及预算预留释放与对应任务事件在同一事务内执行。

`begin_tool_call` 原子登记工具调用及开始事件；`finish_tool_call` / `fail_tool_call` 原子更新工具终态并插入 `tool_call_completed` / `tool_call_failed`。事件插入失败时工具更新回滚。相同终态的重复回调不再插入事件，相互矛盾的终态被拒绝。幂等键保留任务、图节点/步骤和规范化输入信息；恢复沿用原 call ID。生产 Builder 的工具重放保护复用已成功调用的持久化结果，不确定写操作必须先核验，不能当作成功重放。

本次不增加数据库字段。原有增量迁移首次执行时，将仍活跃的运行版本 1 任务归档并保留 `archived_from_status`；新 Worker 只领取运行版本 2 任务。不能通过删除数据库代替升级，也不宣称原有迁移整体是单一事务。

## 4. 证据驱动的研究控制

研究要求与执行计划分别持久化。每个研究维度包含 ID、标题、问题、期望证据、优先级和最少来源数量；Agent 未定义时，Controller 从计划推导，必要时建立兜底研究维度。

Finding 将结论、来源 ID 和主要研究维度关联。覆盖率只计入 Finding 实际链接且通过快照检查的来源；优先级权重为关键 3、高 2、普通 1。进度向量包含覆盖率、有效来源数、Finding 数、关键缺口和最新质量分；存在关键缺口且连续两个符合条件的评估周期没有改善时判为停滞。

Controller 返回 `FILL_EVIDENCE_GAP`、`REPLAN`、`CONTINUE_RESEARCH`、`ENTER_CONVERGENCE`、`REQUEST_HUMAN`，不会自行搜索或重写计划。Agent 通过状态工具与系统 Prompt 接收建议。

`ConditionalResearchModel` 在离线测试中读取实际 HumanMessage、AIMessage 和 ToolMessage：矛盾证据触发 V2 计划和完整审计，支持性证据保持 V1。反事实测试只改变 HTTP 返回证据，保持模型实现、任务目标和初始计划相同。报告修订根据实际质量反馈决定，不以第 N 次调用对应固定响应。测试复用生产 Builder、Profile、System Prompt、工具注册、Executor、Worker 和真实 SQLite Checkpoint，仅注入模拟模型。

## 5. 交付和质量评估

ReportValidator 检查 Markdown 一级标题、独立来源章节、正文长度、直接 URL、有效来源 ID、来源对应 URL 和段落引用。提供登记簿时，至少 40% 的合格正文段落需引用有效来源；合格段落至少 80 字符。引用 Finding 时也检查 ID。这是结构与溯源门禁，不证明来源在语义上支持结论。

ResearchEvaluator 五维权重为目标覆盖 20%、证据充分 25%、来源可靠 20%、结论一致 20%、报告完整 15%。规则和独立评审按 60%/40% 混合，总分默认门槛 80、单维门槛 60。空报告、无来源和无效快照是不可豁免硬阻断。评审隔离 Agent 会话，但使用相同模型配置；材料限制为报告 16000 字符、最多 8 个来源的各 1000 字符快照尾部，以及最多 30 条 Finding。

岗位注册的 `max_revisions=3` 决定实际修复上限。Profile 中另有 `quality_max_revisions=2`，目前不控制 Executor 修复循环，仍是需要未来整合的重复配置。无效评审结构阻止交付，连续低分耗尽修复后转人工。降级交付必须由用户明确批准并通过成果读回。

## 6. 预算与恢复

工具次数、累计时间和 Token 具有硬闸门。主模型每次预留启发式输入估算、4096 输出 Token 和 256 固定开销；可用时按 Provider usage 结算。Evaluator 输出上限为 1400 Token。

预算保护 Token 30%、工具次数 25%、运行时间 20% 用于收敛，并应用最小/最大预留规则。探索额度耗尽后阻止新搜索和读取，保留有限的产物、记忆、证据和进度操作；批准扩额可复用已有任务和 Checkpoint。

恢复以实际副作用为依据：只读请求允许有限重试；Artifact 必须与原输入精确读回匹配；Finding 检查登记簿；计划核对最新事件、版本、文件和步骤进度；已完成步骤核对当前产物哈希；记忆检查持久化正文。冲突保留现场并转人工。`TOOL_RECOVERY_POLICIES` 中未知工具默认人工处理，具有控制状态副作用的查询和申请也采用保守策略。

真实子进程测试覆盖 Artifact 已写但工具终态未提交、工具开始后副作用尚未执行、Checkpoint 已保存首节点但下一节点中断。父进程通过 Pipe 屏障确认时点后强制终止子进程，再以递增 generation 恢复。损坏 Checkpoint 保留原文件并转人工。

## 7. 可观测性与实现边界

SQLite 事件先持久化再通过 SSE 传输；事件 ID 支持 `Last-Event-ID` / `after_id` 续传。模型和工具事件包含有界输入输出、耗时、Token 和错误，持久化前脱敏，不记录隐藏思维链。操作接口提供预算、证据、记忆、质量、恢复决策和人工干预查询。

测试文件包括 `tests/test_autonomous_research_e2e.py`、`tests/test_worker_fencing_races.py`、`tests/test_process_crash_recovery.py`、`tests/test_recovery_consistency.py`、`tests/test_role_extensibility.py`、`tests/test_documentation.py`。所有自动化模型、Reviewer 和 HTTP 数据均为模拟，Provider 真实请求被测试保护阻断。

数据库事务只覆盖任务库，不构成横跨 Checkpoint 与文件系统的分布式事务；跨存储冲突通过核验保留并人工处理。全局租约仍服务于单机单 Worker，不代表多主机调度。快照存在性和 hash/URL 标记检查不是防篡改存储。真实模型判断质量、外部服务长期故障和外部副作用恰好执行一次仍需真实环境验证。
