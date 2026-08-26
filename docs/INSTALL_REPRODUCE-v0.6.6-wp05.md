# v0.6.6 V066-WP05 `fixed-final` installation and reproduction

WP05 requires Python 3.11 or newer and adds no runtime dependency. Use only
the `fixed-final` artifacts:

- `v066-wp05-fixed-final-dist/costweave-0.6.6-py3-none-any.whl`;
- `costweave-v0.6.6-wp05-fixed-final-release.zip`;
- `costweave-v0.6.6-wp05-fixed-final-source-release.zip`;
- `SHA256SUMS-v0.6.6-wp05-fixed-final.txt`;
- `costweave-v0.6.6-wp05-fixed-final-acceptance-20260814.md`.

Do not substitute either the original artifacts or the superseded pre-final
`fixed` wheel/archives/checksum manifest.
The wheel is the preferred installation artifact, the runtime ZIP is for
direct offline execution, and the source ZIP is the reproducible test surface.

## 1. Verify SHA-256 before extraction or installation

On Linux or macOS:

```text
sha256sum -c SHA256SUMS-v0.6.6-wp05-fixed-final.txt
```

On PowerShell:

```powershell
$manifest = Get-Content -Encoding utf8 .\SHA256SUMS-v0.6.6-wp05-fixed-final.txt
foreach ($line in $manifest) {
    if (-not $line.Trim()) { continue }
    $parts = $line -split '\s+', 2
    $path = $parts[1].Trim().Replace('/', [IO.Path]::DirectorySeparatorChar)
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne $parts[0].ToLowerInvariant()) {
        throw "SHA-256 mismatch: $path"
    }
}
```

Expected result: every entry exists and matches. A missing or mismatched file
invalidates the artifact and its recorded acceptance evidence.

## 2. Install the `fixed-final` wheel in an isolated environment

Windows:

```text
py -3.11 -m venv .venv-wp05-fixed-final
.\.venv-wp05-fixed-final\Scripts\python -m pip install --no-deps v066-wp05-fixed-final-dist\costweave-0.6.6-py3-none-any.whl
.\.venv-wp05-fixed-final\Scripts\python -c "import costweave; print(costweave.__version__, costweave.__file__)"
.\.venv-wp05-fixed-final\Scripts\python -m costweave.release_cli --timeout 30 --pretty
```

Linux or macOS:

```text
python3.11 -m venv .venv-wp05-fixed-final
.venv-wp05-fixed-final/bin/python -m pip install --no-deps v066-wp05-fixed-final-dist/costweave-0.6.6-py3-none-any.whl
.venv-wp05-fixed-final/bin/python -c "import costweave; print(costweave.__version__, costweave.__file__)"
.venv-wp05-fixed-final/bin/python -m costweave.release_cli --timeout 30 --pretty
```

Verified `fixed-final` wheel result: version `0.6.6`, import path inside the isolated
target, `success=true`, `release.engineering_ready=true`,
`release.production_ready=false`, 16/16 release-readiness gates passed, and
`external_call_attempts=0`.

## 3. Inspect and run the `fixed-final` runtime ZIP

```text
python -m zipfile -l costweave-v0.6.6-wp05-fixed-final-release.zip
python -m zipfile -e costweave-v0.6.6-wp05-fixed-final-release.zip extracted/wp05-fixed-final-runtime
cd extracted/wp05-fixed-final-runtime
python -B -c "import costweave; print(costweave.__version__, costweave.__file__)"
python -B -m costweave.release_cli --timeout 30 --pretty
```

Expected result: normal `costweave/` and `docs/` trees, no `tests/` directory,
no cache/bytecode/database/temp payload, and the same release result as the
`fixed-final` wheel.

## 4. Inspect and test the `fixed-final` source ZIP

```text
python -m zipfile -l costweave-v0.6.6-wp05-fixed-final-source-release.zip
python -m zipfile -e costweave-v0.6.6-wp05-fixed-final-source-release.zip extracted/wp05-fixed-final-source
cd extracted/wp05-fixed-final-source
python -B -m unittest tests.test_observability_v066_wp05 -v
```

Verified focused/adversarial result: 25/25 tests passed with zero failures and
errors.

## 5. Compatibility, affected and full regression

Run from the extracted `fixed-final` source root:

```text
python -B -m unittest tests.test_gateway_backend_v066 tests.test_gateway_openai_v066_wp02 tests.test_gateway_litellm_v066_wp03 tests.test_gateway_local_v066_wp04 tests.test_contracts_v4 tests.test_v051_integration tests.test_release_v0584 -v

python -B -m unittest tests.test_observability_v066_wp05 tests.test_gateway_backend_v066 tests.test_gateway_openai_v066_wp02 tests.test_gateway_litellm_v066_wp03 tests.test_gateway_local_v066_wp04 tests.test_contracts_v4 tests.test_v051_integration tests.test_release_v0584 tests.test_server tests.test_tournament_api_v060 -v

python -B -m unittest discover -s tests -p "test*.py" -v
python -B tests/run_isolated.py --timeout 180
```

Verified final results:

- compatibility subset: 94/94;
- affected combination, including server and tournament HTTP regressions:
  147/147;
- ordinary full suite: 995/995;
- process-isolated full suite: 995/995;
- failures and errors: zero.

The ordinary full suite initially reproduced a Windows
`ConnectionAbortedError` twice on the tournament 415 path. The corrected server
performs a bounded 100 ms drain before returning 415. A 20-consecutive-request
regression and both final full suites passed after the fix.

A later source snapshot may add tests, but must not silently reduce coverage.
Record exact counts and hashes for every rebuild.

## 6. Trace API check

Start the isolated `fixed-final` wheel or extracted `fixed-final` runtime with
offline defaults:

```text
python -B -m costweave.server --port 8765
```

Create a run with a unique `Idempotency-Key`, poll it to a terminal state, and
then inspect the run, trace and health endpoints:

```text
curl -sS -X POST http://127.0.0.1:8765/api/v1/runs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: wp05-fixed-final-reproduction-0001" \
  -H "X-CostWeave-Project: wp05-fixed-final-reproduction" \
  -d '{"goal":"Verify the fixed-final WP05 local trace contract","budget":1,"max_concurrency":1}'

curl -sS -H "X-CostWeave-Project: wp05-fixed-final-reproduction" \
  http://127.0.0.1:8765/api/v1/runs/<run_id>

curl -sS -H "X-CostWeave-Project: wp05-fixed-final-reproduction" \
  http://127.0.0.1:8765/api/v1/runs/<run_id>/trace

curl -sS -H "X-CostWeave-Project: wp05-fixed-final-reproduction" \
  http://127.0.0.1:8765/api/health
```

Replace `<run_id>` with the returned ID. On Windows PowerShell, invoke
`curl.exe` explicitly.

Verified contract targets:

- health reports `active_work_package: V066-WP05`;
- run and trace contain the same 32-hex Trace ID;
- trace reports `E0-local-in-memory` and `external_exported=false`;
- Basic/Digest/bearer/API-key values, `auth`/`pwd` aliases, nested secrets and
  exception-text fixtures are absent;
- redaction markers retain only classification metadata (`redacted`, `kind`,
  `value_type` and optional `length`), with no digest or preview;
- a different `X-CostWeave-Project` cannot read the run or trace;
- strict JSON succeeds and rejects `NaN`/`Infinity`.

## 7. Lifetime and persistence boundary

The recorder registry is a thread-safe `WeakValueDictionary`; it does not own
recorder lifetime and is not a fixed-capacity cache. `RunRecord` or the caller
must retain the strong reference while provider spans are needed.

Restart preserves the Trace ID and reconstructs lifecycle spans derivable from
durable runtime events. Provider-call spans remain process-local and are not
reconstructed. WP05 therefore does not claim durable or distributed provider
tracing.

## 8. Rollback

Stop calling `/api/v1/runs/<run_id>/trace` and remove WP05 observability imports
from the integrating application. No schema rollback is required: WP05 keeps
runtime schema `v6.5.0`, 35 migrations and projection schema `v5.6`.
