# 在同一批观测上比较执行策略

联合搜索回答“哪条路线能赶上期限”，执行恢复回答“把剩余预算用在哪里，才可能让最终交付通过验收”。两个入口都可以读取 UTF-8 JSON 文件，本地运行时模型调用数为零。

## 用整图耗时选择路线

在已有的 `JointSearchProblemV1` 中加入 `latency_scenarios`。每个窗口对应同一次负载场景。内层键是问题中已声明的节点和路线，值是该节点从获得执行槽位到交付已验证结果的毫秒数。计入该次服务内的重试和验证，排队和规划耗时由回放器另算。

```json
{
  "latency_scenarios": {
    "window-01": {"extract": {"local": 18}, "review": {"route-a": 120, "route-b": 25}},
    "window-02": {"extract": {"local": 22}, "review": {"route-a": 32, "route-b": 140}}
  }
}
```

以上是添加到完整问题中的字段。不同路线应使用相同场景采集的观测。每个候选都会按依赖关系及可用槽位重放所有窗口。它用整图均值估算时间成本，用整图 P95 判断期限，并报告超过期限的窗口占比。路线缺测时，候选的 `latency_replay` 会给出已匹配窗口数，P95 和逾期率留空。输入支持至多 256 个窗口。省略该字段可使用原来的估算方式。

```sh
python -m costweave.joint_search_cli --input problem.json --pretty
python -m unittest tests.test_dag_latency_replay
```

回归用例中，两条并行分支各自都有 4 个慢窗口和 96 个快窗口。慢窗口完全重合时整图 P95 为 10 ms，完全错开时为 100 ms。这个例子可用于检查采集端是否保留了同一时段的关联。

## 给返修分配预算

在已有的 `ExecutionSnapshotV1` 中加入 `repair_allocation`。例如，下列合同允许在 `draft` 上进行一次定向返修，以 `final` 的验收结果判定是否完成。示例中的摘要应替换为实际冻结输入、上游产物和验收配置的 SHA-256。

```json
{
  "checkpoint_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "validator_revision": "final-tests-v1",
  "baseline_policy_id": "continue-v1",
  "final_node_ids": ["final"],
  "remaining_actions": 1,
  "authorized_plan_ids": ["retry-v1"],
  "plans": [{
    "plan_id": "retry-v1", "action": "RETRY_NODE", "target_node_ids": ["draft"],
    "max_cost_usd": 0.06, "max_latency_ms": 600, "action_count": 1
  }],
  "pairs": []
}
```

1. 用空的 `pairs` 运行一次，读取 `decision.final_repair.state_id`。该标识绑定当前图、剩余预算、允许动作、检查点及验收器版本。
2. 要比较两种做法，从相同冻结状态分别运行基线与候选，执行同一套最终验收。每对记录包含唯一的 `pair_id`，两臂各有 `state_id`、独立的 `run_id`、对应的 `policy_id`、布尔值 `final_passed` 和 `cost_usd`。费用覆盖检查点到终验的整段过程，包含失败尝试。应独立采集各对实验，新的编号本身不能提供独立性。
3. 把这些记录填入 `pairs` 并重新运行。查看每项候选的 `reason_codes`、匹配及排除的票据和 `net_gain_lower_usd`。分配器只比较覆盖必要阻塞节点、预算和动作次数充足的已授权方案。收益按终验通过率的改善与增量费用计算，使用单侧 Hoeffding 下界并校正候选数量。信息不足时可得到 `REQUEST_MISSING_INFO`，可用方案耗尽时返回 `STOP_AND_RETURN_PARTIAL`。

```sh
python -m costweave.execution_recovery_cli --input snapshot.json --pretty
python -m unittest tests.test_final_repair
```

如需比较“直接重试”和“先诊断再重试”，分别登记两个有版本的计划。后者使用 `DIAGNOSE_THEN_RETRY` 并指定 `diagnostic_revision` 为 `required-blockers-v1`。该检查在本地读取阻塞节点、验收状态和剩余尝试次数。报价和观测费用都要涵盖后续重试直到最终验收的开销。遇到未知故障会请求补充信息，遇到硬性安全或审批拒绝会停止。

两个服务也接受这些字段。分别将完整问题或快照交给原有 `POST /api/v1/joint-search/decide` 和 `POST /api/v1/execution-recovery/decide`，沿用服务的确认与幂等机制。省略 `repair_allocation` 时使用原有的启发式决策。返回的决定是建议，真实执行和费用需要由调用端记录。

## Aurora 实验

Aurora 实验分支在依赖验收通过、执行槽位和预算允许时立即启动后继节点。事故信息披露与独立验收仍是执行屏障。可在该分支使用以下命令复核已有报告，输出终验的直接或上游阻塞点。离线分析不调用供应商。

```sh
python -m costweave.aurora_real_pilot --analyze-report frozen-result.json
python -m unittest tests.test_aurora_ready_scheduling tests.test_aurora_checkpoint
```

[Aurora 实验分支](https://github.com/Songfored/CostWeave-Vibe-Coding-/tree/feature/costweave-adaptive-execution)
