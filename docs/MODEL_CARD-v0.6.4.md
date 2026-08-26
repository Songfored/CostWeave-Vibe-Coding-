# CostWeave v0.6.4 model/policy card

CostWeave v0.6.4 is a deterministic execution-time decision controller, not a
language model. It updates a conservative progress distribution from a frozen
execution snapshot and scores bounded recovery actions with explicit budget,
deadline, capacity and validator checks.

## Intended use

- offline orchestration research;
- fault-injection and recovery-policy ablations;
- auditable UI/API demonstrations;
- preparing evidence for a later Shadow or production gate.

## Not intended for

- autonomous production changes;
- pretending simulated CPS is a real cost reduction;
- unconstrained retries, recursive replanning or online training;
- replacing independent validation or human approval for irreversible work.

All demonstrations use E0 simulated evidence, with provider and network calls
fixed at zero.
