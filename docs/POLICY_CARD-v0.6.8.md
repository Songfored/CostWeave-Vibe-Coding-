# CostWeave v0.6.8 策略卡

- 当前应用版本只允许由 `costweave/version.py` 定义；
- 外部模型调用、付费调用、真实 Shadow 和策略自动晋升默认关闭；
- 发布物必须来自白名单 staging，并通过成员路径与敏感后缀检查；
- 构建目录只有带 CostWeave 标记时才可由构建器清理；
- `engineering_ready` 只代表发布门禁，`production_ready` 固定为 `false`；
- E0 页面、测试和打包数据不得转换为真实降本或商业领先结论。
