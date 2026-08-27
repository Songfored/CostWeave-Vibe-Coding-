# CostWeave v0.4.7

发布日期：2026-07-28

## 核心升级

- 用首次成功、修复后成功、升级后成功、人工介入和最终失败五种互斥结局替代单点成功率。
- 新增固定种子 DAG 蒙特卡洛，覆盖并行、串行、恢复和共同故障因素。
- 输出质量、成本、完成时间 P50/P95、关键误放概率、预测置信度、样本量和 OOD。
- 新增完整归因 `OutcomeRecord`，记录模型、Prompt、工具、策略、数据和计划版本。
- 引入四级 Beta 后验与保守分层回退；单次 Outcome 不直接改变生产路由。
- 新增 Brier、ECE、Log Loss 和区间覆盖率。
- 冻结基准比较 v0.4.7、v0.3 未校准、固定模型和人工阈值。

## 持久化与接口

- 迁移 006 新增 `forecast_snapshots`、`outcomes`、`forecast_calibrators`、`capability_posteriors` 和 `evaluation_runs`。
- 新增 `GET /api/v1/runs/{id}/forecast`。
- 新增 `GET /api/v1/evaluations`。
- 运行投影升级为 v4.7，并继续读取 v4.2～v4.6 投影。
- 页面新增五结局、质量/成本/时间区间、置信度、样本量和 OOD 展示。

## 验收

- 244 项自动化测试通过。
- 固定基准每桶 200 个样本：ECE 0，Brier 0.17，优于 v0.3 的 0.1873。
- 成本与时间区间经验覆盖率均为 95%。
- JavaScript 语法检查和 Wheel 独立安装检查通过。

## 回滚与边界

`COSTWEAVE_OUTCOME_FORECAST=0` 停止新预测与学习写入，不影响核心运行，旧预测仍可读取。当前指标来自冻结仿真基准，不代表真实模型生产校准或商业收益承诺。
