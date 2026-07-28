# CostWeave v0.4.2

发布日期：2026-07-22

## 新增

- SQLite WAL EventStore 和首个版本化迁移
- `ControlKernel` 唯一持久状态写入入口
- commands、events、runs、tasks、attempts 和 projections
- Command ID、Idempotency-Key、规范请求指纹和乐观 revision
- 事件流重放、投影哈希校验和损坏投影回退
- 未完成运行启动恢复与已验收节点保留
- `AttemptId`、`cancelling` 和 `cancelled` 状态
- `/api/v1/runs`、运行事件、取消和重试 API
- `COSTWEAVE_RUNTIME_V4` 与 `COSTWEAVE_RUNTIME_DB`
- 服务器关闭时等待运行到达持久检查点并释放 SQLite 连接

## 兼容性

- v0.3 模型目录格式不变
- 原 `/api/runs` 路径继续可用
- `CommandEnvelope` 和 `EventEnvelope` 继续使用 v4.1 形状
- 关闭 `COSTWEAVE_RUNTIME_V4` 后回到 v0.4.1 内存运行时
- 默认不调用任何真实或付费模型

## 验证

- 固定事件流重放与当前投影一致
- 相同创建和取消命令无重复事件或副作用
- stale revision 零写入，并发同 revision 只有一个提交者成功
- 完成和未完成运行均可在重新打开数据库后恢复
- Windows 下 SQLite 连接在事务后显式关闭
- 旧目录、Planner、Router、Engine、HTTP 和页面测试继续执行

## 已知边界

- 单机 SQLite，不支持分布式调度
- 外部模型和真实工具仍未接入
- Event payload 当前包含完整运行投影，后续版本再优化增量 reducer 与周期快照
