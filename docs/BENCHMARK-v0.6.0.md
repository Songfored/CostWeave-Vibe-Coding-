# v0.6.0 benchmark card

## What is measured

The offline runner compares result-blind routing policies on the packaged 12-case, 36-record seed dataset. It reports coverage, accepted rate, quality, cost, latency, utility, regret, oracle agreement as a diagnostic, paired bootstrap intervals, and claim-gate status.

## What is not measured

The seed is `E0-simulated`. It contains no real prompts, outputs, vendor calls, billing records, or production traffic. It therefore does not measure real provider quality, real-world latency, real cost reduction, or superiority over any competitor.

## Reproduction

```text
python -B -m costweave.tournament_cli --pretty
```

Expected invariants include four test cases, four policies, zero provider calls, zero network calls, and claim gates that are not commercially claimable because the sample count and evidence level are insufficient.
