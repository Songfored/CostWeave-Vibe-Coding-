# CostWeave 架构说明

CostWeave v0.1 是一个不依赖真实模型的 Agent Runtime 雏形。它通过模拟执行器验证产品最重要的控制面，而不是伪装成已经完成的通用 AI。

## 控制流

```text
RunRequest
    │
    ▼
HeuristicPlanner ── 难度、类型、风险、DAG、任务合同
    │
    ▼
ContractValidator ── Schema、依赖完整性、循环检测
    │
    ▼
PredictiveRouter ── 能力匹配、成功率、成本、延迟
    │
    ▼
OrchestrationEngine ── 异步并行、检查点、预算守卫
    │
    ├── SimulatedExecutor
    ├── Result Validation
    └── Replan / Recovery
```

## 关键设计

### 预测式路由

路由发生在执行之前。每个执行者包含能力向量、成本、延迟和可靠性；系统根据运行模式计算效用，并选择预计达到质量门槛的最优执行者。

### 任务合同

每个节点明确描述目标、范围、依赖、能力要求、输出Schema和验收标准。未来真实模型只会收到当前节点所需的最小充分上下文。

### DAG调度

引擎只启动所有依赖均已 `validated` 的节点，使用 `asyncio.wait(..., FIRST_COMPLETED)` 动态释放下游，而不是固定分批等待。

### 验收与恢复

节点输出先经过结构和证据检查。根本性错误会触发计划版本更新：失败节点失效，受影响依赖被重连到恢复节点，已经通过验收且不受影响的结果继续保留。

### 边界

当前执行结果是结构化模拟内容，不包含真实领域答案。`SimulatedExecutor` 是有意设置的替换点，未来可以实现：

- `OllamaExecutor`
- `OpenAICompatibleExecutor`
- `ToolExecutor`
- `HumanApprovalExecutor`

## 状态

运行状态：`created → planning → executing ↔ replanning → completed/failed`

节点状态：`pending → running → validated/rejected/invalidated`

只有 `validated` 节点能够释放下游依赖。

