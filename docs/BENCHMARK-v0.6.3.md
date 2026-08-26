# v0.6.3 benchmark card

This release provides an engineering benchmark, not a commercial performance
benchmark.

The deterministic fixture compares the safety baseline, planner, local
transforms and multiple management/recovery choices under one shared budget.
The search reports expected loss, tail loss, success lower bound, latency,
candidate pruning and compiler failures. Greedy, beam, Pareto and successive
halving are separately selectable for ablation.

All built-in runs are E0 simulated and make zero Provider or network calls.
Real superiority, cost, latency and quality must be established later with a
pre-registered shared task set and independent acceptance.
