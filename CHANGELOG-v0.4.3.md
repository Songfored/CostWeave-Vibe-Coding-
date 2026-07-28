# CostWeave v0.4.3

发布日期：2026-07-27

## 新增

- 内容寻址、不可变、带 SHA-256 完整性校验的 Artifact
- Artifact 生产模型、计划修订、输入引用和派生血缘
- 版本化 `MessageEnvelope` 模型交接合同
- hard、soft、validation 三类依赖
- `GraphCompiler` 环、生产者、Schema、权限和影响范围检查
- `artifacts`、`artifact_links`、`task_dependencies`、`messages` 数据表
- 任务、Artifact、运行成果列表和血缘图只读 API
- 页面“成果与血缘”视图
- `COSTWEAVE_ARTIFACT_DAG` 回滚开关

## 改进

- 执行器在 Artifact 模式下不再直接读取上游任务的内存结果
- 重规划先计算受影响子图，只使相关 Artifact 过期
- v0.4.2 已验证结果可在恢复时无损物化为 Artifact
- 运行投影和数据库 Schema 升级到 v4.3

## 验收

- 103 项自动化测试
- 覆盖哈希篡改、缺失生产者、过期输入、重复消息、过期计划、Schema/权限冲突、DAG 环和局部影响范围
- 覆盖任务、Artifact 和血缘图 API
- 保留 v0.3、v0.4.1 和 v0.4.2 回归测试

## 边界

- 仍为离线模拟执行器，不调用真实模型 API
- 仍为单机 SQLite，不包含 v0.4.4 的资源准入和分布式并行调度
- 不包含 v0.4.5 成本经济优化或 v0.4.7 在线学习
