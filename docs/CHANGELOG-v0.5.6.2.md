# CostWeave v0.5.6.2

## 新增

- 匿名化 `GovernedOutcome`、版本化数据集清单和数据卡；
- 自动硬验收、独立模型、人工盲评、用户反馈与系统终态标签；
- 未独立验收正向标签拒绝、冲突标签并列和延迟反馈追加；
- 相似任务去重组与样本权重；
- 项目分区泄漏、敏感内容、版本追溯和成本对账检查；
- TTL、用途限制、学习副本删除和无正文审计摘要；
- migration 015 与 `OutcomeDatasetStore`；
- `GET /api/v1/outcomes/governance`；
- `costweave-dataset` / `python -m costweave.outcome_dataset`；
- 不可变 manifest、quality report、samples/labels JSONL 与数据卡导出。

## 验收结论

内置 175 条 E0 观察被安全转换为 175 条路线样本和 35 个有效任务。治理检查通过，但训练固定冻结，原因包括模拟证据、有效样本不足和候选路线集合捕获不完整。本结论不代表真实模型质量、真实降本或商业优势。
