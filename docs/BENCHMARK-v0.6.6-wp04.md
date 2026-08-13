# v0.6.6 V066-WP04 benchmark boundary

The WP04 deterministic fixture measures admission behavior, not model quality.

Required observations:

- default-off network and capacity paths make zero transport calls;
- missing, malformed, stale, future and over-capacity cases are rejected before
  transport;
- one complete fresh snapshot is admitted;
- delayed streaming is admitted only when iteration starts;
- probe failures remain fail-closed and non-leaky through full tracebacks.

The fixture is E0 local/static evidence. Real Ollama/vLLM queue, GPU,
throughput, latency and cost claims require separately collected E2+ traces and
must not be inferred from these tests.
