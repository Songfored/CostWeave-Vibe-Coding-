# CostWeave v0.5.8.3 Changelog

发布日期：2026-08-09

## 新增

- 默认关闭、固定种子可复现的故障注入器，覆盖限流、Provider 宕机、显存不足、进程崩溃、deadline 和歧义副作用。
- 有界 Provider 熔断与故障转移控制，歧义副作用不会自动重放。
- 可解释 deadline miss 风险预测与 recall/AUROC 离线评估。
- 安全恢复规划器、不可变恢复检查点和恢复收据。
- migration 028：韧性实验、观测、恢复检查点与恢复收据。
- 事件、投影、stream revision 与 migration 的逻辑完整性快照。
- 带逻辑快照的备份、验证与恢复拒绝门。
- `GET /api/v1/resilience/status` 和受限的 `POST /api/v1/resilience/campaigns`。
- 页面“韧性验收”面板及桌面/手机响应式布局。

## 验证

- 完整回归：694 项，0 失败。
- 新增韧性专项：15 项。
- 桌面 1280×720 与手机 390×844 页面检查：无横向溢出、无控制台错误。
- 确定性 E0 夹具中，Provider/显存故障在 30 秒合同内转移；deadline recall 和 AUROC 均为 1.0。

## 证据边界

上述故障、并发和预测数值来自本地确定性 E0 夹具，不是现实 Provider、生产流量或商业结果。当前版本仍默认零外部调用、零付费调用；没有完成真实长时间运行、分布式故障或 Provider 兼容性验证。
