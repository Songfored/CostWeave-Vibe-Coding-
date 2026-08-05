# CostWeave v0.5.8.2

发布日期：2026-08-05

## 新增

- 中央 `security_boundary`：Adapter 插件注册表、能力准入、端点/DNS 校验、Prompt 注入检查、短时密钥租约和统一脱敏。
- `security_store`：不可变项目撤销墓碑、外部副作用合同和状态事件。
- migration 027：证据导出项目列、项目墓碑和副作用表；Runtime Schema 升级为 `v5.8.2`。
- `X-CostWeave-Project` 与 `X-CostWeave-Data-Residency` 运行范围；旧数据兼容为 `local-default/local`。
- `GET /api/v1/security/policy` 与 `POST /api/v1/projects/{project_id}/revoke`。
- 页面安全边界状态面板。
- 完整安全加固材料：背景、三种方案、结构图、机器可读清单和实施步骤。

## 加固

- 远程 Adapter 只允许无凭据、无查询、无片段的公网 HTTPS 端点；真实传输拒绝私网/保留 DNS 结果、重定向和超过 4 MB 的响应。
- HTTP Adapter 保持双开关默认关闭、工具权限为空；未知插件、修订不匹配和越权部署在网络前失败。
- 用户目标/假设与可信 WorkOrder 合同分区；命中指令覆盖、密钥外传或工具越权模式时需要单独安全批准。
- 密钥值不进入配置、SQLite、遥测、错误、日志、HTTP JSON 或证据导出；租约关闭时覆盖字节缓冲区。
- Provider 幂等键在每次外部调用中转发；副作用合同在传输前持久化，同键不同请求拒绝，歧义传输结果不自动重放。
- Run、Task、Artifact、Ledger、Approval、Media、Evidence Console 和 Export Receipt 按项目隔离，跨项目统一返回不存在。
- 项目逻辑撤销立即阻断读取和新建任务；API 明确返回 `physical_rows_deleted=false`。

## 兼容性

- Run Projection Schema 仍为 `v5.6`；新增字段均有默认值。
- `local-default` 项目继续使用旧版创建幂等身份，避免升级后重复创建既有默认项目任务。
- Evidence Export Schema 继续为 `v0.5.8.1-export`，只增加兼容的 scope/manifest 信息；migration 026 的不可变收据约束保持有效。
- 默认安装仍不产生真实或付费模型调用。

## 验证

- 新增 8 项安全专项/API 测试；完整基线 679 项。
- 覆盖零密钥公开/持久化、网络前拒绝、Prompt/工具边界、Provider 幂等转发、不可变合同、同键冲突、项目 Run/Artifact/Export 隔离和撤销后不可访问。
- 安全评审选择中央边界方案；隔离 Adapter 网关作为后续公网/多租户部署选项保留。

## 未完成边界

- 未实现公网身份认证、授权服务器、OS 级 Adapter 沙箱或独立网络代理。
- 未实现历史数据库行物理擦除、按项目加密密钥销毁或外部备份擦除证明。
- 未完成真实 Provider 兼容性、真实账单对账和真实商业降本验证。
- 下一工程版本为 v0.5.8.3 Stress, Fault & Migration。
