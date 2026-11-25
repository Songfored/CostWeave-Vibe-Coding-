"""Map authenticated local v2 requests onto a project-scoped artifact ledger."""

from hashlib import sha256
from http import HTTPStatus
import re

from .contracts import Job
from .runtime import Runtime, public_state
from .store import KernelStore, RunConflictError


ROOT = "/api/v2/runs"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")


def dispatch(method, path, body, key, kernel, project_id):
    if kernel is None:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "persistent_runtime_disabled"}
    suffix = path[len(ROOT):]
    parts = suffix[1:].split("/") if suffix.startswith("/") else []
    if parts and not _ID.fullmatch(parts[0]):
        return HTTPStatus.NOT_FOUND, {"error": "resource_not_found"}
    if not parts and (method != "POST" or not isinstance(key, str) or not _ID.fullmatch(key)):
        return HTTPStatus.BAD_REQUEST, {"error": "valid_idempotency_key_required"}
    digest = sha256(project_id.encode("utf-8")).hexdigest()
    base = kernel.store.path
    store = KernelStore(base.with_name(base.name + ".artifacts") / (digest + ".sqlite3"))
    runtime = Runtime(store, mutation_guard=lambda: kernel.security_store.active_project_operation(project_id))
    try:
        if not parts:
            result = runtime.submit(key, Job.from_dict(body))
        elif method == "GET" and len(parts) == 1:
            result = store.snapshot(parts[0])
        elif method == "GET" and len(parts) == 2 and parts[1] == "events":
            return HTTPStatus.OK, {"events": store.events(parts[0])}
        elif method == "POST" and len(parts) == 2 and parts[1] == "resume" and body == {}:
            result = runtime.resume(parts[0])
        elif method == "POST" and len(parts) == 2 and parts[1] == "provide":
            if not isinstance(body, dict) or set(body) != {"artifact", "value"} or not isinstance(body["artifact"], str):
                raise ValueError("supply the requested artifact name and its value")
            result = runtime.provide(parts[0], body["artifact"], body["value"])
        elif method == "POST" and len(parts) == 2 and parts[1] == "reconcile":
            if not isinstance(body, dict) or set(body) != {"attempt_id", "receipt"} or not isinstance(body["attempt_id"], str):
                raise ValueError("supply an attempt_id and a receipt")
            result = runtime.reconcile(parts[0], body["attempt_id"], body["receipt"])
        else:
            return HTTPStatus.NOT_FOUND, {"error": "resource_not_found"}
        return HTTPStatus.OK, public_state(result)
    except RunConflictError:
        if len(parts) == 2 and parts[1] == "provide":
            return HTTPStatus.CONFLICT, {"error": "clarification_conflict"}
        return HTTPStatus.CONFLICT, {"error": "run_id_conflict"}
    except KeyError:
        return HTTPStatus.NOT_FOUND, {"error": "run_or_attempt_not_found"}
    except (TypeError, ValueError) as exc:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid_artifact_request", "message": str(exc)}
    finally:
        store.close()
