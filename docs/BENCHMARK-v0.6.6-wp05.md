# v0.6.6 V066-WP05 benchmark boundary

The WP05 fixture measures contract behavior, not model quality or production
observability performance.

Required observations:

- Trace ID derivation is stable for the same run ID;
- all six required stages can be exported under one trace;
- redaction removes prompt and secret fixtures from JSON;
- the bounded recorder reports overflow rather than unbounded growth;
- an admitted fake gateway creates a provider-call span;
- a persisted run, its EventStore events and trace endpoint share the ID;
- OTLP-shaped output contains the expected resource and span fields.

Any throughput, collector delivery, sampling overhead or production cost
claim requires separately collected E2+ traces and must not be inferred from
these tests.
