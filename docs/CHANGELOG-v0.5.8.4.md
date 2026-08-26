# CostWeave v0.5.8.4 Changelog

发布日期：2026-08-09

## Stable Release

- 新增机器可读稳定版就绪清单，逐项区分工程通过与生产/商业未验证。
- 新增全量 EventLog→Projection 重放一致性审计，不使用抽样。
- migration 029 保存项目范围的不可变发布证明。
- 新增 `GET /api/v1/release/status` 与 `POST /api/v1/release/attestations`。
- 新增 `costweave-release` 离线验证命令，覆盖全新运行、重放、备份和零外部调用。
- 页面新增 v0.5 稳定版状态面板。
- 补齐 Benchmark、模型卡、策略卡、数据卡、安装复现和安全报告。

## 兼容性

- 既有 Run Projection Schema 保持 `v5.6`；
- 既有 API 保持兼容；
- 外部 Adapter、付费调用、Canary 和自动策略晋级继续默认关闭。

## 证据边界

“Stable”只限定为离线单进程工程控制平面。真实 Provider 质量、固定降本比例、真实 P95、生产可用性和世界领先程度仍未验证。
