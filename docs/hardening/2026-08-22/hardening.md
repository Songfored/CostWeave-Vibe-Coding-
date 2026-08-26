# Security Hardening Review: CostWeave v0.6.6

## Evidence Basis

本评审基于已完成的 Standard 安全扫描 `ce00af03-b50a-49ff-85a3-ed679db48d66`、当前根源码以及 [v0.6.6 整体验收报告](../../../outputs/COSTWEAVE_FULL_ACCEPTANCE_2026-08-21.md)。扫描确认 1 项高危、9 项中危和 1 项低危；完整登记见 [context.md](context.md)。

我重新检查了入口路由、项目读取、审批 gate、外部 Adapter、预算终态和副作用幂等路径。最值得关注的不是 11 条独立代码错误，而是两组反复出现的结构条件：安全上下文由调用点拼接，外部调用授权由多个组件分别决定。只做局部补丁可以短期止血，却很容易在下一条 v0.6 服务或下一个 Provider Adapter 中再次漂移。

这份文档是可修改的设计建议，不是漏洞已修复证明。原 PoC、战术修复和回归测试仍然必须保留。

## Constraints

- 当前产品仍是单进程、回环监听、默认零外呼的 E0 原型；
- 下一版本必须继续保持 `production_ready=false` 和真实付费调用关闭；
- 不能用一次性大重写破坏 1011 项既有合同；
- 性能和内存预算尚未实测，路线图使用平衡 profile，并要求迁移期测量；
- 两层 Planner 深度、项目隔离、地域、硬预算和幂等不能为了兼容而降级；
- 战术修复必须先于或与结构迁移同时存在，不能等待“未来架构”。

## Opportunity Portfolio

| Opportunity | Evidence | Options | Recommendation | Proposal |
|---|---|---|---|---|
| 统一入口安全上下文 | Host/DNS rebinding、无认证项目头、撤销遗漏、地域未授权、Windows 静态越界、无界线程 | 1. 路由逐点补丁；2. `RequestSecurityContextV1` 与统一入口 guard | 当前约束下选择 Option 2，同时先落战术补丁 | [完整方案](proposals/unified-request-security-context.md) |
| 单一外部调用授权能力 | Provider 密钥回显、伪造审批、失败预算释放、跨实验重放、DNS-to-socket TOCTOU | 1. Adapter 局部修复；2. `ExternalCallAuthorityV1` 能力边界 | 当前约束下选择 Option 2，同时保留 Adapter 端失败闭合 | [完整方案](proposals/single-external-call-authority.md) |

## Recommendation Summary

我们可以把 11 条发现逐个修好，但那只能证明今天审查到的调用点得到保护。CostWeave 接下来还要接入更多 advisory 服务、Runtime 和 Provider；继续让每个调用点自行组合项目、地域、审批、预算和网络规则，会重复产生相同类型的问题。

我建议在 v0.6.7 采用两个库内结构边界，而不是现在就增加独立安全服务或 sidecar：

1. `RequestSecurityContextV1` 在请求进入资源层前一次性建立认证主体、项目所有权、墓碑、地域、操作范围和请求身份；所有 store/service 只接受这个已验证上下文；
2. `ExternalCallAuthorityV1` 在任何 transport 之前原子决定审批、预算、幂等、秘密租约和固定网络目的地，并在 transport 之后负责安全输出和 ambiguous 对账状态。

这种做法比逐路由补丁多一些迁移工作，但它保留单进程性能和回滚能力，也不引入当前阶段不需要的部署复杂度。独立进程隔离在多用户或远端执行成为生产要求时会更有价值；现在直接采用会增加 IPC、证书、部署和故障处理成本，却不能替代当前漏洞的直接修复。

两项结构建议已经映射到 [Revision 4 路线图](../../REMAINING_ROADMAP.md) 的 v0.6.7。它们不会自动关闭安全发现：必须重新运行所有原攻击路径、全量/隔离测试和标准安全扫描。

## Next Decisions

1. 确认 v0.6.7 采用推荐的两个统一边界，而不是只做局部补丁；
2. 确定本地会话认证是 bearer 文件令牌、OS 用户绑定 transport，还是先 bearer 后迁移；
3. 明确 `data_residency` 是强制安全边界还是展示元数据；
4. 明确 ambiguous Provider 账单的冻结期限与人工对账流程；
5. 选定后再生成实施文件和代码工作包，不在设计阶段宣称发现已关闭。
