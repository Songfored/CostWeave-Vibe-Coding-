# CostWeave v0.6.9 策略卡

| 项目 | v0.6.9 规则 |
|---|---|
| 策略 | `authoritative-decision-bridge-v1` |
| 权限 | `advisory-only` |
| 输入 | baseline 与 shadow 共用一个冻结指纹 |
| 模块 | MetaController、CapabilityGraph、JointSearch、ExecutionRecovery、SafeLearning |
| 管理深度 | 最大 2 |
| 正式路线影响 | 0 |
| 预算/状态/DAG 影响 | 0 |
| Provider/网络/外部调用 | 0 |
| 失败行为 | 完整记录错误并沿用 baseline |
| 学习 | 只产生 E0 候选，不训练、不晋级 |

正式激活任何建议属于 v0.7.0 范围，必须逐个决策面启用，并带独立开关、回滚和 baseline fallback。
