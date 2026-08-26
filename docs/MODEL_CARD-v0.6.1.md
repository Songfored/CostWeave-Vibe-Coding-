# CostWeave v0.6.1 model/policy card

CostWeave v0.6.1 is not a language model. It is a deterministic metareasoning policy over structured task, system, candidate-strategy, planner-member, and budget beliefs.

## Inputs

The controller consumes schema-validated beliefs. Every decision records the task/system belief IDs, a belief hash, candidate IDs, utility intervals, evidence IDs, policy revision, action, budget, and timestamp. Unknown fields and non-finite values are rejected at the service boundary.

## Actions

`fast` avoids extra planning only when hard constraints, low risk, fresh evidence, capacity, and a conservative utility-gap test all pass. `deliberate` buys bounded planner opinions when EVC is positive. `probe` records a side-effect-free information-gathering recommendation. `expert` transfers the decision to a higher-authority path for risk, OOD, stale evidence, capacity, infeasibility, or budget conditions.

## Limits

The committee is capped at four members by default, selected adaptively, and penalized for cost, latency, coordination, and correlated failure. Management depth is capped at two levels. The engine has no provider credentials, tool execution, filesystem mutation, network client, or model adapter.

## Evidence status

Built-in examples are E0 simulated. No calibration, real-provider comparison, training result, or commercial claim is included. A future model or adapter must enter through the same contracts and pass an independent result-blind gate before it can influence runtime routing.
