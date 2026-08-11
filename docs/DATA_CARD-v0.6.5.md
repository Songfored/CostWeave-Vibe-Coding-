# v0.6.5 data card

## Sources

The repository ships only deterministic E0 test fixtures. They are useful for
contract and persistence tests and are not observations of real Providers.
Production or human-benchmark data may be supplied through the API only after
the caller labels origin, evidence level, independent validation and action
propensities.

## Required fields

Each trace records accepted outcome, bounded reward, cost, latency, six
component effects, six confidence values, evidence IDs and an ISO timestamp.
Each logged decision records the logging policy distribution, candidate policy
distribution, predicted rewards, observed reward, task family and risk.

## Quality controls

Payloads reject NaN, infinity, unknown fields, duplicate evidence IDs, invalid
probability distributions, missing logged propensity and cross-project
records. Historical rows without choice probability are never treated as
unbiased learning data.
