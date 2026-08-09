# CostWeave v0.6.0 acceptance snapshot

This file is a release-workspace snapshot. It records the acceptance boundary for the active `v0.6.0` line while preserving `v0.5.8.4` as the frozen stable baseline.

## Scope accepted

- Deterministic contracts and round trips.
- Migration 030 and immutable decision/tournament persistence.
- Safe result-blind benchmark records and importers.
- Offline policy evaluation, paired bootstrap, claim gates, CLI, API, and UI wiring.

## Scope explicitly not accepted as a claim

- No real provider quality result.
- No real cost-reduction percentage.
- No production routing authority.
- No claim of superiority over Claude, OpenAI, or any other competitor.

## Required commands

```text
python -B tests/run_isolated.py
python -B -m costweave.tournament_cli --pretty
python -B -m costweave.release_cli --pretty
```

## Observed verification (2026-08-09)

- Full isolated suite: **842 passed, 0 failed**.
- v0.6.0 targeted suite: **54 passed, 0 failed**.
- Release gates: **engineering_ready=true**, runtime `v6.0.0`, 30 migrations, SQLite WAL, replay parity healthy.
- Offline tournament: completed with zero provider calls and zero network calls; bundled seed remains `E0-simulated` and claim gates remain insufficient-sample.
- Wheel: `costweave-0.6.0-py3-none-any.whl`, SHA-256 `da5140b45b6f6c20973e21d4d58d4a49d714845e328199748853aef3b9de4ad9`.
- Archives contain no `__pycache__`, `.pyc`, or `.pyo` entries.

## Release artifacts

- `costweave-v0.6.0.zip` — runtime archive.
- `costweave-v0.6.0-source.zip` — source archive.
- `costweave-0.6.0-SHA256SUMS.txt` — archive, wheel, and acceptance-report checksums.
