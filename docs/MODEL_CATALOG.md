# CostWeave 模型目录与评测证据快照

评测证据更新：**2026-09-18**。官方参数与价格基线仍为 **2026-08-27**，本次没有重新核价。

## 本次更新位置与使用边界

- 运行目录：`costweave/model_catalog.py`，元数据指向新证据库。
- 当前证据库：`costweave/benchmarks/model_evidence_2026-09-18.json`。
- 历史证据库：`costweave/benchmarks/model_evidence_2026-08-27.json`，原样保留，并在新文件中记录 SHA256。
- 原有 20 个目录项全部保留；新增观察型号保存在 `observed_models`，没有凭榜单名直接注册 API 或启用路由。
- 持久化模型目录、价格、人工编辑、0—1 路由先验、真实 Outcome 后验均未覆盖。已有持久化目录加载时仍优先保留用户记录。
- 本次更新的是公开能力证据，不是自动学习或新策略上线。程序的目录元数据可定位新文件；网页暂不直接展示这些榜单明细。

## 证据类型与日期

| 来源 | 本次采用口径 | 榜单日期 | 抓取日期 |
|---|---|---|---|
| [Artificial Analysis](https://artificialanalysis.ai/leaderboards/models) | Intelligence Index v4.3，保留推理档位及 fallback | 页面未单独给出 | 2026-09-18 |
| [Arena Text](https://arena.ai/leaderboard/text) | Overall 文本偏好 | 2026-09-13 | 2026-09-18 |
| [Arena Code](https://arena.ai/leaderboard/code) | WebDev Overall，含框架标签 | 2026-09-11 | 2026-09-18 |
| [Arena Vision](https://arena.ai/leaderboard/vision) | Overall 视觉理解偏好 | 2026-09-13 | 2026-09-18 |
| [DeepSeek 模型卡](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash) | 厂商自报，最大推理档；保留工具/框架 | 发布说明为 2026-09-10；模型卡当前页 | 2026-09-18 |

只收录当前任务相关的代表性行，不声称穷举。Arena 保存 URL 默认页面；文本提取没有确认各 UI 开关是否选中，不能与其他过滤视图混算。JSON 中保留投票数、排名区间、误差和 Preliminary 标志。下面是便于查看的摘录，完整字段以 JSON 为准。

## 综合能力摘录

AA 当前[方法](https://artificialanalysis.ai/methodology/intelligence-benchmarking)为 v4.3。旧快照没有记录指数版本，不能由数字下降断言模型退步，也不能直接转成成功概率。该指数主要覆盖英语文本，不用于填补中文与视觉能力。

| 评测变体 | AA v4.3 指数 |
|---|---:|
| Claude Fable 5.1 (max with fallback) | 53 |
| GPT-6 Astra (max) | 53 |
| GPT-6 Astra (medium) | 50 |
| Claude Opus 5 (max) | 51 |
| GPT-5.6 Sol (max) | 47 |
| Qwen3.8 Max (0902) | 45 |

`*` 原样保留来源标记，本次未把其解释为完整评测覆盖。不同推理档位单独记录；fallback 配置不等于普通单模型调用。

## 文本偏好摘录

| 评测变体 | 排名 | 分数 ± 来源误差 | Preliminary |
|---|---:|---:|---|
| claude-fable-5-high | 1 | 1506 ± 5 | 未标注 |
| claude-fable-5.1-max | 5 | 1498 ± 8 | 未标注 |
| gemini-3.8-flash-high | 9 | 1493 ± 9 | 是 |
| claude-opus-5-high | 10 | 1493 ± 4 | 未标注 |
| gemini-3.7-flash-high | 12 | 1490 ± 8 | 是 |

## Web 开发摘录

| 评测变体 | 排名 | 分数 ± 来源误差 | Preliminary |
|---|---:|---:|---|
| gpt-6-astra-max | 1 | 1800 ± 16 | 未标注 |
| claude-fable-5.1-max | 2 | 1758 ± 14 | 未标注 |
| claude-opus-5-max | 3 | 1687 ± 7 | 未标注 |
| qwen3.8-max-0902 | 4 | 1681 ± 15 | 是 |
| kimi-k3-max | 5 | 1674 ± 11 | 未标注 |

## 视觉理解摘录

| 评测变体 | 排名 | 分数 ± 来源误差 | Preliminary |
|---|---:|---:|---|
| claude-fable-5-high | 1 | 1310 ± 8 | 未标注 |
| qwen3.8-max | 2 | 1302 ± 8 | 未标注 |
| claude-opus-5-high | 10 | 1289 ± 8 | 未标注 |
| claude-fable-5.1-max | 11 | 1289 ± 12 | 未标注 |
| gpt-5.6-sol-xhigh | 14 | 1286 ± 8 | 未标注 |

名次不代表确定胜负，需结合 JSON 中的排名区间与投票数。WebDev 不能代替仓库修复或测试正确率；codex-harness 成绩不能视为任意中转站同名模型的裸调用表现。

## DeepSeek V4.1 Flash：当前资源相关的新记录

[官方更新日志](https://api-docs.deepseek.com/updates/)确认 V4.1 Flash 支持原生视觉，官方 API 使用 `deepseek-flash`；历史 `deepseek-v4-flash` 名称已作为兼容别名转向新后端。旧 V4 的历史成绩不随别名自动变成 V4.1 成绩。校内或中转部署是否跟随升级仍需单独确认。

模型卡自报成绩单列在 `vendor_reported`，含科学推理、终端任务、代码修复、自动化和视觉工具任务；保留推理档位、框架、metric 与采样设置。未将 Base 模型成绩混入 Instruct 成绩。

NL2Repo-Bench 存在厂商材料冲突：更新日志为 65.4，模型卡为 64.0。新证据库同时保存两项来源，采用值为 null，不选较高值也不求平均。

## 新型号与真实可用性

新增观察覆盖 GPT-6 Astra、Claude Fable 5.1、DeepSeek V4.1 Flash、Qwen3.8 Max 0902、GLM-5.3/Flash、Gemini 3.8 Flash、Qwen3.8-Flash-Next、GPT-5.3 Codex、MiniMax-M3。

[GPT-6 Astra 官方模型页](https://developers.openai.com/api/docs/models/gpt-6-astra)确认了 API 名称、上下文与输出上限；其余只有榜单身份的条目不猜测 API ID。榜单可见不代表用户账户或中转站可用。

官方渠道、校内点数渠道与中转站必须分别核对模型后端、限流和成本。校内 bge 嵌入模型不能用上述聊天模型榜单评分。本次没有足够信息更新 GPU 可部署能力或本机吞吐。

## 未取得或不能直接采用的数据

- [SWE-bench](https://www.swebench.com/)本次取得方法说明，但网页提取没有当前分数行。
- [Terminal-Bench 旧入口](https://www.tbench.ai/?version=2.0)返回含 4.0 描述的页面，未取得独立分数行。没有以厂商自报或二手汇总冒充独立成绩。
- Arena Vision 取得页面未找到 DeepSeek 行；这表示本次没有相应独立视觉证据，不代表不支持视觉。
- 本轮没有专门中文评测、embedding 检索、图片/视频生成评测，不凭综合榜分数补数。

## 后续更新规则

每条能力观测记录来源、抓取日、榜单日、模型变体、推理档位、任务范围及 agent 框架。原始分数分榜保存；未知为 null。公开证据只用于冷启动参考，不覆盖按任务采集的真实能力后验。

旧价格与硬参数见保留的 2026-08-27 JSON；它们不是今日价格承诺。本次没有重新配置模型、改变实验计费、启动真实调用或使用历史预算。
