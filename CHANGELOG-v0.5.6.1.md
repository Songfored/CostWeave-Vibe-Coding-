# CostWeave v0.5.6.1

## Added

- 冻结的 `ExperimentSpec`、`BenchmarkTask`、`ExecutionTaskView`、`EvaluationLimits` 和盲评合同
- 35 个按项目隔离的 E0 种子任务，覆盖代码、研究、写作、分析、长上下文、工具、多语言和视觉
- 五组统一基线：Strongest、Cheapest、Human Static、v0.4 Fixed 和 v0.5 Adaptive
- 确定性 Fixture Backend，以及 35 × 5 的完整观察矩阵
- 硬验收与软质量分离，参考答案和验证器身份不进入执行负载
- 隐藏模型、Provider、策略和价格身份的未评分盲评包
- CPS、Time to Accepted Result、质量非劣、Routing Regret、规划与交接开销比较
- 不可静默覆盖的 JSON、JSONL 和 CSV 实验导出
- 基于现有 `evaluation_runs` 的幂等报告事实源和 observation matrix
- `GET /api/v1/evaluation/protocol` 和 `costweave-evaluate` 命令入口
- migration 014 和 Runtime Schema `v5.6.1`

## Changed

- `PredictiveRouter` 新增只读的确定性候选排序接口，供冻结基线使用
- 健康检查公开评测协议版本、基线数量以及真实调用和商业声明开关
- 页面版本标识升级为 v0.5.6.1 Evaluation Protocol
- 模型目录包现在包含固定种子任务 JSON

## Validation

- 426 项自动化测试通过；测试模块使用独立进程运行，失败和超时会返回非零状态。
- 23 项 v0.5.6.1 专项测试覆盖合同、数据隔离、重放、五基线、路由拒绝、盲评、参考答案隔离、硬失败、预算、成本、导出和持久化。
- 受影响的 EventStore、运行维护、投影兼容、服务 API 和页面回归均通过。
- 内置评测生成 175 个观察项，真实 Provider 调用数为 0。

## Boundary

本版结果均为 `E0-simulated`。Fixture Backend 只用于验证评测系统，不代表任何真实模型成绩；报告固定关闭商业声明资格。v0.5.6.1 不接真实 Provider、不训练预测器、不修改生产路由，也不启用 Shadow 干预。
