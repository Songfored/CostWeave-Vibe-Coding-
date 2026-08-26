# CostWeave v0.6.2 model/policy card

CostWeave v0.6.2 is not a language model. It is a deterministic, evidence-aware
policy that aggregates condition-specific observations and selects an advisory
route or refuses to select one.

## Intended use

- offline route analysis and replay;
- project-scoped evidence provenance;
- conservative cold-start decisions and explicit escalation;
- comparing future router plugins through one structured output.

## Out of scope

- generating model answers;
- asserting that a vendor model is objectively capable;
- online learning, automatic training or policy promotion;
- direct Provider calls, secrets, network access or production intervention.

## Safety behavior

Evidence is never silently merged across task families, model revisions,
deployments, tools, modalities or validators. Intervals widen with sparse data.
Stale, OOD, high-risk or missing-validator conditions produce `ABSTAIN` or
`ESCALATE`. All durable records are hash-verified and immutable.
