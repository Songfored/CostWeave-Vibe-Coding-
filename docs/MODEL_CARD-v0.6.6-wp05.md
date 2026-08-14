# v0.6.6 V066-WP05 model card

CostWeave is not a trained language model. WP05 is a deterministic
observability contract and privacy boundary.

It does not infer task quality, choose a model, summarize prompts or learn
from traces. It only records bounded lifecycle spans and preserves the Trace
ID already selected by the runtime or Gateway request. Local serialization is
E0 evidence; no claim is made about an external OpenTelemetry collector,
distributed deployment, telemetry latency or production reliability.
