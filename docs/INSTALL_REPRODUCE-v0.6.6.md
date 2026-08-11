# v0.6.6 installation and reproduction

From the source root:

1. Create a Python 3.11 or newer environment.
2. Install the wheel or run `python -m pip install -e .`.
3. Run `python -B -m unittest -v tests.test_gateway_backend_v066`.
4. Run `python -B -m costweave.release_cli --pretty` and confirm package
   release v0.6.6, runtime v6.5.0, 35 migrations, WAL and zero external calls.
5. Run `python -B -m unittest discover -v` for the full regression suite.

The gateway work package is contract-only. It does not require an API key,
provider SDK, local model server or network access.
