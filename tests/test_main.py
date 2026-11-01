"""CLI integration tests using a stubbed pipeline (no network access)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

import main
from models import RuleStats, RunReport
from pipeline import PipelineError

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def write_config(tmp_path: Path, *, database: Path | None = None) -> Path:
    config = {
        "version": 1,
        "database": str(database or (tmp_path / "state" / "watchdog.db")),
        "notifications": {"alert_burst_warning_threshold": 10},
        "rules": [
            {
                "name": "jobs",
                "kind": "job",
                "source": {
                    "format": "remoteok_json",
                    "url": "https://example.com/api",
                },
                "match": {"keywords": ["python"]},
            }
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def make_report(status: str = "ok") -> RunReport:
    return RunReport(
        run_id="stub-run",
        status=status,  # type: ignore[arg-type]
        started_at=NOW,
        finished_at=NOW,
        rules=[
            RuleStats(
                rule="jobs",
                status="ok" if status == "ok" else "failed",  # type: ignore[arg-type]
                fetched=2,
                matched=1,
                new_listings=1,
                alerts=1,
                deliveries=1 if status == "ok" else 0,
                errors=0 if status == "ok" else 1,
                detail="" if status == "ok" else "upstream down",
            )
        ],
    )


class StubPipeline:
    instances: ClassVar[list[StubPipeline]] = []
    report: ClassVar[RunReport] = make_report()
    error: ClassVar[Exception | None] = None

    def __init__(self, config, db, notifier) -> None:
        self.config = config
        self.db = db
        self.notifier = notifier
        self.rules = None
        StubPipeline.instances.append(self)

    def run(self, *, rules=None):
        self.rules = rules
        if StubPipeline.error:
            raise StubPipeline.error
        return StubPipeline.report


@pytest.fixture(autouse=True)
def reset_stub() -> None:
    StubPipeline.instances = []
    StubPipeline.report = make_report()
    StubPipeline.error = None


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> type[StubPipeline]:
    monkeypatch.setattr(main, "Pipeline", StubPipeline)
    return StubPipeline


def test_successful_run_prints_summary(
    tmp_path: Path, stub_pipeline: type[StubPipeline], capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    exit_code = main.main(["--config", str(config), "--dry-run"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Run stub-run" in captured.out
    assert "jobs" in captured.out
    assert "Totals:" in captured.out
    assert len(stub_pipeline.instances) == 1


def test_dry_run_does_not_create_configured_database(
    tmp_path: Path, stub_pipeline: type[StubPipeline]
) -> None:
    database = tmp_path / "state" / "watchdog.db"
    config = write_config(tmp_path, database=database)
    assert main.main(["--config", str(config), "--dry-run"]) == 0
    assert not database.exists()
    assert stub_pipeline.instances[0].db.path != database


def test_env_dry_run(
    tmp_path: Path,
    stub_pipeline: type[StubPipeline],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_config(tmp_path, database=tmp_path / "state" / "watchdog.db")
    monkeypatch.setenv("WATCHDOG_DRY_RUN", "true")
    assert main.main(["--config", str(config)]) == 0
    assert not (tmp_path / "state" / "watchdog.db").exists()


def test_dry_run_preview_does_not_send(tmp_path: Path, stub_pipeline: type[StubPipeline]) -> None:
    config = write_config(tmp_path)
    assert main.main(["--config", str(config), "--dry-run"]) == 0
    notifier = stub_pipeline.instances[0].notifier
    assert notifier.dry_run is True


def test_partial_status_exit_code(
    tmp_path: Path, stub_pipeline: type[StubPipeline], capsys: pytest.CaptureFixture[str]
) -> None:
    stub_pipeline.report = make_report("partial")
    config = write_config(tmp_path)
    assert main.main(["--config", str(config), "--dry-run"]) == 1
    assert "PARTIAL" in capsys.readouterr().out


def test_failed_status_exit_code(tmp_path: Path, stub_pipeline: type[StubPipeline]) -> None:
    stub_pipeline.report = make_report("failed")
    config = write_config(tmp_path)
    assert main.main(["--config", str(config), "--dry-run"]) == 2


def test_rule_filter_is_forwarded(tmp_path: Path, stub_pipeline: type[StubPipeline]) -> None:
    config = write_config(tmp_path)
    assert main.main(["--config", str(config), "--dry-run", "--rule", "jobs"]) == 0
    assert stub_pipeline.instances[0].rules == ["jobs"]


def test_db_override_is_applied(tmp_path: Path, stub_pipeline: type[StubPipeline]) -> None:
    config = write_config(tmp_path)
    override = tmp_path / "custom" / "override.db"
    assert main.main(["--config", str(config), "--db", str(override), "--dry-run"]) == 0
    assert stub_pipeline.instances[0].config.database == override


def test_json_report_and_summary(tmp_path: Path, stub_pipeline: type[StubPipeline]) -> None:
    config = write_config(tmp_path)
    report_path = tmp_path / "reports" / "latest.json"
    summary_path = tmp_path / "summary.md"
    assert (
        main.main(
            [
                "--config",
                str(config),
                "--dry-run",
                "--json-report",
                str(report_path),
                "--summary-file",
                str(summary_path),
            ]
        )
        == 0
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "stub-run"
    assert payload["totals"]["alerts"] == 1
    summary = summary_path.read_text(encoding="utf-8")
    assert "## Watchdog run `stub-run`" in summary
    assert "| `jobs` | ok |" in summary


def test_config_error_returns_two(tmp_path: Path, stub_pipeline: type[StubPipeline]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 2\nrules: []\n", encoding="utf-8")
    assert main.main(["--config", str(bad), "--dry-run"]) == 2
    assert stub_pipeline.instances == []


def test_pipeline_error_returns_two(tmp_path: Path, stub_pipeline: type[StubPipeline]) -> None:
    stub_pipeline.error = PipelineError("unknown rule")
    config = write_config(tmp_path)
    assert main.main(["--config", str(config), "--dry-run"]) == 2


def test_unexpected_error_returns_two(tmp_path: Path, stub_pipeline: type[StubPipeline]) -> None:
    stub_pipeline.error = RuntimeError("boom")
    config = write_config(tmp_path)
    assert main.main(["--config", str(config), "--dry-run"]) == 2


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main.main(["--version"])
    assert exc_info.value.code == 0
    assert main.__version__ in capsys.readouterr().out


def test_parser_rejects_unknown_log_format(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main.main(["--config", str(tmp_path / "x.yaml"), "--log-format", "xml"])
    assert exc_info.value.code == 2
