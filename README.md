# CostWeave

**以资源准入、原子预留和不可变成果为边界，可持久恢复、可重放且由证据验收驱动的多模型 Agent 编排原型。**

CostWeave v0.4.4 在 Artifact Graph 上加入可靠并行运行时。任务启动前会依次检查容量、期限、队列、Provider 熔断和 Token Bucket，并在同一事务中预留资源与预算；等待人工审批的节点不会阻塞无关分支。

> v0.4.4 仍是离线决策与编排原型，不调用真实模型 API，也不读取真实 GPU 遥测。调度、准入、租约、成本预留、审批、熔断、Artifact、血缘、持久化、幂等、DAG、验收和重规划是真实逻辑；最终领域内容和资源容量仍由模拟器产生。

## v0.4.4 新增：可靠并行运行时与资源准入

- 就绪节点按关键路径长度、优先级和关键度排序，并保留 FIFO 回滚策略
- CPU、GPU、显存、并发槽和队列容量在任务执行前检查
- 资源租约与预计成本使用 SQLite `BEGIN IMMEDIATE` 原子预留
- 同一 `run + task + attempt` 重试只会得到同一份预留，不重复占用
- 期限明显无法满足或资源要求超过池上限时，在首次执行前拒绝
- Token Bucket 控制吞吐，Provider 熔断器支持 closed、open、half-open
- 暂时性 Provider 错误有限重试，成功后复位熔断状态
- 需要审批的节点生成持久审批单；其他无依赖分支继续执行
- 重启时安全释放遗留租约，资源与成本不会永久悬挂
- 页面显示资源预留、活跃租约、在途成本、排队和等待审批状态
- 新增资源与审批 API
- `COSTWEAVE_PARALLEL_RUNTIME=0` 可回到 v0.4.3 兼容执行路径

## 继承的 v0.4.3 Artifact Graph 与结构化交接

- 每个任务输出内容寻址、不可变且带 Schema 的 Artifact
- Artifact 记录生产任务、模型、计划版本、尝试次数、敏感级别和输入血缘
- 内容变化生成新 Artifact ID，不覆盖旧内容
- 上游 Artifact 必须通过哈希与 `validated` 状态检查后才能进入交接包
- `MessageEnvelope` 明确目标、范围、排除项、输入引用、输出规格和验收标准
- 重复交接消息幂等，不会造成重复执行
- hard、soft、validation 三类任务依赖
- 图编译阶段拒绝环、缺失生产者、Schema 冲突和权限冲突
- 重规划计算受影响子图，只把相关 Artifact 标记为 stale
- 新增任务、Artifact、运行 Artifact 列表和血缘图 API
- 页面新增“成果与血缘”视图
- `COSTWEAVE_ARTIFACT_DAG=0` 可关闭新写入并继续读取既有数据库

## 继承的 v0.4.2 可靠控制内核

- SQLite WAL 和版本化 Schema migration
- `Command → Event → Projection` 原子事务
- `runs`、`tasks`、`attempts`、`commands`、`events` 和 `projections` 持久化
- 每个运行独立、连续的 stream revision 与 `expected_revision` 乐观并发
- Command ID 和 Idempotency-Key 双重去重；相同请求返回首次结果
- 损坏投影自动回退到追加事件流重放
- 未完成运行启动恢复；已验收节点不重复执行
- 显式 `cancelling → cancelled` 终态和幂等取消
- `/api/v1/runs`、事件时间线、取消和重试接口
- `COSTWEAVE_RUNTIME_V4=0` 可回退到 v0.4.1 内存运行时，已有事件库保持只读

## 继承的 v0.4.1 合同与状态守卫

- `RunId`、`TaskId`、`EventId`、`ArtifactId` 使用可校验的统一格式
- 旧语义 ID 通过确定性哈希映射为稳定 v4 ID，不破坏 v0.3 顶层 API
- `CommandEnvelope` 与 `EventEnvelope` 使用 `schema_version: "v4.1"`
- Envelope 严格校验字段、ID、时间、事件类型和有限 JSON 数据
- 运行与任务状态使用显式合法转换表；终态不能回退
- 重复写入同一状态按无副作用操作处理
- 所有引擎状态赋值都经过兼容守卫
- 接受和拒绝的转换都会记录来源、前后状态与拒绝原因
- `COSTWEAVE_CONTRACTS_V4=0` 可关闭守卫并恢复 v0.3 行为
- 不新增数据库、迁移器、调度器、模型调用或页面主流程

## 继承的 v0.3 核心能力

### 可维护的模型知识库

- 页面直接新增、编辑和删除模型
- JSON / CSV 原子导入；支持合并或完整替换
- JSON / CSV 导出和恢复内置基线
- 字段、类型、范围、URL、货币、Token 上限和重复 ID 校验
- 每次修改产生新的目录修订号，并通过乐观锁防止覆盖他人修改
- 临时文件加原子替换持久化；保存失败时继续使用旧目录
- 持久化目录损坏时回退内置基线，页面仍可打开并执行恢复默认
- 新任务立即读取最新目录；运行中的任务固定使用启动时不可变快照
- 默认数据保存在 `~/.costweave/model_catalog.json`
- 可通过 `COSTWEAVE_CATALOG_PATH` 指定便携目录或测试目录

### 分层任务理解

- 独立评分的多标签专业域，而不是任意关键词命中
- 主意图：构建、分析、研究、决策、转换、验收、故障诊断
- 交付物：代码、报告、方案、数据集、决策、译文和视觉成果
- 显式约束抽取：预算、期限、格式、本地、开源、并行等
- 分类结果包含分数、触发证据、分类置信度与词表版本
- 复杂度拆成认知、协作、证据、工具、领域和范围六个维度
- 医疗、法律、金融、安全等风险结合“动作 + 对象”判断，减少只因领域名词而误报
- 每个专业分支拥有独立难度、关键度、不确定性和能力权重

### 风险调整路由

- 工具能力严格匹配；`function_calling` 不再被错误视为 `code_execution`
- 模型目录资料时效和 `data_confidence` 参与预测
- 同时输出成功率均值、保守下界和预测不确定性
- 质量门槛约束保守下界，而不是乐观均值
- 模型淘汰会保留上下文、工具、模态、货币、输出上限和能力短板原因
- 组合路由先求满足质量与独立验收约束的最低成本，再按边际收益升级
- 验收节点优先使用不同模型；高风险节点进一步优先不同 Provider
- 路由结果记录准确目录修订号，支持复现

### 可执行验收闭环

- 验收、风险、安全和高级规划节点必须返回逐条 `criteria_results`
- 只有 `decision=pass` 才能解锁下游
- `revise`、`reject` 和 `human_review` 不会再被格式校验误判为通过
- 当前成功率根据任务保守先验和实际验收证据重新计算
- 失败后不再无条件抬高成功率
- 重规划仍固定使用本次运行的目录快照，避免中途模型资料变化

## 快速运行

需要 Python 3.11 或更高版本，不需要第三方依赖。

```bash
python -m costweave.server
```

v0.4.4 默认启用合同守卫、持久运行时、Artifact DAG 和并行运行时。只关闭 v0.4.4 资源准入、回到 v0.4.3 执行路径：

```powershell
$env:COSTWEAVE_PARALLEL_RUNTIME="0"
python -m costweave.server
```

只关闭 Artifact DAG、保留 v0.4.2 运行时：

```powershell
$env:COSTWEAVE_ARTIFACT_DAG="0"
python -m costweave.server
```

关闭持久运行时、回到 v0.4.1 内存模式：

```powershell
$env:COSTWEAVE_RUNTIME_V4="0"
python -m costweave.server
```

同时关闭合同守卫并回退到 v0.3 状态赋值语义：

```powershell
$env:COSTWEAVE_CONTRACTS_V4="0"
python -m costweave.server
```

打开：

```text
http://127.0.0.1:8765
```

安装命令行入口：

```bash
python -m pip install -e .
costweave
```

## 模型管理

页面底部的“模型管理中心”支持：

1. 搜索和按 Provider、路由状态筛选。
2. 新增模型，填写厂商事实与 CostWeave 路由先验。
3. 编辑价格、上下文、工具、能力、来源和核验日期。
4. 导入 JSON 或 CSV。
5. 导出当前目录作为备份或迁移文件。
6. 恢复内置模型基线。

编辑后的模型会被标记为“用户修改”。核验日期超过 180 天会在页面标记“资料过期”。非 USD 云模型若没有汇率快照必须保持不可路由，但仍可用于资料展示。

### JSON 导入

可以导入完整导出文件：

```json
{
  "metadata": {
    "schema_version": 1
  },
  "models": [
    {
      "id": "example-model",
      "name": "Example Model",
      "provider": "example",
      "specialty": "分析与结构化输出",
      "capabilities": {
        "analysis": 0.82,
        "planning": 0.78,
        "validation": 0.8
      },
      "cost_per_task": 0,
      "latency_factor": 0.8,
      "reliability": 0.9
    }
  ]
}
```

缺省字段会使用安全默认值。导入模型默认被标记为用户数据；任意一项校验失败时整批导入回滚。

### CSV 导入

建议先从页面导出 CSV 模板，再修改后重新导入。`capabilities` 是 JSON 对象；`modalities`、`tools`、`strengths` 和 `limitations` 可使用 JSON 数组，或用 `|` 分隔。

## 决策流程

```text
用户目标
  ↓
分层画像：意图 / 专业域 / 交付物 / 约束 / 风险 / 六维复杂度
  ↓ 低置信、高难或高风险
高级规划复核门
  ↓
任务级合同、能力权重、关键度与并行 DAG
  ↓
冻结当前模型目录修订
  ↓
硬约束过滤 → 成功均值 / 保守下界 / 不确定性
  ↓
最低可行组合 → 独立验收约束 → 预算内边际升级
  ↓
结构化 MessageEnvelope → 校验输入 Artifact → 并行专业执行
  ↓
不可变输出 Artifact → 逐条验收 → 冲突审查 → 最终汇总
  ↓ 失败证据
受影响子图分析 → 仅将相关 Artifact 标记过期 → 保留有效兄弟成果
  ↓
同快照重新路由 → 恢复执行
```

## 项目结构

```text
costweave/
├── contracts_v4.py    # 统一 ID、版本化命令与事件 Envelope
├── state_guard.py     # 运行/任务状态转换表与纯函数守卫
├── state_machine.py   # 持久内核使用的统一状态机出口
├── commands.py        # 命令类型、幂等键和规范请求指纹
├── event_store.py     # SQLite WAL、事务、投影和事件重放
├── control_kernel.py  # 唯一持久状态写入者与启动恢复
├── projections.py     # 稳定投影序列化和 RunRecord 水合
├── artifacts.py       # 不可变 Artifact、内容哈希和完整性检查
├── artifact_store.py  # Artifact、血缘、交接消息与图持久化
├── messages.py        # 版本化模型交接 MessageEnvelope
├── graph_compiler.py  # 依赖、生产者、Schema、权限和影响范围检查
├── scheduler.py       # 关键路径就绪队列、准入和暂时性错误重试
├── admission.py       # 容量、期限、背压、Token Bucket 与熔断
├── resource_inventory.py # 仿真资源池与容量快照
├── reservations.py    # 资源和预计成本的原子租约
├── approvals.py       # 非阻塞持久审批
├── contracts.py       # v0.4.x 公共合同入口
├── migrations/        # 版本化运行数据库 Schema
├── compat_v4.py       # v0.3/v0.4.1 兼容与功能开关
├── domain.py          # 运行、模型、任务画像、合同与计划
├── model_catalog.py   # 可恢复的内置模型基线
├── catalog_store.py   # 校验、CRUD、导入导出、版本与持久化
├── planner.py         # 分层画像、复杂度、风险、DAG 与升级门
├── router.py          # 快照、硬约束、保守预测和组合优化
├── validator.py       # DAG、Schema、逐条标准和门控语义
├── executor.py        # 离线结构化模拟执行器
├── engine.py          # 快照冻结、并行、预算、证据概率与重规划
├── server.py          # 本地 HTTP API 与静态页面
└── web/               # 任务工作台和模型管理中心
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 运行状态、版本、合同 Schema、功能开关和目录修订 |
| `GET` | `/api/catalog` | 当前模型目录和元数据 |
| `GET` | `/api/catalog/schema` | 字段与导入格式 |
| `GET` | `/api/catalog/export?format=json` | 导出 JSON |
| `GET` | `/api/catalog/export?format=csv` | 导出 CSV |
| `POST` | `/api/catalog/models` | 新增模型 |
| `PUT` | `/api/catalog/models/{id}` | 编辑模型 |
| `DELETE` | `/api/catalog/models/{id}` | 删除模型 |
| `POST` | `/api/catalog/import` | 合并或替换导入 |
| `POST` | `/api/catalog/reset` | 恢复内置目录 |
| `POST` | `/api/runs` | 创建任务画像、计划和模拟运行 |
| `GET` | `/api/runs` | 最近运行 |
| `GET` | `/api/runs/{id}` | 计划、路由、事件、成本和成果 |
| `POST` | `/api/v1/runs` | 使用 `Idempotency-Key` 幂等创建持久运行 |
| `GET` | `/api/v1/runs/{id}` | 获取带 stream revision 的运行投影 |
| `GET` | `/api/v1/runs/{id}/events` | 按 revision 分页读取持久事件流 |
| `GET` | `/api/v1/runs/{id}/artifacts` | 读取运行的 Artifact 元数据 |
| `GET` | `/api/v1/runs/{id}/graph` | 读取任务依赖与 Artifact 血缘图 |
| `GET` | `/api/v1/tasks/{task_id}` | 读取持久任务合同和状态 |
| `GET` | `/api/v1/artifacts/{artifact_id}` | 校验并读取 Artifact 元数据与血缘（不返回内容） |
| `GET` | `/api/v1/resources` | 读取资源池、容量和当前可用量 |
| `GET` | `/api/v1/approvals?status=pending` | 按状态读取审批单 |
| `POST` | `/api/v1/approvals/{id}/resolve` | 用 revision 与幂等键批准或拒绝节点 |
| `POST` | `/api/v1/runs/{id}/cancel` | 使用幂等键和 `expected_revision` 取消 |
| `POST` | `/api/v1/runs/{id}/retry` | 为失败或取消运行创建关联重试 |

所有模型写操作都可携带 `expected_revision`。目录已经变化时返回 `409 catalog_revision_conflict`。

## 测试

```bash
python -B -m unittest discover -s tests -v
```

当前基线为 141 项自动化测试。v0.4.4 新增测试覆盖 1000 节点 DAG、关键路径与 FIFO、原子资源/成本预留、并发去重、容量和期限前置拒绝、Token Bucket、背压、熔断全状态、暂时性重试、租约恢复、审批幂等、审批并发冲突、无关分支继续和功能回滚。

测试覆盖：

- 模型 CRUD、JSON/CSV 往返、原子导入和 revision 冲突
- 新运行热加载、进行中运行快照隔离
- 分类消歧、六维复杂度和任务级难度
- 显式工具约束、保守质量门槛和验收模型独立性
- `reject` 门控、安全边界、预算、异步并行和重规划
- 统一 ID、Envelope JSON 往返、非法转换、终态保护和开关回滚
- 固定事件流 100% 重放、命令去重、CAS 冲突和跨运行任务 ID 隔离
- 完成与未完成运行重启恢复、已验收成果保留和终态不重启
- Artifact 哈希、不可变版本、血缘、过期传播和结构化交接
- DAG 环、生产者、Schema、权限与 stale plan revision 拒绝
- 资源准入、原子租约、预算预留、熔断、背压和期限前置拒绝
- 非阻塞审批、暂时性错误重试、启动租约恢复和调度策略回滚
- 页面结构、响应式基础规则和管理交互绑定

## 当前边界

- 可导入最新资料不等于资料已被自动核实；来源、核验时间和能力先验仍由导入者负责。
- 能力、速度、可靠性和资料可信度是可校准先验，不是厂商官方基准成绩。
- 当前没有汇率服务、阶梯价格、批处理折扣和本地硬件成本折算。
- 当前执行器不调用真实模型，因此不能证明最终领域答案质量。
- 运行 EventStore 默认保存在 `~/.costweave/runtime-v4.sqlite3`，可用 `COSTWEAVE_RUNTIME_DB` 指定路径。
- v0.4.4 是单进程、单机 SQLite 控制内核，不提供分布式事件总线或多进程调度。
- 资源池、CPU/GPU/显存和 Provider 吞吐目前是目录派生的仿真值，不是实时硬件遥测。
- 重启恢复按单实例所有权释放遗留租约；多实例租约续期与节点所有权尚未实现。
- 真实外部 Provider 尚未接入，因此幂等保证目前覆盖控制状态和模拟执行；真实外部副作用仍需 Provider 幂等协议。
- 高级规划节点已经具备严格门控，但模拟执行器不会生成真正的外部模型 `PlanPatch`。
- 当前服务只允许回环地址，不应直接暴露到公网。

下一版本 v0.4.5 将实现模型 × Provider × 资源池 × 推理配置 × 开始窗口的联合成本决策，并显式计算本地硬件、排队、失败返工与机会成本；后续结果分布预测、媒体 DAG 和学习闭环设计见 [`docs/V0.4_UPGRADE_ROADMAP.md`](docs/V0.4_UPGRADE_ROADMAP.md)。

为避免一次性实现过大蓝图，v0.4 已拆分为 v0.4.1～v0.4.9 九个可独立验收的稳定版本；v0.4.1～v0.4.4 已完成。详见 [`docs/V0.4_INCREMENTAL_RELEASES.md`](docs/V0.4_INCREMENTAL_RELEASES.md)。

## License

MIT
