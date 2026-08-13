# CostWeave v0.6.6 V066-WP03 修复版验收报告

- 日期：2026-08-13（Asia/Shanghai）
- 结论：**PASS——两个阻断问题均已修复**
- Outcome：`fixed`
- 证据等级：E0 local/fake-gateway

## 修复结果

1. LiteLLM 模型新增 `free | paid | unknown` 计费模式；未声明时按 `unknown` 处理。
2. 当 `paid_calls_enabled=false` 时，`paid` 和 `unknown` 模型均在调用 transport 前被拒绝，实测调用次数为 0。
3. 明确标记为 `free` 的模型，在付费调用关闭时仍可正常执行。
4. 只有明确的 `LiteLLMTransportError` 会被映射为可重试网络错误；意外异常被映射为 `unknown` 且 `retryable=false`。
5. Release readiness 新增 LiteLLM Gateway 和 LiteLLM 付费调用两个门禁，总门禁从 7 项增加到 9 项。

## 验证证据

- 原始付费绕过 PoC：不再复现。
- `paid` 绕过变体：调用前拒绝，transport calls = 0。
- `unknown` 绕过变体：调用前拒绝，transport calls = 0。
- 合法免费路径：`completed`，transport calls = 1。
- 意外程序异常：category = `unknown`，retryable = `false`。
- 修复聚焦与兼容测试：75/75 通过，用时 56.765 秒。
- 全量回归：951/951 通过，用时 331.132 秒。
- 修复 wheel 在全新隔离目录安装成功；相同 PoC 在 wheel 中不再复现。
- Release CLI：9/9 门禁通过，`engineering_ready=true`，`production_ready=false`，外部调用 0 次。
- Runtime：v6.5.0，35 migrations，WAL，integrity/replay healthy。

## 变更文件

- `costweave/gateway_litellm.py`
- `costweave/release_readiness.py`
- `costweave/release_cli.py`
- `costweave/server.py`
- `costweave/adapters/__init__.py`
- `tests/test_gateway_litellm_v066_wp03.py`
- `tests/test_release_v0584.py`
- WP03 acceptance、benchmark、policy、security、changelog 文档

## 剩余边界

本修复只验证本地 Fake LiteLLM 边界，不证明真实 Provider 价格、远程兼容性、生产 SLA 或商业成本收益。调用者仍需负责真实 transport 的凭据、TLS、网络隔离和账单对账；生产状态继续保持 `production_ready=false`。
