# CostWeave v0.6.0 install and reproduction

The `v0.5.8.4` package remains the stable baseline. Install the v0.6.0 wheel in a clean environment, then run:

```text
python -m costweave.tournament_cli --pretty
python -m costweave.release_cli --pretty
python -m unittest tests.test_tournament_v060 tests.test_tournament_api_v060
```

The default tournament uses only the packaged E0 seed and makes zero network/provider calls. Existing output files are never overwritten by the CLI `--output` option.
