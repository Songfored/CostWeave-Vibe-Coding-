from __future__ import annotations

import argparse
import json
import mimetypes
import posixpath
from dataclasses import fields
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .catalog_store import (
    CATALOG_STORE,
    CatalogConflictError,
    CatalogError,
    CatalogStore,
)
from .compat_v4 import contract_health, runtime_v4_enabled
from .domain import RunMode, RunRequest, WorkerProfile
from .engine import RunManager
from .event_store import IdempotencyConflictError, RevisionConflictError


MANAGER: RunManager | None = None
WEB_ROOT = files("costweave").joinpath("web")


class RequestError(ValueError):
    def __init__(self, message: str, status: HTTPStatus, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class CostWeaveServer(ThreadingHTTPServer):
    def server_close(self) -> None:
        manager = getattr(self, "manager", None)
        if manager is not None:
            manager.shutdown(wait_timeout=10)
        super().server_close()


class CostWeaveHandler(BaseHTTPRequestHandler):
    server_version = "CostWeave/0.4.3"

    @property
    def manager(self) -> RunManager:
        manager = getattr(self.server, "manager", MANAGER)
        if manager is None:
            raise RuntimeError("run manager is not configured")
        return manager

    @property
    def catalog_store(self) -> CatalogStore:
        return getattr(self.server, "catalog_store", CATALOG_STORE)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            runtime_health = self.manager.health()
            contracts = contract_health()
            contracts["features"]["runtime_v4"] = runtime_v4_enabled()
            contracts["contract_trace_persistent"] = bool(
                runtime_health.get("persistent")
            )
            self._json({
                "status": "ok",
                "runtime": "offline-decision-simulation",
                "version": __version__,
                "catalog_revision": self.catalog_store.revision,
                **contracts,
                "control_kernel": runtime_health,
            })
            return
        if path == "/api/catalog":
            payload = self.catalog_store.payload()
            self._json({**payload, "workers": payload["models"]})
            return
        if path == "/api/catalog/schema":
            self._json({
                "schema_version": 1,
                "fields": [item.name for item in fields(WorkerProfile)],
                "import_formats": ["json", "csv"],
                "import_modes": ["merge", "replace"],
            })
            return
        if path == "/api/catalog/export":
            format_name = parse_qs(parsed.query).get("format", ["json"])[0]
            if format_name == "json":
                self._text(
                    self.catalog_store.export_json(),
                    "application/json; charset=utf-8",
                    "costweave-model-catalog.json",
                )
            elif format_name == "csv":
                self._text(
                    "\ufeff" + self.catalog_store.export_csv(),
                    "text/csv; charset=utf-8",
                    "costweave-model-catalog.csv",
                )
            else:
                self._json(
                    {"error": "unsupported_format", "message": "format 必须是 json 或 csv"},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if path in {"/api/runs", "/api/v1/runs"}:
            self._json({"runs": self.manager.list()})
            return
        if path.startswith("/api/v1/tasks/"):
            task_id = unquote(path.removeprefix("/api/v1/tasks/").strip("/"))
            task = self.manager.task(task_id)
            if task is None:
                self._json({"error": "task_not_found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(task)
            return
        if path.startswith("/api/v1/artifacts/"):
            artifact_id = unquote(
                path.removeprefix("/api/v1/artifacts/").strip("/")
            )
            try:
                artifact = self.manager.artifact(artifact_id)
            except Exception as exc:
                self._json(
                    {
                        "error": "artifact_integrity_failed",
                        "message": str(exc),
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if artifact is None:
                self._json(
                    {"error": "artifact_not_found"}, HTTPStatus.NOT_FOUND
                )
                return
            self._json(artifact)
            return
        if path.startswith("/api/v1/runs/"):
            suffix = path.removeprefix("/api/v1/runs/").strip("/")
            if suffix.endswith("/graph"):
                run_id = suffix.removesuffix("/graph").strip("/")
                graph = self.manager.graph(run_id)
                if graph is None:
                    self._json(
                        {"error": "run_not_found"}, HTTPStatus.NOT_FOUND
                    )
                    return
                self._json(graph)
                return
            if suffix.endswith("/artifacts"):
                run_id = suffix.removesuffix("/artifacts").strip("/")
                if self.manager.get(run_id) is None:
                    self._json(
                        {"error": "run_not_found"}, HTTPStatus.NOT_FOUND
                    )
                    return
                self._json(
                    {
                        "run_id": run_id,
                        "artifacts": self.manager.artifacts(run_id),
                    }
                )
                return
            if suffix.endswith("/events"):
                run_id = suffix.removesuffix("/events").strip("/")
                record = self.manager.get(run_id)
                if not record:
                    self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    query = parse_qs(parsed.query)
                    after = int(query.get("after_revision", ["0"])[0])
                    limit = int(query.get("limit", ["200"])[0])
                    if after < 0 or not 1 <= limit <= 1000:
                        raise ValueError
                except (TypeError, ValueError):
                    self._json(
                        {"error": "invalid_pagination"}, HTTPStatus.BAD_REQUEST
                    )
                    return
                events = self.manager.events(
                    run_id, after_revision=after, limit=limit
                )
                self._json({
                    "run_id": run_id,
                    "events": events,
                    "after_revision": after,
                    "next_revision": events[-1].get("revision", after) if events else after,
                })
                return
            record = self.manager.get(suffix)
            if not record:
                self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(record.snapshot())
            return
        if path.startswith("/api/runs/"):
            run_id = path.removeprefix("/api/runs/").strip("/")
            record = self.manager.get(run_id)
            if not record:
                self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(record.snapshot())
            return
        if path.startswith("/api/"):
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/v1/runs":
            self._create_run(v1=True)
            return
        if path.startswith("/api/v1/runs/"):
            suffix = path.removeprefix("/api/v1/runs/").strip("/")
            if suffix.endswith("/cancel"):
                self._cancel_run(suffix.removesuffix("/cancel").strip("/"))
                return
            if suffix.endswith("/retry"):
                self._retry_run(suffix.removesuffix("/retry").strip("/"))
                return
        if path == "/api/runs":
            self._create_run(v1=False)
            return
        if path == "/api/catalog/models":
            self._catalog_create()
            return
        if path == "/api/catalog/import":
            self._catalog_import()
            return
        if path == "/api/catalog/reset":
            self._catalog_reset()
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        prefix = "/api/catalog/models/"
        if not path.startswith(prefix):
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        model_id = unquote(path.removeprefix(prefix).strip("/"))
        if not model_id:
            self._json({"error": "invalid_model_id"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            payload = self._read_write_json()
            expected = self._expected_revision(payload)
            changes = payload.get("model", payload)
            if changes is payload:
                changes = {
                    key: value for key, value in payload.items()
                    if key != "expected_revision"
                }
            model = self.catalog_store.update(
                model_id,
                changes,
                expected_revision=expected,
            )
            self._json({
                "model": model.to_dict(),
                "metadata": self.catalog_store.payload()["metadata"],
            })
        except Exception as exc:
            self._catalog_error(exc)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        prefix = "/api/catalog/models/"
        if not parsed.path.startswith(prefix):
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            self._validate_write_headers(require_json=False)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
            return
        model_id = unquote(parsed.path.removeprefix(prefix).strip("/"))
        try:
            raw_revision = parse_qs(parsed.query).get("expected_revision", [None])[0]
            expected = int(raw_revision) if raw_revision is not None else None
            self.catalog_store.delete(model_id, expected_revision=expected)
            self._json({
                "deleted": model_id,
                "metadata": self.catalog_store.payload()["metadata"],
            })
        except Exception as exc:
            self._catalog_error(exc)

    def _create_run(self, *, v1: bool = False) -> None:
        try:
            payload = self._read_write_json()
            goal = str(payload.get("goal", "")).strip()
            if len(goal) < 6:
                raise ValueError("任务目标至少需要6个字符")
            try:
                mode = RunMode(payload.get("mode", "balanced"))
            except ValueError as exc:
                raise ValueError("mode必须是 economy、balanced 或 turbo") from exc
            simulate_replan = payload.get("simulate_replan", False)
            if not isinstance(simulate_replan, bool):
                raise ValueError("simulate_replan必须是布尔值")
            request = RunRequest(
                goal=goal,
                mode=mode,
                budget=float(payload.get("budget", 1.0)),
                quality_floor=float(payload.get("quality_floor", .78)),
                max_concurrency=int(payload.get("max_concurrency", 4)),
                simulate_replan=simulate_replan,
            )
            key = self.headers.get("Idempotency-Key")
            if v1 and not key:
                raise RequestError(
                    "v1 创建运行必须提供 Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            record = self.manager.create(request, idempotency_key=key)
            self._json(record.snapshot(), HTTPStatus.ACCEPTED)
        except (RevisionConflictError, IdempotencyConflictError) as exc:
            self._runtime_conflict(exc)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _cancel_run(self, run_id: str) -> None:
        try:
            payload = self._read_write_json()
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "取消运行必须提供 Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            expected = payload.get("expected_revision")
            if isinstance(expected, bool) or expected is None:
                raise ValueError("expected_revision 必须是整数")
            record = self.manager.cancel(
                run_id,
                idempotency_key=key,
                expected_revision=int(expected),
            )
            self._json(record.snapshot(), HTTPStatus.ACCEPTED)
        except KeyError:
            self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
        except (RevisionConflictError, IdempotencyConflictError) as exc:
            self._runtime_conflict(exc)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _retry_run(self, run_id: str) -> None:
        try:
            self._read_write_json()
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "重试运行必须提供 Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            record = self.manager.retry(run_id, idempotency_key=key)
            self._json(record.snapshot(), HTTPStatus.ACCEPTED)
        except KeyError:
            self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
        except (RevisionConflictError, IdempotencyConflictError) as exc:
            self._runtime_conflict(exc)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _runtime_conflict(self, exc: Exception) -> None:
        code = (
            "runtime_revision_conflict"
            if isinstance(exc, RevisionConflictError)
            else "idempotency_conflict"
        )
        payload = {"error": code, "message": str(exc)}
        if isinstance(exc, RevisionConflictError):
            payload.update({"expected_revision": exc.expected, "current_revision": exc.actual})
        self._json(payload, HTTPStatus.CONFLICT)

    def _catalog_create(self) -> None:
        try:
            payload = self._read_write_json()
            expected = self._expected_revision(payload)
            model_payload = payload.get("model")
            if not isinstance(model_payload, dict):
                raise CatalogError("model 必须是对象")
            model = self.catalog_store.create(
                model_payload,
                expected_revision=expected,
            )
            self._json({
                "model": model.to_dict(),
                "metadata": self.catalog_store.payload()["metadata"],
            }, HTTPStatus.CREATED)
        except Exception as exc:
            self._catalog_error(exc)

    def _catalog_import(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=2_000_000)
            expected = self._expected_revision(payload)
            result = self.catalog_store.import_data(
                payload.get("data"),
                format=str(payload.get("format", "json")).lower(),
                mode=str(payload.get("mode", "merge")).lower(),
                expected_revision=expected,
            )
            self._json({
                "result": result,
                "metadata": self.catalog_store.payload()["metadata"],
            })
        except Exception as exc:
            self._catalog_error(exc)

    def _catalog_reset(self) -> None:
        try:
            payload = self._read_write_json()
            expected = self._expected_revision(payload)
            if payload.get("confirm") != "RESET":
                raise CatalogError("恢复默认需要 confirm=RESET")
            self.catalog_store.reset(expected_revision=expected)
            self._json(self.catalog_store.payload())
        except Exception as exc:
            self._catalog_error(exc)

    @staticmethod
    def _expected_revision(payload: dict) -> int | None:
        raw = payload.get("expected_revision")
        if raw is None:
            return None
        if isinstance(raw, bool):
            raise CatalogError("expected_revision 必须是整数")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise CatalogError("expected_revision 必须是整数") from exc

    def _catalog_error(self, exc: Exception) -> None:
        if isinstance(exc, CatalogConflictError):
            status = HTTPStatus.CONFLICT
            code = "catalog_revision_conflict"
        elif isinstance(exc, CatalogError):
            status = HTTPStatus.UNPROCESSABLE_ENTITY
            code = "catalog_validation_failed"
        elif isinstance(exc, RequestError):
            status = exc.status
            code = exc.code
        elif isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
            status = HTTPStatus.BAD_REQUEST
            code = "invalid_request"
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            code = "catalog_write_failed"
        self._json({"error": code, "message": str(exc)}, status)

    def _read_write_json(self, *, max_bytes: int = 256_000) -> dict:
        self._validate_write_headers(require_json=True)
        payload = self._read_json(max_bytes=max_bytes)
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是JSON对象")
        return payload

    def _validate_write_headers(self, *, require_json: bool) -> None:
        if require_json:
            content_type = (
                self.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if content_type != "application/json":
                raise RequestError(
                    "Content-Type 必须是 application/json",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "unsupported_media_type",
                )
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "127.0.0.1", "localhost", "::1",
            }:
                raise RequestError(
                    "只允许本地页面发起写请求",
                    HTTPStatus.FORBIDDEN,
                    "cross_origin_forbidden",
                )

    def _read_json(self, *, max_bytes: int = 64_000) -> object:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求体")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            length = 0
        else:
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Content-Length无效") from exc
        if length < 0:
            raise ValueError("Content-Length不能为负数")
        if length > max_bytes:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        return json.loads(raw or b"{}")

    def _json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, content_type: str, filename: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"',
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, request_path: str) -> None:
        if "\\" in request_path or ".." in request_path.split("/"):
            self._json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
            return
        clean = posixpath.normpath(request_path).lstrip("/") or "index.html"
        if clean.startswith("..") or "/../" in clean:
            self._json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
            return
        target = WEB_ROOT.joinpath(clean)
        if not target.is_file():
            target = WEB_ROOT.joinpath("index.html")
        data = target.read_bytes()
        mime, _ = mimetypes.guess_type(str(target))
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            f"{mime or 'application/octet-stream'}; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def build_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    manager: RunManager | None = None,
    catalog_store: CatalogStore | None = None,
    runtime_db_path: str | None = None,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "离线原型仅允许绑定本机回环地址；公开监听需要先实现身份认证"
        )
    store = catalog_store or CATALOG_STORE
    server = CostWeaveServer((host, port), CostWeaveHandler)
    server.catalog_store = store  # type: ignore[attr-defined]
    if runtime_db_path is None and store is not CATALOG_STORE:
        runtime_db_path = str(store.path.with_name("runtime-v4.sqlite3"))
    server.manager = manager or RunManager(  # type: ignore[attr-defined]
        store,
        runtime_db_path=runtime_db_path,
    )
    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the CostWeave v0.4.3 Artifact Graph prototype"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    server = build_server(args.host, args.port)
    print(f"CostWeave running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
