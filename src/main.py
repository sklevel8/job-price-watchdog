"""Command-line entry point for the watchdog ETL.

Run locally with ``python src/main.py`` from the repository root (the
``src/`` directory is the import root).  Exit codes: 0 = all rules succeeded,
1 = partial, 2 = configuration/fatal failure.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from config import AppConfig, ConfigError, load_config
from database import Database, DatabaseError
from models import RunReport
from notifier import Notifier
from pipeline import Pipeline, PipelineError

__version__ = "1.0.0"
LOGGER = logging.getLogger("watchdog")


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, useful in CI log viewers."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-price-watchdog",
        description="Run the serverless job/price watchdog ETL once.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/config.yaml"),
        help="YAML configuration file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="override the SQLite database path from the config",
    )
    parser.add_argument(
        "--rule",
        action="append",
        dest="rules",
        metavar="NAME",
        help="run only this rule (repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview matches without writing the real database or sending messages",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        metavar="PATH",
        help="write the machine-readable run report to PATH",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        metavar="PATH",
        help="append a Markdown summary (use $GITHUB_STEP_SUMMARY in Actions)",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=os.environ.get("WATCHDOG_LOG_LEVEL", "INFO").upper(),
        help="logging verbosity (default: INFO or WATCHDOG_LOG_LEVEL)",
    )
    parser.add_argument(
        "--log-format",
        choices=("text", "json"),
        default="text",
        help="human-readable or JSON logs (default: text)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def configure_logging(level: str, log_format: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _effective_dry_run(explicit: bool, config: AppConfig) -> bool:
    return explicit or config.notifications.dry_run or _env_truthy("WATCHDOG_DRY_RUN")


def _open_dry_run_database(config: AppConfig, temporary: tempfile.TemporaryDirectory) -> Database:
    """Build a disposable DB so a preview never marks real alerts as seen."""

    path = Path(temporary.name) / "dry-run.db"
    database = Database(path, ledger_path=Path(temporary.name) / "ledger.json")
    real_ledger = config.database.parent / f"{config.database.stem}.ledger.json"
    if real_ledger.is_file():
        try:
            database.import_ledger(real_ledger)
        except DatabaseError as exc:
            LOGGER.warning("Could not import ledger for dry run: %s", exc)
    return database


def _write_json_report(path: Path, report: RunReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(report.to_json() + "\n", encoding="utf-8")
    temporary.replace(path)


def _append_summary(path: Path, report: RunReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(report.to_markdown())
        handle.write("\n")


def print_report(report: RunReport, db_summary: dict[str, Any] | None = None) -> None:
    print(f"\nRun {report.run_id} — {report.status.upper()}")
    print("-" * 78)
    for stats in report.rules:
        print(
            f"{stats.rule:<28} {stats.status:<8} "
            f"fetched={stats.fetched:<4} matched={stats.matched:<4} "
            f"new={stats.new_listings:<4} alerts={stats.alerts:<3} errors={stats.errors}"
        )
    totals = report.totals
    print("-" * 78)
    print(
        "Totals: "
        f"fetched={totals['fetched']} matched={totals['matched']} "
        f"new={totals['new_listings']} alerts={totals['alerts']} "
        f"deliveries={totals['deliveries']} errors={totals['errors']}"
    )
    if report.errors:
        print("Errors:")
        for error in report.errors:
            print(f"  - {error}")
    if db_summary:
        last = db_summary.get("last_run") or {}
        print(
            f"Database: listings={db_summary.get('listings', 0)} "
            f"price_observations={db_summary.get('price_observations', 0)} "
            f"alerts_sent={db_summary.get('alerts_sent', 0)} "
            f"last_run={last.get('run_id', '-')}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(override=False)
    args = build_parser().parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(
            AttributeError, ValueError
        ):  # pragma: no cover - platform dependent
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    configure_logging(args.log_level, args.log_format)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return 2

    if args.db is not None:
        config = dataclasses.replace(config, database=args.db)

    dry_run = _effective_dry_run(args.dry_run, config)
    temporary: tempfile.TemporaryDirectory | None = None
    database: Database | None = None
    notifier: Notifier | None = None
    try:
        if dry_run:
            temporary = tempfile.TemporaryDirectory(prefix="watchdog-dry-run-")
            database = _open_dry_run_database(config, temporary)
            LOGGER.info("Dry run: using disposable database %s", database.path)
        else:
            database = Database(config.database)

        notifier = Notifier(config.notifications, dry_run=dry_run)
        if notifier.channels == ("console",) and not dry_run:
            LOGGER.warning(
                "No Discord/Telegram credentials configured; alerts will be printed to the log"
            )

        pipeline = Pipeline(config, database, notifier)
        report = pipeline.run(rules=args.rules)

        if args.json_report:
            _write_json_report(args.json_report, report)
            LOGGER.info("Wrote JSON report to %s", args.json_report)
        if args.summary_file:
            _append_summary(args.summary_file, report)
            LOGGER.info("Appended run summary to %s", args.summary_file)

        summary = database.summary()
        print_report(report, summary)
        return {"ok": 0, "partial": 1, "failed": 2}.get(report.status, 2)
    except (ConfigError, DatabaseError, PipelineError) as exc:
        LOGGER.error("Pipeline error: %s", exc)
        return 2
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted")
        return 130
    except Exception:
        LOGGER.exception("Unhandled watchdog failure")
        return 2
    finally:
        if notifier is not None:
            notifier.close()
        if database is not None:
            database.close()
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
