# CostWeave v0.6.3 model/policy card

CostWeave v0.6.3 is not a language model. It is a deterministic joint
strategy-search policy that evaluates complete plan–route–schedule candidates.

## Inputs

Task DAG nodes, deliverables, authority/tool scopes, route options with
condition-specific estimates, recovery choices and a multi-dimensional search
budget.

## Outputs

`SELECT`, `ABSTAIN` or `ESCALATE`, the selected strategy when safe, the Pareto
frontier, hard-constraint results, Prompt/Handoff audit, search budget usage
and an immutable decision trace.

## Known limitations

The bundled demonstration uses E0 simulated estimates. It does not establish
real model quality, real cost reduction, production throughput, or superiority
over another router. Search authority is advisory-only until real evidence and
the later Shadow/Canary gates are passed.
