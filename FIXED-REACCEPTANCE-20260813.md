# CostWeave v0.6.6 V066-WP04 修复复验报告

- 日期：2026-08-13
- 结论：PASS
- 证据等级：E0 本地静态夹具、完整回归与独立 wheel 安装验证
- 生产就绪：否

## 已修复

1. 容量快照的有效年龄现在取调用方报告年龄与可信本地时钟根据
   `captured_at` 推导年龄的较大值；旧时间戳与明显的未来时间戳均失败关闭。
2. 流式容量准入在迭代器真正开始执行时发生，紧邻 provider I/O；延迟消费期间
   容量下降会阻止 transport。
3. 不受信任的探针异常不会保留在导出的 `LocalEndpointAdmissionError` 异常链中，
   完整 traceback 不包含探针内部文本。
4. 完整遥测成为不可关闭的安全不变量；`require_complete_telemetry=False` 被明确
   拒绝，避免无效配置误导调用方。
5. 服务器轮询测试使用单调时钟与 30 秒明确截止，修复固定 5 秒预算造成的偶发失败。
6. 修复后的 SHA-256 清单记录 wheel 的真实相对路径；runtime 与 source 归档不再
   使用完全相同的文件集合。

## 验证结果

- WP04 聚焦回归：17/17 通过。
- WP02/WP03/WP04/release/server 兼容回归：80/80 通过。
- 全量回归：969/969 通过，356.739 秒。
- 服务器轮询原失败用例：并行复跑 3/3 通过。
- Release CLI：11/11 门禁通过；`success=true`、`engineering_ready=true`、
  `production_ready=false`、外部调用次数 0。
- 修复后 wheel：独立安装成功；默认关闭与正常准入烟测通过。
- 原始攻击 PoC 在工作区和独立安装的 wheel 中均不再复现：
  - 旧时间戳：`telemetry_stale`，transport 0 次；
  - 延迟流容量下降：`insufficient_slots`，transport 0 次；
  - 探针秘密出现在 traceback：`false`。

## 保留边界

WP04 仍是 E0 本地/静态合同证据。它不证明真实 Ollama/vLLM 指标兼容性、GPU
吞吐量、分布式容量、生产并发、队列预留、成本降低或商业领先性。网络与容量准入
仍默认关闭，真实集成需要调用方提供受信任的实时探针并单独授权本地端点。

