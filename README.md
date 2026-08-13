# CostWeave

## v0.6.6 Gateway/Runtime Interop (WP03)

The current increment adds an explicit LiteLLM Gateway boundary. LiteLLM is
treated as an injected external gateway: CostWeave does not import its SDK,
store credentials, or enable provider calls by default. The contract maps
model aliases, usage, provider request IDs, structured output, error classes
and optional response cost into the same provider-neutral Gateway Backend
objects. Every alias can be classified as `free`, `paid`, or `unknown`;
missing classifications fail closed as `unknown`, and both `paid` and
`unknown` require explicit paid-call authority before transport is invoked.
Unexpected implementation exceptions are non-retryable, preventing automatic
retry amplification. Streaming and provider cancellation remain deferred until a later
work package. See docs/CHANGELOG-v0.6.6-wp03.md,
docs/V0.6.6_WP03_IMPLEMENTATION_SPEC.md and
docs/ACCEPTANCE-v0.6.6-wp03.md.

## v0.6.6 Gateway/Runtime Interop (WP02)

The current increment adds an OpenAI-compatible local Gateway Backend on top
of the provider-neutral contract. It supports text requests, tools,
structured-output parsing, usage, provider billing headers, streaming events,
error mapping and fail-closed cancellation semantics. A deterministic
in-process Fake Server verifies the wire contract without opening a socket.

An actual local endpoint is accepted only when the caller explicitly enables
the local network policy. Remote endpoints and paid calls are not representable
in this work package. See docs/CHANGELOG-v0.6.6-wp02.md,
docs/V0.6.6_WP02_IMPLEMENTATION_SPEC.md and
docs/ACCEPTANCE-v0.6.6-wp02.md.

## v0.6.6 Gateway Backend Contract (WP01)

This foundational work package defines a
provider-neutral Gateway Backend contract for requests, streaming events,
structured output, tools, usage, billing, errors, cancellation and provider
request IDs. It is contract-only: network, paid calls and provider SDKs remain
disabled by default. The existing v0.6.5 safe-learning schema and migration set
remain compatible. See docs/CHANGELOG-v0.6.6.md, docs/V0.6.6_MIGRATION.md and
docs/ACCEPTANCE-v0.6.6.md.

## v0.6.5 Trace Credit Assignment & Safe Learning

The previous lab line is v0.6.5. It adds a provider-free, advisory-only
learning contract: Planner, Router, Handoff, Executor, Validator and Recovery
receive separate credit; deterministic single-node counterfactual replay and
IPS/DR logged-policy estimates are persisted with evidence, propensity,
version and rollback metadata. E0 fixtures can exercise the full workflow but
are never treated as training data.

Quick reproduction: python -B -m costweave.safe_learning_cli --pretty

API: GET /api/v1/safe-learning/status; POST /api/v1/safe-learning/credit;
POST /api/v1/safe-learning/replay; POST /api/v1/safe-learning/evaluate.

Every v0.6.5 candidate is advisory_only=true, cannot mutate the live route,
cannot auto-promote, and cannot make commercial claims. Real learning requires
E2+ independently validated production or human-benchmark observations,
common support, calibrated rewards, minimum effective sample size and a
rollback policy. See docs/CHANGELOG-v0.6.5.md, docs/V0.6.5_MIGRATION.md and
docs/ACCEPTANCE-v0.6.5.md.

## Version line (important)

`v0.5.8.4` is the frozen stable engineering baseline. `v0.6.0 Decision Intelligence Lab`, `v0.6.1 Rational MetaController`, `v0.6.2 Capability Evidence Graph & Calibrated Router`, `v0.6.3 Joint Plan–Route–Schedule Search`, `v0.6.4 Execution-Time Belief Update & Economic Recovery` and `v0.6.5 Safe Learning` remain provider-free decision-intelligence lines. The active development release is `v0.6.6 Gateway/Runtime Interop`; WP03 extends the interop boundary without enabling network or paid calls.

Quick offline reproduction:

```text
python -B -m costweave.tournament_cli --pretty
python -B -m costweave.meta_controller_cli --pretty
python -B -m costweave.capability_router_cli --pretty
python -B -m costweave.execution_recovery_cli --pretty
```

The v0.6.4 regression suite includes the prior v0.6.0-v0.6.3 contracts plus execution-time progress belief, critical-path recomputation, artifact reuse, pollution scoping, correlated-validator checks, dynamic resource shocks, recovery economics, idempotency, API, UI and release-gate tests. All built-in demonstrations are E0 simulated and make zero provider or network calls; they do not support a commercial cost-reduction claim.

The loopback API is offline by default: `GET /api/v1/execution-recovery/status`, `POST /api/v1/execution-recovery/decide`, and `GET /api/v1/execution-recovery/{decision_id}` expose the v0.6.4 advisory contract. The v0.6.3 joint search and v0.6.2 capability graph remain available beside it. See [`docs/CHANGELOG-v0.6.4.md`](docs/CHANGELOG-v0.6.4.md), [`docs/V0.6.4_MIGRATION.md`](docs/V0.6.4_MIGRATION.md), and [`docs/ACCEPTANCE-v0.6.4.md`](docs/ACCEPTANCE-v0.6.4.md).

## v0.6.3 Joint Plan–Route–Schedule Search

v0.6.3 jointly searches management mode, task DAG, model/reasoning route, tools, parallel schedule, validation strength and recovery policy. The candidate factory combines a safety baseline, historical preference, planner preference and bounded local transforms. Hard constraints prune unsafe candidates before scoring; Pareto retention, greedy, beam and successive-halving searchers provide reproducible ablations. CVaR-like tail loss, chance constraints, deadline/cost limits and amortised planning cost are part of the objective. Prompt/Handoff compilation checks deliverable coverage, shared-field contradiction, authority/tool scope and information loss.

The result is always advisory-only. It does not change the stable execution route, call a Provider, access a network or enable training.

## v0.6.4 Execution-Time Belief Update & Economic Recovery

The execution loop turns half-way acceptance into a first-class decision point. `ExecutionBeliefUpdater` recomputes the conservative success interval, remaining critical path, remaining budget, accepted/reusable artifacts, pollution scope, resource pressure and Validator diversity. `EconomicRecoveryPlanner` then compares `CONTINUE`, `RETRY_NODE`, `SWITCH_MODEL`, `REQUEST_MISSING_INFO`, `REPLAN_SUBGRAPH`, `FULL_REPLAN` and `STOP_AND_RETURN_PARTIAL`.

Future cost is incremental: sunk cost is recorded for audit but does not bias the next action. Validated artifacts can reduce future work, correlated Validators trigger an orthogonal-validator requirement, and queue/price/deadline changes can make a previously cheap continuation infeasible. The fixture selects `SWITCH_MODEL` and reports a positive CPS advantage over fixed retry and full-replan baselines, but this remains E0 simulated evidence.

## v0.6.2 Capability Evidence Graph & Calibrated Router

The router does not treat a model name as a universal skill score. It joins task family, model revision, reasoning tier, provider deployment, tools, modalities, and Validator conditions into an immutable evidence graph. Evidence keeps source provenance, evidence level, sample counts, recency, cost, latency, retry/escalation/rework and correlated-failure fields. A decayed, deliberately wide calibration interval is used for cold start; stale, OOD, high-risk or low-coverage conditions can return `ABSTAIN` or `ESCALATE`.

`costweave-capability-router --pretty` runs the same deterministic, offline fixture as the UI and API. The fixture is E0 simulated, and the active route remains advisory-only until a preregistered real-data tournament passes the release gates.

## v0.6.1 Rational MetaController

The release adds five bounded control-plane capabilities:

- `MetaControllerEngine` evaluates hard constraints, risk, out-of-distribution score, evidence freshness, local capacity, utility intervals, and planning budget before selecting an action.
- `FAST` is permitted only when a conservative utility gap is safe; otherwise the controller computes an explicit expected value of computation (EVC).
- A planner committee is adaptive rather than fixed: each member is selected by marginal information value, cost, latency, and correlated-failure discount, and selection stops at consensus, budget exhaustion, or non-positive EVC.
- `PROBE` is advisory and side-effect-free; `EXPERT` is an authority transfer for high risk, stale evidence, OOD, capacity, or infeasible strategies. The controller itself never executes a model, tool, provider, or network call.
- Decisions and multi-dimensional planning budgets are append-only, project-scoped, hash-verified, and idempotency-bound in the shared runtime database.

The management-depth limit is two levels (`MetaController → Planner/MultiPlanner → Executor/Validator`); recursive planner hierarchies are rejected by policy. `costweave-metacontroller --pretty` runs the same deterministic demo as the UI and API.

> 下一阶段已经按 GitHub 竞品调研进行第二次重构：不再采用“先规划、再路由”的简单顺序，而是联合选择管理拓扑、任务分解、模型、并发、验证和恢复，并以信息价值决定是否值得继续思考。下一版为 **v0.6.0 Decision Intelligence Lab**；产品方向见 [`竞争计划`](docs/V0.6_COMPETITIVE_UPGRADE_PLAN.md)，算法合同见 [`决策智能架构`](docs/V0.6_DECISION_INTELLIGENCE_ARCHITECTURE.md)，逐步开发必须从 [`低能力模型施工总手册`](docs/V0.6_LOW_CAPABILITY_MODEL_PLAYBOOK.md) 开始，并按 [`v0.6.0 精确实施规格`](docs/V0.6.0_IMPLEMENTATION_SPEC.md) 一项一项执行。

## v0.5.8.4：v0.5 Stable Engineering Release

v0.5.8.4 不再增加新的路由启发式，而是把整个 v0.5 控制平面收束为可安装、可重放、可验证、可诚实发布的工程稳定版：

- 新增机器可读 Release Readiness Manifest，逐项检查 Runtime Schema、migration、WAL、事件/投影完整性、全部 EventLog 重放，以及外部/付费调用默认关闭；
- 新增全量而非抽样的 EventLog→Projection 一致性审计；任一运行缺失、篡改或重放不一致都会阻断工程稳定证明；
- migration 029 新增项目范围、幂等且不可修改/删除的发布证明；
- 新增 `GET /api/v1/release/status` 与 `POST /api/v1/release/attestations`；页面新增“工程稳定，不冒充生产领先”状态面板；
- 新增 `costweave-release --pretty`，在临时全新环境执行离线任务、完整重放、逻辑备份校验和零外部调用检查；
- 补齐 Benchmark、模型卡、策略卡、数据卡、安装/迁移/回滚/复现指南、安全报告和发布说明；
- Stable 的范围固定为“离线、单进程工程控制平面”，真实 Provider、生产负载和商业收益仍明确标记为未验证。

安装复现见 [`docs/INSTALL_REPRODUCE-v0.5.8.4.md`](docs/INSTALL_REPRODUCE-v0.5.8.4.md)，发布内容见 [`docs/CHANGELOG-v0.5.8.4.md`](docs/CHANGELOG-v0.5.8.4.md)，Benchmark 见 [`docs/BENCHMARK-v0.5.8.4.md`](docs/BENCHMARK-v0.5.8.4.md)，完整结果见 [`docs/ACCEPTANCE-v0.5.8.4.md`](docs/ACCEPTANCE-v0.5.8.4.md)。

> 稳定边界：v0.5 Stable 代表工程合同在本地、离线、单进程范围内可复现，不代表生产就绪。当前仍没有足够真实样本支持固定降本比例、真实质量提升、真实 P95 改善、生产可用性或世界领先结论。

## v0.5.8.3：Stress、Fault & Migration

本版把“能够正常运行”推进到“能够在受控故障中恢复，并证明恢复过程没有破坏事件、投影和已验收成果”：

- 新增默认关闭、固定种子可复现的故障注入器，覆盖限流、Provider 宕机、显存不足、进程崩溃、期限超时和外部副作用结果不明；
- Provider 故障通过既有熔断器在 30 秒合同内选择可行备用路线；副作用结果不明时保持失败关闭，不自动重放；
- 新增透明的 deadline miss 风险预测与离线评估器，公开 recall、AUROC 和硬门结果；
- 启动恢复先生成不可变检查点与收据，仅重启安全且未完成的节点；终态任务不重启，已验收且未污染成果继续复用；
- 备份清单除数据库文件哈希外，还绑定 migration、事件、投影和流修订的逻辑快照；恢复前后必须一致，篡改或旧清单会被拒绝；
- migration 028 新增不可变韧性实验、观测、恢复检查点和恢复收据；事件完整性审计检查哈希、投影与 stream revision 连续性；
- 新增 `GET /api/v1/resilience/status` 与离线 `POST /api/v1/resilience/campaigns`，并在页面提供桌面、平板和手机可用的“韧性验收”面板。

升级说明见 [`docs/V0.5.8.3_MIGRATION.md`](docs/V0.5.8.3_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.8.3.md`](docs/CHANGELOG-v0.5.8.3.md)，完整验收记录见 [`docs/ACCEPTANCE-v0.5.8.3.md`](docs/ACCEPTANCE-v0.5.8.3.md)。

> 证据边界：30 秒故障转移和 deadline recall/AUROC 来自确定性 E0 夹具，只证明工程合同，不代表真实 Provider、真实高并发或生产性能。默认安装仍不会产生外部或付费调用，也不能据此宣传商业降本、提速或可靠性收益。

## v0.5.8.2：Adapter、Secret 与 Data Security

本版把 v0.5.8.1 的统一证据能力放进一个默认拒绝的中央安全边界，重点解决“真实 Adapter 一旦接入后，如何不泄密、不越权、不串项目、不重复产生外部副作用”：

- `openai-compatible-http` 与 `local-http` 现在是固定版本、固定部署范围、固定能力的 Adapter 插件；未知插件、错误修订或超出能力范围全部在网络前拒绝；
- 外部连接与付费调用继续默认关闭；远程端点强制 HTTPS，拒绝 URL 凭据、查询密钥、回环/私网/保留地址、解析到非公网地址的域名、HTTP 重定向和超过 4 MB 的响应；
- Adapter 密钥只能通过短时 `SecretLease` 读取，使用后覆盖内存；持久化和公开遥测只记录不含值的轮换版本；
- 用户目标和假设被标为不可信输入，与禁止动作、输出 Schema、验收条件和工具权限分离；命中指令覆盖、密钥外传或工具越权模式时，必须单独批准，否则零网络调用；
- 运行、账本、审批、任务、Artifact、媒体项目、证据控制台和导出收据都按 `X-CostWeave-Project` 隔离；跨项目查询统一表现为不存在；
- 项目撤销使用不可变墓碑立即阻断读取和新建任务。当前诚实标注为逻辑撤销，尚未声称物理删除历史数据库行；
- 所有外部调用在网络前建立副作用合同并向 Provider 转发幂等键；相同请求可安全重放，不同请求复用同一键会拒绝，传输结果不明时不会自动重放；
- 日志、错误、JSON 响应和证据导出统一使用结构化与文本双层脱敏；migration 027 保存项目范围、撤销墓碑和不可变副作用事件；
- 页面新增非技术安全边界面板，公开显示外部连接、项目隔离、密钥持久化和歧义副作用状态。

升级说明见 [`docs/V0.5.8.2_MIGRATION.md`](docs/V0.5.8.2_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.8.2.md`](docs/CHANGELOG-v0.5.8.2.md)，安全设计与取舍见 [`docs/security/v0.5.8.2-hardening/hardening.md`](docs/security/v0.5.8.2-hardening/hardening.md)。

> 安全边界：本版增强的是单机回环服务和实验 Adapter 的应用层控制，不等于公网身份认证、操作系统级沙箱、硬件安全模块或可证明的物理数据擦除。默认安装仍不会调用真实或付费模型。

## v0.5.8.1：Unified Evidence Console

本版不再把规划、路由、成本、容量、Outcome、Shadow、策略和 Canary 数据分散在不同页面与接口，而是建立统一、可导出、可追溯的证据视图：

- 所有指标强制标注为 `actual`（实际控制面记录）、`predicted`（执行前预测）、`simulated`（仿真/夹具）或 `missing`（尚无可信值），缺失值不再自动补零；
- 每项指标绑定证据等级 E0～E5、来源名称、数据版本、原始 API、记录数、新鲜度和来源快照哈希；
- 工作区视图汇总真实运行数、外部 Provider 尝试、冻结评测案例、资源池，以及真实成功率、CPS 和端到端 P95 的缺失状态；
- 单运行视图进一步展示规划模式、Planner panel、路由策略、Placement、预测成本/时延/成功率、模拟成本账本、实际控制面耗时和验收状态；
- 商业结论门分别检查真实质量、真实成本和真实响应速度，当前样本量为 0、置信区间未形成，全部明确返回“不支持”；
- 实验包导出包含有界的运行、事件、规划、Placement、账本、验收、失败、制品元数据、预测、健康状态和目录快照，不含密钥、Prompt 原文或 Artifact 内容；
- 导出包使用规范化 SHA-256 校验，migration 026 保存不可修改、不可删除的导出收据；
- 新增 `GET /api/v1/evidence/console`、`GET /api/v1/evidence/exports` 和 `POST /api/v1/evidence/exports`；页面新增桌面、平板和手机响应式证据控制台。

升级说明见 [`docs/V0.5.8.1_MIGRATION.md`](docs/V0.5.8.1_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.8.1.md`](docs/CHANGELOG-v0.5.8.1.md)。

> 证据边界：统一显示和可追溯导出提高了数据诚实性，但不会凭空产生真实效果证据。当前真实验收成功率、真实 CPS 和真实端到端 P95 都是 `missing`；真实效果样本、置信区间和可声明商业结论均为 0。默认安装仍不会调用付费模型。

## v0.5.7.4：Bounded Canary & Automatic Rollback

本版把 v0.5.7.3 的“已审查策略资产”接到一个默认关闭、显式授权、可立即失败关闭的运行时发布控制层：

- Canary 只能从 `5% → 25% → 50% → 100%` 逐级前进，不能跳级；各级候选样本门分别为 50、100、250 和 1,000，样本不足只保持当前阶段；
- 启动部署必须显式设置 `COSTWEAVE_CANARY_ENABLED=1`，同时核对候选处于 `canary`、稳定回滚版本处于 `active`、注册表指针、签名清单哈希、E3+ Canary 门、人工批准和签名回滚目标；
- 新任务按 `deployment_id + run_id` 确定性分桶，只有签名允许范围内的 normal/low 风险任务可进入候选；elevated/high、禁止任务族、范围外或分类缺失任务全部回到稳定策略；
- 每个运行在路由前固化策略 ID、版本、阶段、队列、分桶和快照哈希；该快照进入权威投影并在重启后恢复，回滚不会改变进行中任务；
- 成功率 95% CI 下界 `<-1pp`、严重错误率上升、预算/权限越界、P95 恶化 `>10%`、漂移/校准门失败、重复外部副作用、高风险候选或策略解释/版本/回滚目标缺失，任一条件都会立即自动回滚；
- 回滚以不可变指针事件把后续新任务切回稳定策略；旧快照保持原值，阶段升级、观察窗、决定和影响范围均可审计；
- migration 025 新增部署、运行时指针、任务快照、观察窗和阶段决定五类不可变记录；运行时投影升级至 `v5.6`；
- `GET /api/v1/canary/policy` 公开启用状态、阶段、硬门、当前指针和计数；页面新增桌面/平板/手机响应式 Canary 控制台。

升级说明见 [`docs/V0.5.7.4_MIGRATION.md`](docs/V0.5.7.4_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.7.4.md`](docs/CHANGELOG-v0.5.7.4.md)。

> 证据边界：本版完成的是运行时策略选择、快照隔离和自动回滚的工程合同。仓库仍没有满足门槛的真实 Canary 数据，也没有接入真实 Provider 路由负载；`canary_runtime_policy_changes` 表示候选策略版本被选入运行快照，不等于外部模型已经调用或商业降本已被证明。默认安装保持 Canary 关闭、真实调用为 0。

## v0.5.7.3：Policy Registry & Promotion Gate

本版把候选策略从散落的报告提升为可签名、可追踪、可冻结和可回滚的受控资产，但仍不让注册表接管真实路由：

- `PolicyManifest` 使用 HMAC-SHA256 绑定策略名称/版本、候选/回放/Shadow 来源、代码、目标、约束、适用/禁止任务族、MultiPlanner 意图和回滚目标；密钥不入库；
- 主路径固定为 `draft → evaluated → shadow → canary → active`，并支持从受控状态失败关闭到 `frozen/retired`；跳级、过期修订、缺少签名、门报告或批准都会拒绝；
- evaluation/shadow/canary/activation 分别要求 E2/30、E3/30、E3/200、E4/1,000 个真实且独立案例；Shadow P95 开销原则门仍为端到端成本的 10%；
- 正式 Canary/Activation 门包含最终成功率非劣界 `≥-1pp`、高风险成功率不下降、CPS 改善 95% CI 下界 `>15%`、并行 P95 改善 `≥15%`、Handoff 返工 `<2%` 和四类硬违规为 0；
- MultiPlanner 只有在困难集成功率提高 `≥5pp` 或 Routing Regret 改善 `≥15%` 时才允许成为默认；
- 策略卡展示覆盖范围、失败模式、阻断原因、下一阶段和回滚目标；一键回滚在同一事务冻结当前策略并重指向已审查版本；
- migration 024 保存七类不可变策略治理记录；`GET /api/v1/policies/registry` 公开精确门槛、注册表指针和零运行时干预计数；
- 页面新增策略生命周期和证据门面板，并保留手机、平板和桌面响应式布局。
- 统一运行记录锁与内核写锁顺序，修复并行分支写事件与审批解析在极端时序下互相等待的问题。

升级说明见 [`docs/V0.5.7.3_MIGRATION.md`](docs/V0.5.7.3_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.7.3.md`](docs/CHANGELOG-v0.5.7.3.md)。

> 证据边界：当前仓库仍没有满足门槛的真实策略证据。专项测试只证明签名、状态机、门槛计算、持久化和回滚合同。注册表中的 `active` 不是生产激活，真实路线变化、运行时激活与运行时回滚计数均固定为 0；有界 Canary 属于 v0.5.7.4。

## v0.5.7.2：Production-like Shadow

本版让候选策略在旁路中接收与基线完全相同的结构化任务特征、模型目录和容量快照，生成建议但不改变实际路线：

- `ProductionShadowInput` 冻结匿名任务特征、目录、容量、实际路线、基线质量/成本/时延预测和端到端估计；原始 Prompt、用户/项目标识及密钥禁止进入 Shadow 特征；
- 五种候选策略只在共享容量快照、硬约束和容量准入全部通过后生成建议；容量过期、无候选、无可行候选、开销超预算和超时均形成明确缺失原因；
- Shadow 建议同时记录质量下界、预期总成本、P95 时延、容量资格及相对基线差值，`applied_to_actual_route` 永远为 `false`；
- 失败隔离边界把校验失败和内部失败转换为不含异常正文的回执，基线继续执行，实际路线、外部调用和执行副作用计数均保持为 0；
- 实际 Outcome 只用于校验基线路线；建议路线与实际路线不同时，反事实结果保持 `unknown`，不生成虚构节省；
- 批量审计报告覆盖率、比较覆盖率、缺失原因、Shadow 延迟 P50/P95、成本开销 P50/P95 和延迟开销 P95；Shadow 成本开销 P95 原则门为端到端估计成本的 10%；
- 至少 30 个 E2+ 真实、独立且完整比较案例才允许形成方向性 Shadow 证据；即使通过也没有生产晋级权；
- migration 023 保存不可变输入、决定、失败、Outcome、比较和审计报告；`GET /api/v1/shadow/policy` 公开隔离、开销和证据边界。

升级说明见 [`docs/V0.5.7.2_MIGRATION.md`](docs/V0.5.7.2_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.7.2.md`](docs/CHANGELOG-v0.5.7.2.md)。

> 证据边界：当前仍没有真实生产近似负载、实时容量遥测或 30 个真实 Shadow 案例。专项测试只证明合同、计算、缺失状态和失败隔离，不证明现实开销低于 10% 或候选策略优于基线。

## v0.5.7.1：Offline Replay & Approximate Oracle

本版把“策略看起来更聪明”改造成可以复查的历史回放问题，同时严格保留未知反事实：

- `ReplayVersionSnapshot` 冻结策略、特征、任务合同、模型目录、Prompt、工具、容量、目标、约束和代码版本；相同快照、任务与策略必定生成相同候选顺序和决定指纹；
- `OfflineReplayEngine` 支持最低预期总损失、最低直接成本、最高成功率下界、冻结分数和历史实选路线五种多基线回放，不调用外部模型；
- 仅在低/中风险、可逆、非权限敏感的任务子集中建立近似 Oracle；高风险、不可逆或权限敏感任务固定拒绝多路线比较；
- 只用实际观测且通过验收的路线计算 Oracle 和 Routing Regret；未执行路线明确记录为 `unknown`，不会被补成零损失或成功样本；
- 报告诊断仅记录实选路线、propensity 缺失、独立验收缺失、样本不足和路线覆盖不足等偏差；至少 30 个 E2+ 真实、独立、完整案例才允许评估 median `≤ 8%` 与 P90 `≤ 20%` 的方向性门槛；
- migration 022 保存不可变快照、案例、观测结果、回放、近似 Oracle 和多基线报告；数据库固定外部调用、实际路由干预、全局最优声明与生产晋级为 0；
- `GET /api/v1/replay/policy` 公开回放规则、证据边界、未知反事实语义和运行计数。

升级说明见 [`docs/V0.5.7.1_MIGRATION.md`](docs/V0.5.7.1_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.7.1.md`](docs/CHANGELOG-v0.5.7.1.md)。

> 证据边界：当前通过的是 E0 工程合同和已知值测试，不是 30 个真实任务。近似 Oracle 只代表已观测路线中的最好结果，不证明全局最优，也不能证明真实降本、提速或商业领先。

## v0.5.6.5：Safe Policy Candidate & Offline Counterfactual Gate

本版把通过离线预测器验收的结果封装成可审计策略候选，但仍不允许它接管实际路线：

- `PolicyCandidate` 绑定预测器制品、离线评估、数据哈希、特征与目标 Schema、代码版本、目标函数、约束、适用/禁止任务族、数据时间窗和旧规则回滚目标；
- 新增 IPS、SNIPS 与 Doubly Robust 离线反事实评估；若候选动作不在日志策略支持范围内、重要性权重超过 20、有效样本不足 30、来源不是 E2+ 真实证据，系统拒绝给出反事实结论；
- 低风险、可逆、非权限敏感任务的未来探索率最高 5%；高风险、关键风险、不可逆或权限敏感任务的探索率固定为 0；本版本身不会执行探索；
- 新增策略冻结门：`PSI > 0.20`、ECE 恶化超过 `0.03`、任一任务族成功率下降超过 5 个百分点、成本或时延 P95 连续三个窗口恶化超过 20%、数据质量或隐私检查失败都会冻结候选；
- 策略卡公开已知优势、失败模式、覆盖范围、不支持范围、方向性收益区间和回滚目标；E3+、共同支持、足够样本且未冻结最多进入 v0.5.7 人工审查，不会自动晋级；
- migration 021 保存候选、反事实报告、冻结评估和策略卡；支持完整导出与带不可变墓碑的审计删除，数据库固定零实际路由权和零生产晋级；
- `/api/v1/predictors/policy` 公开本版共同支持、探索禁区、冻结阈值、证据门和零权威状态。

升级说明见 [`docs/V0.5.6.5_MIGRATION.md`](docs/V0.5.6.5_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.6.5.md`](docs/CHANGELOG-v0.5.6.5.md)。

> 证据边界：当前仓库没有真实 E2/E3 策略数据。专项测试中的数值只验证 IPS/SNIPS/DR 公式和安全门，不能证明真实收益、降本比例或商业领先。

## v0.5.6.4.4：Exact Oracle & Sensitivity Audit

本版为上一版的有界 Beam Search 增加独立验收层，避免“启发式程序给自己打分”：

- 新增小型 DAG 精确 Oracle，在默认不超过 10 个节点、10 万候选组合时穷举全部可行分配，给出可证明的最优或不可行结论；超界时固定返回 `skipped_bounds`，不冒充已验证；
- 新增 Beam/Oracle 差分报告，同时检查可行性、选中组合、目标值、绝对与相对 Routing Regret，并核验启发式求解器的 `optimality_proven` 声明；
- 新增价格、成功率下界、P95 时延和失败返工成本敏感性探针，报告已测试点中最接近的路线翻转值与稳健性等级；它不是连续参数的数学阈值证明；
- 专项测试包含一个真实剪枝反例：窄 Beam 选择低价上游，而精确 Oracle 找到更低全链路损失的强上游组合，并量化其预期损失差；
- migration 020 保存不可变 Oracle、差分和敏感性报告，数据库继续固定零正式路由权、零实际干预和零商业声明权；
- `GET /api/v1/portfolio/policy` 现在同时公开 v0.5.6.4.3 的组合审计与 v0.5.6.4.4 的精确验收边界。

升级说明见 [`docs/V0.5.6.4.4_MIGRATION.md`](docs/V0.5.6.4.4_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.6.4.4.md`](docs/CHANGELOG-v0.5.6.4.4.md)。

> 证据边界：精确 Oracle 只证明给定 E0 输入、离散候选和实现约束下的数学最优，不证明输入概率、价格或时延符合现实，更不能证明真实降本或商业领先。

## v0.5.6.4.3：Global DAG Portfolio Audit

本版在不使用真实模型调用、也不伪造效果数据的前提下，先把“整张任务 DAG 应该怎样分配模型与算力”的可审计决策合同做出来：

- 新增全局组合问题合同，同时表达任务节点、依赖边、候选部署、推理成本、验收成本、工具成本、交接成本、返工、升级、下游失效和失败相关性；
- 通过有界、确定性的 Beam Search 比较整张 DAG 的候选组合，而不是逐节点各自选择最便宜模型；搜索截断与剪枝会显式披露，不冒充全局最优证明；
- 关键路径按 DAG 并行结构计算，不把可并行节点的时延简单相加；Provider 配额与同层资源池容量属于硬约束；
- 高风险验收节点不得复用被验收节点的模型或失败域；能力、数据驻留、预算、截止时间、节点成功率下界与组合成功率下界均会失败关闭；
- 可用同一比较组审计 `fine_grained` 与 `consolidated` 等拓扑，比较“多拆分便宜模型”和“少拆分强模型”，但比较结果固定为建议，不接管当前正式路由；
- migration 019 保存不可变审计与场景比较，Runtime Schema 升级为 `v5.6.4.3`；`GET /api/v1/portfolio/policy` 公开目标项、硬约束、证据边界和零干预计数。

升级说明见 [`docs/V0.5.6.4.3_MIGRATION.md`](docs/V0.5.6.4.3_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.6.4.3.md`](docs/CHANGELOG-v0.5.6.4.3.md)。

> 证据边界：当前审计输入仍是 E0 规则先验，只证明合同、约束、公式、持久化与回放行为；它不会调用外部模型、不会改变实际路线，也不能证明真实降本、提速、成功率提升或商业领先。

## v0.5.6.4.2：Offline Evaluation & Promotion Freeze

本版不依赖真实数据训练，而是先把未来预测器必须经过的科学验收流程固定下来：

- 新增不可变 `PredictorCandidateManifest`，绑定准入报告、数据版本、特征/目标 Schema、代码版本、训练配置和模型载荷哈希；制品固定为 `frozen_candidate`；
- 用五类 Outcome 分布计算成功率置信区间、Brier、ECE、高风险 ECE、难度—失败 Spearman、P95 覆盖率、Cost per Accepted Task 与 Time per Accepted Task；
- 测试集必须匿名、项目化、版本一致、样本唯一且只来自 test 分区；E0/E1 即使指标完美也会被硬冻结；
- E2 最多形成 `offline_pass`，E3+ 且全部预注册阈值通过才可成为 `shadow_candidate`；Shadow 候选仍然没有正式路由权；
- Champion/Challenger 必须使用相同 Cohort，同时满足成功率非劣、校准不恶化、高风险不恶化，以及成本至少改善 3% 或时延至少改善 10%；
- migration 018 保存冻结预测制品、离线评估报告与挑战者比较，Runtime Schema 升级为 `v5.6.4.2`；
- `/api/v1/predictors/policy` 现在同时公开数据准入与离线评估阈值，默认状态为 `frozen_no_real_evidence`。

升级说明见 [`docs/V0.5.6.4.2_MIGRATION.md`](docs/V0.5.6.4.2_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.6.4.2.md`](docs/CHANGELOG-v0.5.6.4.2.md)。

> 证据边界：内置观察仅用于验证公式和合同，不能作为真实模型效果。当前没有训练预测器、没有真实 Provider 调用、没有 Shadow 实验，也没有策略晋级。

## v0.5.6.4.1：Predictor Admission & Replay Contract

本版在不调用任何付费模型的前提下，先把学习型路由最容易被忽略的“数据准入门”做实：

- 自动把受治理 Outcome 数据集分别送入方向性、内部校准和正式基准三档准入检查；
- 30 个 E2+ 有效任务最多只形成方向性信号，不能训练；内部校准候选至少需要 200 个有效任务，正式基准需要 1000 个、8 个任务族且每族至少 100 个；
- E0/E1 混入、项目分区泄漏、标签覆盖或冲突异常、候选路线/选择概率缺失、成本未对账、版本不可追溯都会冻结训练；
- 即使数据达到校准候选标准，本版也不允许策略晋级或接管正式路由；现有 `ResourcePortfolioOptimizer` 继续拥有路由权；
- 样本不足时生成可回放的冻结凭证，明确返回 `outcome_probabilities=null` 与最大不确定区间 `[0,1]`，不拿拍脑袋概率冒充学习结果；
- migration 017 持久化不可变准入报告和冻结预测凭证，Runtime Schema 升级为 `v5.6.4.1`；
- `GET /api/v1/predictors/policy` 可查看三档门槛、冻结状态和运行时计数。

升级说明见 [`docs/V0.5.6.4.1_MIGRATION.md`](docs/V0.5.6.4.1_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.6.4.1.md`](docs/CHANGELOG-v0.5.6.4.1.md)。

> 证据边界：本版没有真实 Provider 调用，也没有训练预测器。它只完成准入、冻结和重放合同，不能据此宣称真实降本、成功率提升或商业领先。

## v0.5.6.3：Experimental Adapters & Shadow Router

v0.5.6.3 完成了真实连接前必须具备的安全控制面，但默认仍不会调用任何外部模型：

- 新增 OpenAI-compatible HTTP 与本地 Loopback HTTP 两类 Provider-neutral 文本适配器；厂商差异由配置表达，核心路由不按厂商名称分支。
- 外部调用使用双开关：`COSTWEAVE_EXPERIMENTAL_ADAPTERS=1` 才允许实验调用，远程或计费连接还必须设置 `COSTWEAVE_PAID_CALLS=1`。
- 每次实验必须先注册总预算、Provider 预算、逻辑调用数和单次尝试数上限；所有重试按最坏情况在网络请求前一次性预留。
- 密钥只从环境变量读取，不进入配置公开视图、SQLite 结果、遥测、异常正文、页面或导出文件。
- 所有调用带实验、Run、Task、Deployment、模型、推理档位和 Adapter 版本归因，并记录 Token、费用、请求/排队时延、缓存、限流、取消和重试。
- Shadow Router 只保存候选、淘汰原因和建议；它会校验任务在判断前后字节级一致，且固定 `advisory_only=true`、`intervention_count=0`，不能改变正式路线、Prompt、工具、预算或执行顺序。
- 测试 Transport 固定标记为 `E0-transport-fixture`；只有真实网络 Transport 才可产生 `E2-real-unvalidated`，两者不能混作真实效果证据。
- migration 016 新增实验预算、外部调用/尝试、Shadow 决策与比较表；Runtime Schema 升级为 `v5.6.3`，Run Projection Schema 继续兼容 `v5.5`。

默认安全状态可通过 `GET /api/v1/experiments/policy` 查看。升级说明见 [`docs/V0.5.6.3_MIGRATION.md`](docs/V0.5.6.3_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.6.3.md`](docs/CHANGELOG-v0.5.6.3.md)。

> 证据边界：本版已通过 481 项自动化测试，但没有使用用户密钥，也没有产生真实 Provider 调用。路线图要求的 30～50 个方向性真实任务尚未执行，因此 v0.5.6.3 的“工程机制”已完成，“真实效果退出门槛”仍未通过；不得据此宣称真实降本、真实成功率提升或商业领先。

## v0.5.6.2：Outcome Dataset & Label Governance

v0.5.6.2 把 v0.5.6.1 的冻结评测事实转换为受治理的 Outcome 学习副本，但仍不训练模型、不晋级策略：

- `GovernedOutcome` 只保留结构化任务特征、合同摘要、路线、实际成本/时延、失败分类和版本绑定；原始目标、项目/用户/任务标识与证据引用默认不进入学习副本。
- 项目、用户、任务、合同、路线和证据使用带私密盐的 HMAC 标识；项目分区泄漏、重复观察、敏感内容、版本缺失或成本不对账都会触发治理失败。
- 五组基线对同一任务的 175 条路线观察保留为独立 Outcome，但通过 `dedupe_group` 和 `sample_weight` 折算为 35 个有效任务，避免重复任务放大。
- 标签区分自动硬验收、独立模型、人工盲评、用户反馈和系统终态；未经独立验收的正向反馈不能成为成功标签，冲突标签保持并列。
- 延迟反馈只能追加。TTL 或删除请求只删除学习副本及其标签正文，同时保留不含原文的不可变删除审计摘要。
- migration 015 持久化数据集版本、学习副本、追加式标签与删除审计；Runtime Schema 升级为 `v5.6.2`，Run Projection Schema 继续兼容 `v5.5`。
- 数据质量报告覆盖缺失字段、标签覆盖、冲突、重复、项目泄漏、版本追溯、候选路线/propensity 捕获、敏感信息与成本对账。
- `assert_training_ready()` 和持久化训练入口执行硬门禁。当前 E0 数据只有 35 个有效任务且候选集合不完整，因此固定冻结，不能训练路由器或支撑商业声明。

从 v0.5.6.1 实验报告生成治理数据：

```powershell
$env:COSTWEAVE_DATASET_PRIVACY_SALT="<至少16字节的私密随机值>"
python -B -m costweave.outcome_dataset `
  outputs/CostWeave-v0.5.6.1-evaluation/experiment.json `
  --output-dir work/v0562-outcomes `
  --database work/v0562-runtime.sqlite3
```

升级说明见 [`docs/V0.5.6.2_MIGRATION.md`](docs/V0.5.6.2_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.6.2.md`](docs/CHANGELOG-v0.5.6.2.md)，后续路线见 [`docs/REMAINING_ROADMAP.md`](docs/REMAINING_ROADMAP.md)。

## v0.5.6.1：Evaluation Protocol & Baseline Runner

v0.5.6.1 不再继续增加未经验证的路由规则，而是先建立一套能证明或推翻 CostWeave 分流效果的实验基础设施：

- 冻结 `ExperimentSpec`、`BenchmarkTask`、`ExecutionTaskView`、共享预算和盲评合同；参考答案、验证器身份和数据分区不会进入执行负载。
- 内置 35 个 E0 种子任务，按项目隔离为 15 个 train、10 个 calibration 和 10 个 test，覆盖代码、研究、写作、分析、长上下文、工具、多语言与视觉。
- 同一批任务统一比较五组基线：最强可行模型、最低价可行模型、人工静态分配、v0.4 固定规则和 v0.5 自适应策略。
- 结果矩阵同时记录关键条件、软质量、重试、验证、工具、完整成本、Time to Accepted Result、CPS、非劣差值和 Routing Regret。
- 盲评包隐藏模型、Provider、策略和价格身份，并保持未评分状态；私有身份映射与公开评审材料分离。
- `python -m costweave.baseline_runner` 可导出不可静默覆盖的 `experiment.json`、任务/观察 JSONL、盲评 JSONL 和基线 CSV。
- migration 014 把 175 个观察项幂等写入现有 `evaluation_runs` 事实源；Runtime Schema 升级到 `v5.6.1`，运行投影继续兼容 `v5.5`。
- `GET /api/v1/evaluation/protocol` 明确返回任务规模、五组基线、E0 证据等级和零真实 Provider 调用边界。

运行内置零调用评测：

```powershell
python -B -m costweave.baseline_runner --output-dir work/v0561-evaluation
```

如需把报告同时存入现有运行数据库：

```powershell
python -B -m costweave.baseline_runner --output-dir work/v0561-evaluation --database work/v0561-runtime.sqlite3
```

> 证据边界：本版证明的是评测协议、数据隔离、盲评、成本记账和重放机制能够运行。内置结果全部标记为 `E0-simulated`，真实 Provider 调用数为 0，不能用来宣传真实成功率、真实降本比例或商业优势。v0.5.6.2 只建立数据治理，实验型真实调用从 v0.5.6.3 开始。

升级说明见 [`docs/V0.5.6.1_MIGRATION.md`](docs/V0.5.6.1_MIGRATION.md)，发布内容见 [`docs/CHANGELOG-v0.5.6.1.md`](docs/CHANGELOG-v0.5.6.1.md)，后续路线见 [`docs/REMAINING_ROADMAP.md`](docs/REMAINING_ROADMAP.md)。

## v0.5.5：Handoff Intelligence & Adaptive Recovery

v0.5.5 把“把提示词发给下一个模型”升级为可验证的执行合同与证据匹配恢复：

- `HandoffCompiler` 为每个节点生成版本化 `WorkOrder`、最小 Context Manifest、确定性幂等键和合同摘要；Artifact 正文不复制进 Prompt，只传可信元数据与内容哈希。
- 接收方在执行前验证计划版本、依赖完整性、Artifact 哈希/Schema/敏感度、模型工具、模态、上下文、输出容量和审批状态；不满足合同会在消耗模型算力前拒绝。
- Pcomplete 检查点融合路由下界、验收通过率、约束覆盖、证据完整度、规划冲突、预算消耗、依赖阻塞和剩余关键路径；低/中/高风险门槛分别为 0.60、0.75 和 0.90。
- 污染分析只沿 `hard` 与 `validation` 依赖传播失效，`soft` 依赖仅标记可疑；无关且已验收的 Artifact 保留。
- 恢复按节点重试、节点升级、子图重规划、一次全局重规划的有界顺序执行；每次升级都保存原因、证据、预计成本、影响范围和门禁回执。
- 恢复节点复制而不扩大原任务的数据权限、工具和依赖 Schema；高风险与不可逆操作继续受独立证据和全新审批约束。
- 新增 `GET /api/v1/runs/{id}/handoffs` 与 `GET /api/v1/runs/{id}/recovery`；页面展示当前 Pcomplete、门槛、交接预检与污染隔离结果。
- Runtime / Projection Schema 升级到 `v5.5`，migration 013 持久化交接合同摘要、预检与恢复回执；旧 `v5.4` 投影继续可读。

> 能力边界：本版实现了确定性、可回放的交接与恢复控制面，并未证明这些启发式门槛已经在真实生产流量中完成校准。默认执行器仍是离线模拟，不调用付费模型，也不执行真实不可逆操作。

升级说明见 [`docs/V0.5.5_MIGRATION.md`](docs/V0.5.5_MIGRATION.md)，完整路线见 [`docs/V0.5_UPGRADE_PLAN.md`](docs/V0.5_UPGRADE_PLAN.md)。

## v0.5.4：Cross-provider Economics & Admission

v0.5.4 把“模型单价比较”升级为可审计的跨供应商期望价值路由：

- 每个“模型 × Provider × Endpoint × 推理档位 × 任务族”拥有独立部署键和分层 Beta 后验；样本不足时保守回退，验收结果持续回写。
- 路由同时预测成功率及下界、端到端成本、预计返工、执行 P50、完成 P95 和 deadline miss 概率。
- 本地满载、显存不足、队列超限、Provider Bulkhead 饱和或熔断开启时，在第一次执行前分流，而不是先失败再升级。
- 经济、均衡、极速和质量四种策略共享同一组硬约束，只改变可行候选之间的选择目标。
- 质量或极速模式的理想组合超过预算时，会在保持质量门槛、独立验收和期限约束的前提下寻找预算内最优降配组合；只有不存在可行组合才拒绝运行。
- `GET /api/v1/runs/{id}/economics` 返回跨 Provider 判定回执；Placement API 同时返回部署、后验、准入和时延预测。
- Runtime / Projection Schema 升级到 `v5.4`，migration 012 持久化每个 Placement 的决策回执；旧投影继续可读。

> 能力边界：本版实现的是离线可验证的决策与模拟执行控制平面，不会调用付费模型 API，也不宣称当前启发式预测已达到生产流量中的最优策略。真实 Provider 适配、实时价格/配额采集与在线 A/B 策略学习仍需后续接入。

升级说明见 [`docs/V0.5.4_MIGRATION.md`](docs/V0.5.4_MIGRATION.md)，完整路线见 [`docs/V0.5_UPGRADE_PLAN.md`](docs/V0.5_UPGRADE_PLAN.md)。

## v0.5.3：Bounded MultiPlanner

v0.5.3 把 v0.5.2 已经决定“何时值得多规划”的 Governor 接到真正受限的 MultiPlanner 控制流程：

- Domain、Economics、Risk 最多三个互补角色作为独立异步作业并行形成提案；规划时延按最慢分支计算，而不是把三段时延线性相加。
- 评分前把角色、模型、Provider 和部署身份移除，只保留 A/B/C 匿名提案，按目标对齐、可执行性、成本、时延、风险控制和证据质量评分。
- 硬约束先于综合分数；提案数量和多数投票不构成事实证据，也不能覆盖失败的预算、权限或验收门。
- 显式检测并行力度、预算储备等策略冲突。分数领先且没有高危冲突时提前停止，否则只允许一轮裁决，禁止 Planner 再创建 Planner。
- 裁决会解释选中、合并和淘汰原因，并把验收追踪、预算硬停、独立验证和回滚证据编译进已有 Artifact DAG。
- 规划调用数、成本和时延均受 Governor 预算约束；容量不足会在发起提案前缩减面板，而不是先超支再补救。
- 完整回执随权威 Run Projection 与事件流持久化；Runtime / Projection Schema 升级到 `v5.3`，migration 011 增加跨运行裁决审计索引，旧 `v5.2` 投影继续可读。
- 页面展示匿名得分、硬约束状态、提案冲突、选中/合并结果和实际裁决轮数。

> 能力边界：这一版实现了真实的并行控制语义、匿名规则评分和 DAG 编译，但默认角色仍由确定性的离线适配器生成，不会产生付费 API 请求，也不宣称已经完成基于真实流量的策略学习。跨 Provider 的真实调用、实时价格/容量和熔断属于后续版本。

升级说明见 [`docs/V0.5.3_MIGRATION.md`](docs/V0.5.3_MIGRATION.md)，完整 v0.5 路线见 [`docs/V0.5_UPGRADE_PLAN.md`](docs/V0.5_UPGRADE_PLAN.md)。

## v0.5.2：Planning Governor

v0.5.2 把 v0.5.1 的确定性模式仿真升级为规则优先、置信度感知的规划治理器。系统会在执行前估计任务难度区间、比较六条候选路线的预期总损失，并在低置信、高风险或本地容量不足时直接升级，避免让弱模型先失败再逐级试错。

- 冻结 11 维 `TaskDifficultyVector`，同时输出难度下界、期望、上界、判断置信度与触发特征。
- 为 `NO_DELEGATION`、直接委派、SinglePlanner、MultiPlanner、高级模型和询问用户生成统一候选回执。
- 预期总损失同时计入直接费用、时间价值、失败返工、错误风险和协调开销，不再只比较 Token 单价。
- MultiPlanner 必须同时通过硬约束和 VoD（讨论价值）收益边际；简单任务会被规则门直接挡住。
- 高级模型仅在高风险、低置信、专业能力缺口或难度越界时可用，防止常规任务被高价路线吞噬。
- 角色匹配先执行模式对应的推理能力下限，再按标准化价格、本地优先、能力与可靠性评分；低难度直接委派不会仅因高级模型分数略高就选择最贵候选。
- 本地轻量分类器接口不调用 Provider；后续可以替换为本地开源分类模型而不改变 Governor 合同。
- 执行前检查本地候选数量、推理能力上限、并发压力和期限压力；容量不足时在第一次执行前请求远程增强。
- 低置信或高风险决策会把 `senior-review` 验证门写入现有 DAG，而不是仅在页面上提示。
- 页面展示难度信号、区间、升级原因、六条候选路线、成功率下界、预期损失、规划费用、VoD 与淘汰理由。
- Runtime / Projection Schema 升级为 `v5.2`，migration 010 增加规划事件复合审计索引；v4.9 与 v5.1 投影继续可读。

> 能力边界：v0.5.2 的 Governor 是可解释的确定性决策内核，不宣称已经通过真实流量学习出最优策略。真实并行 MultiPlanner 提案与裁决属于 v0.5.3；跨 Provider 实时价格、容量和熔断属于 v0.5.4。当前默认配置仍不会产生付费 API 请求。

升级说明见 [`docs/V0.5.2_MIGRATION.md`](docs/V0.5.2_MIGRATION.md)，完整 v0.5 路线见 [`docs/V0.5_UPGRADE_PLAN.md`](docs/V0.5_UPGRADE_PLAN.md)。

## v0.5.1：Deliberation Contracts

v0.5.1 是 v0.5 跨厂商 MultiPlanner 路线的第一个稳定切片。它先冻结“谁负责判断、谁负责提出计划、信息怎样交接、管理层最多有几层”，不提前把规则仿真器包装成学习型 Planning Governor。

- 新增 Provider-neutral 的 `PlanningDecision`、`RoleSpec`、`PlanProposal`、`WorkOrder` 与 `ResultEnvelope` 合同。
- 每个新任务都会留下可解释、可复现的规划回执，支持 `NO_DELEGATION`、`DIRECT_DELEGATION`、`SINGLE_PLANNER`、`MULTI_PLANNER`、`EXPERT_OVERRIDE` 和 `ASK_USER`。
- MetaPlanner 位于第 1 层，Planner 角色固定在第 2 层；禁止 Governor 创建 Governor、Planner 创建 Planner。单轮最多 3 个 Planner、1 次裁决和 1 次全局重规划。
- MultiPlanner 面板使用领域、经济、风险三个互补角色；角色与可编辑模型目录按能力、可靠性、推理、成本和 Provider / 部署多样性匹配，核心选择逻辑不包含厂商名称分支。
- 规划预算受任务预算硬约束：普通路径不超过 15%，高风险路径绝不超过 25%；零价值路径可以选择不委派。
- 新增统一 Provider Adapter 接口和确定性的 `MockProvider`，可模拟成功、失败、限流、升级、成本和时延。默认配置无法产生付费调用。
- 页面新增 MetaPlanner 回执卡，展示选择原因、淘汰路线、角色组合、预算、调用数、时延和管理深度。
- 新增 `GET /api/v1/planning/policy` 与 `GET /api/v1/runs/{id}/planning`；规划回执随权威 Run Projection 和事件流持久化，不创建第二套状态事实源。
- Projection / Runtime Schema 升级为 `v5.1`，migration 009 增加规划事件审计索引；可直接恢复 v4.9 投影。

> 能力边界：v0.5.1 的模式选择仍是确定性、可重复的离线场景仿真，用来验收合同和产品交互；基于难度向量、VoD、置信区间和实时容量的 Planning Governor 属于 v0.5.2。当前版本不调用真实模型，不产生任何付费 API 请求。

升级说明见 [`docs/V0.5.1_MIGRATION.md`](docs/V0.5.1_MIGRATION.md)，完整 v0.5 路线见 [`docs/V0.5_UPGRADE_PLAN.md`](docs/V0.5_UPGRADE_PLAN.md)。

## v0.4.9：整合、加固与 v0.4 完成版

v0.4.9 把 v0.4.1～v0.4.8 的能力收束成一个更适合发布和长期升级的独立产品：

- 模型目录统一使用 `/api/v1/catalog`，旧 `/api/catalog` 保留兼容并返回弃用提示。
- 运行数据库升级到 Schema `v4.9` / migration 008；事件负载和完整信封都写入 SHA-256 完整性记录，读取与重放时验证。
- 提供运行库健康诊断、经过 SQLite 在线快照验证的备份，以及仅允许停机执行的安全恢复。
- 本地 HTTP 服务增加 10 秒请求读取时限、守护工作线程、有限监听队列、JSON 深度/节点上限、Host/Origin 检查和 CSP 等响应头。
- 过期资源租约会自动释放；已释放的执行尝试不能伪装成成功重放；实际成本按尝试分别归因。
- 媒体批准真正传递并持久化 `Idempotency-Key`，并用运行时锁避免并发重复渲染。
- 所有相邻镜头都用已批准终稿或动态分镜检查首尾边界，同时保留无生成依赖镜头的并行能力。
- Artifact API 明确标记 `metadata-only`，不通过公共接口返回产物内容。

> CostWeave v0.4.9 仍是离线决策与编排原型，不调用真实模型或付费媒体 API。外部模型证据和付费媒体适配器在接入前仍须重新进行信任边界与幂等性验收。

**以端到端成本、资源准入和不可变成果为边界，可解释、可恢复且由证据验收驱动的多模型 Agent 编排原型。**

CostWeave v0.4.8 新增受控图片、短视频和 AI 电影生产工作流：先锁定创作意图并形成视觉圣经，再把影片拆成可并行镜头合同，自动编译提示词，在关键帧和低清动态分镜获批后才进入昂贵终稿；失败镜头只在必要范围内重做，并按合格镜头和合格视频秒核算成本。

> v0.4.8 仍是离线决策与编排原型，不调用真实模型或付费媒体 API，也不生成真实图片或视频。媒体适配器只生成可审计元数据；DAG、审批门、连续性规则、局部恢复、成本账本、持久化和 API 是真实实现。

## v0.4.8 新增：受控图片 / 视频制作工作流

- 自动识别单图、图组、短视频和电影需求；30 秒视频默认形成 6～10 个镜头
- 单图走轻量流程，不为简单任务建立电影级 DAG
- 建立 Creative Brief、Film/Character/Location Bible 和 Asset Registry
- 每个镜头使用结构化 `ShotContract`，锁定创作意图、规格、引用、首尾状态和验收条件
- `PromptCompiler` 继承视觉圣经并输出版本、目标适配器和语义差异；禁止静默改写锁定意图
- 关键帧、低清动态分镜与终稿采用分级成本门；supervised 模式下未批准预览时终稿次数严格为 0
- 独立镜头并行；共享参考帧的镜头只等待必要依赖
- 连续性检查覆盖技术格式、角色身份、场景、动作方向和首尾帧
- 失败镜头局部重渲染；只有边界变化才把相邻镜头标记为待复核
- 成本账本分别记录故事板、关键帧、动态分镜、终稿、重渲染和连续性验收
- 媒体项目、圣经、镜头、Prompt、渲染尝试、连续性检查和媒体成本全部持久化
- 新增媒体查询与批准 API，页面提供非技术化媒体生产控制台
- `COSTWEAVE_MEDIA_WORKFLOW=0` 可停止创建新媒体项目，通用编排保持可用

## 继承的 v0.4.7 多结局预测、校准与评估闭环

- 用五种互斥结局替代伪精确单点成功率，概率总和严格为 1
- 显示首次、修复、升级和人工介入后的单调累计成功率
- 可设种子的 DAG 蒙特卡洛覆盖串行、并行、恢复和共同故障因子
- 同时输出质量、成本、完成时间 P50/P95，以及关键错误放行概率
- 显示预测置信度、样本量、OOD 标记、OOD 原因和主要失败模式
- `OutcomeRecord` 强制归因模型、Prompt、工具、策略、目录数据和计划版本
- 模型能力按“模型×任务×风险 → 模型×任务 → 模型 → 全局”四级 Beta 后验学习
- 样本不足时保守回退，不允许单次运行直接覆盖生产先验
- 正式指标包含 Brier、ECE、Log Loss 和区间覆盖率
- 冻结基准比较 CostWeave v0.4.7、v0.3 未校准、固定模型和人工阈值
- 新增预测与评估 API；页面显示五结局、区间、置信度和分布外提醒
- `COSTWEAVE_OUTCOME_FORECAST=0` 可停止新预测与学习写入，核心运行不受影响

## 继承的 v0.4.6 证据驱动验收与分级恢复

- 每个任务生成版本化 `SuccessContract`，明确关键标准、普通质量标准、最低分数和证据要求
- 关键标准失败不能被其他高分标准平均掉；执行模型自评不能单独解锁关键下游
- Schema、确定性检查、外部证据、专业验证和高风险高级审查形成分级验证
- 每条标准记录通过状态、证据引用、证据独立性与失败原因
- 验收结果记录假阳性和假阴性风险，为 v0.4.7 校准闭环提供可靠标签
- 失败被确定性分类为瞬时故障、能力不足、合同错误、根假设错误、证据不足或策略风险
- 瞬时故障只重试当前节点，能力不足只升级当前节点，合同错误替换相关子图，根假设错误才全局重规划
- `PlanPatch` 由规划层拥有且每次只推进一个计划修订，执行器不能直接改写计划
- 无关且已验证的兄弟 Artifact 不因局部或全局恢复而失效
- 恢复达到次数、任务尝试、时间或剩余预算上限后确定性停止
- 新增验收与失败查询 API，页面展示关键标准、逐条证据、误验风险和恢复记录
- `COSTWEAVE_EVIDENCE_RECOVERY=0` 可关闭高级恢复并“失败即终止”，拒绝结果仍保持拒绝

## 继承的 v0.4.5 端到端成本经济与资源组合优化

- 为每个节点生成模型、Provider、资源池、推理强度、量化和缓存策略的有限候选前沿
- 质量下界、期限和预算作为硬约束；经济、均衡和极速模式使用不同选择目标
- 成本分解覆盖模型调用、本地算力、能源、稀缺容量、排队机会、交接、验收合并和预期返工
- 本地模型不再被视为“免费”；排队和稀缺资源也会进入机会成本
- 弱模型失败、检测、重做和下游失效的期望损失会与强模型直接调用成本比较
- 任务保留两个备用 Placement；切换需要越过绝对与相对收益迟滞，避免来回抖动
- 验收节点继续优先使用独立模型，高风险任务进一步要求 Provider 独立
- 正常执行只使用预算的一部分，预留可配置恢复预算
- `placements` 和 `cost_ledger` 持久化预测与实际归因；页面展示成本组成及 P95
- `COSTWEAVE_PLACEMENT_V4=0` 可回退到 v0.3 风险调整路由，既有审计记录仍可读取

## 继承的 v0.4.4 可靠并行运行时与资源准入

- 就绪节点按关键路径长度、优先级和关键度排序，并保留 FIFO 回滚策略
- CPU、GPU、显存、并发槽和队列容量在任务执行前检查
- 资源租约与预计成本使用 SQLite `BEGIN IMMEDIATE` 原子预留
- 同一 `run + task + attempt` 重试只会得到同一份预留，不重复占用
- 期限明显无法满足或资源要求超过池上限时，在首次执行前拒绝
- Token Bucket 控制吞吐，Provider 熔断器支持 closed、open、half-open
- 暂时性 Provider 错误有限重试，成功后复位熔断状态
- 需要审批的节点生成持久审批单；其他无依赖分支继续执行
- 重启时安全释放遗留租约，资源与成本不会永久悬挂
- 页面显示资源预留、活跃租约、在途成本、排队和等待审批状态
- 新增资源与审批 API
- `COSTWEAVE_PARALLEL_RUNTIME=0` 可回到 v0.4.3 兼容执行路径

## 继承的 v0.4.3 Artifact Graph 与结构化交接

- 每个任务输出内容寻址、不可变且带 Schema 的 Artifact
- Artifact 记录生产任务、模型、计划版本、尝试次数、敏感级别和输入血缘
- 内容变化生成新 Artifact ID，不覆盖旧内容
- 上游 Artifact 必须通过哈希与 `validated` 状态检查后才能进入交接包
- `MessageEnvelope` 明确目标、范围、排除项、输入引用、输出规格和验收标准
- 重复交接消息幂等，不会造成重复执行
- hard、soft、validation 三类任务依赖
- 图编译阶段拒绝环、缺失生产者、Schema 冲突和权限冲突
- 重规划计算受影响子图，只把相关 Artifact 标记为 stale
- 新增任务、Artifact、运行 Artifact 列表和血缘图 API
- 页面新增“成果与血缘”视图
- `COSTWEAVE_ARTIFACT_DAG=0` 可关闭新写入并继续读取既有数据库

## 继承的 v0.4.2 可靠控制内核

- SQLite WAL 和版本化 Schema migration
- `Command → Event → Projection` 原子事务
- `runs`、`tasks`、`attempts`、`commands`、`events` 和 `projections` 持久化
- 每个运行独立、连续的 stream revision 与 `expected_revision` 乐观并发
- Command ID 和 Idempotency-Key 双重去重；相同请求返回首次结果
- 损坏投影自动回退到追加事件流重放
- 未完成运行启动恢复；已验收节点不重复执行
- 显式 `cancelling → cancelled` 终态和幂等取消
- `/api/v1/runs`、事件时间线、取消和重试接口
- `COSTWEAVE_RUNTIME_V4=0` 可回退到 v0.4.1 内存运行时，已有事件库保持只读

## 继承的 v0.4.1 合同与状态守卫

- `RunId`、`TaskId`、`EventId`、`ArtifactId` 使用可校验的统一格式
- 旧语义 ID 通过确定性哈希映射为稳定 v4 ID，不破坏 v0.3 顶层 API
- `CommandEnvelope` 与 `EventEnvelope` 使用 `schema_version: "v4.1"`
- Envelope 严格校验字段、ID、时间、事件类型和有限 JSON 数据
- 运行与任务状态使用显式合法转换表；终态不能回退
- 重复写入同一状态按无副作用操作处理
- 所有引擎状态赋值都经过兼容守卫
- 接受和拒绝的转换都会记录来源、前后状态与拒绝原因
- `COSTWEAVE_CONTRACTS_V4=0` 可关闭守卫并恢复 v0.3 行为
- 不新增数据库、迁移器、调度器、模型调用或页面主流程

## 继承的 v0.3 核心能力

### 可维护的模型知识库

- 页面直接新增、编辑和删除模型
- JSON / CSV 原子导入；支持合并或完整替换
- JSON / CSV 导出和恢复内置基线
- 字段、类型、范围、URL、货币、Token 上限和重复 ID 校验
- 每次修改产生新的目录修订号，并通过乐观锁防止覆盖他人修改
- 临时文件加原子替换持久化；保存失败时继续使用旧目录
- 持久化目录损坏时回退内置基线，页面仍可打开并执行恢复默认
- 新任务立即读取最新目录；运行中的任务固定使用启动时不可变快照
- 默认数据保存在 `~/.costweave/model_catalog.json`
- 可通过 `COSTWEAVE_CATALOG_PATH` 指定便携目录或测试目录

### 分层任务理解

- 独立评分的多标签专业域，而不是任意关键词命中
- 主意图：构建、分析、研究、决策、转换、验收、故障诊断
- 交付物：代码、报告、方案、数据集、决策、译文和视觉成果
- 显式约束抽取：预算、期限、格式、本地、开源、并行等
- 分类结果包含分数、触发证据、分类置信度与词表版本
- 复杂度拆成认知、协作、证据、工具、领域和范围六个维度
- 医疗、法律、金融、安全等风险结合“动作 + 对象”判断，减少只因领域名词而误报
- 每个专业分支拥有独立难度、关键度、不确定性和能力权重

### 风险调整路由

- 工具能力严格匹配；`function_calling` 不再被错误视为 `code_execution`
- 模型目录资料时效和 `data_confidence` 参与预测
- 同时输出成功率均值、保守下界和预测不确定性
- 质量门槛约束保守下界，而不是乐观均值
- 模型淘汰会保留上下文、工具、模态、货币、输出上限和能力短板原因
- 组合路由先求满足质量与独立验收约束的最低成本，再按边际收益升级
- 验收节点优先使用不同模型；高风险节点进一步优先不同 Provider
- 路由结果记录准确目录修订号，支持复现

### 可执行验收闭环

- 验收、风险、安全和高级规划节点必须返回逐条 `criteria_results`
- 只有 `decision=pass` 才能解锁下游
- `revise`、`reject` 和 `human_review` 不会再被格式校验误判为通过
- 当前成功率根据任务保守先验和实际验收证据重新计算
- 失败后不再无条件抬高成功率
- 重规划仍固定使用本次运行的目录快照，避免中途模型资料变化

## 快速运行

需要 Python 3.11 或更高版本，不需要第三方依赖。

```bash
python -m costweave.server
```

v0.5.6.3 默认保留智能交接、媒体工作流、Outcome 预测、证据恢复、Placement、合同守卫、持久运行时、Artifact DAG、并行运行时、评测协议、Outcome 治理和 Shadow 存储。真实 Adapter 仍默认关闭；评测与数据集命令均为显式离线命令，不会自动执行、训练或晋级策略。只关闭智能交接与自适应恢复，回到 v0.5.4 兼容路径：

```powershell
$env:COSTWEAVE_HANDOFF_INTELLIGENCE="0"
python -B -m costweave.server
```

只关闭媒体工作流：

```powershell
$env:COSTWEAVE_MEDIA_WORKFLOW="0"
python -B -m costweave.server
```

只关闭 Outcome 预测与学习写入：

```powershell
$env:COSTWEAVE_OUTCOME_FORECAST="0"
python -m costweave.server
```

只关闭 v0.4.6 高级恢复并保留严格拒绝：

```powershell
$env:COSTWEAVE_EVIDENCE_RECOVERY="0"
python -m costweave.server
```

只关闭 v0.4.5 联合成本决策、回到 v0.3 风险调整路由：

```powershell
$env:COSTWEAVE_PLACEMENT_V4="0"
python -m costweave.server
```

只关闭 v0.4.4 资源准入、回到 v0.4.3 执行路径：

```powershell
$env:COSTWEAVE_PARALLEL_RUNTIME="0"
python -m costweave.server
```

只关闭 Artifact DAG、保留 v0.4.2 运行时：

```powershell
$env:COSTWEAVE_ARTIFACT_DAG="0"
python -m costweave.server
```

关闭持久运行时、回到 v0.4.1 内存模式：

```powershell
$env:COSTWEAVE_RUNTIME_V4="0"
python -m costweave.server
```

同时关闭合同守卫并回退到 v0.3 状态赋值语义：

```powershell
$env:COSTWEAVE_CONTRACTS_V4="0"
python -m costweave.server
```

打开：

```text
http://127.0.0.1:8765
```

安装命令行入口：

```bash
python -m pip install -e .
costweave
```

## 模型管理

页面底部的“模型管理中心”支持：

1. 搜索和按 Provider、路由状态筛选。
2. 新增模型，填写厂商事实与 CostWeave 路由先验。
3. 编辑价格、上下文、工具、能力、来源和核验日期。
4. 导入 JSON 或 CSV。
5. 导出当前目录作为备份或迁移文件。
6. 恢复内置模型基线。

编辑后的模型会被标记为“用户修改”。核验日期超过 180 天会在页面标记“资料过期”。非 USD 云模型若没有汇率快照必须保持不可路由，但仍可用于资料展示。

### JSON 导入

可以导入完整导出文件：

```json
{
  "metadata": {
    "schema_version": 1
  },
  "models": [
    {
      "id": "example-model",
      "name": "Example Model",
      "provider": "example",
      "specialty": "分析与结构化输出",
      "capabilities": {
        "analysis": 0.82,
        "planning": 0.78,
        "validation": 0.8
      },
      "cost_per_task": 0,
      "latency_factor": 0.8,
      "reliability": 0.9
    }
  ]
}
```

缺省字段会使用安全默认值。导入模型默认被标记为用户数据；任意一项校验失败时整批导入回滚。

### CSV 导入

建议先从页面导出 CSV 模板，再修改后重新导入。`capabilities` 是 JSON 对象；`modalities`、`tools`、`strengths` 和 `limitations` 可使用 JSON 数组，或用 `|` 分隔。

## 决策流程

```text
用户目标
  ↓
分层画像：意图 / 专业域 / 交付物 / 约束 / 风险 / 六维复杂度
  ↓ 低置信、高难或高风险
高级规划复核门
  ↓
任务级合同、能力权重、关键度与并行 DAG
  ↓
冻结当前模型目录修订
  ↓
硬约束过滤 → 成功均值 / 保守下界 / 不确定性
  ↓
最低可行组合 → 独立验收约束 → 预算内边际升级
  ↓
结构化 MessageEnvelope → 校验输入 Artifact → 并行专业执行
  ↓
不可变输出 Artifact → 逐条验收 → 冲突审查 → 最终汇总
  ↓ 失败证据
受影响子图分析 → 仅将相关 Artifact 标记过期 → 保留有效兄弟成果
  ↓
同快照重新路由 → 恢复执行
```

## 项目结构

```text
costweave/
├── contracts_v4.py    # 统一 ID、版本化命令与事件 Envelope
├── state_guard.py     # 运行/任务状态转换表与纯函数守卫
├── state_machine.py   # 持久内核使用的统一状态机出口
├── commands.py        # 命令类型、幂等键和规范请求指纹
├── event_store.py     # SQLite WAL、事务、投影和事件重放
├── control_kernel.py  # 唯一持久状态写入者与启动恢复
├── projections.py     # 稳定投影序列化和 RunRecord 水合
├── artifacts.py       # 不可变 Artifact、内容哈希和完整性检查
├── artifact_store.py  # Artifact、血缘、交接消息与图持久化
├── messages.py        # 版本化模型交接 MessageEnvelope
├── graph_compiler.py  # 依赖、生产者、Schema、权限和影响范围检查
├── scheduler.py       # 关键路径就绪队列、准入和暂时性错误重试
├── admission.py       # 容量、期限、背压、Token Bucket 与熔断
├── resource_inventory.py # 仿真资源池与容量快照
├── reservations.py    # 资源和预计成本的原子租约
├── approvals.py       # 非阻塞持久审批
├── contracts.py       # v0.4.x 公共合同入口
├── migrations/        # 版本化运行数据库 Schema
├── compat_v4.py       # v0.3/v0.4.1 兼容与功能开关
├── domain.py          # 运行、模型、任务画像、合同与计划
├── model_catalog.py   # 可恢复的内置模型基线
├── catalog_store.py   # 校验、CRUD、导入导出、版本与持久化
├── planner.py         # 分层画像、复杂度、风险、DAG 与升级门
├── router.py          # 快照、硬约束、保守预测和组合优化
├── evaluation_protocol.py # 实验、任务、共享预算、执行隔离和盲评合同
├── baseline_runner.py # 五基线 Runner、E0 Fixture、比较指标和不可变导出
├── evaluation_store.py # 评测报告与 observation matrix 幂等持久化
├── benchmarks/        # 固定种子任务和评测入口
├── validator.py       # DAG、Schema、逐条标准和门控语义
├── executor.py        # 离线结构化模拟执行器
├── engine.py          # 快照冻结、并行、预算、证据概率与重规划
├── server.py          # 本地 HTTP API 与静态页面
└── web/               # 任务工作台和模型管理中心
```

## HTTP API

运行数据接口默认属于 `local-default` 项目；需要隔离时，在所有相关请求中使用稳定的 `X-CostWeave-Project`，并可用 `X-CostWeave-Data-Residency` 记录驻留标签。项目 ID 不属于认证凭据，因此服务仍只允许绑定本机回环地址。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 运行状态、版本、合同 Schema、功能开关和目录修订 |
| `GET` | `/api/catalog` | 当前模型目录和元数据 |
| `GET` | `/api/catalog/schema` | 字段与导入格式 |
| `GET` | `/api/catalog/export?format=json` | 导出 JSON |
| `GET` | `/api/catalog/export?format=csv` | 导出 CSV |
| `POST` | `/api/catalog/models` | 新增模型 |
| `PUT` | `/api/catalog/models/{id}` | 编辑模型 |
| `DELETE` | `/api/catalog/models/{id}` | 删除模型 |
| `POST` | `/api/catalog/import` | 合并或替换导入 |
| `POST` | `/api/catalog/reset` | 恢复内置目录 |
| `POST` | `/api/runs` | 创建任务画像、计划和模拟运行 |
| `GET` | `/api/runs` | 最近运行 |
| `GET` | `/api/runs/{id}` | 计划、路由、事件、成本和成果 |
| `POST` | `/api/v1/runs` | 使用 `Idempotency-Key` 幂等创建持久运行 |
| `GET` | `/api/v1/runs/{id}` | 获取带 stream revision 的运行投影 |
| `GET` | `/api/v1/runs/{id}/events` | 按 revision 分页读取持久事件流 |
| `GET` | `/api/v1/runs/{id}/artifacts` | 读取运行的 Artifact 元数据 |
| `GET` | `/api/v1/runs/{id}/graph` | 读取任务依赖与 Artifact 血缘图 |
| `GET` | `/api/v1/runs/{id}/validations` | 读取逐标准验收、证据和误验风险 |
| `GET` | `/api/v1/runs/{id}/failures` | 读取失败分类、影响范围与恢复决策 |
| `GET` | `/api/v1/runs/{id}/forecast` | 读取五结局、质量/成本/时间区间与 OOD |
| `GET` | `/api/v1/runs/{id}/planning` | 读取 MetaPlanner / MultiPlanner 决策回执 |
| `GET` | `/api/v1/runs/{id}/economics` | 读取跨 Provider 准入、成本、后验和路由回执 |
| `GET` | `/api/v1/runs/{id}/handoffs` | 读取 WorkOrder、最小上下文清单、合同摘要与接收方预检 |
| `GET` | `/api/v1/runs/{id}/recovery` | 读取 Pcomplete 检查点、污染分析、恢复门禁与全局重规划余额 |
| `GET` | `/api/v1/evaluations` | 读取冻结基准、Brier、ECE、Log Loss 与覆盖率 |
| `GET` | `/api/v1/evaluation/protocol` | 读取 v0.5.6.1 任务集、五组基线、证据等级和零真实调用边界 |
| `GET` | `/api/v1/outcomes/governance` | 读取 v0.5.6.2 标签来源、隔离、删除、冻结训练和零策略晋级边界 |
| `GET` | `/api/v1/experiments/policy` | 读取 v0.5.6.3 双开关、硬预算、密钥与 Shadow 零干预边界 |
| `GET` | `/api/v1/predictors/policy` | 读取数据准入、离线评估以及 v0.5.6.5 策略候选、IPS/SNIPS/DR、探索禁区、冻结门和零晋级边界 |
| `GET` | `/api/v1/portfolio/policy` | 读取 v0.5.6.4.3 全局 DAG 组合目标以及 v0.5.6.4.4 精确 Oracle、差分验收、敏感性和零路由权边界 |
| `GET` | `/api/v1/replay/policy` | 读取 v0.5.7.1 冻结版本回放、多基线、近似 Oracle、后悔值、未知反事实和零干预边界 |
| `GET` | `/api/v1/shadow/policy` | 读取 v0.5.7.2 输入快照对齐、容量新鲜度、失败隔离、覆盖率、开销门和零副作用边界 |
| `GET` | `/api/v1/policies/registry` | 读取 v0.5.7.3 签名策略生命周期、分级晋级门、注册表指针、冻结/回滚和零运行时干预边界 |
| `GET` | `/api/v1/canary/policy` | 读取 v0.5.7.4 阶段、硬回滚门、任务策略快照、当前运行时指针和零高风险探索边界 |
| `GET` | `/api/v1/security/policy` | 读取 Adapter 白名单、网络/密钥/项目/幂等边界和当前项目状态 |
| `GET` | `/api/v1/resilience/status` | 读取韧性策略、不可变实验、事件完整性、备份与恢复诊断 |
| `POST` | `/api/v1/resilience/campaigns` | 使用幂等键记录 E0/E1 离线或回放故障实验；拒绝真实、付费和外部调用 |
| `GET` | `/api/v1/release/status` | 全量重放并读取 v0.5 稳定版硬门、已验证范围和未验证结论 |
| `POST` | `/api/v1/release/attestations` | 显式确认后保存项目范围、幂等且不可变的工程稳定证明 |
| `POST` | `/api/v1/projects/{project_id}/revoke` | 使用项目头、确认文本和幂等键执行不可变逻辑撤销 |
| `GET` | `/api/v1/evidence/console?run_id={id}` | 读取全局或单运行统一证据视图、来源血缘、证据等级和商业结论门 |
| `GET` | `/api/v1/evidence/exports?limit={n}` | 读取不可变实验包导出收据 |
| `POST` | `/api/v1/evidence/exports` | 导出脱敏实验包并保存 SHA-256 收据；可选 `run_id` |
| `GET` | `/api/v1/media/projects/{run_id}` | 读取媒体意图、圣经、镜头、渲染和单位成本 |
| `GET` | `/api/v1/media/projects/{run_id}/shots` | 读取结构化镜头合同、Prompt 与渲染尝试 |
| `POST` | `/api/v1/media/projects/{run_id}/approve` | 批准预览并按 DAG 生成全部仿真终稿 |
| `POST` | `/api/v1/media/shots/{shot_id}/approve` | 只批准并生成一个镜头的仿真终稿 |
| `GET` | `/api/v1/runs/{id}/placements` | 读取选定与备用资源组合及成本预测 |
| `GET` | `/api/v1/tasks/{task_id}` | 读取持久任务合同和状态 |
| `GET` | `/api/v1/artifacts/{artifact_id}` | 校验并读取 Artifact 元数据与血缘（不返回内容） |
| `GET` | `/api/v1/resources` | 读取资源池、容量和当前可用量 |
| `GET` | `/api/v1/resources/ledger?run_id={id}` | 读取预测与实际端到端成本账本 |
| `GET` | `/api/v1/approvals?status=pending` | 按状态读取审批单 |
| `POST` | `/api/v1/approvals/{id}/resolve` | 用 revision 与幂等键批准或拒绝节点 |
| `POST` | `/api/v1/runs/{id}/cancel` | 使用幂等键和 `expected_revision` 取消 |
| `POST` | `/api/v1/runs/{id}/retry` | 为失败或取消运行创建关联重试 |

所有模型写操作都可携带 `expected_revision`。目录已经变化时返回 `409 catalog_revision_conflict`。

## 测试

```bash
python -B tests/run_isolated.py
```

当前基线为 706 项自动化测试。v0.5.8.4 新增 12 项稳定版专项验收，覆盖 migration 029、全部运行重放、事件篡改发现、默认调用硬门、不可变发布证明、幂等冲突、项目隔离、公开证据边界、全新离线安装验证和 Release API；此前 694 项继续作为韧性、安全边界、统一证据、Canary、策略注册表、Production-like Shadow、离线回放、治理、评测、交接、MultiPlanner、媒体、成本、恢复、预测和页面回归门。

测试模块采用独立进程运行，避免一个集成测试的后台编排线程、全局时钟或临时数据库状态污染后续模块；任一模块失败或超过 180 秒都会使总命令返回失败。

测试覆盖：

- 模型 CRUD、JSON/CSV 往返、原子导入和 revision 冲突
- 新运行热加载、进行中运行快照隔离
- 分类消歧、六维复杂度和任务级难度
- 显式工具约束、保守质量门槛和验收模型独立性
- `reject` 门控、安全边界、预算、异步并行和重规划
- 统一 ID、Envelope JSON 往返、非法转换、终态保护和开关回滚
- 固定事件流 100% 重放、命令去重、CAS 冲突和跨运行任务 ID 隔离
- 完成与未完成运行重启恢复、已验收成果保留和终态不重启
- Artifact 哈希、不可变版本、血缘、过期传播和结构化交接
- DAG 环、生产者、Schema、权限与 stale plan revision 拒绝
- 资源准入、原子租约、预算预留、熔断、背压和期限前置拒绝
- 非阻塞审批、暂时性错误重试、启动租约恢复和调度策略回滚
- 关键标准不可平均、自评不可解锁、失败分类与分级恢复
- 恢复次数、尝试次数、时间、预算上限和 PlanPatch 持久化
- 多结局概率守恒、固定种子、DAG 时间结构和分布外检测
- Outcome 完整归因、幂等学习、四级 Beta 后验与正式评估指标
- 单图轻量路径、30 秒影片拆镜、预览门、镜头并行与局部重渲染
- 媒体锁定意图零漂移、连续性缺陷、有效秒成本和媒体 API
- 页面结构、响应式基础规则和管理交互绑定
- MetaPlanner 回执、淘汰路线、跨 Provider 角色、零付费 Adapter 和两层管理上限
- 难度区间、ExpectedLoss、VoD、容量预检、执行前升级和六路线可解释性
- WorkOrder、接收方预检、最小上下文、Pcomplete、污染隔离和恢复权限门禁
- 35 个评测种子任务、五基线完整矩阵、盲评身份隔离、E0 证据边界和不可变 JSONL/CSV 导出
- Outcome 匿名学习副本、标签证据追加、冲突保留、项目隔离、去重权重、TTL/删除审计和训练硬冻结
- 全局 DAG 组合损失、强上游投入、并行关键路径、共享配额/容量、相关失败、独立验收和拓扑场景比较
- 小图精确最优/不可行证明、Beam 差分 Regret、超界失败关闭和离散参数敏感性
- 策略候选版本绑定、IPS/SNIPS/DR 已知值、共同支持、有效样本、探索禁区、冻结门、策略卡和带墓碑删除
- 历史版本冻结、多基线确定性回放、近似 Oracle、观测覆盖、未知反事实、Routing Regret、选择偏差与不可变证据链
- Shadow 输入/目录/容量快照对齐、旁路建议、容量过期、失败隔离、覆盖/缺失原因、成本与时延开销和零副作用证据链
- 签名策略版本、指定质量/CPS 基线、四级证据门、状态机、批准、策略卡、防并发过期、一键冻结、原子注册表回滚和零运行时激活
- Canary 5/25/50/100 逐级门、确定性分桶、零高风险探索、启动策略快照、严格回滚边界、旧任务隔离和新任务立即回稳
- Adapter 插件白名单、密钥租约、共享脱敏、Prompt/工具边界、端点防 SSRF、项目隔离/撤销和外部副作用幂等
- 限流/宕机/显存不足故障注入、30 秒内熔断转移、deadline miss 预警、恢复检查点、并发泄漏、事件完整性和逻辑备份恢复
- Runtime Schema/migration 稳定门、全量 EventLog 重放、全新环境离线执行、不可变发布证明和发布资产完整性

## 当前边界

- 可导入最新资料不等于资料已被自动核实；来源、核验时间和能力先验仍由导入者负责。
- 能力、速度、可靠性和资料可信度是可校准先验，不是厂商官方基准成绩。
- 当前没有汇率服务或厂商阶梯价格；批处理、缓存与量化仅定义安全策略接口，尚未连接真实推理后端。
- 当前执行器不调用真实模型，因此不能证明最终领域答案质量。
- 运行 EventStore 默认保存在 `~/.costweave/runtime-v4.sqlite3`，可用 `COSTWEAVE_RUNTIME_DB` 指定路径。
- v0.5.8.4 Stable 仍是单进程、单机 SQLite 控制内核，不提供分布式事件总线、多进程调度或公网身份认证。
- 当前 Planning Governor 是确定性的规则与损失决策内核，尚未通过真实生产数据完成校准。
- 资源池、CPU/GPU/显存和 Provider 吞吐目前是目录派生的仿真值，不是实时硬件遥测。
- 重启恢复按单实例所有权释放遗留租约；多实例租约续期与节点所有权尚未实现。
- 实验 HTTP Adapter 已定义并测试 Provider 幂等转发与歧义失败关闭，但默认关闭且未完成真实 Provider 兼容性验收。
- 失败分类与 PlanPatch 当前采用可审计确定性规则；尚未用真实生产失败数据完成统计校准。
- 固定基准中的 ECE、Brier 和 95% 覆盖率只证明评估实现与仿真数据一致，不代表真实模型生产表现。
- 部署后验已参与后续路由校准，但不会因单次 Outcome 训练模型权重；真实流量仍需样本门槛、漂移监控和受控策略发布。
- 媒体适配器只生成元数据，不代表真实图像/视频质量；肖像、声音、音乐和素材授权仍需人工处理。
- 当前服务只允许回环地址，不应直接暴露到公网。

v0.5.1～v0.5.8.4 的工程合同已经完成，并收束为可安装、可重放、可验证的 v0.5 Stable Engineering Release；v0.5.6.3 的真实效果退出门槛仍未通过，真实效果样本继续为 0。未取得真实证据和真实 Provider 路由负载前，Canary 与真实连接默认关闭，证据控制台会把真实收益保持为缺失。下一阶段不是自动照搬旧路线图，而是重新评审 v0.6 Production Optimization 的范围、成本和验证顺序。总体设计与完成状态见 [`docs/V0.5_UPGRADE_PLAN.md`](docs/V0.5_UPGRADE_PLAN.md)，后续门槛见 [`docs/REMAINING_ROADMAP.md`](docs/REMAINING_ROADMAP.md)。

为避免一次性实现过大蓝图，v0.4 已拆分为 v0.4.1～v0.4.9 九个可独立验收的稳定版本；v0.4.1～v0.4.9 已全部完成。详见 [`docs/V0.4_INCREMENTAL_RELEASES.md`](docs/V0.4_INCREMENTAL_RELEASES.md)。

## License

MIT
