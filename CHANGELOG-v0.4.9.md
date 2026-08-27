# CostWeave v0.4.9

v0.4.9 是 v0.4 发布列车的整合、加固与完成版。

## 新增

- 统一 `/api/v1/catalog` 模型目录接口。
- `GET /api/v1/system/diagnostics` 运行数据库诊断。
- `POST /api/v1/system/backups` 在线 SQLite 备份。
- `RuntimeMaintenance` 备份校验和显式停机恢复。
- migration 008：事件完整性与媒体命令幂等回执。
- 31 项发布加固专项测试。

## 改进

- 事件负载和信封在读取、重放时校验 SHA-256。
- HTTP 请求体 10 秒超时、守护线程、有限监听队列、Host/Origin 和 JSON 结构限制。
- CSP、禁止嵌入、禁止 MIME 猜测和 Referrer 安全策略。
- 过期租约自动释放；已释放尝试禁止重放。
- 成本账本按执行尝试独立归因。
- 媒体项目和镜头批准传递并持久化幂等键。
- 所有相邻镜头执行首尾连续性检查，同时保留镜头并行生成。
- Artifact 公共接口明确为 metadata-only。

## 兼容

- 旧 `/api/catalog` 接口仍可使用，并返回弃用和后继版本响应头。
- v0.4.8 数据库自动升级，不丢失已有表和运行。
- 真实模型与付费媒体 API 仍未接入，默认运行不产生付费调用。
