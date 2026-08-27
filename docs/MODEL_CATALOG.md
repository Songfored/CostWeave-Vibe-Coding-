# CostWeave 模型目录与评测证据快照

核验日期：**2026-08-27**。本快照覆盖 CostWeave 当前需要参与路由比较的 20 个代表性条目；它不是对全世界所有模型的穷举。

## 本次重导入结果

- 运行目录：`costweave/model_catalog.py`
- 原始证据库：`costweave/benchmarks/model_evidence_2026-08-27.json`
- 持久化目录：`~/.costweave/model_catalog.json`
- 价格单位：每 100 万 tokens；币种逐模型记录
- 能力字段：CostWeave 可编辑路由先验，不是排行榜原始分数
- 未公开字段：明确保存为 `null`，不靠猜测补齐

| 模型 | 输入 / 缓存 / 输出 | 上下文 | 状态或特别说明 |
|---|---:|---:|---|
| GPT-5.6 Luna | $0.20 / $0.02 / $1.20 | 1.05M | 经济型 |
| GPT-5.6 Terra | $2 / $0.20 / $12 | 1.05M | 平衡型 |
| GPT-5.6 Sol | $4 / $0.40 / $20 | 1.05M | 前沿型 |
| GPT-OSS 20B（本地） | API 账单 $0 | 131K | 21B/3.6B 激活；本地硬件成本另计 |
| Claude Haiku 4.5 | $1 / $0.10 / $5 | 200K | 快速批处理 |
| Claude Sonnet 5 | $2 / $0.20 / $10 | 1M | 平衡型代理编码 |
| Claude Opus 5 | $5 / $0.50 / $25 | 1M | 前沿复杂任务 |
| Claude Fable 5 | $10 / $1 / $50 | 1M | 高成本长周期智能体 |
| Gemini 3.5 Flash-Lite | $0.30 / $0.03 / $2.50 | 1M | 标准实时价；Batch/Flex 为 $0.15/$1.25 |
| Gemini 3.7 Flash | $0.75 / — / $3.75 | 1M | 2026 年底前阶段性价格 |
| Gemini 3.1 Pro Preview | $2 / $0.20 / $12 | 1.048M | 预览；>200K 有阶梯价 |
| DeepSeek V4 Flash | $0.44 / $0.014 / $1.32 | 1M | 采用峰时保守价 |
| DeepSeek V4 Pro | $1.32 / $0.044 / $3.96 | 1M | 采用峰时保守价 |
| Grok 4.6 | $2 / $0.50 / $6 | 500K | >200K 价格翻倍 |
| Mistral Small 4 | $0.15 / $0.015 / $0.60 | 256K | 119B/6.5B 激活，Apache 2.0 |
| Mistral Large 3 | $0.50 / $0.05 / $1.50 | 256K | 675B/41B 激活，Apache 2.0 |
| Qwen 3.8 Max | $2 / $0.25 / $6 | 1M | 国际区 USD 价；2.4T MoE |
| Kimi K3 | CNY 20 / 2 / 100 | 1.048M | 2.8T/104B 激活；无汇率快照时不参与 USD 路由 |
| MiniMax M2.7 | $0.30 / $0.06 / $1.20 | 204.8K | 速度与基准主要为厂商自报 |
| 本地规则核心 | 0 | 64K | 确定性模块，不是 LLM |

## 评测数据如何进入系统

证据库按来源等级分开保存：

1. **官方事实**：型号、上下文、最大输出、模态、工具、参数量、许可证和价格。
2. **独立评测**：当前保存 LMArena 2026-08-21 Text Arena 与 Artificial Analysis 2026-08-27 Intelligence Index 快照。
3. **厂商自报**：例如 MiniMax M2.7 的 SWE-Pro、Terminal-Bench 2 等成绩；明确标注后只作冷启动参考。

排行榜的最小粒度是“模型版本 + 推理档位”。例如 `gpt-5.6-sol-xhigh`、`claude-opus-5-max` 和普通档并不是一个可互换的观测。LMArena 的分数带统计不确定性，排名区间重叠时不能声称存在确定胜负；预览或投票较少的条目也保留 `preliminary` 标记。

公开排行榜只用于新模型冷启动和交叉检查。它不能替代 CostWeave 自己按任务类型、难度、提示词、工具、时延、失败返工与实际账单采集的 Outcome 数据，因此不会直接覆盖真实能力后验。

## 价格口径

- DeepSeek 存在峰谷价格，目录采用峰时缓存未命中输入与峰时输出价，避免预算低估。
- Grok 4.6 超过 200K 上下文时价格翻倍；目录主字段保存普通档，限制字段保留阶梯价。
- Gemini 3.1 Pro Preview 的主字段保存不超过 200K 输入档；更长输入为 $4/$18。
- Gemini 3.7 Flash 当前价格有效期有限，过期前应再次刷新。
- Qwen 使用国际区 USD 价格；Kimi 使用中国站 CNY 价格。没有带时间戳的汇率快照时，Kimi 被标记为不可参与美元预算路由，避免跨币种伪比较。
- 本地模型的 API 价格可以为 0，但电力、GPU 占用、排队、显存和运维成本不是 0，后续必须由资源经济模型计算。

## 主要来源

- [OpenAI 模型比较](https://developers.openai.com/api/docs/models/compare)
- [Anthropic 模型总览](https://platform.claude.com/docs/en/models/overview)与[价格](https://platform.claude.com/docs/en/about-claude/pricing)
- [Google Gemini 模型](https://ai.google.dev/gemini-api/docs/models)与[价格](https://ai.google.dev/gemini-api/docs/pricing)
- [DeepSeek 模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)
- [xAI Grok 4.6](https://docs.x.ai/developers/models/grok-4.6)
- [Mistral 价格](https://docs.mistral.ai/inference/pricing)
- [Alibaba Cloud Qwen 3.8 Max](https://www.alibabacloud.com/help/en/model-studio/qwen3-8-max)
- [Moonshot Kimi API](https://platform.kimi.com/)与[Kimi K3 开放权重资料](https://github.com/MoonshotAI/Kimi-K3)
- [MiniMax 按量价格](https://platform.minimax.io/docs/guides/pricing-paygo)
- [LMArena Text Arena](https://arena.ai/leaderboard/text?style=false)
- [Artificial Analysis 模型排行榜](https://artificialanalysis.ai/leaderboards/models)

## 后续更新规则

少量改动可在页面编辑；批量更新使用 JSON/CSV 导入。每次更新必须同时保存来源 URL、核验日期、币种、价格口径与预览状态。删除或替换型号前先导出备份；导入后运行目录测试和路由回归。真实 Outcome 仍进入 Capability Posterior，不反写成“官方能力分”。
