# CostWeave v0.6.7 候选版验收报告

验收更新日期：2026-08-26  
版本：v0.6.7 Security Foundation Recovery  
证据等级：E0（离线、模拟、确定性攻击夹具）  
生产就绪：`false`  
最终状态：**当前源码工程验收与修复后标准安全复扫通过；发布包尚未按最新源码重建，因此仍保留候选版标记。**

## 1. 本轮验收范围

本轮从标准扫描 `bf36015f-c02b-4bc1-8b9b-b600daa97f36` 报告的 7 项发现出发，修复并验证：

- 项目撤销与工件、成本、金丝雀、调度准入、媒体和运行时状态的原子边界；
- 组合百分号、JSON Unicode、Base64/URL-safe Base64 与十六进制编码的活动秘密回显；
- 外部调用 capability 的活动状态、一次性 transport、唯一终态、来源与目的地重放绑定；
- DNS 解析和小预算 Shadow 的进程隔离、绝对截止时间、并发上限与硬终止；
- 重试前的原子预算扩容，以及成功、失败、取消三种结算中的全部尝试成本；
- 运行请求身份不可变，以及查询、列表、撤销、审批、媒体、取消和 checkpoint 对持久化权威归属的校验；
- 后台线程异常不能再被普通测试结果掩盖。

没有启用真实 Provider、付费调用、公网监听或生产数据。

## 2. 验收结果

| 验收门 | 最新结果 |
|---|---|
| 联合安全与控制回归 | 114/114 通过 |
| 驻留区跨入口矩阵 | 4/4 通过 |
| 全量测试 | 1058/1058 通过，478.103 秒 |
| 逐模块独立解释器测试 | 1058/1058 通过，489.17 秒 |
| 严格 `ResourceWarning` 回归 | 54/54 通过，52.174 秒 |
| 发布门禁 | 19/19 通过 |
| 真实外部调用尝试 | 0 |
| `engineering_ready` | `true`（仅限离线单进程控制平面） |
| `production_ready` | `false` |
| 修复后标准安全复扫 | 12/12 威胁面闭合，0 项可报告发现 |

全量测试的 1058 项也在逐测试文件独立启动解释器的模式下重新执行。独立运行器会把 `Exception in thread` 和 `Exception ignored in:` 视为失败，本轮没有检测到这类后台异常。

## 3. 安全复扫

- 修复前扫描：`bf36015f-c02b-4bc1-8b9b-b600daa97f36`；5 Medium、2 Low；
- 修复后扫描：`6897135e-a85a-4c65-9088-d62a3ae6f573`；状态 `complete`；
- 修复后快照：`codex-security-snapshot/v1:sha256:1950bd7a7bd6caa5cc4f96d030afd17d5d2f7d49e0d17afa30e43cf263f76ef8`；
- 扫描覆盖：12/12 威胁面，`coverage=complete`；
- 最终发现：0。

扫描把 `build/`、`dist/`、`outputs/`、`work/`、`.git/`、`.agents/`、`.codex/` 与 `costweave.egg-info/` 识别为生成物、历史副本或本地元数据，不把它们当作独立权威实现。当前可执行源码、测试、README、v0.6.7 验收/安全文档和打包配置均在扫描清单中。

## 4. 关键修复的可验证行为

- 每次瞬时重试必须在下一次执行前原子扩充活动成本预留；预算不足时不会再次调用执行器；
- 最终结算按“初始尝试 + 已资助重试”计费，成功、失败和取消路径使用同一规则；
- 项目撤销可阻止或等待所有受保护写入，撤销后不能新增工件失效、成本、金丝雀、调度、媒体或 checkpoint 状态；
- 查询、列表和撤销后的运行筛选使用持久化项目与驻留区归属；内存归属冲突失败关闭；
- DNS 与 Shadow 子进程超时后会被终止或杀死，并且在子进程真正结束前不归还并发槽；
- 外部副作用终态必须来自仍活动、已消费 transport 且合同一致的 capability；
- 秘密检查对受控组合编码进行有界遍历，达到深度或候选预算时失败关闭。

## 5. 发布门禁证据

```text
python -B -m unittest discover -s tests -t .
python -B tests/run_isolated.py --timeout 240
python -B -W error::ResourceWarning -m unittest tests.test_engine tests.test_parallel_runtime tests.test_security_rescan_fixes_v067.MediaOwnerAndRevocationTests tests.test_residency_scope_matrix_v067
python -B -m costweave.release_cli --pretty
```

发布 CLI 返回版本 v0.6.7、运行时 Schema v6.5.0、35 个迁移、19/19 门禁通过、`success=true`、`engineering_ready=true`、`production_ready=false`、真实外部调用尝试 0。

## 6. 验收决定与剩余工作

- **已确认**：当前源码的安全修复、兼容性、资源释放、驻留区矩阵、全量/隔离测试、发布门禁和修复后标准复扫通过；
- **尚未确认**：真实供应商回答质量、真实账单、真实成本下降比例、生产吞吐、分布式恢复、多用户隔离和公网部署安全；
- **发布资产状态**：先前 wheel、sdist 和哈希对应旧源码，不能作为当前最终包；
- **下一动作**：从干净 staging 重建 wheel 与源码包，干净安装后复跑 19 项门禁，再生成新哈希与 GitHub Release 资产。

因此，当前可以称为“E0 离线工程验收和修复后安全复扫通过的候选源码”，不能称为生产就绪产品。
