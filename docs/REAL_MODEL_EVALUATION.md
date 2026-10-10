# CostWeave 本地环境与真实模型评测指南

## 当前边界

CostWeave v0.6.9 可以在没有模型密钥、网络调用和付费请求的情况下运行完整控制平面、模型目录、DAG、规划治理、影子决策、回放与安全学习合同。

当前版本已经具备一键密钥录入、固定渠道配置、只读模型发现、自动型号选择，以及一个显式授权后才能运行的三渠道真实冒烟评测入口。仓库还具备 OpenAI-compatible、LiteLLM、Ollama/vLLM、本地端点、预算、幂等、秘密租约、调用记录和 Shadow 合同。默认 Web 服务仍没有注入可改变正式路由的真实 `ShadowProbeExecutor`；直接修改普通启动开关不会绕过专用真实评测入口。完整批量 Real Evidence Bridge 仍属于后续 v0.7.1 工作。

## 本机离线配置

环境要求为 Windows、PowerShell、Python 3.11 或更高版本。在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

脚本会创建 `.venv`、安装当前源码、保留或创建 `.env.local`，将模型目录和 SQLite 数据库放入 `work/local-data/`，并执行一次零 Provider 冒烟检查。

随后运行一键模型配置：

```powershell
.\scripts\configure-models.ps1
```

程序内置并锁定以下已确认信息，不要求用户填写 URL、协议或模型名称：

| 渠道 | 地址 | 协议 | 自动选择目标 |
|---|---|---|---|
| DeepSeek 官方 | `https://api.deepseek.com` | OpenAI-compatible | 优先 `deepseek-v4-pro`，其次 `deepseek-v4-flash` |
| TravelToken GPT | 读取 `.env.local` 中的 `COSTWEAVE_TRAVELTOKEN_BASE_URL` | OpenAI-compatible | `gpt-5.5` |
| TravelToken Claude | 同上 | 先按 OpenAI-compatible 验证 | `claude-sonnet-4-6` 及兼容别名 |

中转站地址不写入公开源码。使用中转渠道前，在 `.env.local` 中加一行 `COSTWEAVE_TRAVELTOKEN_BASE_URL=https://你的中转地址/v1`（必须是 https）；未填写时脚本会跳过两个中转渠道，不提示输入密钥。

已有的 `COSTWEAVE_GPT_API_KEY` 和 `COSTWEAVE_CLAUDE_API_KEY` 会自动迁移为渠道明确的新变量名，不需要重新输入。脚本只调用模型列表接口，不执行内容生成；结果写入 `work/local-data/providers.local.json`，该文件不含密钥并且默认不启用推理。中转渠道返回的模型名称只能证明中转站声明可用，不能证明其背后模型身份或官方计费真实性，因此初始信任标记为 `relay-unverified`。

### DeepSeek 结构化响应配置

DeepSeek 官方渠道使用独立的 `deepseek-chat-json-v1` 适配配置，而不是直接照搬通用 OpenAI-compatible Payload：

- 结构化工作单显式发送 `response_format={"type":"json_object"}`；
- System Prompt 明确要求只返回一个 JSON 对象，工作单附带由输出 Schema 生成的无答案示例；
- `low/minimal/none` 推理档关闭 Thinking，避免短任务把全部输出额度消耗在 `reasoning_content`；
- 更高推理档继续启用 Thinking，并将推理 Token、最终 `finish_reason` 和“只有推理、没有正文”的次数分别记账；
- 思维链正文不会写入报告、Artifact 或遥测；
- `prompt_cache_hit_tokens` 会映射到统一缓存 Token 字段。

这只修复协议与结构化输出兼容性，不代表 DeepSeek 能力评分已经得到验证。官方 API 仍提示 JSON Output 偶尔可能返回空正文，所以空正文必须由节点验收拒绝，不能自动视为成功，也不能在未获预算授权时自动重试。

## 首轮真实冒烟入口

只有用户给出明确美元预算以后才能运行：

```powershell
.\scripts\run-real-smoke.ps1 -BudgetUsd 0.5 -AuthorizePaidCalls
```

该入口具有以下固定边界：

- 仅调用已经通过模型列表验证的 DeepSeek、GPT 和 Claude 三个渠道；
- 每个渠道一次请求，零自动重试，最多 512 输出 Token；
- 总预算上限不能超过 0.50 美元，并在持久化账本中按渠道拆分；
- DeepSeek 使用官方峰时价格，GPT/Claude 使用未扣中转优惠的官方原价作保守估算；
- 结果由本地确定性验收器独立检查，原始输出不进入评测报告；
- 脚本结束后恢复关闭外部调用与付费开关；
- 只证明调用链、Token/延迟记录和小样本验收可工作，不允许据此发布能力排行、降本比例或商业声明。

结果保存在 Git 忽略的 `work/local-data/real-eval/first-real-smoke.json`。中转站最终扣费仍应与其使用记录或账单对账；官方模型原价估算不能冒充中转站真实账单。

启动本地控制台：

```powershell
.\scripts\run-local.ps1
```

终端会显示一个带 `#session=...` 的本机地址。完整打开该地址即可，按 `Ctrl+C` 停止。端口被占用时运行：

```powershell
.\scripts\run-local.ps1 -Port 8766
```

快速复查与完整测试分别为：

```powershell
.\scripts\check-local.ps1
.\scripts\check-local.ps1 -Full
```

`.env.local`、`.venv`、数据库和本地模型目录均被 Git 忽略。不要把模型密钥写入 `.env.example`、源码、测试夹具、Issue、日志或提交历史。

## 接入真实模型的正确顺序

### 1. 先完成一个本地端点

优先使用一个 OpenAI-compatible 本地端点，例如由 Ollama 或 vLLM 暴露的回环地址。它用于验证请求、Token、延迟、输出哈希、失败分类和 Outcome 链路，不用于证明商业降本。

v0.7.1 需要完成：

1. 为现有 `OpenAICompatibleHTTPAdapter` 建立可序列化的 `ShadowProbeExecutor` 委托；
2. 用 `ProcessIsolatedShadowProbeExecutor` 包装委托，保证 Windows `spawn` 可工作；
3. 通过新的显式启动入口把执行器注入 `build_server(..., small_budget_shadow=...)`；
4. 本地端点继续限制在回环地址，调用前检查容量；
5. 默认 `costweave` 启动入口仍保持零网络调用。

### 2. 再接一个外部 Provider

首轮只选择一个支持 OpenAI-compatible 协议的外部 Provider。完成首个 Provider 的协议、用量、账单和失败对账以后，再接第二个。

密钥只放在 `.env.local`，变量名通过 `secret_env` 引用。推荐变量名由一键脚本自动维护：

```dotenv
COSTWEAVE_DEEPSEEK_API_KEY=replace-with-the-real-key
COSTWEAVE_TRAVELTOKEN_GPT_API_KEY=replace-with-the-real-key
COSTWEAVE_TRAVELTOKEN_CLAUDE_API_KEY=replace-with-the-real-key
```

执行器还需要显式配置端点、Provider、模型修订、输入/输出价格快照、超时、数据范围和密钥环境变量名。密钥值不得进入配置对象、数据库或 Trace。

### 3. 从小预算 Shadow 开始

执行器通过网络边界测试后，才在专用会话中临时启用：

```dotenv
COSTWEAVE_EXPERIMENTAL_ADAPTERS=1
COSTWEAVE_PAID_CALLS=1
```

每次实验必须预注册：

- 总美元硬预算和单 Provider 预算；
- 单次 Probe 最坏成本；
- 最大逻辑调用数与最大输出 Token；
- 单次最大尝试数，首轮设为 1；
- 不超过 24 小时的失效时间；
- 网络、远程付费和 Shadow-only 明确同意；
- `metrics_only` 或 `metrics_and_output_hash` 数据范围。

真实调用首先只能进入 Shadow 和证据链，不得自动改变正式路由、晋级策略或扩大预算。测试结束后立即把两个调用开关恢复为 `0`。

## 第一批评测任务

先固定 30～50 个小任务。每个任务都要有稳定任务族、固定输入、允许数据范围、可重复验收器、最大 Token/时间/费用、失败标签、禁止动作和停止条件。

同一任务至少运行四种基线：始终使用最高能力模型、始终使用最低成本可用模型、固定人工规则、CostWeave Shadow 建议。任务集、Prompt、模型修订、推理档位和价格快照不一致时，不比较结果。正式按任务族得出稳定结论前，应进一步扩大到每族 50～100 个以上有效样本。

## 必须记录和对账的数据

每条样本应从决策追踪到最终 Outcome，并记录：

- 输入、缓存、推理和输出 Token；
- Provider Request ID、重试、429、5xx、超时和不确定失败；
- Provider 报告费用、内部估算费用和最终账单差异；
- 请求、排队、端到端延迟及 P50/P95；
- 独立验收、返工、人工覆盖和最终成功状态；
- Planner、交接、Validator 和 Recovery 的额外开销；
- 模型、Provider、端点、价格和策略版本。

HTTP 200 只表示请求成功，不等于任务验收成功。无法确认是否已经发生副作用的请求标为 `ambiguous`，不得自动重放；无法对账的成本不得按零处理，也不得用于公开商业结论。

## 首轮成败门槛

建议在看到结果前固定门槛：相对“始终使用最高能力模型”，成功率或质量下降不超过 2 个百分点，实际总成本至少下降 30%，P95 延迟没有不可接受的恶化，预算超支、密钥泄漏和未授权调用均为 0，并且 Token、重试、失败和最终账单可以逐项解释。

这些数值是工程门槛，不是 CostWeave 已取得的成绩。数据达到门槛后仍应依次经过 Offline Replay、Shadow、低风险 Canary 和可回滚晋级，不能由单次成败直接修改长期能力分数。

## 推荐的下一开发包

v0.7.1 的最小 Real Evidence Bridge 应严格限制为：一个本地 OpenAI-compatible 端点、一个外部 OpenAI-compatible Provider、一个默认不装载的进程隔离执行器、30～50 个预注册任务与硬验收器、完整用量/费用/延迟/错误/输出哈希/Outcome Join，以及一键冻结实验和导出证据。在链路能够守恒、回放和回滚以前，不扩展更多 Provider，也不让真实数据自动取得路由权。
