# v0.6.5 installation and reproduction

From the source root:

1. Create a Python 3.11 or newer environment.
2. Install the package with pip install -e . or build a wheel.
3. Run python -B -m costweave.safe_learning_cli --pretty.
4. Run python -B -m unittest tests.test_safe_learning_v065 tests.test_safe_learning_api_v065.
5. Run python -B -m costweave.release_cli --pretty and confirm runtime v6.5.0,
   35 migrations, WAL and zero external calls.

The loopback API is available from the normal server command. The safe-learning
status endpoint is GET /api/v1/safe-learning/status. Credit and evaluation
requests must carry the RUN_SAFE_LEARNING confirmation token; credit and
evaluation also require Idempotency-Key.
