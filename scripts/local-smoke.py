"""Fast, provider-free smoke check for the local CostWeave profile."""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path

import costweave
from costweave.server import build_server


def _required_path(name: str) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is not configured")
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be resolved to an absolute path")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    catalog_path = _required_path("COSTWEAVE_CATALOG_PATH")
    runtime_path = _required_path("COSTWEAVE_RUNTIME_DB")
    for name in (
        "COSTWEAVE_EXPERIMENTAL_ADAPTERS",
        "COSTWEAVE_PAID_CALLS",
        "COSTWEAVE_CANARY_ENABLED",
    ):
        if os.environ.get(name, "0").strip() != "0":
            raise RuntimeError(f"safe local profile requires {name}=0")

    server = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/health"
        request = urllib.request.Request(
            url,
            headers={"X-CostWeave-Session": server.session_token},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        if response.status != 200 or payload.get("status") != "ok":
            raise RuntimeError("local health endpoint did not report ok")
        if payload.get("version") != costweave.__version__:
            raise RuntimeError("installed package version does not match the service")
        adapters = payload.get("experimental_adapters") or {}
        if adapters.get("experimental_calls_enabled") or adapters.get("paid_calls_enabled"):
            raise RuntimeError("external-call gates are unexpectedly enabled")
        if not (payload.get("control_kernel") or {}).get("persistent"):
            raise RuntimeError("project-local persistent runtime is not active")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    print(
        json.dumps(
            {
                "status": "ok",
                "version": costweave.__version__,
                "catalog_path": str(catalog_path),
                "runtime_path": str(runtime_path),
                "external_calls_enabled": False,
                "paid_calls_enabled": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
