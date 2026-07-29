# CostWeave v0.5.2 架构说明

CostWeave v0.5.2 在两层规划合同之上加入 Planning Governor：第 1 层先用本地轻量分类器形成难度区间，再用硬门、容量检查、预期总损失和 VoD 选择模式；第 2 层 Planner 仍只能提出受限方案。管理层不得递归扩张，跨 Provider 角色通过目录能力画像匹配，默认只使用确定性的 MockProvider。真实模型、图片与视频生成仍由仿真适配器隔离。

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
SuccessContract + Validator：关键标准、逐条证据与误验风险
                    │
                    ▼
FailureClassifier + RecoveryPolicy：节点 / 子图 / 全局恢复
                    │
                    ▼
DagMonteCarlo + OutcomeStore：结局分布 / 归因 / 校准
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

本地容量判断在执行前完成，输入包括可用本地候选数、本地推理能力上限、请求并发和期限压力。低置信、高风险或容量不足会触发增强路线，并把 `senior-review` 写入后续计划。v0.5.2 不执行真实多模型讨论；提案并行、匿名裁决和 Plan Compiler 属于 v0.5.3。

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
