# 手动实验台（2026-09-18）

双击项目根目录的 `start-experiment-lab.cmd`，浏览器会自动打开。已检测到本机 Python 3.14.6 和项目虚拟环境，不需要安装依赖或手动启动数据库。

页面内填写 API 地址、API Key、准确模型 ID，选择接口类型并保存。保存配置不会联网，也不会验证密钥有效性。DeepSeek 官方预填 `https://api.deepseek.com` 和 `deepseek-flash`；其他平台的 ID 必须使用该平台实际提供的名称。

## 已配置内容

- 当前 Windows 用户 DPAPI 加密保存密钥；页面及结果不返回密钥。改变接口地址必须重新填写密钥。
- 通用 Chat Completions、DeepSeek 非思考模式、新版 GPT Chat Completions 三种参数配置。
- 三项固定任务：计算与格式、订单统计、空结果边界。严格校验字段、类型、结果，拒绝重复 JSON 字段和 Markdown 围栏。
- 离线校验演示，零模型 API 请求；其通过只说明校验功能工作，不证明模型能力。
- 复制任务、人工粘贴答案、保存校验结果与模型自报名称。人工结果不虚构 Token 或费用。
- 单次真实调用：一次请求、512 输出 Token、45 秒超时、零自动重试。复用现有 HTTP Adapter、ExternalCallAuthority 和 ExperimentalCallStore。
- 独立运行编号、独立运行数据库，报告自动写入 `work/manual-lab/reports/`。页面可下载 JSON。
- 实验进程被中断后，运行中的记录标记为中断，不自动重放；上次请求可能已计费。
- 启动时关闭旧环境的付费开关，不加载 `.env.local`。新入口的明确页面授权仅作用于本次固定任务。

## 真实调用前仍需确认的内容

API 地址本身不能说明中转站价格。为避免把未知收费当作免费，当前金额预算需要提供商的 **美元 / 百万 Token** 输入、输出单价。可在页面“计费信息”填写。每次点击真实试跑前，页面显示本次预算并要求明确勾选授权。默认显示 $0.05 是建议值，不是本次配置过程已经取得的支出授权；最大 $0.50。多次试跑的预算分别计算，不是长期累计上限。

调用前按完整请求 UTF-8 字节数上界和输出 Token 上限预留费用。该上界以所填单价与服务商遵守协议为前提，不替代服务商账单。超时或响应缺失用量时记录费用未知，不自动补发请求。缺失 usage 不记成零费用成功。

校园点数、人民币套餐、订阅包量不能填进美元单价。它们的接口信息可以保存，人工结果可以评测；自动付费试跑需后续接入独立计费规则。未提供价格或预算时，环境不会自行发起真实调用。

## 明确边界

这个入口是固定小任务实验台。它没有把 CostWeave 主引擎的 SimulatedExecutor 改成真实多模型 DAG，也没有实现尚未完成的 D–F 工作包。Responses-only / 原生 Anthropic、Embedding、图片任务和校园 GPU 模型部署不在本次已验证范围；未知中转站协议不能仅凭模型名称判定兼容。

本机配置位置 `work/manual-lab/connections.json`，里面的 credential 是 DPAPI 密文，绑定当前 Windows 用户；复制到其他用户或系统后需要重新填写密钥。运行数据是本地私人实验记录，不应提交到版本库。共享实验报告前应检查其中的模型输出是否适合分享。

## 验证与来源

新增测试 `tests/test_experiment_lab.py` 覆盖授权、预算拒绝、加密存储、地址变更、接口类型、严格验收、无用量响应、失败不重试、会话鉴权与中断恢复。真实调用路径使用离线 Transport 替身验证，不消耗模型额度。

- [OpenAI Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)：新版配置使用 `max_completion_tokens`，不凭模型名称附加思考参数。
- [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)：复用现有 DeepSeek Adapter 的非思考模式配置。
- [DeepSeek 更新记录](https://api-docs.deepseek.com/updates/)：官方模型标识更新为 `deepseek-flash`。服务商转发别名不能据此推断。
