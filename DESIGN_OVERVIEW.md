# 设计说明：长时运行的研究数字员工

## 1. 如何理解“数字员工”与普通 LLM 应用的区别

普通 LLM 应用通常围绕一次请求组织：接收输入、调用模型、返回回答，状态主要依附于当前会话。它可以接入工具，但通常不负责一个持续几十分钟甚至更久的任务如何推进、保存、恢复和验收。

我理解的数字员工是一种**以持久化任务为中心的自治执行系统**。用户交付的是目标，而不是固定步骤；Agent 需要自行规划、调用工具、阅读资料、分析证据，并根据新信息调整计划。系统还必须承担任务状态、资源预算、中间成果、执行审计、故障恢复和最终交付质量的责任。

因此，数字员工与普通 LLM 应用的本质区别不在于是否使用 Agent Framework，而在于是否具备可持续推进的任务生命周期：任务能跨请求运行，进度和成果可持久化，失败后可诊断和恢复，交付前有可验证的质量门槛。

## 2. 系统核心模块如何划分，以及为什么

系统按“岗位能力”和“公共运行能力”分层，避免把研究逻辑写进所有岗位都必须复用的基础设施。

| 模块 | 当前实现 | 划分原因 |
|---|---|---|
| 岗位 Agent 层 | Research Agent、岗位 Profile/Prompt、研究工具集 | 研究策略、资料来源和交付规范属于岗位职责，应允许不同岗位独立替换 |
| 公共任务运行层 | FastAPI、Worker、Executor、任务状态机、租约/心跳、重试、Checkpoint、预算 | 所有岗位都需要稳定的任务执行、控制和恢复能力，不应由岗位 Prompt 自行保证 |
| 持久化与证据层 | SQLite 任务/事件/控制记录、LangGraph Checkpoint、任务 Artifact、Evidence Registry | 区分任务事实、Agent 图状态和研究成果，支持断点恢复与结果溯源 |
| 研究质量控制层 | Research Controller、ReportValidator、ResearchEvaluator、人工干预 | 自主研究需要覆盖缺口识别、证据约束、质量评价和必要的人机协同 |
| 可观测层 | 事件日志、模型/工具回调、SSE、计划进度和诊断信息 | 让开发者能解释任务如何执行、在哪里失败以及恢复采取了什么动作 |

当前采用 Deep Agents + LangGraph 承担开放式工具使用和图状态 Checkpoint；FastAPI 提供操作接口；SQLite 和单 Worker 降低 MVP 的部署复杂度。当前没有提前引入消息队列、分布式调度和多 Agent 协作，因为本阶段更重要的是先验证任务状态、证据和恢复的一致性。

## 3. 当前方案最重要的三个技术问题

**问题一：长任务中的状态一致性与可靠恢复。** 任务数据库、LangGraph Checkpoint、工具副作用和工作区文件分别保存不同事实。进程可能在工具执行后、结果落库前中断。当前通过工具调用生命周期、幂等键、Worker generation、Checkpoint 检查和按工具类型核验，尽量避免盲目重放。核心难点是判断“上一步究竟有没有完成”，而不只是重新启动模型。

**问题二：自主研究的证据驱动控制与交付质量。** 仅靠 Prompt 要求 Agent “充分研究”无法判断研究是否真的覆盖用户问题。当前通过研究维度矩阵、Finding—Source 关联、覆盖率和停滞检测识别缺口，再由 Evaluator 检查目标覆盖、证据充分性、来源可靠性、结论一致性和报告完整性。难点是让研究自由度与确定性质量约束并存，同时承认当前控制器仍有一部分依赖 Agent 遵循建议。

**问题三：长时执行的资源边界与可诊断性。** 模型调用、工具请求和运行时间都可能累积，外部服务也会超时或返回不稳定结果。当前对工具次数、累计运行时间和 Token 预留设置硬闸门，并把模型/工具调用、错误、重试、恢复决策写入事件日志。难点是预算不能只在任务开始时检查，而必须在实际执行边界生效；出现问题后还要能追溯根因。

## 4. 如果运行半年并积累大量成功和失败任务，最值得做的三项改进

**第一，建立可回放的任务评测与失败分析体系。** 将任务目标、计划版本、来源、Finding、工具调用、质量评分、修复轮次和最终结果整理成脱敏评测样本。构建代表性研究任务集和故障注入集，支持版本间回放比较，定位哪些提示词、模型、工具或控制策略真正提高了成功率和证据质量。先让改进可测量，而不是直接用历史日志训练模型。

**第二，增强运行基础设施和恢复能力。** 当任务量和岗位数量增长后，将 SQLite 单机存储升级为适合并发的数据库，增加队列与多 Worker 调度、分布式租约、租户隔离和更可靠的产物存储。继续完善工具幂等契约、外部副作用核验和恢复演练，避免并发条件下的重复执行与状态竞争。

**第三，基于历史结果优化研究策略与岗位能力。** 分析不同任务类型下的来源质量、工具成功率、模型表现、预算消耗和修复收益，逐步实现模型/工具路由、来源优先级、预算分配、重试策略和研究模板的自适应优化。同时沉淀岗位 Profile 和可复用工具契约，让新岗位复用公共运行时，而不复制整套 Agent 执行系统。

---

**当前实现边界：** 项目目前是单机单 Worker MVP，默认只注册 Research Agent；Controller 的多数行动建议仍由 Agent 执行；来源快照检查尚未达到防篡改级别；CI 主要验证离线工程机制，真实模型端到端研究和真实进程中断恢复仍需运行验收。上述三项改进是基于未来半年任务记录的演进方向，不应误认为当前已经实现。


## Offline runtime verification

The offline integration suite drives a real LangChain agent graph with structured fake model tool calls through ResearchToolService, the public executor/worker, SQLite checkpoints and task workspaces. It verifies evidence-driven plan changes, preserved unchanged-step progress, reset changed steps, controller coverage growth, registered citations and a failed review followed by targeted repair. Invalid reviewer structure fails delivery; repeated low scores exhaust the repair allowance and park for human review.

`tests/test_role_extensibility.py` registers a test-only analysis role, never a production default. It exercises FastAPI creation, common tools, JSON artifact validation, its own finalizer and budget defaults, checkpoint recovery after pause, cancellation and bounded transient retry. Research-specific quality assessment is not invoked.

Recovery checks cover structured tool failures (including ToolMessage output), ordinary exceptions, late callbacks after generation takeover, corrupt checkpoints, exact Artifact/Finding read-back and incomplete plan publication. Existing conflicting artifacts, inconsistent plan files/progress and changed completed-step artifacts are preserved for human review. Explicit tool policies live in `TOOL_RECOVERY_POLICIES`; unrecognized tools remain manual. `get_research_status`, `set_active_plan_step` and budget-extension requests are conservatively manual during uncertain recovery because they mutate control/audit state. Memory writes require exact persisted read-back. Recovery confirmations emit a completion event.

All automated model responses, reviewer responses and external HTTP data are fake. The test fixture prevents external socket connections and supplies deterministic DNS; Windows asyncio loopback wakeup sockets remain allowed. No real model API or paid token usage participates. CI runs the full pytest suite on Python 3.12 plus the JavaScript syntax check.

Boundary: this establishes runtime orchestration under simulated model decisions, not real-model research judgment. External-provider behavior, multi-host concurrent filesystem writes and exactly-once execution of external services remain outside this verification. Callback ownership checks reject a worker already superseded before its callback starts; they do not provide a distributed transaction spanning SQLite, graph checkpoints and workspace files. The production Deep Agents builder and real model quality still need a separate manual smoke test.
