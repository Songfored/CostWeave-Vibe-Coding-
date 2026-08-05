# CostWeave v0.5.8.1 架构说明

v0.5.8.1 在既有控制面之上增加只读的 Unified Evidence Console。它不改变 Planner、路由、执行、Canary 或回滚决定，而是把分散的数据转换成统一证据合同，并拒绝用预测、模拟或缺失值替代真实效果。

```text
Run/EventLog ───────┐
Planning/Placement ─┤
Ledger/Capacity ────┤
Outcome/Evaluation ─┼──> Evidence Assembler
Shadow/Registry ────┤       actual / predicted / simulated / missing
Canary/Rollback ────┘       E0..E5 + source + version + n + freshness
                                |
                     +----------+----------+
                     |                     |
             Global / per-run view   Business claim gates
                     |               quality / CPS / P95
                     |                     |
                     +----------+----------+
                                v
                    bounded redacted export package
                       canonical SHA-256 receipt
                                |
                    append-only migration 026 store
```

证据类别是互斥语义，不是视觉标签：`actual` 只表示确实由当前控制面记录的事实，不自动代表真实模型效果；`predicted` 表示执行前估计；`simulated` 表示夹具、仿真或离线估算；`missing` 表示没有达到定义所需的数据。比如运行数和外部 Provider 尝试数可以是 E1 actual，但没有独立验收与账单对账时，真实成功率和真实 CPS 仍必须是 E0 missing。

每个来源记录 API 路径、数据版本、记录数、新鲜度、范围和确定性内容哈希。单运行视图沿用同一合同，并额外汇总规划模式、Planner panel、路由策略、Placement、预测成本/时延/成功率、模拟账本和实际控制面耗时；控制面耗时明确不等于外部 Provider 端到端时延。

商业结论门与指标展示分离。真实质量、成本和响应速度必须分别具有 E2+ 独立真实样本、共同口径基线、足量样本与置信区间；当前全部失败关闭，样本量为 0，任务族为空。导出最多包含 20 个运行和 1,000 个事件，默认排除密钥、Prompt 原文、用户标识和 Artifact 内容，只包含结构化合同、元数据和来源快照。

导出包先规范化序列化再计算 SHA-256；migration 026 保存包哈希、范围、记录计数和生成时间，并用触发器拒绝更新或删除。同一包哈希幂等复用收据。公开 API 为 `GET /api/v1/evidence/console`、`GET /api/v1/evidence/exports` 和 `POST /api/v1/evidence/exports`。Runtime Schema 为 `v5.8.1`，Run Projection Schema 继续为 `v5.6`。

---

## v0.5.7.4 Bounded Canary Runtime Plane

v0.5.7.4 在签名策略注册表之上增加一个独立的 Bounded Canary Runtime Plane。注册表 `active` 仍只是已审查资产；只有显式启用、候选/稳定版本血缘完整、Canary 门通过且人工批准后，运行时部署才获得受限的低风险策略选择权。

```text
Signed candidate in registry state=canary
stable rollback target in registry state=active
eligible E3+ canary gate + explicit approval + scope
                         |
                         v
             Immutable CanaryDeployment
                         |
                         v
                 5% low-risk stage
                         |
        deterministic hash(run_id + deployment_id)
             +-----------+-----------+
             |                       |
             v                       v
      candidate snapshot       stable/control snapshot
             |                       |
             +-----------+-----------+
                         v
          immutable per-run policy snapshot
             survives restart and later rollback
                         |
                         v
                CanaryWindow guardrails
 success CI / severe errors / budget / permission / P95
 drift / calibration / duplicate side effects / metadata
             +-----------+-----------+
             |                       |
          all pass                 any fail
             |                       |
             v                       v
       25% → 50% → 100%      automatic stable rollback
                                     |
                   in-flight snapshots unchanged
                   new runs use stable immediately
```

分桶不是可变随机数，而是 `sha256(deployment_id:run_id) mod 10000`。任务只有同时满足签名允许任务族、不命中禁止任务族且风险为 normal/low 时才可进入候选；elevated/high 风险的探索率固定为 0。回滚后的新任务仍会写入 `stable-only` 快照，因而“没有候选曝光”和“缺失审计记录”不会混为一谈。

每个运行快照包含部署、阶段、分桶、候选/稳定队列、所选策略 ID/版本和内容哈希。该快照进入 `v5.6` 权威投影；进程重启时恢复同一值。自动回滚只追加运行时指针事件，不改写旧任务，也不扩大 Provider 凭据、工具或数据权限。

阶段决定先执行硬回滚门，再判断样本量。成功率非劣 95% CI 下界必须 `≥-1pp`，严重错误率不得上升，P95 恶化不得超过 10%，预算/权限/重复副作用/高风险候选必须为 0，漂移、校准和策略元数据必须通过。硬门失败即使样本很少也立即回滚；硬门通过但样本不足只保持当前阶段。

migration 025 保存部署、运行时指针、运行分配、观察窗和阶段决定。所有表只追加不可修改；存储层会重新计算确定性分配和自动门决定，拒绝调用方伪造 cohort、跳级或压制回滚。当前仍没有真实 Provider 策略负载与真实 Canary 证据，因此默认开关关闭，外部调用和执行副作用计数保持为 0。

---

## v0.5.7.3 Policy Registry Plane

v0.5.7.3 在 Production-like Shadow 之后增加 Policy Registry Control Plane。它把候选策略、证据、门报告、状态转换、策略卡和回滚目标绑定到签名版本；注册表只管理“哪些资产已被审查”，不拥有运行时路由权。

```text
Signed PolicyManifest
candidate / replay / shadow / code / objective / constraints / scope
                    |
                    v
          Stage-specific Evidence Gate
 real origin / independent / cases / quality / CPS / latency / risk
 calibration / drift / privacy / hard violations / MultiPlanner value
                    |
           blocked  |  eligible
             +------+- - - - - - - - -+
             |                        |
             v                        v
        Policy Card       Immutable State Transition
 reasons / limits         draft → evaluated → shadow
 next evidence stage              → canary → active
             |                        |
             +------------+-----------+
                          |
                  anomaly or regression
                          |
                          v
              atomic freeze + registry rollback
                          |
                          v
 runtime_route_changed=false / execution_side_effect_count=0
```

每个策略版本的内容哈希由 HMAC-SHA256 签名。密钥不入库；注册时重新验签。状态由初始 `draft` 与不可变事件推导，转换包含连续修订号并在事务中防止过期并发写入。`canary`、`active` 和 `retired` 还要求显式批准标识。

证据门按阶段逐步收紧：evaluation 为 E2/30，shadow 为 E3/30，canary 为 E3/200，activation 为 E4/1,000。正式门逐项检查成功率非劣、CPS 置信下界、并行 P95、Handoff 返工、高风险成功率、校准/漂移/隐私和硬违规；不以一个可相互抵消的总分替代硬门。

一键回滚是注册表事务：先冻结当前版本，再把注册表指针指向其签名清单中指定的已审查版本。该动作本身不会切换运行任务；v0.5.7.4 的独立部署授权和运行时指针负责受限选择及自动回滚。

migration 024 保存策略、证据、门报告、转换、卡片、回滚和指针事件。所有表不可修改或删除；数据库约束将运行路线变化、运行时激活/回滚和执行副作用固定为零。

---

## v0.5.7.2 Production-like Shadow Plane

v0.5.7.2 在离线回放之后增加 Production-like Shadow Plane。它与基线共享相同的结构化任务特征、目录快照和容量快照，只计算建议及其预测差值；实际路线已经冻结，Shadow 无法修改任务、Prompt、工具、预算、执行顺序或外部副作用。

```text
Authoritative Baseline Input
task features / catalog / capacity / actual route / E2E estimate
                    |
                    | immutable shared hashes
                    v
          ProductionShadowInput
                    |
      stale / empty / infeasible / budget / deadline gates
                    |
        +-----------+-----------+
        |                       |
        v                       v
 advisory route suggestion    missing reason
 quality / cost / latency     or isolated failure receipt
 capacity / expected loss           |
        |                       |
        +-----------+-----------+
                    v
        baseline_execution_continues=true
        actual_route_changed=false
        execution_side_effect_count=0
                    |
                    v
             Actual baseline Outcome
                    |
                    v
       Comparison + Batch Shadow Audit
 coverage / missing / P50-P95 latency / E2E cost overhead
                    |
                    v
advisory_only=true / routing_authority=false / promotion=false
```

当建议路线与实际基线路线相同，系统可以校验该路线预测误差；当两者不同时，建议路线没有实际 Outcome，必须保持 `counterfactual_outcome_known=false`。系统只展示预测差值，不生成节省声明。

成本开销使用 Shadow 自身计算成本除以基线端到端估计成本，而不是用宽松预算作分母；批量 P95 不超过 10% 才通过开销原则门。容量快照必须与基线哈希一致且未超过新鲜度上限。至少 30 个 E2+ 真实、独立、完整比较案例才允许形成方向性报告。

migration 023 保存输入、决定、失败、实际 Outcome、比较和批量审计。决定与失败互斥，所有表不可修改或删除；数据库约束把外部调用、执行副作用、实际路线变化、路由权、生产晋级和商业声明固定为零。

---

以下为 v0.5.7.1 的 Offline Replay Plane，它继续负责进入 Shadow 前的历史回放与偏差检查。

v0.5.7.1 增加 Offline Replay Plane。它冻结产生历史决定所需的全部版本输入，用多个只读策略重新排序同一候选集，并把选中路线与已观测结果对照。未观测路线始终是未知；所谓近似 Oracle 只是在允许多路线比较的任务子集中，从已执行、已验收路线中选择最低实际总损失。

```text
Historical Version Snapshot
policy / features / contracts / catalog / prompt / tools / capacity
                    |
                    v
             Immutable Replay Case
task scope / risk / candidates / constraints / recorded route
                    |
        +-----------+-----------+
        |           |           |
        v           v           v
 expected loss   direct cost   frozen/recorded baselines
        |           |           |
        +-----------+-----------+
                    v
          Deterministic Replay Results
                    |
        +-----------+-----------+
        |                       |
        v                       v
Observed accepted outcomes    Unknown counterfactuals
        |                       |
        +-----------+-----------+
                    v
          Approximate Oracle + Routing Regret
                    |
                    v
coverage / selected-only bias / propensity / evidence diagnostics
                    |
                    v
advisory_only=true / external_calls=0 / route_intervention=0
```

30 例门槛不是用例数量达到就自动解锁：每个参与比较的策略都必须有至少 30 个方向性 Regret 观测，全部结果须为 E2+ 真实来源、独立验收、完整 propensity，所有实选反事实均已观测，且不存在只记录历史实选路线的偏差。即使通过，报告仍不获得生产晋级或商业声明权。

migration 022 保存快照、回放案例、路线结果、回放决定、近似 Oracle 与 Benchmark 报告。所有记录不可修改或删除；数据库约束把外部调用、路由干预、全局最优声明、生产晋级和商业声明固定为零。

---

以下为 v0.5.6.5 的 Safe Policy Candidate Plane，它仍是回放比较的候选来源之一。

v0.5.6.5 在冻结预测器和全局组合审计之后增加 Safe Policy Candidate Plane。只有通过 v0.5.6.4.2 离线验收的预测器才能形成候选；候选只允许接受离线日志评估，不获得实际路由、自动晋级或商业声明权。

```text
Passing Predictor Artifact + Evaluation
                    |
                    v
          Immutable PolicyCandidate
 data / schemas / code / objective / constraints / scope / rollback
                    |
                    v
       Logged Contextual Bandit Observations
 logging propensity + candidate propensity + reward model + outcome
                    |
                    v
        Common Support & Safety Gate
 scope / E2+ real origin / ESS >= 30 / max weight <= 20
 high-risk + irreversible + permission-sensitive exploration = 0
                    |
                    v
             IPS / SNIPS / DR
       value + delta + interval + diagnostics
                    |
                    v
            Policy Freeze Gate
 PSI / ECE / family success / cost P95 / latency P95 / quality / privacy
                    |
                    v
                Policy Card
 strengths / failures / coverage / unsupported / rollback
                    |
                    v
 advisory_only=true / route_intervention=0 / auto_promotion=0
```

完整动作分布用于证明共同支持：候选给某动作非零概率，而日志策略在同一上下文对该动作概率为零时，IPS/SNIPS/DR 结论被拒绝。有效样本量使用重要性权重计算；权重超过 20 或 ESS 低于 30 同样失败关闭。E2 最多形成离线方向信号，E3+ 才可能进入 v0.5.7 的人工 Shadow 审查。

migration 021 保存策略候选、反事实报告、冻结评估和策略卡。记录不可原地修改；删除必须先写入不可变墓碑，再级联清除候选负载。所有表的权限字段固定为零，旧的 `legacy-resource-optimizer` 仍是回滚和正式路由目标。

---

以下为 v0.5.6.4.4 的 Exact Validation Plane，它继续负责检查小型组合搜索质量。

v0.5.6.4.4 在 Global DAG Portfolio Audit 旁边加入独立的 Exact Validation Plane。生产候选仍由有界 Beam Search 生成；对于足够小的同一问题，精确 Oracle 穷举全部离散组合并复用完全相同的损失函数和硬约束，然后生成差分验收报告。

```text
PortfolioProblem
      |----------------------------|
      v                            v
Bounded Beam Search        Exact small-DAG Oracle
candidate route            exhaustive enumeration
      |                            |
      |----------------------------|
                   v
          Differential Report
 feasibility / selection / objective / routing regret
                   |
                   v
          Sensitivity Probes
 price / success lower / P95 latency / rework cost
 nearest observed route flip in tested points
                   |
                   v
 advisory_only=true / routing_authority=false / intervention=0
```

默认 Oracle 上限为 10 个节点和 100,000 个静态候选组合。界内结果可以证明给定离散输入下的最优或不可行；界外返回 `skipped_bounds`。实现只保留当前最优组合，不把全部可行组合常驻内存。敏感性分析最多接受 32 个探针、128 个测试点，所得“翻转值”只是离散测试点中最接近的已观察翻转，不是连续函数的精确根。

migration 020 保存 Oracle、差分和敏感性报告，并通过外键保证候选审计、Oracle 和敏感性基线引用同一个问题。全部记录带内容哈希，数据库约束固定零正式路由权、零实际干预与零商业声明权。

---

以下为 v0.5.6.4.3 的 Global DAG Portfolio Audit Plane，它仍负责生成待验收的非权威组合建议。

v0.5.6.4.3 新增独立的 Global DAG Portfolio Audit Plane。它接收任务 DAG、部署候选、资源限制和损失参数，在整张图上比较候选组合，而不是让各节点独立追求最低单次价格。

```text
PortfolioProblem
 nodes + edges + deployment options + evidence levels
                 |
                 v
Static fail-closed filters
 capability / residency / success floor / declared resources
                 |
                 v
Bounded deterministic beam search
 budget / deadline / provider quota / parallel pool capacity
 validator independence / two-level management cap
                 |
                 v
Whole-DAG expected loss
 direct + validation + tools + handoff + rework + escalation
 + downstream invalidation + correlated failure + latency + failure loss
                 |
                 v
PortfolioAuditSolution / PortfolioScenarioComparison
 advisory_only=true / routing_authority=false / intervention=0
```

关键路径按照依赖图计算，可并行的兄弟节点只计入较慢分支；资源池容量按拓扑层保守核算。高风险验收要求模型与失败域独立。审计器可比较细粒度拆分和合并拓扑，但搜索是有界 Beam Search，因此只在未发生截断和 Beam 剪枝时标记 `optimality_proven=true`。

migration 019 把审计结果和比较结果保存为不可变、带哈希的记录，并以数据库约束固定 `advisory_only=true`、`routing_authority=false`、`may_change_actual_route=false` 和 `commercial_claims_allowed=false`。当前输入是 E0 规则先验，正式路由仍由 `ResourcePortfolioOptimizer` 掌握。

---

以下为 v0.5.6.4.2 的离线预测器评估层，它继续作为全局审计未来接入真实校准概率之前的数据与证据防线。

v0.5.6.4.2 在准入门之后加入 Offline Evaluation Plane。预测器首先成为绑定数据、代码、Schema、配置和载荷哈希的冻结制品；随后只能在匿名且隔离的 test cohort 上生成离线评估报告。评估同时检查成功概率校准、高风险行为、难度排序、完整成本与端到端时延，不允许用单一平均分掩盖风险退化。

```text
Predictor Admission Report (training_allowed)
                    |
                    v
Frozen Predictor Candidate Manifest
 data + feature + target + code + config + payload hashes
                    |
                    v
Anonymous Test Prediction Observations
                    |
                    v
Offline Evaluation Gate
 Brier / ECE / High-risk ECE / Spearman / P95 coverage / CPS / Time
          |                          |
          | E0/E1 or failed gate     | E2 pass / E3+ pass
          v                          v
       BLOCKED             OFFLINE_PASS / SHADOW_CANDIDATE
                                      |
                                      v
                         Champion-Challenger Comparison
                                      |
                                      v
                        routing_authority=false always
```

E2 通过只说明评估流程得到方向性信号，不能进入 Shadow；E3+ 也只是 Shadow 候选。migration 018 的所有制品、报告和比较均固定 `policy_promotion_allowed=false`，正式路由继续由 `ResourcePortfolioOptimizer` 掌握。

v0.5.6.4.1 在 Outcome 数据治理与 Shadow 路由之间加入不可绕过的 Predictor Admission Plane。`DatasetEvidenceSummary` 只提取匿名化、结构化质量统计；`PredictorAdmissionGate` 对 E2 方向观察、E3 内部校准和 E4 正式基准执行不同门槛；`PredictorAdmissionStore` 通过 migration 017 保存不可变决策。数据不足或质量失败时，系统只生成 `FrozenPredictionReceipt`，不生成概率、不训练、不晋级，也不改变现有正式路线。

```text
Governed Outcome Dataset
          |
          v
DatasetEvidenceSummary -- hash/replay --> PredictorAdmissionReport
          |                                     |
          | blocked / directional-only          | calibration-candidate
          v                                     v
FrozenPredictionReceipt                  future offline fitter
probability=null, interval=[0,1]          (v0.5.6.4.1 does not fit)
          |
          v
ResourcePortfolioOptimizer remains authoritative
```

三档门槛是用途门而不是“样本够了就自动上线”：30 个 E2+ 有效任务只能观察方向；200 个合格有效任务最多允许离线拟合候选；1000 个、至少 8 个任务族且每族 100 个才满足正式基准的数据规模门。任何一档都固定 `policy_promotion_allowed=false`、`routing_authority=false`。

v0.5.6.3 在 Outcome 治理平面之后加入两个严格隔离的实验组件：`ExperimentalCallStore` 负责外部调用预算、幂等与遥测；`ShadowRouter` 负责只读候选判断。它们复用同一 SQLite Runtime，但不取得 Run 状态所有权，也不允许 Shadow 修改正式执行路线。

```text
显式实验开关 + 显式付费开关 + ExperimentBudget
                    │
                    ▼
           Experimental Adapter
                    │
        请求前最坏成本与重试预留
                    │
                    ▼
 external_calls / external_call_attempts
                    │
                    └── E0 fixture 或 E2 real-unvalidated

冻结 TaskContract + 实际路线哈希
                    │
                    ▼
             ShadowRouter（只读）
                    │
         候选 / 淘汰 / 建议，不应用
                    │
                    ▼
 shadow_decisions / shadow_comparisons
 advisory_only=true / intervention_count=0
```

真实 Adapter 默认关闭，远程或计费连接必须同时打开实验与付费开关。所有重试在第一次网络请求前按最坏情况预留；密钥只在发送瞬间从环境变量读取，不进入数据库、公开配置或异常正文。测试 Transport 固定使用 E0，不能伪装成 E2。当前仍未执行 30～50 个真实任务，学习和策略晋级继续冻结。

v0.5.6.2 在冻结评测平面之后加入 Outcome 数据治理平面。`build_outcome_dataset` 直接消费 v0.5.6.1 的不可变实验报告，不复制第二份运行事实；每条路线观察形成一个 `GovernedOutcome` 学习副本，五条同任务路线通过相似指纹和样本权重折算为一个有效任务。

学习副本不保存原始目标、项目/用户/任务标识、参考答案、输出正文或原始证据引用。带私密盐的 HMAC 提供可关联但不可直接还原的项目、用户、任务、合同、路线和证据标识。标签事件保持追加式，成功标签必须有独立验收，正负冲突保持并列；TTL 和删除请求清除学习正文及标签正文，只留下样本哈希、标签集合哈希和原因码组成的审计摘要。

migration 015 保存不可变数据集清单、学习副本、标签事件和删除审计。质量报告分别计算治理是否通过与训练是否允许：E0 模拟证据、有效样本不足、候选集合/propensity 缺失、标签覆盖或冲突异常、项目泄漏、敏感内容、版本缺失和成本不对账都会冻结训练。此门禁不读取环境开关绕过，也不会改变正式路由。

v0.5.6.1 在运行控制面之外加入独立但可归因的评测平面。`ExperimentSpec` 冻结任务集、模型目录、五组基线、预算、随机种子和验收版本；`ExecutionTaskView` 在进入执行后端前剥离参考答案、验证器身份、数据分区和路线身份。每个基线使用同一任务顺序、权限、工具、质量门槛和资源上限，形成 35 × 5 的观察矩阵。

内置 `DeterministicFixtureBackend` 只验证协议、成本记账、盲评和重放，不代表真实模型。盲评材料不包含模型、Provider、策略、价格、硬验收结论或软评分；身份映射单独保存。实验报告写入既有 `evaluation_runs`，逐项观察写入 migration 014 的 `evaluation_observations`，因此报告仍只有一个权威事实源。

v0.5.5 在跨供应商经济与预执行准入之后加入智能交接和证据匹配恢复。每个执行节点先被编译为 WorkOrder 与最小 Context Manifest；接收方在执行前验证版本、依赖、Artifact 完整性、Schema、权限、工具、模态、上下文、输出容量和审批。执行中通过 Pcomplete 检查点持续更新完成预期，失败时沿依赖类型分析污染范围，并在权限不扩张、全局重规划最多一次的门禁下选择最小恢复动作。

v0.5.4 把模型、Provider、Endpoint、推理档位和任务族组合成稳定部署键，使用历史验收形成分层后验，再把成功率下界、预计返工、完整成本、完成 P95、本地负载、显存、Bulkhead 和熔断共同用于 Placement 选择。目录数据和运行容量都通过合同输入，核心路由不包含厂商名称分支。

CostWeave v0.5.3 在 Planning Governor 之后加入受限 MultiPlanner：第 1 层继续用难度区间、硬门、容量、ExpectedLoss 和 VoD 决定是否值得规划；第 2 层最多三个互补 Planner 并行提出方案。提案先匿名，再按统一约束评分、检测冲突，最多经过一轮裁决后由 Plan Compiler 写入现有 Artifact DAG。管理层不得递归扩张，默认仍使用确定性的离线适配器，不产生付费模型调用。

```text
HTTP / RunManager Facade / Engine
                    │
                    ▼
HeuristicDifficultyClassifier：11 维难度 + 置信区间
                    │
                    ▼
PlanningGovernor：硬门 → ExpectedLoss → VoD → 容量升级
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
NO / Direct / Single     Multi / Expert / Ask
          │                   │
          └─────────┬─────────┘
                    ▼
PlanningDecision：候选路线、预算、角色、淘汰原因与指纹
                    │
                    ▼
ResourcePortfolioOptimizer：质量 / 期限 / 预算硬约束
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
InferencePolicy      ResourceEconomics
推理档位与缓存边界     八类端到端成本与 P95
          │                   │
          └─────────┬─────────┘
                    ▼
ParallelScheduler：关键路径、准入、重试与审批
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
ReservationManager       ApprovalStore
资源 + 成本原子租约        非阻塞持久审批
          │                   │
          └─────────┬─────────┘
                    ▼
HandoffCompiler：WorkOrder + 最小上下文 + 接收方预检
                    │
                    ▼
SuccessContract + Validator：关键标准、逐条证据与误验风险
                    │
                    ▼
FailureClassifier + RecoveryPolicy：节点 / 子图 / 全局恢复
                    │
                    ▼
Pcomplete + PollutionAnalyzer + RecoveryGate
                    │
                    ▼
DagMonteCarlo + OutcomeStore：结局分布 / 归因 / 校准
                    │
                    ▼
Evaluation Protocol：五基线 / 盲评 / CPS / Regret / JSONL
                    │
                    ▼
MediaRuntime：意图 / 圣经 / 镜头 DAG / Prompt / 审批门
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
SimulatedMediaAdapter   ContinuityValidator
关键帧/动态预览/终稿      身份/场景/动作/边界
          │                   │
          └─────────┬─────────┘
                    ▼
MediaCostLedger：合格镜头与合格视频秒成本
                    │
                    ▼
ControlKernel：状态所有权、Artifact、幂等与恢复审计
                    │
     ┌──────────────┼──────────────┬────────────┐
     ▼              ▼              ▼            ▼
EventStore      ArtifactStore  GraphCompiler state_guard
SQLite WAL      哈希与血缘      合同编译门      合法转换表
```

- `contracts_v4.py` 冻结 `v4.1` Schema、统一 ID 和 JSON Envelope。
- `state_guard.py` 是无 I/O 的纯确定性校验层，终态不能回退。
- `compat_v4.py` 保留现有 API 字段，并把状态写入委派给 ControlKernel。
- `COSTWEAVE_CONTRACTS_V4=0` 会恢复 v0.3 直接赋值和快照形状。
- `COSTWEAVE_RUNTIME_V4=0` 只关闭持久运行时，保留 v0.4.1 合同守卫。
- `COSTWEAVE_ARTIFACT_DAG=0` 关闭 Artifact 新写入，保留 v0.4.2 结果字段。
- `COSTWEAVE_PARALLEL_RUNTIME=0` 关闭资源准入与租约，回到 v0.4.3 兼容执行路径。
- `COSTWEAVE_PLACEMENT_V4=0` 关闭联合 Placement，回到 v0.3 风险调整路由。
- `COSTWEAVE_EVIDENCE_RECOVERY=0` 关闭高级恢复并失败即终止，不放宽验收。
- `COSTWEAVE_OUTCOME_FORECAST=0` 停止新预测和学习写入，旧预测保持可读。
- `COSTWEAVE_MEDIA_WORKFLOW=0` 停止创建新媒体项目，通用编排、旧媒体记录和其他功能保持可用。
- `COSTWEAVE_HANDOFF_INTELLIGENCE=0` 关闭 v0.5.5 WorkOrder 预检、Pcomplete 与污染感知门禁，回到 v0.5.4 兼容交接和恢复路径。
- v0.5.6.1 评测命令固定使用 E0 Fixture Backend；没有启用真实 Provider 或策略晋级的环境开关。
- v0.5.6.2 数据集命令必须显式提供私密盐；E0、低样本或数据质量失败时训练入口硬失败。
- EventStore 是运行事实源；`runs`、`tasks`、`attempts` 和 `projections` 是可重建索引。

## 0.1 Planning Governor 决策边界

Governor 的目标不是挑“最聪明”或“最便宜”的单一模型，而是在满足质量和权限硬约束后最小化：

```text
ExpectedLoss
= 直接费用
 + 规划与执行时间价值
 + 失败后返工损失
 + 错误结果的业务风险
 + 多角色协调开销
```

六条候选路线使用同一损失结构。MultiPlanner 还必须满足 `VoD > 收益边际`；高级模型必须存在高风险、低置信、专业缺口或难度越界触发条件。任何已选路线都必须是可行候选，全部淘汰理由随决策指纹和权威 Run Projection 持久化。

本地容量判断在执行前完成，输入包括可用本地候选数、本地推理能力上限、请求并发和期限压力。低置信、高风险或容量不足会触发增强路线，并把 `senior-review` 写入后续计划。v0.5.3 会并行调度受限提案、匿名评分和限一轮裁决，再由 Plan Compiler 把裁决指令写入既有 DAG；角色生成器仍是离线确定性适配器，不代表已接入真实付费模型。

模式选择完成后，角色匹配不能重新破坏成本决策。Direct、Single、Multi 和 Expert 分别携带不同的推理能力下限与成本敏感度：先淘汰低于能力下限的候选，再按角色能力、可靠性、推理、标准化输入/输出价格、本地部署收益和组合多样性评分。这样既不会为了便宜把任务交给明显不足的模型，也不会让低风险直接委派静默落到最高价模型。

## 0.3 成功契约、证据与恢复边界

每个任务在执行前形成版本化成功契约：

```text
acceptance_criteria
→ critical / quality criteria
→ required result fields + minimum score
→ independent evidence requirement
→ criterion_results
→ validation result + FP/FN risk
```

关键标准使用硬门，不能被普通标准平均；高风险任务的关键标准必须含非执行器自评的独立证据。失败随后进入确定性分类，并由策略映射到最小恢复范围：

```text
transient             → retry_node
capability/evidence   → upgrade_node
contract_error        → replan_subgraph
root_assumption_error → replan_global
limit_exceeded        → stop
```

`PlanPatch` 只能由规划层生成，并且从当前修订精确推进到下一修订。恢复前计算受影响任务集合；已验证且与失败无关的兄弟 Artifact 保持 `validated`。`validation_results`、`criterion_results`、`failure_records` 和 `plan_revisions` 记录完整审计链。

### 0.3.1 交接、完成概率与污染边界

交接不复制整段会话。Context Manifest 只含任务合同、接收方能力和 Artifact 元数据；Artifact 正文通过受控运行时通道交付，Prompt 只携带引用、哈希和 Schema。接收方预检的任何硬失败都会在执行前拒绝该节点，并进入合同错误恢复。

Pcomplete 不是模型自报置信度，而是路由先验与运行证据的组合。它记录测试通过率、约束覆盖、证据完整度、模型冲突、预算消耗、阻塞依赖和剩余关键路径；低、中、高风险恢复判断阈值分别为 0.60、0.75、0.90。

污染传播遵守依赖语义：`hard` 和 `validation` 边传播失效，`soft` 边只产生可疑标记。全局重规划可以重新审视全图，但不能因此删除没有污染血缘的已验收 Artifact。恢复节点必须复制原工具、权限和依赖 Schema，不得静默扩权；不可逆替换节点需要新的审批单。

## 0.4 Outcome 预测与校准边界

预测在任务执行前冻结，不控制运行状态：

```text
Task DAG + Placement 先验 + 恢复合同
→ 固定种子 Monte Carlo
→ 五种互斥 Outcome
→ 质量 / 成本 / 时间 P50、P95
→ OOD、置信度、关键误放与主要失败模式
```

运行终止后生成一个幂等 `OutcomeRecord`，要求模型、Prompt、工具、策略、数据和计划版本归因完整率为 100%。能力后验按四级键更新：

```text
model × task × risk
→ model × task
→ model
→ global
```

样本达到门槛前只进行有限权重校准；单次运行不会直接修改生产路由。`forecast_snapshots` 保存执行前预测，`outcomes` 保存事实标签，`capability_posteriors` 与 `forecast_calibrators` 保存可回滚统计状态，`evaluation_runs` 保存冻结基准结果。

## 0.2 Placement 与成本账本

每个任务先形成有限候选集：

```text
模型候选 × Provider/本地池 × low/medium/high 推理档位
→ 质量下界过滤 → 单节点期限过滤
→ 端到端预期成本评分 → 模式选择
→ 验收独立性修复 → DAG 关键路径修复
→ 预算复核 → 选定项 + 两个备用项
```

`ResourceEconomics` 计算模型调用、本地算力、能源、稀缺容量、排队机会、交接、验收合并和预期返工。预测记录 P50、P95 与预留上限；实际执行按同一分类写入 `cost_ledger`，每项使用唯一归因键，支持守恒检查。`placements` 按运行隔离，避免相同任务合同在不同运行间发生身份碰撞。

## 0. 可靠控制内核

每个写入采用同一事务边界：

```text
幂等查重 → expected_revision 校验 → 状态守卫
→ 追加 EventEnvelope → 更新运行/任务/尝试索引与 Projection → commit
```

- 命令去重先于 revision 检查，因此成功请求使用旧 revision 重试仍返回首次结果。
- 同一幂等键对应不同规范请求时返回冲突。
- 每个运行的事件 revision 从 1 连续增长，重放按 revision 而不是时间戳排序。
- Projection 带规范 JSON 哈希；哈希损坏或 Schema 不兼容时从事件流重放。
- 任务语义 ID 按 `run_id + legacy_task_id` 生成持久 TaskId，避免跨运行碰撞。
- 启动恢复只续跑非终态运行；`validated` 节点保留，`running` 节点经 `suspect → pending` 合法路径恢复。

## 0.1 Artifact 与交接边界

```text
TaskContract + 当前 plan revision
              ↓
GraphCompiler：环 / 生产者 / Schema / 权限 / 依赖类型
              ↓
MessageEnvelope：目标 / 范围 / 输入引用 / 输出规格 / 验收标准
              ↓
执行器只消费 validated 且哈希正确的上游 Artifact
              ↓
结果形成新的内容寻址 Artifact，并建立 derived_from 血缘
```

- Artifact 内容使用规范 JSON 的 SHA-256；读取和再次消费时都会校验。
- Artifact ID 由运行、生产任务、尝试序号、类型、Schema 和内容哈希确定。同一尝试重复提交保持幂等；新尝试或内容变化产生新版本。
- 内容不会原地覆盖；`status` 可从 `validated/rejected` 进入 `stale`，保留历史内容用于审计。
- `hard` 和 `validation` 依赖会阻塞消费者；`soft` 依赖允许消费者在其未完成时运行。
- Message ID 由运行、接收任务、计划修订和尝试序号确定；同一身份不同内容被拒绝。
- 重规划先计算受影响子图，再只将相关生产任务的 Artifact 标记为 stale；无关兄弟成果不变。
- v0.4.2 的已验证内存结果在恢复时可物化为 Artifact，不需要重新执行节点。

## 1. 可靠并行运行时

每轮调度只考虑依赖已通过验收的就绪节点：

```text
就绪节点 → 关键路径长度 / 优先级 / 关键度排序
        → 审批门
        → 永久容量与期限可行性
        → 当前可用容量与队列
        → Provider 熔断与 Token Bucket
        → BEGIN IMMEDIATE
        → 资源租约 + 成本预留
        → 异步执行
```

- 资源池按 `provider + local/cloud` 聚合，记录并发槽、CPU、GPU、显存、队列和 Token 吞吐。
- 永久容量或期限不可能满足时直接拒绝，任务 `attempt` 仍为 0。
- 临时容量不足、背压或熔断时节点留在 `pending`，无关分支继续运行。
- 资源与预计成本在同一 SQLite 写事务内预留；`run + task + attempt` 是唯一键。
- 执行成功、失败、取消或启动异常都会释放租约；重启会回收单实例遗留租约。
- Provider 暂时性异常最多重试两次，并驱动 closed → open → half-open 熔断状态。
- 审批单拥有独立 revision 和 Idempotency-Key；审批只阻塞对应节点。
- 默认使用关键路径策略，`COSTWEAVE_SCHEDULER_POLICY=fifo` 可切换兼容排序。

## 2. 模型知识库

`model_catalog.py` 只保存可恢复的内置基线。运行时由 `CatalogStore` 管理活动目录：

```text
内置基线 ──恢复默认──┐
用户编辑 ──校验─────┼→ 完整候选快照 → 原子持久化 → 活动 revision
JSON/CSV ──原子导入─┘
```

每次写入先构造完整候选目录并校验；通过后写临时文件并原子替换。若持久化失败，内存 revision 和模型集合一起回滚。`expected_revision` 实现乐观并发控制。

启动时若持久化 JSON 损坏，服务保留损坏文件并回退到内置基线，同时在目录元数据中返回 `load_warning`。用户仍能进入页面检查数据或执行“恢复默认”；下一次成功写入会原子替换损坏文件并清除警告。

目录只保存模型事实与路由先验，不保存 API Key。非 USD 云模型在没有汇率快照时不能启用路由。

## 3. 运行快照隔离

新运行开始时调用 `PredictiveRouter.freeze()`：

```text
活动目录 r7
  ├─ 运行 A 启动 → 冻结 r7
  ├─ 用户编辑 → 活动目录 r8
  ├─ 运行 A 重规划 → 仍使用 r7
  └─ 运行 B 启动 → 冻结 r8
```

因此模型被编辑、停用或删除不会破坏正在执行的任务；计划中的 `model_snapshot` 和路由汇总记录实际 revision。

## 4. 分层任务画像

画像不是单一类别。规划器分别抽取：

- 主意图与操作：构建、分析、研究、决策、转换、验收、排错
- 多标签专业域及每个标签的分数、证据和来源
- 代码、报告、方案、数据集、决策等交付物
- 预算、期限、格式、本地、开源和并行等显式约束
- 文本、图像等模态和搜索、代码执行等工具
- 认知、协作、证据、工具、领域、范围六维复杂度
- 意图歧义、知识不确定性、分类置信度与澄清问题
- 普通、提升和高风险等级

难度由六维复杂度加权得到，不再主要依赖文本长度。每个专业分支再根据任务性质调整自己的难度。

## 5. 任务合同与 DAG

任务合同除目标、依赖、Schema 和验收标准外，还包含：

- 能力权重
- 任务不确定性
- 关键度
- 分类置信度
- 升级策略
- Token 估计
- 模态与工具硬约束
- CPU、GPU、显存和并发槽需求
- 审批类型、原因、队列状态、阻塞原因和资源租约

专业分支可并行执行，每个分支都由独立 `verify-*` 节点验收。高风险任务增加安全复核，所有分支通过后才能进入冲突审查和最终汇总。

## 6. 风险调整路由

路由分为三步。

### 6.1 硬约束

- 可路由状态和 USD 预算可计算性
- 上下文与最大输出
- 输入模态
- 显式工具
- 时效检索
- 关键能力短板

函数调用不等价于代码执行；必须显式声明 `code_execution`。

### 6.2 保守质量预测

冷启动均值综合：

- 加权能力与最短板
- 可靠性和可用性
- 推理能力与难度差
- 工具、上下文和风险匹配
- 目录资料可信度和核验时效
- Preview、任务不确定性和分类不确定性

路由同时输出均值、预测不确定性与保守下界。质量门槛约束下界，避免把带有高不确定性的乐观均值当成可靠保证。

### 6.3 组合优化

1. 为每个节点找到满足保守质量门槛的候选。
2. 验收节点优先使用不同模型；高风险时优先不同 Provider。
3. 求满足全部硬约束与独立性约束的最低可行成本。
4. 按任务关键度加权的边际质量收益/额外成本升级。
5. 极速模式额外考虑关键路径速度。
6. 完成升级后再次检查执行—验收独立性。

## 7. 验收门

普通执行结果至少检查 Schema、合同字段、摘要、证据和置信度。规划、验证、风险和安全节点还必须提供：

```json
{
  "decision": "pass",
  "criteria_results": [
    {
      "criterion": "逐条对照验收标准",
      "passed": true,
      "evidence_refs": ["evidence-1"]
    }
  ]
}
```

只有 `pass` 且每条标准都有通过记录才能解锁下游。`revise`、`reject` 和 `human_review` 会进入失败处理，不会因为 JSON 外形正确而继续执行。

## 8. 证据驱动成功率

当前成功率由各任务的保守先验、关键度、状态和验收分数组合重算：

- 未执行节点使用路由保守下界。
- 已验证节点融合路由先验与实际验收分数。
- 可疑节点折损。
- 拒绝节点显著折损。
- 未解决高风险节点增加额外惩罚。

失败后不会无条件提高成功率。恢复节点重新路由后，也只有新证据通过才能恢复估计。

## 9. 恢复

失败节点被标记失效，GraphCompiler 计算其下游影响范围，相关 Artifact 变为 `stale`，下游未完成节点回到等待态；有效兄弟分支及其 Artifact 继续保留。恢复节点增加一档诊断难度并提高规划、风险能力要求，但保留原任务的业务风险级别，避免把执行失败重复计算为高风险。待执行节点在扣除在途成本后重新组合路由，且继续使用原运行快照。

v0.3 仍限制最多一次全局重规划，防止模拟器无限循环。真正接入模型后应进一步按临时适配器错误、能力不足、合同错误、根假设失效和证据过期选择不同恢复策略。

## 10. 安全边界

- 仅监听回环地址
- 写请求执行 Content-Type 和 Origin 检查
- 导入限制大小和模型数量
- 拒绝未知字段、非有限数字、越界能力和危险 URL 协议
- CSV 导出防止电子表格公式注入
- 不接受服务器文件路径或远程 URL 抓取
- 路径穿越和未知 API 返回受控错误

## 11. 当前边界

- 目录可编辑不代表信息已自动核实。
- 预测是带不确定性的冷启动先验，尚未使用真实运行数据做概率校准。
- 高级规划门能阻止错误计划继续，但模拟执行器不会产生并应用真正的外部模型 PlanPatch。
- 执行结果仍为离线模拟。
- EventStore 当前为单进程、单机 SQLite，不是多进程或分布式调度器。
- 资源容量来自模型目录派生的仿真资源池，不是操作系统或 GPU 实时遥测。
- 启动回收假设单实例拥有数据库；多实例需要租约所有者、心跳和 fencing token。
- Event payload 暂以完整运行投影换取简单确定的重放；后续版本可引入更细粒度 reducer 和周期快照。
- Artifact 与运行事件目前分别使用短事务；外部 Provider 接入前仍需设计跨系统 outbox 和 Provider 幂等协议。
