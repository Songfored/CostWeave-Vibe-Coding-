# v0.6.6 benchmark and evidence boundary

## What is measured

The benchmark is a deterministic contract check: object construction,
serialization round-trip, validation failures and fake-backend protocol shape.
It runs without a socket, provider SDK, API key or paid request.

## What is not measured

No conclusion is made about answer quality, model capability, token cost,
provider pricing, throughput, p95 latency, high-concurrency behavior, savings
or production availability. Those require preregistered E2+ real-data tests.

## Reproduction

```text
python -B -m unittest -v tests.test_gateway_backend_v066
python -B -m unittest discover -v
```

The release acceptance report records the observed test counts and elapsed
time; simulated contract results must not be presented as commercial gains.

Observed local run: 60 focused tests passed; the full suite passed 924 tests in
321.691 seconds. This is a regression-health measurement, not a provider
benchmark.
