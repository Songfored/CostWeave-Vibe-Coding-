# CostWeave v0.4.7 验收报告

验收日期：2026-07-28  
版本：0.4.7  
结论：通过，可作为离线原型发布

## 1. 本版目标

建立可校准的多结局预测、完整 Outcome 数据和正式评估框架，使系统能衡量“首次成功、恢复成功、升级成功、人工介入或最终失败”，并以真实归因数据持续修正模型能力先验。

## 2. 已实现范围

- 五种互斥 Outcome 与单调累计成功率。
- 固定种子 DAG 蒙特卡洛、共同故障因素和恢复路径。
- 质量、成本和完成时间 P50/P95。
- 关键错误放行概率、预测置信度、样本量、OOD 和失败模式。
- 模型、Prompt、工具、策略、数据和计划版本完整归因。
- 四级 Beta 后验、样本门槛与分层回退。
- Brier、ECE、Log Loss 和区间覆盖率。
- v0.3、固定模型、人工阈值与 v0.4.7 冻结基准比较。
- 预测、Outcome、校准器、后验与评估持久化及 API。
- 页面多结局预测展示和预测功能回滚。

## 3. 自动化与定量验收

```text
python -B -m unittest discover -s tests -q

Ran 244 tests
OK
```

冻结基准结果：

| 指标 | CostWeave v0.4.7 | v0.3 未校准 |
|---|---:|---:|
| Brier Score | 0.1700 | 0.1873 |
| ECE | 0.0000 | 0.1300 |
| Log Loss | 0.5130 | 0.5834 |

- 每个校准桶 200 个样本。
- 成本区间覆盖率 95%。
- 时间区间覆盖率 95%。
- 五种互斥概率和为 1。
- 累计成功率保持单调。
- 相同种子结果完全一致。
- Outcome 归因完整率 100%。

## 4. 静态、页面与接口验收

- Python 模块编译检查通过。
- `app.js` Node 语法检查通过。
- 页面唯一 ID、ARIA、标签、响应式和核心交互测试通过。
- 预测与评估 API 通过真实 HTTP 集成测试。
- Wheel 独立安装后确认版本 0.4.7、Schema v4.7、6 次迁移。

## 5. 数据迁移

迁移 006 只新增：

- `forecast_snapshots`
- `outcomes`
- `forecast_calibrators`
- `capability_posteriors`
- `evaluation_runs`

## 6. 已知边界

- 冻结基准是确定性仿真数据，不是第三方生产基准。
- 当前不调用真实模型 API，也不自动修改生产路由。
- 后验需要积累真实、完整归因的 Outcome 后才具备生产校准意义。
- 本报告不构成节省率、准确率或商业收益承诺。

## 7. 发布建议

GitHub Release 标题：

```text
CostWeave v0.4.7 — Calibrated Outcome Forecasting & Evaluation
```
