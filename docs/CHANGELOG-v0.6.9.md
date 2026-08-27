# CostWeave v0.6.9 发布说明

## 主题

Authoritative Decision Bridge：让已有五个智能模块第一次共同参与同一个 Run，同时保持零执行权。

## 新增

- `DecisionOrchestratorV1`：用适配器串联 MetaController、CapabilityGraph、JointSearch、ExecutionRecovery 和 SafeLearning；
- 冻结的 `AuthoritativeDecisionEnvelopeV1`：保存输入、目录、价格、容量、候选拓扑、候选模型组合、分布、预算、路线差异、模块回执、证据等级、选择概率与确定性指纹；
- 每个新 Run 在 baseline 路由冻结后生成完整影子决策轨迹；
- 任一模块异常时生成五模块齐全的失败关闭信封，baseline 继续执行；
- 决策信封随现有运行投影持久化，无新增数据库迁移；
- `GET /api/v1/runs/{run_id}/decision-bridge` 只读接口；
- 运行页面新增统一影子决策卡片。

## 不变边界

- shadow 对正式路线、预算、任务状态、DAG 和外部调用的影响均为 0；
- 不允许 Provider/网络调用，不允许自动训练或策略晋级；
- 管理深度上限仍为两层，不新增第三层 Planner；
- `SimulatedExecutor` 仍是默认执行器，`production_ready=false`。

## 兼容性

Runtime Schema 保持 `v6.5.0`，迁移数保持 35。旧投影没有 `decision_bridge` 字段时可继续载入，新 Run 会写入该字段。
