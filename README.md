# CostWeave

**版本化合同、确定性状态守卫、可编辑模型知识库与并行验收驱动的多模型 Agent 决策编排原型。**

CostWeave v0.4.1 在完整保留 v0.3 决策能力的基础上，冻结统一 ID、`CommandEnvelope`、`EventEnvelope` 和状态转换合同；运行与任务状态变更经过确定性守卫，并留下带来源与拒绝原因的进程内调试踪迹。

> v0.4.1 仍是离线决策与编排原型，不调用真实模型 API。合同、状态守卫、模型选择、成本计算、目录热更新、DAG、验收和重规划是真实逻辑；最终领域成果仍由模拟执行器产生。状态踪迹不持久化，重启恢复属于 v0.4.2。

## v0.4.1 新增：合同与状态守卫

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

v0.4.1 默认启用合同守卫。临时回退到 v0.3 状态赋值语义：

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
并行专业执行 → 逐条验收 → 冲突审查 → 最终汇总
  ↓ 失败证据
影响范围分析 → 保留有效成果 → 同快照重新路由 → 恢复执行
```

## 项目结构

```text
costweave/
├── contracts_v4.py    # 统一 ID、版本化命令与事件 Envelope
├── state_guard.py     # 运行/任务状态转换表与纯函数守卫
├── compat_v4.py       # v0.3 快照兼容、功能开关与进程内踪迹
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

所有模型写操作都可携带 `expected_revision`。目录已经变化时返回 `409 catalog_revision_conflict`。

## 测试

```bash
python -B -m unittest discover -s tests -v
```

当前基线为 60 / 60 通过，其中 19 项专门覆盖 v0.4.1 合同、状态守卫和兼容回滚。

测试覆盖：

- 模型 CRUD、JSON/CSV 往返、原子导入和 revision 冲突
- 新运行热加载、进行中运行快照隔离
- 分类消歧、六维复杂度和任务级难度
- 显式工具约束、保守质量门槛和验收模型独立性
- `reject` 门控、安全边界、预算、异步并行和重规划
- 统一 ID、Envelope JSON 往返、非法转换、终态保护和开关回滚
- 页面结构、响应式基础规则和管理交互绑定

## 当前边界

- 可导入最新资料不等于资料已被自动核实；来源、核验时间和能力先验仍由导入者负责。
- 能力、速度、可靠性和资料可信度是可校准先验，不是厂商官方基准成绩。
- 当前没有汇率服务、阶梯价格、批处理折扣和本地硬件成本折算。
- 当前执行器不调用真实模型，因此不能证明最终领域答案质量。
- v0.4.1 的合同踪迹只存在于当前进程；重启恢复和 EventStore 属于 v0.4.2。
- 高级规划节点已经具备严格门控，但模拟执行器不会生成真正的外部模型 `PlanPatch`。
- 当前服务只允许回环地址，不应直接暴露到公网。

下一版本 v0.4.2 将实现 SQLite EventStore、幂等命令、快照重放和启动恢复；后续结果分布预测、资源经济联合优化、媒体 DAG、准入调度和学习闭环设计见 [`docs/V0.4_UPGRADE_ROADMAP.md`](docs/V0.4_UPGRADE_ROADMAP.md)。

为避免一次性实现过大蓝图，v0.4 已拆分为 v0.4.1～v0.4.9 九个可独立验收的稳定版本；v0.4.1“合同与状态守卫”已经完成。详见 [`docs/V0.4_INCREMENTAL_RELEASES.md`](docs/V0.4_INCREMENTAL_RELEASES.md)。

## License

MIT
