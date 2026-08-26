# CostWeave v0.5.6.3

## Experimental Adapters & Shadow Router

本版把“真实模型连接”从一个普通 HTTP 请求升级为受预算、幂等、归因和证据等级约束的实验能力，同时确保新路由在获得足够证据前只能观察，不能接管正式执行。

### 新增

- `OpenAICompatibleHTTPAdapter`：面向 OpenAI-compatible 文本接口的最小实验适配器。
- `LocalHTTPAdapter`：仅允许 `127.0.0.1`、`localhost` 或 `::1` 的本地文本接口。
- `ExperimentBudget` 与 `ExperimentalCallStore`：实验总预算、Provider 预算、逻辑调用数、单次尝试数、重试预留和实际费用冻结。
- `ShadowRouter` 与 `ShadowStore`：只读候选排序、硬淘汰原因、建议路线、实际路线哈希及事后实际结果比较。
- migration 016：新增 `external_experiments`、`external_calls`、`external_call_attempts`、`shadow_decisions`、`shadow_comparisons`。
- `GET /api/v1/experiments/policy`：公开默认开关、预算、密钥、证据和 Shadow 隔离边界。

### 安全边界

- 默认外部调用数为 0。
- 实验调用和付费调用使用两个独立显式开关。
- 未注册预算、Provider 未分配预算、重试预留超限、逻辑调用数超限或 WorkOrder 上限不足时，均在网络 I/O 前拒绝。
- 同一幂等键只能对应同一语义请求；完成后的结果和遥测不可变。
- 密钥只在发送请求时从环境变量读取，不写入数据库、结果、遥测、公开配置或异常正文。
- 远程连接只允许 HTTPS；本地连接只允许明确的 Loopback 地址。
- 文本先行；工具调用、批处理、图片、视频和语音保持关闭。
- Shadow 决策固定为 `advisory_only=true`、`intervention_count=0`，不能修改 TaskContract 或实际路线。

### 遥测与证据

- 记录实验、Run、Task、Deployment、Provider、模型、推理档位和 Adapter 版本。
- 记录输入/输出/缓存 Token、实际或估算费用、请求与排队时延、重试、限流和取消。
- 测试 Transport 标记为 `E0-transport-fixture`；真实网络 Transport 标记为 `E2-real-unvalidated`。
- Adapter 结果始终处于“待独立验收”状态，不能自行把自己的输出标记为通过。
- Shadow 比较只知道实际路线的结果，不虚构未执行建议路线的反事实质量或节省。

### 兼容性与测试

- 包版本：`0.5.6.3`。
- Runtime Schema：`v5.6.3`。
- Run Projection Schema：继续兼容 `v5.5`。
- 自动化测试：481 项；其中 26 项 Adapter/Shadow 专项测试和 1 项策略 API 回归。

## 尚未完成的证据门

本次开发没有配置用户密钥，也没有发起真实 Provider 请求。路线图要求的 30～50 个方向性真实任务尚未完成。因此：

- 可以声明工程控制机制已实现并通过自动化测试；
- 不可以声明已经证明真实降本、真实提速、真实成功率提升或达到世界顶尖水平；
- 在真实任务报告通过数据质量与治理检查前，不启用学习训练、自动策略晋级或 Shadow 接管。
