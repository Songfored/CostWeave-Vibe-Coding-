# CostWeave v0.4.4

发布日期：2026-07-28

## 新增

- 关键路径优先的可靠并行调度器，并保留 FIFO 兼容策略
- 仿真 Provider 资源池、CPU/GPU/显存/并发槽和队列容量
- 容量、期限、队列、Token Bucket 与 Provider 熔断准入门
- 资源租约和预计成本的 SQLite 原子预留
- 持久、幂等、带 revision 的非阻塞审批
- 暂时性 Provider 错误有限重试与 closed/open/half-open 熔断状态
- `resource_pools`、`resource_snapshots`、`resource_reservations`
- `cost_reservations`、`approvals`、`provider_circuits`
- 资源、审批读取和审批决议 API
- 页面资源预留、活跃租约、在途成本、排队与审批状态
- `COSTWEAVE_PARALLEL_RUNTIME` 功能回滚开关

## 改进

- 资源或期限必然不满足的任务在第一次执行前拒绝
- 同一运行、任务和尝试的并发请求只产生一份资源与成本预留
- 单个节点等待审批时，无关并行分支继续执行
- 失败诊断先于终态发布，API 不再短暂出现“失败但无原因”
- 重规划不再把一次执行失败重复计算为高业务风险
- 暂时性重试次数进入任务合同与运行投影
- 运行投影和数据库 Schema 升级到 v4.4

## 验收

- 141 项自动化测试全部通过
- 1000 节点链式 DAG 无死锁并完成确定性关键路径评分
- 16 线程争抢同一尝试时重复资源与成本预留率为 0
- 审批等待期间无关分支继续率为 100%
- 永久容量不足时首次执行前拒绝率为 100%
- 熔断 closed、open、half-open 状态与恢复行为通过测试
- JavaScript 语法、HTML 解析、Python 编译和资源/审批 API 冒烟通过

## 边界

- 仍为离线模拟执行器，不调用真实模型 API
- 资源容量是仿真值，不是 CPU/GPU 实时遥测
- 运行时仍为单进程、单机 SQLite，不支持多实例共享租约
- 不包含 v0.4.5 的本地能源、机会成本和全局 Placement 优化
