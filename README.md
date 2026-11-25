# CostWeave

这个分支提供新的**产物执行内核**。任务声明要交付的产物、独立验收条件和预算。CostWeave 从可用操作中选择一条生产路径，实际执行并结算；如果结果未通过验收，已通过的产物可以供下一条路径复用。

在仓库根目录用 Python 3.11 或更新版本运行：

```sh
python -m costweave plan examples/artifact-runtime/handoff.json --catalog examples/artifact-runtime/catalog.json
python -m costweave run examples/artifact-runtime/handoff.json --catalog examples/artifact-runtime/catalog.json --id first-handoff
python -m costweave events first-handoff
```

示例先读工单摘要。摘要缺少工时，系统随后从工单中分别读取负责人和工时，合并为通过验收的交接单。重复提交同一编号可以查看同一次运行。该示例在本地执行，计费单位是测试报价。[新架构、接口和基线对照](docs/ARTIFACT_RUNTIME.md)

发票补件示例会暂停并指出缺少的资料。第二条命令验收明细后继续同一次运行。第三条命令在独立核定金额的七个本地案例上比较固定查原件、最低报价和按证据选路三种策略。

```sh
python -m costweave run examples/evidence-invoice/job.json --id invoice-17
python -m costweave provide invoice-17 line_items examples/evidence-invoice/line-items.json
python -m scripts.compare_invoice_evidence
```

> 面向多模型、多智能体工作流的成本感知控制平面：先理解和拆解任务，再在质量、成本、时间与风险约束下选择模型、规划并行执行、验收结果，并从可审计证据中持续校准决策。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.6.9-blue.svg)](costweave/version.py)
[![Status](https://img.shields.io/badge/production_ready-false-orange.svg)](docs/ACCEPTANCE-v0.6.9.md)

## v1 工程基线

原有自由文本工作流与 `/api/v1` 服务沿用 **v0.6.9 决策桥**，整图 P95 回放和返修预算分配可通过离线决策接口使用。[v1 输入格式和验证方法](docs/ADAPTIVE_EXECUTION.md)

| 项目 | 当前结论 |
|---|---|
| 稳定工程基线 | v0.5.8.4 |
| 当前开发版本 | v0.6.9 Authoritative Decision Bridge（候选版） |
| 工程就绪 | 仅在全部发布门禁通过时为 `true` |
| 生产就绪 | `false` |
| 证据等级 | E0：离线、模拟与确定性夹具证据 |
| 默认外部调用 | 0；外部适配器和付费调用默认关闭 |
| 当前工作树验收证据 | v0.6 全面验收：全量 `1070/1070`、隔离全量 `1070/1070`、严格资源检查 `54/54`、发布门禁 `19/19`；真实外部调用 0 |

v0.6.9 用统一、不可变、可追踪的 Envelope 把 MetaController、CapabilityGraph、JointSearch、ExecutionRecovery 与 SafeLearning 接入每个 Run。五个模块共享同一份任务、目录、价格、容量和预算快照，但全部保持 `advisory-only`：正式路线、预算、任务状态、DAG 和外部调用影响均为 0；任一模块异常时自动失败关闭到原 baseline。它仍不等于真实供应商质量、账单、生产并发容量或生产可靠性已验证。详见[v0.6.9 简短验收](docs/ACCEPTANCE-v0.6.9.md)、[发布说明](docs/CHANGELOG-v0.6.9.md)与[数据卡](docs/DATA_CARD-v0.6.9.md)。

## 它解决什么问题

复杂任务不应该被粗暴地整体交给一个最贵的模型，也不应该为了省钱而从最弱模型开始逐级试错。CostWeave 的目标是在执行前先做结构化判断：

- 识别用户目标、约束、成功条件与错误代价；
- 把任务编译为带依赖关系的 DAG，而不是线性待办列表；
- 判断任务是否值得拆分、是否需要单规划器或多规划器；
- 根据能力证据、上下文、成本、延迟、并发容量与风险选择执行者；
- 让无依赖节点并行执行，让审批只阻塞受影响的分支；
- 对计划、交接、执行结果和最终合并分别验收；
- 在证据不足、范围漂移或失败概率升高时升级、重规划或止损；
- 记录决策与结果，经过离线评估和晋升门禁后再更新路由策略。

CostWeave 是一个**控制平面与实验平台**，不是大模型本身，也不是已经接好所有厂商并可直接替代生产网关的成品 SaaS。

## 总体架构

```text
用户目标
   │
   ▼
目标理解与成功契约
   │
   ▼
MetaController ── 判断：直接处理 / Planner / MultiPlanner / 升级
   │
   ▼
任务 DAG 与候选计划
   │
   ├── 能力证据图与校准路由
   ├── 成本、时间、容量与风险约束
   └── 联合计划－路由－调度搜索
   │
   ▼
并行执行池 ── 本地模型 / 云模型 / 专业 Agent / 工具
   │
   ▼
节点验收、交接检查与动态恢复
   │
   ├── 继续执行
   ├── 局部重试或换模型
   ├── 回滚并重规划
   └── 升级给更强的指挥模型或人工审批
   │
   ▼
结果合并、全局验收、证据与追踪
```

最高层不是无限嵌套的“管理层”。MetaController 只负责决定所需的规划强度和治理边界；Planner/MultiPlanner 负责产出候选方案；执行者负责边界明确的工作。若额外思考的预期价值低于其成本，系统可以选择 `NO_DELEGATION`，由当前线程直接完成。

## 核心能力

### 1. 任务理解与难度画像

任务难度不是一个关键词标签。系统同时考虑范围、依赖数量、不确定性、知识新鲜度、上下文长度、工具需求、可验证性、错误成本、可逆性和资源压力，并保存判断依据与置信度。

### 2. 有界 Planner / MultiPlanner

- 简单且边界明确的任务可直接执行；
- 中等任务使用单 Planner；
- 高风险、高歧义或高价值任务可让多个规划器并行提出方案；
- MetaController 依据预期决策价值决定是否启用 MultiPlanner，并限制候选数、时间和 Token 预算；
- 规划器无权因为推理等级更高而自行扩大权限。

### 3. 能力证据图与校准路由

模型选择同时使用硬约束与可学习证据：

- 模态、上下文窗口、工具与结构化输出能力；
- 按任务类型和难度分桶的成功率与校准误差；
- 输入、输出、缓存、图片、视频等价格；
- 首 Token 延迟、总延迟、吞吐和并发容量；
- 可用性、地区、隐私、供应商和本地部署限制；
- 重试、验收、交接和失败恢复的预期附加成本。

模型目录可编辑，也支持 JSON/CSV 导入。价格和能力不是写死在路由器里的常量；更新后的数据仍需来源、时间与验证状态，避免“最新”但不可信的数据直接改变生产决策。

当前内置目录已核验到 **2026-08-27**，覆盖 20 个代表性本地/云端条目，并把官方参数、独立排行榜和厂商自报成绩分层保存。具体价格口径、模型版本与证据边界见[模型目录与评测证据快照](docs/MODEL_CATALOG.md)。

### 4. 联合计划、路由与调度

CostWeave 不把“先定计划、再给每个节点找最便宜模型”当作两个孤立问题。候选计划的并行度、关键路径、交接数量、模型容量和失败恢复成本会共同影响最终选择。

一个简化的决策目标是：

```text
最小化：模型费用 + 规划费用 + 验收费用 + 重试费用 + 延迟损失 + 失败风险损失
约束：质量下界、截止时间、并发容量、隐私与权限、总预算
```

因此，“便宜模型单次调用价格更低”不等于整条工作流更省。若低能力模型的失败、返工和交接成本更高，系统应在第一次路由时就选择更强模型。

### 5. 可靠 DAG 与并行运行时

- 依赖满足的节点可以并行；
- 调度器执行容量准入、租约、预算预留和幂等控制；
- 审批事件只暂停受影响的节点与后继分支；
- Artifact Graph 保存结构化产物、来源与依赖；
- 状态机和事件存储支持恢复、重放与审计。

### 6. 精确交接与验收

任务合同会描述目标、输入、上下文、边界、允许的工具、输出结构、验收标准、预算和升级条件。Handoff Compiler 将这些信息编译为面向不同模型的工作单，减少模糊提示词造成的浪费。

每个关键阶段都有检查，但检查强度按风险分级：低风险节点可做确定性校验，高风险节点可要求独立评审、交叉验证或人工批准。系统不会为了“每一步都审一次”而无条件增加调用。

### 7. 执行中信念更新与经济恢复

系统在执行前给出成功概率区间，并根据中间证据动态更新。当发现范围扩大、能力不匹配、质量下降或剩余预算不足时，可选择继续、局部修复、切换执行者、重规划、回滚或停止。决策忽略已经无法收回的沉没成本，只比较从当前时刻开始的预期收益与风险。

### 8. 安全学习闭环

运行数据不会直接“训练自己并立即上线”。CostWeave 先进行：

1. 结果与证据采集；
2. 数据质量、隐私与标签治理；
3. 任务节点和决策的信用分配；
4. 离线重放、反事实评估与敏感性分析；
5. 候选策略注册、影子验证和受控 Canary；
6. 通过晋升门禁后才允许替换基线策略；
7. 漂移或异常触发自动冻结与回滚。

这套流程避免少量偶然成功或错误标签把路由器“越学越歪”。

### 9. 图片与视频工作流

媒体任务可以拆成创意目标、剧本、镜头表、视觉连续性、提示词、低成本预览、关键镜头升级、成片生成和质量验收。当前实现主要提供任务合同、Artifact、成本和审批边界；它不包含真实图片/视频供应商执行器，也不宣称已经测得真实生成成本降幅。

## v0.6 决策智能路线

| 版本 | 主题 | 主要结果 |
|---|---|---|
| v0.6.0 | Decision Intelligence Lab | 建立可复现的决策实验基线与竞争升级框架 |
| v0.6.1 | Rational MetaController | 在直接处理、Planner、MultiPlanner 与升级之间做有界选择 |
| v0.6.2 | Capability Evidence Graph | 以分桶证据、置信区间和漂移状态描述模型能力 |
| v0.6.3 | Joint Plan–Route–Schedule Search | 联合优化计划结构、模型路由和并行调度 |
| v0.6.4 | Execution-Time Belief & Recovery | 根据执行中证据更新成功预期并选择经济恢复动作 |
| v0.6.5 | Trace Credit & Safe Learning | 做决策级信用分配、离线评估和受控策略晋升 |
| v0.6.6 | Gateway / Runtime / Observability / Shadow | 建立真实接入前所需的供应商中立边界、持久运行时、追踪与小预算 Shadow |
| v0.6.7 | Security Foundation Recovery | 统一本地请求与外部调用安全边界；修复后标准复扫覆盖 12/12 威胁面，0 项可报告发现 |
| v0.6.8 | Delivery & UX Convergence | 统一版本源、响应式导航、仓库边界和干净发布流水线 |
| v0.6.9 | Authoritative Decision Bridge | 五个智能模块共享同一 Run/Trace 与冻结输入，形成零执行权的完整影子决策链 |

## v0.6.6 工作包

| 工作包 | 已实现边界 |
|---|---|
| WP01 | 供应商中立的 Gateway 合同、请求与响应规范 |
| WP02 | 本地 OpenAI-compatible 适配边界 |
| WP03 | LiteLLM 适配边界 |
| WP04 | Ollama / vLLM 本地端点与容量感知 |
| WP05 | 本地优先、严格脱敏的追踪与离线 OTLP 形状导出 |
| WP06 | 持久化 Runtime Backend、提交、取消、检查点、恢复与事件分页 |
| WP07 | 默认关闭、项目隔离、调用前原子预算预留的小预算 Shadow |

WP07 只允许影子指标进入证据链，不允许改变基线路由、晋升策略或支持商业宣传。真实账单、缓存、重试、并发和质量标签仍需在后续显式授权的小预算阶段完成对账。

## v0.6.7 安全恢复

- 所有 API 都要求高熵本地会话令牌，GET/POST/静态入口统一检查本地 Host 与 Origin；
- 静态文件解析拒绝 Windows 盘符、UNC、反斜杠和目录逃逸，并在解析后验证 web 根目录包含关系；
- `RequestSecurityContextV1` 统一保存认证主体、项目、驻留域、操作范围和请求身份，项目撤销在路由分发前集中生效；
- `ExternalCallAuthorityV1` 统一验证风险 Prompt 的真实审批，签发绑定 endpoint、请求哈希、副作用和尝试次数的一次性 transport capability；
- Provider 输出在解析、哈希和持久化前检查当前活跃秘密及其受控编码形式；
- 远端 HTTPS 只连接预先验证的公网地址，并验证实际 TLS peer 仍在固定地址集合内；
- 可能已经计费但结果未知的失败保守占用最坏成本，跨实验全局幂等结果不会再次发起 transport；
- HTTP 活跃请求有硬上限，超限快速返回服务繁忙，槽位可恢复。

这些结论来自离线确定性攻击夹具，不等于已完成公网、多用户或分布式安全认证。默认部署仍是只监听回环地址的单用户工具，`production_ready=false`。

## v0.6.8 交付与体验收口

- `costweave/version.py` 是网页、服务、CLI、包元数据与发布清单的唯一当前版本源；
- 低于 768px 的页面使用可折叠、可按 Escape 关闭、可键盘访问的完整导航，不再隐藏功能入口；
- 桌面导航允许自然换行，中文标签不会因挤压而一字一行；
- `.gitignore` 排除本地工作区、输出、日志、运行数据库、缓存与代理元数据；
- `costweave-package` 在 `work/` 的干净 staging 中构建，不读取根目录旧 `build/`；
- wheel、sdist、源码 zip、发布清单与 SHA256 清单均经过归档内容检查；
- 当前 `.git` 元数据不是有效仓库，未擅自覆盖，恢复步骤见[Git 恢复说明](docs/GIT_RECOVERY-v0.6.8.md)。

## v0.6.9 权威决策桥

- 每个新 Run 在原路由冻结后生成 `AuthoritativeDecisionEnvelopeV1`；
- 统一保存任务/目录/价格/容量快照、候选管理拓扑、候选模型组合、分布、预算和 baseline/shadow 差异；
- 五个建议模块共用运行编号、追踪编号和输入指纹。执行前没有观测结果时，安全学习模块记录 `insufficient_evidence`。
- 影子路线的费用按所选操作和各自的报价汇总。`structured_diff.cost_comparable` 为 `true` 时可以比较两条路线的预计费用。字段为 `false` 或历史记录缺少该字段时，费用差额未知。
- 影子链不写任务状态、不改 DAG、不占正式预算、不调用 Provider、不晋级策略；
- 任一模块异常都生成完整失败关闭信封，原 baseline 继续执行；
- `GET /api/v1/runs/{run_id}/decision-bridge` 与运行页面提供只读追踪入口；
- 管理深度仍固定最多两层，没有新增第三层 Planner。

## 快速开始

### 环境要求

- Python 3.11 或更高版本；
- 无必需的第三方运行时依赖；
- 默认只监听本机回环地址；
- 默认不调用外部模型，也不会产生付费请求。

### 从源码运行

```bash
python -m costweave.server
```

服务器会打印一个带 `#session=...` 的一次性本地地址。请直接打开该完整地址；网页会把令牌保存在当前标签页的会话存储中并立即从地址栏移除。直接访问不含令牌的 API 会返回 `401`。

也可以安装为可编辑包：

```bash
python -m pip install -e .
costweave
```

服务器会打印实际监听地址。若端口被占用，可使用：

```bash
costweave --port 8766
```

## 常用离线命令

这些命令使用确定性夹具与本地数据，不需要模型密钥：

```bash
costweave-release --pretty
costweave-tournament --pretty
costweave-metacontroller --pretty
costweave-capability-router --pretty
costweave-joint-search --pretty
costweave-execution-recovery --pretty
costweave-safe-learning --pretty
```

## 模型目录

Web 界面可编辑模型，也可导入 JSON 或 CSV。JSON 的最小示意如下：

```json
{
  "schema_version": 1,
  "models": [
    {
      "id": "provider:model-name",
      "name": "Model Name",
      "provider": "provider",
      "specialty": "通用推理与编程",
      "model_id": "model-name",
      "local": false,
      "routable": true,
      "modalities": ["text"],
      "context_window": 128000,
      "max_output_tokens": 16000,
      "cost_per_task": 0.02,
      "latency_factor": 1.0,
      "reliability": 0.9,
      "input_price_per_mtok": 1.0,
      "output_price_per_mtok": 4.0,
      "pricing_currency": "USD",
      "capabilities": {
        "coding": 0.8,
        "reasoning": 0.8,
        "tool_use": 0.7
      },
      "source_url": "https://example.com/model-docs",
      "verified_at": "2026-08-21",
      "data_confidence": 0.8
    }
  ]
}
```

实际导入时以 `GET /api/catalog/schema` 返回的版本化 Schema 为准。导入前会进行结构、标识符、数值范围和重复项检查；建议先备份已有目录。

## 关键配置

| 环境变量 | 作用 | 默认值 |
|---|---|---|
| `COSTWEAVE_CATALOG_PATH` | 自定义模型目录路径 | 项目默认目录 |
| `COSTWEAVE_RUNTIME_V4` | 启用持久化运行时 | 关闭 |
| `COSTWEAVE_RUNTIME_DB` | 指定运行时 SQLite 文件 | 未指定 |
| `COSTWEAVE_EXPERIMENTAL_ADAPTERS` | 允许实验性外部适配边界 | 关闭 |
| `COSTWEAVE_PAID_CALLS` | 允许付费调用层级 | 关闭 |
| `COSTWEAVE_MEDIA_WORKFLOW` | 启用媒体工作流边界 | 关闭 |
| `COSTWEAVE_PARALLEL_RUNTIME` | 启用并行运行时能力 | 关闭 |

安全原则是**默认拒绝**。仅打开环境变量并不足以产生外部调用：执行仍需通过端点、密钥引用、项目范围、会话有效期、预算、调用次数、Token 上限和明确授权等检查；默认发行版没有内置真实 Shadow 执行器。

## HTTP API 概览

| 领域 | 代表接口 |
|---|---|
| 健康与发布 | `GET /api/health`、`GET /api/v1/release/status` |
| 模型目录 | `GET /api/catalog`、`GET /api/catalog/schema`、`POST /api/catalog/import` |
| 任务运行 | `POST /api/v1/runs`、`GET /api/v1/runs/<id>` |
| 追踪与产物 | `GET /api/v1/runs/<id>/trace`、`GET /api/v1/artifacts/<id>` |
| 持久运行时 | `GET /api/v1/runtime/status`、`POST /api/v1/runtime/submit` |
| 小预算 Shadow | `GET /api/v1/runtime/shadow/status`、`POST /api/v1/runtime/shadow/experiments` |
| MetaController | `GET /api/v1/meta-controller/status`、`POST /api/v1/meta-controller/decide` |
| 能力路由 | `GET /api/v1/capability-router/status`、`POST /api/v1/capability-router/decide` |
| 联合搜索 | `GET /api/v1/joint-search/status`、`POST /api/v1/joint-search/decide` |
| 执行恢复 | `GET /api/v1/execution-recovery/status`、`POST /api/v1/execution-recovery/decide` |
| 安全学习 | `GET /api/v1/safe-learning/status`、`POST /api/v1/safe-learning/evaluate` |

服务定位是本机控制台，不带面向公网的身份认证系统。不要把它直接暴露到公网；项目头用于本地数据分区，不是安全凭据。

## 验收与复现

运行完整测试：

```bash
python -B -m unittest discover -s tests -p "test*.py" -q
```

运行逐模块隔离测试：

```bash
python -B tests/run_isolated.py
```

运行发布门禁：

```bash
python -B -m costweave.release_cli --pretty
```

安装和冻结证据的详细步骤见[v0.6.9 复现指南](docs/INSTALL_REPRODUCE-v0.6.9.md)。测试通过只说明已经定义的工程合同得到满足，不会自动把 E0 证据提升为生产证据。

## 项目结构

```text
costweave/
├── server.py                    # 本地 HTTP 控制台与 API
├── control_kernel.py            # 目标、计划、路由和运行控制内核
├── engine.py                    # 运行记录、幂等与执行协调
├── planner.py / multiplanner.py # 单规划与多规划候选
├── router.py                    # 基础风险调整路由
├── meta_controller_service.py   # 规划强度与升级决策
├── capability_graph_service.py  # 能力证据图与校准路由
├── joint_search_service.py      # 计划－路由－调度联合搜索
├── execution_recovery_service.py# 执行中信念与恢复决策
├── safe_learning_service.py     # 信用分配与安全学习
├── decision_bridge.py           # v0.6.9 五模块统一影子决策信封
├── runtime_backend.py           # 持久化运行时边界
├── small_budget_shadow.py       # 小预算 Shadow 控制平面
├── observability.py             # 追踪、脱敏与导出
├── migrations/                  # 版本化数据库迁移
└── web/                         # 本地 Web 界面

tests/                           # 单元、契约、故障与隔离测试
docs/                            # 架构、规格、迁移、数据卡和验收证据
```

## 重要文档

- [模型目录与评测证据快照（2026-08-27）](docs/MODEL_CATALOG.md)
- [后续版本路线图（Revision 9，跨系列权威排期）](docs/REMAINING_ROADMAP.md)
- [v0.7 重构实施路线（当前 v0.7 施工依据）](docs/V0.7_RECONSTRUCTED_ROADMAP.md)
- [v0.6 系列全面验收报告](docs/ACCEPTANCE-v0.6-FULL.md)
- [2026-08-22 安全加固组合](docs/hardening/2026-08-22/hardening.md)
- [v0.6.9 简短验收报告](docs/ACCEPTANCE-v0.6.9.md)
- [v0.6.9 发布说明](docs/CHANGELOG-v0.6.9.md)
- [v0.6.9 数据卡](docs/DATA_CARD-v0.6.9.md)
- [v0.6.9 模型卡](docs/MODEL_CARD-v0.6.9.md)
- [v0.6.9 策略卡](docs/POLICY_CARD-v0.6.9.md)
- [v0.6.9 安全边界](docs/SECURITY_REPORT-v0.6.9.md)
- [v0.6.9 安装与复现](docs/INSTALL_REPRODUCE-v0.6.9.md)
- [v0.6.9 迁移说明](docs/V0.6.9_MIGRATION.md)
- [v0.6.8 Git 恢复说明](docs/GIT_RECOVERY-v0.6.8.md)
- [总体架构](docs/ARCHITECTURE.md)
- [v0.6 竞争升级计划](docs/V0.6_COMPETITIVE_UPGRADE_PLAN.md)
- [低能力模型实施手册](docs/V0.6_LOW_CAPABILITY_MODEL_PLAYBOOK.md)
- [v0.6.6 WP07 实施规格](docs/V0.6.6_WP07_IMPLEMENTATION_SPEC.md)
- [v0.6.6 WP07 验收报告](docs/ACCEPTANCE-v0.6.6-wp07.md)
- [v0.6.6 WP07 安全报告](docs/SECURITY_REPORT-v0.6.6-wp07.md)
- [v0.6.6 WP07 安装与复现](docs/INSTALL_REPRODUCE-v0.6.6-wp07.md)

## 当前边界

在得到真实证据前，请不要把以下内容当作已完成能力：

- 尚未用真实供应商响应、真实账单和真实质量标签证明成本下降比例；
- v0.6.1–v0.6.5 的五个智能模块已进入 `/api/v1/runs` 的统一影子决策链，但仍为 advisory-only，尚未接管权威执行路径；
- 默认任务运行仍使用 `SimulatedExecutor`；Gateway 与 Shadow 已建立受控接入边界，但不是开箱即用的真实模型执行链；
- 尚未证明生产并发、跨进程协调、灾难恢复、P50/P95 延迟和长期稳定性；
- 当前持久层以单机 SQLite 为主，不是分布式调度数据库；
- 默认部署是仅回环地址的单用户工具，不具备公网身份认证和多租户安全边界；
- 能力评分主要来自配置、离线基准和模拟证据，不能代替持续的真实评测；
- 图片/视频支持目前是控制与规划边界，不是内置生成服务；
- 外部 OpenTelemetry Exporter 尚未提供，追踪以本地和离线导出为主；
- 小预算 Shadow 的真实执行需要应用侧显式注入受信执行器；
- `engineering_ready=true` 只表示发布门禁通过，`production_ready=false` 仍是权威结论。

## 下一步

v0.6 全面验收完成后，下一开发版本是 **v0.7.0 Data-Ready Main Chain**。先把未来真实数据可以直接接入和反哺的底层合同一次建好，再按决策面逐步激活：

1. **v0.7.0**：建立统一决策点、证据信封、结果归因、候选召回审计、回放和受限策略补丁；只激活规划拓扑，保持零付费；
2. **v0.7.1**：通过一个成熟网关适配器，以极小显式预算打通真实响应、账单、质量标签和重试对账；
3. **v0.7.2～v0.7.3**：依次校准快慢路由、本地容量、恢复权和安全策略候选，开发可并行，决策权只能串行激活；
4. **v0.7.4 以后**：完成预注册竞争证明，再扩展多模态经济编排和生产控制面。

验收采用持续快速门、工作包增量门、版本候选门和风险触发门；不再为每个小工作包重复跑全仓安全复扫和全量回归。在真实样本量、置信区间、回滚条件和竞品基线满足前，不宣称固定节省比例。详细施工顺序见[v0.7 重构实施路线](docs/V0.7_RECONSTRUCTED_ROADMAP.md)。

## 许可证

[MIT License](LICENSE)
