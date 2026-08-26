# CostWeave 安全加固分析上下文

分析日期：2026-08-22  
本地源码根目录：`C:\Users\asus\Documents\Codex\2026-07-16\w`  
分析 ID：`hardening_20260822_costweave`  

## 权威扫描

- 扫描 ID：`ce00af03-b50a-49ff-85a3-ed679db48d66`
- 模式：Standard / repository snapshot
- 扫描状态：completed
- 快照：`codex-security-snapshot/v1:sha256:429e72d4cd2a9556915a389fb87cabda1f8c3110671184e0797865abcfa0ce01`
- findings.json SHA-256：`fbc57d5b076737b142f1c06c40064dc56e64efa4bef62264c3c69ebfcab379a1`
- coverage.json SHA-256：`70848f515ebfdd76b683522a0024c2749dbafb2c68f5606db59dd8a65c4b97d0`
- 本地扫描报告：`C:\Users\asus\AppData\Local\Temp\codex-security-scans-ES5nsk\w\unversioned_20260821T141317Z_uroo_oxf\report.md`
- 源漂移：`present`。扫描完成后修改了 README、路线图与验收产物；本次设计重新检查了涉及的当前根源码，但没有把扫描快照误称为当前 Git revision。当前 `.git` 不可用。

## 补充证据

| 证据 | 文件 | SHA-256 | 用途 |
|---|---|---|---|
| `A001` | `outputs/COSTWEAVE_FULL_ACCEPTANCE_2026-08-21.md` | `77492FB03782050AE6E0FF4C92384C8CA20DAEECD3210FF6D0F9895F2D51DD76` | 功能、架构、页面、发布与安全综合验收 |
| `A002` | `docs/SECURITY_REPORT-v0.6.6-wp07.md` | `5EDC68568710C62ACF6805DC3CA4730551D082143F02EDB1B8E8925E180DBAFB` | WP07 已知安全与真实数据边界 |
| `A003` | `docs/REMAINING_ROADMAP.md` | `9D433110C3319F942014FC75CCA19FE064812187E5260AA71F420DBAD9C1CB99` | Revision 4 实施顺序 |

## 扫描发现登记

| ID | 级别 | 标题 | 主要位置 |
|---|---|---|---|
| `csf_afbe83962f948e1062ed4a46` | High | Provider 回显可把当前有效密钥写入结果与 SQLite | `costweave/security_boundary.py:53`、`costweave/adapters/http.py:700` |
| `csf_f0dc0afc4bbdc952e9418b59` | Medium | Windows 盘符路径可越过静态资源目录读取本机文件 | `costweave/server.py:2835` |
| `csf_11aebc6fb58879228fdefb9b` | Medium | GET 接口未校验 Host，可被 DNS rebinding 读取本地数据 | `costweave/server.py:236` |
| `csf_0259e30cb2f655aafc1a87dc` | Medium | 回环管理 API 没有身份认证，项目头可由调用方任意声明 | `costweave/server.py:212` |
| `csf_b161cf81db311acca2687333` | Medium | 项目撤销没有覆盖 evidence 与 v0.6 决策/学习服务 | `costweave/server.py:455` |
| `csf_c45f21351f60228cb3040624` | Medium | 数据地域只被记录，不参与后续读取与动作授权 | `costweave/engine.py:1952` |
| `csf_63792ed1c45566b8f9e22121` | Medium | 任意格式正确的 approval ID 都能冒充安全审批 | `costweave/security_boundary.py:235` |
| `csf_f987e97218460d8b9ec22e7e` | Medium | 可能已计费的失败调用会释放全部美元预算预留 | `costweave/experimental_calls.py:336` |
| `csf_e0bd253ba41addebede73568` | Medium | 跨实验的全局副作用 replay 判定被适配器忽略 | `costweave/adapters/http.py:594` |
| `csf_87708be056f6391c9a8e5923` | Medium | 远端端点 DNS 校验与实际 urllib 连接之间存在重绑定窗口 | `costweave/adapters/http.py:238` |
| `csf_3216b2dff44d7f383d554b7c` | Low | HTTP 服务按连接无限创建线程，缺少并发上限 | `costweave/server.py:149` |

## 聚类依据

这些问题没有按 CWE 机械归类，而是按控制所有权聚成两个高杠杆机会：

1. **统一入口安全上下文**：Host、身份、项目、撤销、地域、静态文件和并发目前由不同路由或处理器自行负责；
2. **单一外部调用授权能力**：审批、秘密、网络目的地、预算和副作用幂等目前分散在 Adapter、SecurityStore 和 ExperimentalCallStore 之间。

线程并发上限属于入口安全上下文的资源约束；Windows 静态路径仍需要直接战术修复，不能等待架构迁移。Provider 精确秘密回显同样需要立即修补，即使之后采用统一外部调用能力。

## 限制

- 本分析没有运行真实 Provider、真实 DNS rebinding、真实账单或破坏性并发压力；
- `work/`、`outputs/` 和历史发布副本不属于当前权威源码；
- 性能、内存和迁移代价均为 source-derived 或 hypothetical，未写成已测量结果；
- 方案不代表发现已经修复，只有代码实现并重新验证原攻击路径后才能关闭发现。
