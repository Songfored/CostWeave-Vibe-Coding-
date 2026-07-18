# CostWeave v0.3 内置模型基线

内置基线核验日期：2026-07-18。该表不是只读目录；v0.3 的模型管理中心可以随时编辑、导入、导出或恢复这份基线。

以下价格为每百万 Token 的厂商公开标价。`输入 / 输出` 默认使用美元；Qwen 使用人民币。缓存、批处理、长上下文、区域部署、工具调用和限时优惠可能产生不同价格，使用前应再次查看来源。

| 模型 | 主要定位 | 输入 / 输出 | 上下文 | 重要优势 |
|---|---|---:|---:|---|
| GPT-5.6 Luna | 高吞吐、成本敏感任务 | $1 / $6 | 1.05M | 工具完整、长上下文 |
| GPT-5.6 Terra | 能力与成本平衡 | $2.5 / $15 | 1.05M | 推理、编码、代理工具 |
| GPT-5.6 Sol | 复杂专业工作 | $5 / $30 | 1.05M | 高难推理与编码 |
| GPT-OSS 20B（本地） | 本地低延迟与专用部署 | API $0 | 131K | Apache 2.0、可配置推理；本地算力另计 |
| Claude Haiku 4.5 | 快速批量任务 | $1 / $5 | 200K | 低延迟、自然表达 |
| Claude Sonnet 5 | 速度与智能平衡 | $3 / $15 | 1M | 代理编码、分析、长上下文 |
| Claude Opus 4.8 | 复杂代理编码 | $5 / $25 | 1M | 长周期工作、稳健分析 |
| Claude Fable 5 | 最高能力长周期智能体 | $10 / $50 | 1M | 复杂企业任务 |
| Gemini 3.1 Flash-Lite | 高吞吐多模态 | $0.25 / $1.5 | 1M | 文本、图像、音频、视频 |
| Gemini 3.1 Pro Preview | 高级多模态与代理 | $2 / $12 | 1M | 搜索、代码执行、长上下文 |
| DeepSeek V4 Flash | 极低成本推理与编码 | $0.14 / $0.28 | 1M | 高并发、超长输出 |
| DeepSeek V4 Pro | 高性价比复杂推理 | $0.435 / $0.87 | 1M | 数学、编码、思考模式 |
| Grok 4.5 | 编码与知识工作 | $2 / $6 | 500K | 代理任务；表中为短上下文价 |
| Mistral Small 4 | 混合指令、推理和编码 | $0.15 / $0.60 | 256K | 低成本、工具和文档能力 |
| Qwen 3.7 Max | 中文、多模态和工具调用 | CNY 12 / 36 | 1M | 中文、思考/非思考双模式 |

## 官方来源

- OpenAI：[Models](https://developers.openai.com/api/docs/models)
- Anthropic：[Models overview](https://platform.claude.com/docs/en/about-claude/models/overview)
- Google：[Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- DeepSeek：[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)
- xAI：[Pricing](https://docs.x.ai/developers/pricing)
- Mistral：[Mistral Small 4](https://docs.mistral.ai/models/model-cards/mistral-small-4-0-26-03)
- Alibaba Cloud：[Model Studio pricing](https://help.aliyun.com/en/model-studio/model-pricing)

## 如何更新

- 页面新增或编辑：适合维护少量模型。
- JSON 导入：完整且无损，适合版本迁移和自动生成目录。
- CSV 导入：适合在表格软件中批量维护。
- 合并导入：保留当前模型，新增或覆盖相同 ID。
- 替换导入：活动目录完全替换成导入文件。
- 恢复默认：回到本文件所描述的内置基线。

每次成功写入都会产生新的 `catalog_revision`。新任务立即使用新修订；已经启动的任务继续使用旧快照。

## 如何参与路由

厂商事实决定价格、上下文、输出上限、模态和工具等硬约束。能力、推理、速度、可靠性和资料可信度属于 CostWeave 的可编辑先验。路由器不会把厂商营销文案直接当成质量结论。

对于每个任务节点，系统按以下顺序处理：

1. 排除上下文、最大输出、模态或工具不满足要求的模型。
2. 根据加权能力、任务难度、推理水平、风险适配、可靠性和目录时效估计成功率均值。
3. 根据任务和目录不确定性计算保守下界，并排除下界低于用户质量门槛的候选。
4. 计算输入/输出 Token 的预计费用。
5. 在整个任务图上先选择最低成本可行组合。
6. 强制或尽量保证执行模型与验收模型相互独立。
7. 在预算允许时，把模型升级到关键度加权的质量增益/额外成本最高候选。
