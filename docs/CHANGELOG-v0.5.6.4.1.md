# CostWeave v0.5.6.4.1

## 新增

- `predictor_admission.py`：三档真实证据准入、隐私安全的数据摘要、可回放特征键和冻结预测凭证。
- `predictor_admission_store.py`：不可变准入报告、完整性校验、幂等重放与运行时计数。
- migration 017：`predictor_admission_reports` 与 `frozen_prediction_receipts`。
- `GET /api/v1/predictors/policy`：公开门槛、冻结状态、正式路由所有者与零权威学习预测计数。
- Outcome 数据集写入 ControlKernel 后自动执行全部三档准入判断，不需要用户手工触发。

## 安全边界

- E0/E1 不可作为真实校准证据；混入即冻结。
- 30 个 E2+ 有效任务只能做方向性观察，不能训练。
- 内部校准候选至少需要 200 个有效任务、完整 train/calibration/test 分区和至少 4 个任务族。
- 正式基准至少需要 1000 个有效任务、8 个任务族、每族至少 100 个。
- 所有档位都不能在本版晋级策略或控制正式路由。
- 冻结状态不产生伪概率，使用 `null` 与 `[0,1]` 表达未知。
- 不读取 Provider 密钥，不产生外部模型调用。

## 版本

- 包版本：`0.5.6.4.1`。
- Runtime Schema：`v5.6.4.1`。
- Run Projection Schema：继续兼容 `v5.5`。

## 证据声明

本版完成的是学习前的工程安全门，不是学习效果。真实 Provider 调用数、真实训练次数和策略晋级次数均为 0，不支持任何真实降本或性能领先声明。
