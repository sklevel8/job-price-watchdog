"""Web dashboard and JSON API for the watchdog.

The scheduled GitHub Actions workflow remains the primary deployment mode.
This module adds an optional, dependency-light Flask application so a human can
inspect state, edit the YAML configuration safely, and trigger a run on demand.

Run it from the repository root::

    python src/webapp.py --host 127.0.0.1 --port 8000

Set ``WATCHDOG_UI_TOKEN`` before exposing the app beyond localhost. The token is
sent by the browser as ``X-Watchdog-Token`` (or a bearer token).
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hmac
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from config import AppConfig, ConfigError, RuleSettings, load_config
from database import Database, DatabaseError
from models import RunReport, isoformat, utcnow
from notifier import Notifier
from pipeline import Pipeline, PipelineError

__version__ = "1.0.0"
LOGGER = logging.getLogger("watchdog.web")
WEBUI_DIR = Path(__file__).with_name("webui")
MAX_CONFIG_BYTES = 1_000_000
MAX_LOG_LINES = 400

JSONValue = dict[str, Any]


class ApiError(RuntimeError):
    """An API error that should be rendered as JSON with a specific status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class RunBusyError(RuntimeError):
    """Raised when a run is already in progress."""


class SafeJSONProvider(DefaultJSONProvider):
    """Serialize the extra scalar types used by the domain models."""

    @staticmethod
    def default(value: object) -> object:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, datetime):
            return isoformat(value)
        return DefaultJSONProvider.default(value)


class JobLogHandler(logging.Handler):
    """Collect pipeline log records for the live run viewer."""

    def __init__(self, callback: Callable[[str, str], None]) -> None:
        super().__init__()
        self.callback = callback
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - defensive formatting fallback
            message = record.getMessage()
        self.callback(record.levelname, message)


class RunManager:
    """Execute at most one pipeline run at a time in a background thread."""

    def __init__(
        self,
        config_path: Path,
        *,
        db_path: Path | None = None,
        reports_dir: Path | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.db_path = Path(db_path) if db_path is not None else None
        self.reports_dir = Path(reports_dir) if reports_dir is not None else Path("reports")
        self._lock = threading.Lock()
        self._job: JSONValue | None = None

    # -- public API ---------------------------------------------------------

    def start(self, *, dry_run: bool = True, rules: Sequence[str] | None = None) -> JSONValue:
        config = self._config()
        requested = list(dict.fromkeys(rules or ()))
        available = {rule.name for rule in config.enabled_rules}
        unknown = [name for name in requested if name not in available]
        if unknown:
            raise ConfigError(f"unknown or disabled rule(s): {', '.join(unknown)}")

        effective_dry_run = bool(
            dry_run or config.notifications.dry_run or _env_truthy("WATCHDOG_DRY_RUN")
        )
        with self._lock:
            if self._job is not None and self._job["status"] == "running":
                raise RunBusyError("a run is already in progress")
            job: JSONValue = {
                "job_id": f"web-{utcnow():%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}",
                "status": "running",
                "dry_run": effective_dry_run,
                "rules": requested,
                "started_at": isoformat(utcnow()),
                "finished_at": None,
                "report": None,
                "report_path": None,
                "error": None,
                "logs": [],
            }
            self._job = job
            worker = threading.Thread(
                target=self._execute,
                args=(job["job_id"], config, effective_dry_run, requested or None),
                daemon=True,
                name="watchdog-web-run",
            )
            worker.start()
            return copy.deepcopy(job)

    def snapshot(self) -> JSONValue | None:
        with self._lock:
            return copy.deepcopy(self._job) if self._job is not None else None

    # -- execution ----------------------------------------------------------

    def _config(self) -> AppConfig:
        config = load_config(self.config_path)
        if self.db_path is not None:
            config = dataclasses.replace(config, database=self.db_path)
        return config

    def _execute(
        self,
        job_id: str,
        config: AppConfig,
        dry_run: bool,
        rules: list[str] | None,
    ) -> None:
        handler = JobLogHandler(lambda level, message: self._append_log(job_id, level, message))
        watchdog_logger = logging.getLogger("watchdog")
        watchdog_logger.addHandler(handler)
        if watchdog_logger.level == logging.NOTSET:
            watchdog_logger.setLevel(logging.INFO)

        temporary: tempfile.TemporaryDirectory[str] | None = None
        database: Database | None = None
        notifier: Notifier | None = None
        try:
            if dry_run:
                temporary = tempfile.TemporaryDirectory(prefix="watchdog-web-dry-run-")
                database = _open_dry_run_database(config, temporary)
            else:
                database = Database(config.database)
            notifier = Notifier(config.notifications, dry_run=dry_run)
            pipeline = Pipeline(config, database, notifier)
            report = pipeline.run(rules=rules)

            report_path: Path | None = None
            try:
                report_name = "dry-run-latest.json" if dry_run else "latest.json"
                report_path = _write_report(self.reports_dir / report_name, report)
            except OSError as exc:  # pragma: no cover - filesystem dependent
                self._append_log(job_id, "WARNING", f"Could not write JSON report: {exc}")

            with self._lock:
                if self._job is not None and self._job["job_id"] == job_id:
                    self._job["status"] = report.status
                    self._job["report"] = report.to_dict()
                    self._job["report_path"] = str(report_path) if report_path else None
                    self._job["finished_at"] = isoformat(report.finished_at or utcnow())
            self._append_log(
                job_id,
                "INFO",
                f"Run finished with status={report.status}: {report.totals}",
            )
        except (ConfigError, DatabaseError, PipelineError) as exc:
            LOGGER.warning("Web-triggered run failed: %s", exc)
            self._fail(job_id, str(exc))
        except Exception as exc:
            LOGGER.exception("Web-triggered run crashed")
            self._fail(job_id, f"{type(exc).__name__}: {exc}")
        finally:
            if notifier is not None:
                notifier.close()
            if database is not None:
                database.close()
            if temporary is not None:
                temporary.cleanup()
            watchdog_logger.removeHandler(handler)

    def _fail(self, job_id: str, message: str) -> None:
        with self._lock:
            if self._job is not None and self._job["job_id"] == job_id:
                self._job["status"] = "failed"
                self._job["error"] = message
                self._job["finished_at"] = isoformat(utcnow())
        self._append_log(job_id, "ERROR", message)

    def _append_log(self, job_id: str, level: str, message: str) -> None:
        with self._lock:
            if self._job is None or self._job["job_id"] != job_id:
                return
            logs: list[JSONValue] = self._job["logs"]
            logs.append({"at": isoformat(utcnow()), "level": level, "message": message})
            if len(logs) > MAX_LOG_LINES:
                del logs[: len(logs) - MAX_LOG_LINES]


# ---------------------------------------------------------------------------
# Configuration and response helpers
# ---------------------------------------------------------------------------


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _open_dry_run_database(
    config: AppConfig, temporary: tempfile.TemporaryDirectory[str]
) -> Database:
    path = Path(temporary.name) / "dry-run.db"
    database = Database(path, ledger_path=Path(temporary.name) / "ledger.json")
    real_ledger = config.database.parent / f"{config.database.stem}.ledger.json"
    if real_ledger.is_file():
        try:
            database.import_ledger(real_ledger)
        except DatabaseError as exc:
            LOGGER.warning("Could not import ledger for dry run: %s", exc)
    return database


def _write_report(path: Path, report: RunReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(report.to_json() + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _rule_summary(rule: RuleSettings) -> JSONValue:
    source = rule.source
    return {
        "name": rule.name,
        "kind": rule.kind,
        "enabled": rule.enabled,
        "bootstrap": rule.bootstrap,
        "source": {
            "format": source.format,
            "url": source.url,
            "host": urlsplit(source.url).hostname or "",
            "params": source.params,
            "max_pages": source.max_pages,
        },
        "match": dataclasses.asdict(rule.match),
    }


def _config_summary(config: AppConfig) -> JSONValue:
    return {
        "path": str(config.path) if config.path else None,
        "database": str(config.database),
        "version": config.version,
        "http": dataclasses.asdict(config.http),
        "notifications": dataclasses.asdict(config.notifications),
        "rules": [_rule_summary(rule) for rule in config.rules],
    }


def _channel_status(config: AppConfig) -> list[JSONValue]:
    settings = config.notifications
    environment = os.environ
    discord = bool(environment.get(settings.discord_webhook_env, "").strip())
    token = bool(environment.get(settings.telegram_bot_token_env, "").strip())
    chat = bool(environment.get(settings.telegram_chat_id_env, "").strip())
    return [
        {"name": "discord", "configured": discord},
        {"name": "telegram", "configured": token and chat},
        {"name": "console", "configured": True},
    ]


def _job_summary(job: JSONValue | None) -> JSONValue | None:
    if job is None:
        return None
    summary = {key: value for key, value in job.items() if key != "logs"}
    summary["log_lines"] = len(job.get("logs", []))
    return summary


def _pagination_args(default_limit: int = 50, max_limit: int = 200) -> tuple[int, int]:
    try:
        limit = int(request.args.get("limit", default_limit))
        offset = int(request.args.get("offset", 0))
    except ValueError as exc:
        raise ApiError("limit and offset must be integers") from exc
    if not 1 <= limit <= max_limit:
        raise ApiError(f"limit must be between 1 and {max_limit}")
    if offset < 0:
        raise ApiError("offset must be zero or greater")
    return limit, offset


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    config_path: str | Path = "config/config.yaml",
    *,
    db_path: str | Path | None = None,
    reports_dir: str | Path | None = None,
    auth_token: str | None = None,
    run_manager: RunManager | None = None,
) -> Flask:
    app = Flask(__name__, static_folder=str(WEBUI_DIR), static_url_path="/static")
    app.json = SafeJSONProvider(app)
    resolved_config = Path(config_path).expanduser().resolve()
    manager = run_manager or RunManager(
        resolved_config,
        db_path=Path(db_path) if db_path is not None else None,
        reports_dir=Path(reports_dir) if reports_dir is not None else None,
    )
    token = (
        auth_token if auth_token is not None else os.environ.get("WATCHDOG_UI_TOKEN", "")
    ).strip()

    app.config.update(
        WATCHDOG_CONFIG_PATH=resolved_config,
        WATCHDOG_UI_TOKEN=token,
        WATCHDOG_RUN_MANAGER=manager,
    )

    # -- helpers bound to this app -----------------------------------------

    def current_config() -> AppConfig:
        try:
            config = load_config(resolved_config)
        except ConfigError as exc:
            raise ApiError(f"Configuration is invalid: {exc}", 422) from exc
        if db_path is not None:
            config = dataclasses.replace(config, database=Path(db_path))
        return config

    def open_database(config: AppConfig) -> Database:
        try:
            return Database(config.database)
        except (DatabaseError, OSError) as exc:
            raise ApiError(f"Could not open database: {exc}", 500) from exc

    def run() -> RunManager:
        return app.config["WATCHDOG_RUN_MANAGER"]

    # -- security -----------------------------------------------------------

    @app.before_request
    def protect_api() -> Response | None:
        if not request.path.startswith("/api/") or request.path == "/api/health":
            return None
        expected = app.config["WATCHDOG_UI_TOKEN"]
        if not expected:
            return None
        provided = request.headers.get("X-Watchdog-Token", "").strip()
        authorization = request.headers.get("Authorization", "")
        if not provided and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        if not provided or not hmac.compare_digest(provided, expected):
            return jsonify(error="Unauthorized. Provide a valid dashboard token."), 401
        return None

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # -- pages --------------------------------------------------------------

    @app.get("/")
    def index() -> Response:
        response = send_from_directory(WEBUI_DIR, "index.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/favicon.svg")
    def favicon() -> Response:
        return send_from_directory(WEBUI_DIR, "favicon.svg")

    # -- API ----------------------------------------------------------------

    @app.get("/api/health")
    def health() -> Response:
        return jsonify(
            {
                "status": "ok",
                "version": __version__,
                "time": isoformat(utcnow()),
                "auth_required": bool(app.config["WATCHDOG_UI_TOKEN"]),
            }
        )

    @app.get("/api/overview")
    def overview() -> Response:
        config = current_config()
        database = open_database(config)
        try:
            states = {state["rule"]: state for state in database.list_rule_states()}
            rules = [
                {**_rule_summary(rule), "state": states.get(rule.name)} for rule in config.rules
            ]
            return jsonify(
                {
                    "summary": database.summary(),
                    "rules": rules,
                    "recent_runs": database.list_runs(limit=5),
                    "recent_alerts": database.list_alerts(limit=5)[0],
                    "price_watch": database.recent_price_watch(limit=5),
                    "channels": _channel_status(config),
                    "config": {
                        "path": str(resolved_config),
                        "database": str(config.database),
                    },
                    "auth_required": bool(app.config["WATCHDOG_UI_TOKEN"]),
                    "run": _job_summary(run().snapshot()),
                }
            )
        finally:
            database.close()

    @app.get("/api/rules")
    def rules() -> Response:
        config = current_config()
        database = open_database(config)
        try:
            states = {state["rule"]: state for state in database.list_rule_states()}
            return jsonify(
                {
                    "rules": [
                        {**_rule_summary(rule), "state": states.get(rule.name)}
                        for rule in config.rules
                    ]
                }
            )
        finally:
            database.close()

    @app.get("/api/runs")
    def runs() -> Response:
        limit, offset = _pagination_args()
        config = current_config()
        database = open_database(config)
        try:
            return jsonify(
                {
                    "items": database.list_runs(limit=limit, offset=offset),
                    "total": database.count_runs(),
                    "limit": limit,
                    "offset": offset,
                }
            )
        finally:
            database.close()

    @app.get("/api/runs/<run_id>")
    def run_detail(run_id: str) -> Response:
        config = current_config()
        database = open_database(config)
        try:
            run_row = database.get_run(run_id)
            if run_row is None:
                raise ApiError("Run not found", 404)
            run_row["rule_states"] = database.list_rule_states()
            return jsonify(run_row)
        finally:
            database.close()

    @app.get("/api/listings")
    def listings() -> Response:
        limit, offset = _pagination_args()
        kind = request.args.get("kind") or None
        if kind not in (None, "job", "price"):
            raise ApiError("kind must be 'job' or 'price'")
        source = (request.args.get("source") or "").strip() or None
        query = (request.args.get("q") or "").strip() or None
        config = current_config()
        database = open_database(config)
        try:
            items, total = database.list_listings(
                kind=kind, source=source, query=query, limit=limit, offset=offset
            )
            return jsonify(
                {
                    "items": items,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "sources": database.distinct_sources(),
                }
            )
        finally:
            database.close()

    @app.get("/api/listings/<fingerprint>")
    def listing_detail(fingerprint: str) -> Response:
        config = current_config()
        database = open_database(config)
        try:
            detail = database.get_listing_detail(fingerprint)
            if detail is None:
                raise ApiError("Listing not found", 404)
            return jsonify(detail)
        finally:
            database.close()

    @app.get("/api/alerts")
    def alerts() -> Response:
        limit, offset = _pagination_args()
        status = (request.args.get("status") or "").strip() or None
        rule = (request.args.get("rule") or "").strip() or None
        channel = (request.args.get("channel") or "").strip() or None
        config = current_config()
        database = open_database(config)
        try:
            items, total = database.list_alerts(
                status=status,
                rule=rule,
                channel=channel,
                limit=limit,
                offset=offset,
            )
            return jsonify(
                {
                    "items": items,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "rules": [
                        rule_summary["name"] for rule_summary in map(_rule_summary, config.rules)
                    ],
                    "channels": [channel_info["name"] for channel_info in _channel_status(config)],
                }
            )
        finally:
            database.close()

    @app.get("/api/config")
    def get_config() -> Response:
        content = ""
        error: str | None = None
        summary: JSONValue | None = None
        if resolved_config.is_file():
            try:
                content = resolved_config.read_text(encoding="utf-8")
            except OSError as exc:
                raise ApiError(f"Could not read configuration: {exc}", 500) from exc
        try:
            summary = _config_summary(current_config())
        except ApiError as exc:
            error = exc.message
        return jsonify(
            {
                "path": str(resolved_config),
                "content": content,
                "valid": error is None,
                "error": error,
                "summary": summary,
            }
        )

    @app.post("/api/config/validate")
    def validate_config() -> Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise ApiError("Request body must be JSON with a string 'content' field")
        content = payload["content"]
        if len(content.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ApiError("Configuration is too large (1 MB limit)", 413)
        try:
            summary = _validate_config_text(resolved_config, content)
        except ConfigError as exc:
            raise ApiError(f"Configuration is invalid: {exc}", 422) from exc
        return jsonify({"valid": True, "summary": summary})

    @app.put("/api/config")
    def save_config() -> Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise ApiError("Request body must be JSON with a string 'content' field")
        content = payload["content"]
        if len(content.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ApiError("Configuration is too large (1 MB limit)", 413)
        try:
            summary = _validate_and_write_config(resolved_config, content)
        except ConfigError as exc:
            raise ApiError(f"Configuration is invalid: {exc}", 422) from exc
        except OSError as exc:
            raise ApiError(f"Could not write configuration: {exc}", 500) from exc
        LOGGER.info("Configuration updated through the dashboard")
        return jsonify({"saved": True, "path": str(resolved_config), "summary": summary})

    @app.post("/api/run")
    def start_run() -> tuple[Response, int]:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise ApiError("Request body must be a JSON object")
        dry_run = payload.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise ApiError("dry_run must be true or false")
        selected = payload.get("rules") or []
        if not isinstance(selected, list) or not all(isinstance(name, str) for name in selected):
            raise ApiError("rules must be a list of rule names")
        try:
            job = run().start(dry_run=dry_run, rules=selected)
        except RunBusyError as exc:
            raise ApiError(str(exc), 409) from exc
        except ConfigError as exc:
            raise ApiError(f"Configuration is invalid: {exc}", 422) from exc
        return jsonify({"job": _job_summary(job)}), 202

    @app.get("/api/run/status")
    def run_status() -> Response:
        return jsonify({"job": run().snapshot()})

    # -- errors -------------------------------------------------------------

    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError) -> tuple[Response, int]:
        return jsonify(error=exc.message), exc.status

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException) -> tuple[Response, int] | HTTPException:
        if request.path.startswith("/api/"):
            return jsonify(error=exc.description), int(exc.code or 500)
        return exc

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception) -> tuple[Response, int]:
        LOGGER.exception("Unhandled dashboard error")
        if request.path.startswith("/api/"):
            return jsonify(error="Internal server error"), 500
        return jsonify(error="Internal server error"), 500

    return app


def _validate_config_text(path: Path, content: str) -> JSONValue:
    """Validate YAML in a temporary sibling file without touching the real config."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.validate")
    temporary.write_text(content, encoding="utf-8")
    try:
        validated = load_config(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    validated = dataclasses.replace(validated, path=path)
    return _config_summary(validated)


def _validate_and_write_config(path: Path, content: str) -> JSONValue:
    """Validate YAML in a temporary sibling file, back up, then replace atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    backup = path.with_name(path.name + ".bak")
    temporary.write_text(content, encoding="utf-8")
    try:
        load_config(temporary)
    except ConfigError:
        temporary.unlink(missing_ok=True)
        raise
    if path.is_file():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    temporary.replace(path)
    # The temporary path differs from the real path; keep summaries truthful.
    validated = load_config(path)
    return _config_summary(validated)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchdog-web",
        description="Run the Job / Price Watchdog web dashboard.",
    )
    parser.add_argument("--host", default=os.environ.get("WATCHDOG_UI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WATCHDOG_UI_PORT", "8000")))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/config.yaml"),
        help="YAML configuration file (default: config/config.yaml)",
    )
    parser.add_argument("--db", type=Path, help="override the SQLite database path")
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--debug", action="store_true", help="use the Flask development server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - server bootstrap
    args = build_parser().parse_args(argv)
    load_dotenv(override=False)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    app = create_app(
        args.config,
        db_path=args.db,
        reports_dir=args.reports_dir,
    )
    public_bind = args.host not in {"127.0.0.1", "localhost", "::1"}
    if public_bind and not app.config["WATCHDOG_UI_TOKEN"]:
        LOGGER.warning(
            "Dashboard is bound to %s without WATCHDOG_UI_TOKEN. Set a token before "
            "exposing this service to a network.",
            args.host,
        )
    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
        return 0
    try:
        from waitress import serve
    except ImportError:  # pragma: no cover - optional dependency fallback
        LOGGER.warning("Waitress is not installed; falling back to the Flask development server")
        app.run(host=args.host, port=args.port, debug=False)
        return 0
    LOGGER.info("Dashboard listening on http://%s:%d", args.host, args.port)
    serve(app, host=args.host, port=args.port, threads=8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
