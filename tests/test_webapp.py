"""Web dashboard and API tests. All network and pipeline work is stubbed."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

import webapp
from config import ConfigError
from database import Database
from models import Alert, DeliveryResult, Listing, RuleStats, RunReport

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def write_config(tmp_path: Path, database: Path, *, dry_run: bool = True) -> Path:
    payload = {
        "version": 1,
        "database": str(database),
        "notifications": {"dry_run": dry_run, "alert_burst_warning_threshold": 10},
        "rules": [
            {
                "name": "jobs",
                "kind": "job",
                "source": {"format": "remoteok_json", "url": "https://example.com/api"},
                "match": {"keywords": ["python"], "max_age_days": 30},
            },
            {
                "name": "books",
                "kind": "price",
                "source": {
                    "format": "books_toscrape",
                    "url": "https://example.com/catalogue/page-1.html",
                    "max_pages": 1,
                },
                "match": {"max_price": 20.0, "currency": "GBP"},
            },
            {
                "name": "disabled-rule",
                "kind": "job",
                "enabled": False,
                "source": {"format": "hn_algolia_json", "url": "https://example.com/search"},
                "match": {},
            },
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def make_listing(kind: str = "job", **overrides) -> Listing:
    values = {
        "kind": kind,
        "source": "test",
        "external_id": "id-1",
        "title": "Senior Python Engineer",
        "url": "https://example.com/jobs/1",
        "company": "Acme",
        "location": "Remote",
        "remote": True,
        "description": "Build reliable pipelines",
        "tags": ("python", "backend"),
        "posted_at": NOW - timedelta(days=1),
        "raw": {},
    }
    if kind == "price":
        values.update(
            {
                "title": "A Light in the Attic",
                "url": "https://example.com/books/1",
                "company": "Books to Scrape",
                "price": Decimal("12.50"),
                "currency": "GBP",
                "tags": ("books",),
            }
        )
    values.update(overrides)
    return Listing(**values)


def seed(database_path: Path) -> None:
    with Database(database_path) as database:
        job = make_listing()
        product = make_listing("price", external_id="book-1")
        database.upsert_listing(job, NOW)
        database.upsert_listing(product, NOW)
        database.record_price(product.fingerprint, Decimal("12.50"), "GBP", NOW)
        database.record_price(
            product.fingerprint, Decimal("10.00"), "GBP", NOW + timedelta(hours=1)
        )
        database.start_run("run-1", NOW)
        database.finish_run(
            "run-1",
            "ok",
            {
                "fetched": 2,
                "matched": 2,
                "new_listings": 2,
                "alerts": 1,
                "deliveries": 1,
                "errors": 0,
            },
            NOW + timedelta(seconds=3),
        )
        database.record_rule_run("jobs", "ok", NOW)
        alert = Alert(listing=job, rule="jobs", reason="new_match", trigger_key="new")
        database.record_delivery(DeliveryResult(alert, "console", "console"), NOW)


@pytest.fixture
def client(tmp_path: Path):
    database_path = tmp_path / "state" / "watchdog.db"
    config_path = write_config(tmp_path, database_path)
    seed(database_path)
    app = webapp.create_app(config_path, reports_dir=tmp_path / "reports")
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client, tmp_path, config_path


def test_health_is_public(client) -> None:
    test_client, _, _ = client
    response = test_client.get("/api/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["auth_required"] is False


def test_index_and_static_assets(client) -> None:
    test_client, _, _ = client
    responses = [
        test_client.get("/"),
        test_client.get("/static/app.js"),
        test_client.get("/static/styles.css"),
        test_client.get("/favicon.svg"),
    ]
    try:
        assert responses[0].status_code == 200
        assert b"Watchdog" in responses[0].data
        assert b'name="description"' in responses[0].data
        # Mobile browsers must not render the page at a fake 980px width.
        assert b'name="viewport" content="width=device-width, initial-scale=1"' in responses[0].data
        assert b'id="layout-switch"' in responses[0].data
        assert b'data-layout="auto"' in responses[0].data
        assert b'data-layout="mobile"' in responses[0].data
        # Grouped navigation becomes a fixed bottom tab bar on compact layouts.
        assert b'class="nav-section"' in responses[0].data
        # Error banners must use the `hidden` attribute, which JS toggles.
        # A `hidden` class would keep them invisible after the attribute clears.
        assert b'class="banner error hidden"' not in responses[0].data
        assert b'class="form-error hidden"' not in responses[0].data
        assert responses[1].status_code == 200
        assert b"watchdog.layout" in responses[1].data
        assert b"applyLayout" in responses[1].data
        assert responses[2].status_code == 200
        assert b"container-type: inline-size" in responses[2].data
        assert b"@container (max-width: 860px)" in responses[2].data
        assert b'html[data-layout="auto"]' in responses[2].data
        assert b'html[data-layout="mobile"]' in responses[2].data
        assert b'html[data-layout="desktop"]' in responses[2].data
        assert b"safe-area-inset-bottom" in responses[2].data
        assert responses[3].status_code == 200
    finally:
        for response in responses:
            response.close()


def test_overview_merges_config_and_state(client) -> None:
    test_client, _, _ = client
    response = test_client.get("/api/overview")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["summary"]["listings"] == 2
    assert payload["summary"]["price_observations"] == 2
    assert payload["summary"]["last_run"]["run_id"] == "run-1"
    assert [rule["name"] for rule in payload["rules"]] == ["jobs", "books", "disabled-rule"]
    jobs = next(rule for rule in payload["rules"] if rule["name"] == "jobs")
    assert jobs["state"]["last_status"] == "ok"
    assert jobs["source"]["host"] == "example.com"
    assert len(payload["recent_runs"]) == 1
    assert payload["recent_alerts"][0]["listing_title"] == "Senior Python Engineer"
    assert payload["price_watch"][0]["price_stats"]["minimum"] == "10.00"
    assert {channel["name"] for channel in payload["channels"]} == {
        "discord",
        "telegram",
        "console",
    }


def test_rules_endpoint(client) -> None:
    test_client, _, _ = client
    response = test_client.get("/api/rules")
    assert response.status_code == 200
    names = [rule["name"] for rule in response.get_json()["rules"]]
    assert names == ["jobs", "books", "disabled-rule"]


def test_runs_endpoint_and_detail(client) -> None:
    test_client, _, _ = client
    listing = test_client.get("/api/runs?limit=10&offset=0")
    assert listing.status_code == 200
    payload = listing.get_json()
    assert payload["total"] == 1
    assert payload["items"][0]["totals"]["alerts"] == 1

    detail = test_client.get("/api/runs/run-1")
    assert detail.status_code == 200
    assert detail.get_json()["rule_states"][0]["rule"] == "jobs"

    missing = test_client.get("/api/runs/missing")
    assert missing.status_code == 404
    assert "not found" in missing.get_json()["error"].lower()


def test_runs_pagination_validation(client) -> None:
    test_client, _, _ = client
    assert test_client.get("/api/runs?limit=0").status_code == 400
    assert test_client.get("/api/runs?offset=-1").status_code == 400
    assert test_client.get("/api/runs?limit=abc").status_code == 400


def test_listings_filters_and_detail(client) -> None:
    test_client, _, _ = client
    response = test_client.get("/api/listings")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 2
    assert set(payload["sources"]) == {"test"}

    jobs = test_client.get("/api/listings?kind=job").get_json()
    assert jobs["total"] == 1
    assert jobs["items"][0]["kind"] == "job"

    search = test_client.get("/api/listings?q=attic").get_json()
    assert search["total"] == 1
    assert search["items"][0]["kind"] == "price"

    detail = test_client.get(f"/api/listings/{search['items'][0]['fingerprint']}")
    assert detail.status_code == 200
    detail_payload = detail.get_json()
    assert detail_payload["price_stats"]["minimum"] == "10.00"
    assert len(detail_payload["price_history"]) == 2

    assert test_client.get("/api/listings?kind=bogus").status_code == 400
    assert test_client.get("/api/listings/missing").status_code == 404


def test_alerts_filters(client) -> None:
    test_client, _, _ = client
    response = test_client.get("/api/alerts?status=console&channel=console")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert payload["items"][0]["rule"] == "jobs"
    assert payload["items"][0]["listing_url"].startswith("https://example.com")
    assert "jobs" in payload["rules"]

    empty = test_client.get("/api/alerts?status=failed").get_json()
    assert empty["total"] == 0


def test_get_config(client) -> None:
    test_client, _, config_path = client
    response = test_client.get("/api/config")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["valid"] is True
    assert payload["path"] == str(config_path.resolve())
    assert "rules:" in payload["content"]
    assert len(payload["summary"]["rules"]) == 3


def test_validate_config_does_not_write(client) -> None:
    test_client, _, config_path = client
    original = config_path.read_text(encoding="utf-8")
    valid = test_client.post("/api/config/validate", json={"content": original})
    assert valid.status_code == 200
    assert valid.get_json()["valid"] is True
    assert config_path.read_text(encoding="utf-8") == original

    invalid = test_client.post("/api/config/validate", json={"content": "version: 2\nrules: []\n"})
    assert invalid.status_code == 422
    assert "invalid" in invalid.get_json()["error"].lower()
    assert test_client.post("/api/config/validate", json={}).status_code == 400


def test_save_config_validates_and_backs_up(client) -> None:
    test_client, _, config_path = client
    original = config_path.read_text(encoding="utf-8")
    assert (
        test_client.put("/api/config", json={"content": "version: 2\nrules: []\n"}).status_code
        == 422
    )
    assert config_path.read_text(encoding="utf-8") == original

    updated = original.replace("max_age_days: 30", "max_age_days: 45")
    response = test_client.put("/api/config", json={"content": updated})
    assert response.status_code == 200
    assert "max_age_days: 45" in config_path.read_text(encoding="utf-8")
    assert (config_path.parent / f"{config_path.name}.bak").read_text(encoding="utf-8") == original
    assert test_client.put("/api/config", json={}).status_code == 400


def test_auth_token_protects_api(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    config_path = write_config(tmp_path, database_path)
    app = webapp.create_app(config_path, auth_token="secret-token")
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        assert test_client.get("/api/health").status_code == 200
        assert test_client.get("/api/overview").status_code == 401
        assert (
            test_client.get("/api/overview", headers={"X-Watchdog-Token": "wrong"}).status_code
            == 401
        )
        assert (
            test_client.get(
                "/api/overview", headers={"X-Watchdog-Token": "secret-token"}
            ).status_code
            == 200
        )
        assert (
            test_client.get(
                "/api/overview", headers={"Authorization": "Bearer secret-token"}
            ).status_code
            == 200
        )


def test_unknown_api_route_returns_json(client) -> None:
    test_client, _, _ = client
    response = test_client.get("/api/nope")
    assert response.status_code == 404
    assert response.is_json


def test_invalid_config_file_reports_error(tmp_path: Path) -> None:
    config_path = tmp_path / "bad.yaml"
    config_path.write_text("version: 2\nrules: []\n", encoding="utf-8")
    app = webapp.create_app(config_path)
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        assert test_client.get("/api/overview").status_code == 422
        payload = test_client.get("/api/config").get_json()
        assert payload["valid"] is False
        assert "invalid" in payload["error"].lower()


# -- run manager -----------------------------------------------------------


class FakeNotifier:
    def __init__(self, settings, *, dry_run=False) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self.closed = False
        self.available_channels = ("console",)

    def close(self) -> None:
        self.closed = True


class FakePipeline:
    calls: ClassVar[list[tuple]] = []
    gate: ClassVar[object | None] = None
    report: ClassVar[RunReport | None] = None

    def __init__(self, config, db, notifier) -> None:
        self.config = config
        self.db = db
        self.notifier = notifier

    def run(self, *, rules=None):
        FakePipeline.calls.append((rules, self.notifier.dry_run))
        if FakePipeline.gate is not None:
            FakePipeline.gate.wait(3)
        return FakePipeline.report or RunReport(
            run_id="fake-run",
            status="ok",
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=2),
            rules=[RuleStats(rule="jobs", status="ok", fetched=1, matched=1, alerts=1)],
        )


@pytest.fixture(autouse=True)
def reset_fake_pipeline() -> None:
    FakePipeline.calls = []
    FakePipeline.gate = None
    FakePipeline.report = None


def wait_for(manager: webapp.RunManager, timeout: float = 4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.snapshot()
        if job and job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("run did not finish in time")


def test_run_manager_executes_and_records_report(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "state.db"
    config_path = write_config(tmp_path, database_path)
    monkeypatch.setattr(webapp, "Pipeline", FakePipeline)
    monkeypatch.setattr(webapp, "Notifier", FakeNotifier)
    manager = webapp.RunManager(config_path, reports_dir=tmp_path / "reports")

    job = manager.start(dry_run=True, rules=["jobs"])
    assert job["status"] == "running"
    finished = wait_for(manager)
    assert finished["status"] == "ok"
    assert finished["report"]["totals"]["alerts"] == 1
    assert finished["rules"] == ["jobs"]
    assert Path(finished["report_path"]).name == "dry-run-latest.json"
    assert Path(finished["report_path"]).is_file()
    assert not (tmp_path / "reports" / "latest.json").exists()
    assert any("Run finished" in line["message"] for line in finished["logs"])
    assert FakePipeline.calls == [(["jobs"], True)]


def test_run_manager_rejects_unknown_rule(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, tmp_path / "state.db")
    manager = webapp.RunManager(config_path, reports_dir=tmp_path / "reports")
    with pytest.raises(ConfigError, match="unknown or disabled"):
        manager.start(dry_run=True, rules=["nope"])


def test_run_manager_rejects_concurrent_runs(tmp_path: Path, monkeypatch) -> None:
    import threading

    database_path = tmp_path / "state.db"
    config_path = write_config(tmp_path, database_path)
    gate = threading.Event()
    FakePipeline.gate = gate
    monkeypatch.setattr(webapp, "Pipeline", FakePipeline)
    monkeypatch.setattr(webapp, "Notifier", FakeNotifier)
    manager = webapp.RunManager(config_path, reports_dir=tmp_path / "reports")

    manager.start(dry_run=True)
    with pytest.raises(webapp.RunBusyError):
        manager.start(dry_run=True)
    gate.set()
    wait_for(manager)


def test_run_manager_records_failure(tmp_path: Path, monkeypatch) -> None:
    config_path = write_config(tmp_path, tmp_path / "state.db")

    class BrokenPipeline(FakePipeline):
        def run(self, *, rules=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(webapp, "Pipeline", BrokenPipeline)
    monkeypatch.setattr(webapp, "Notifier", FakeNotifier)
    manager = webapp.RunManager(config_path, reports_dir=tmp_path / "reports")
    manager.start(dry_run=False)
    finished = wait_for(manager)
    assert finished["status"] == "failed"
    assert "boom" in finished["error"]


def test_run_endpoints(client, monkeypatch) -> None:
    test_client, tmp_path, _ = client
    monkeypatch.setattr(webapp, "Pipeline", FakePipeline)
    monkeypatch.setattr(webapp, "Notifier", FakeNotifier)
    manager = webapp.RunManager(tmp_path / "config.yaml", reports_dir=tmp_path / "reports")
    test_client.application.config["WATCHDOG_RUN_MANAGER"] = manager

    status = test_client.get("/api/run/status")
    assert status.status_code == 200
    assert status.get_json()["job"] is None

    started = test_client.post("/api/run", json={"dry_run": True, "rules": ["jobs"]})
    assert started.status_code == 202
    assert started.get_json()["job"]["status"] == "running"
    finished = wait_for(manager)
    assert finished["status"] == "ok"

    payload = test_client.get("/api/run/status").get_json()["job"]
    assert payload["report"]["run_id"] == "fake-run"

    assert test_client.post("/api/run", json={"dry_run": "yes"}).status_code == 400
    assert test_client.post("/api/run", json={"rules": "jobs"}).status_code == 400
    assert test_client.post("/api/run", json={"rules": ["nope"]}).status_code == 422


def test_run_endpoint_busy_returns_409(client, monkeypatch) -> None:
    import threading

    test_client, tmp_path, _ = client
    gate = threading.Event()
    FakePipeline.gate = gate
    monkeypatch.setattr(webapp, "Pipeline", FakePipeline)
    monkeypatch.setattr(webapp, "Notifier", FakeNotifier)
    manager = webapp.RunManager(tmp_path / "config.yaml", reports_dir=tmp_path / "reports")
    test_client.application.config["WATCHDOG_RUN_MANAGER"] = manager
    assert test_client.post("/api/run", json={"dry_run": True}).status_code == 202
    busy = test_client.post("/api/run", json={"dry_run": True})
    assert busy.status_code == 409
    gate.set()
    wait_for(manager)


def test_dry_run_database_imports_ledger(client) -> None:
    _test_client, _tmp_path, config_path = client
    from config import load_config

    config = load_config(config_path)
    ledger = config.database.parent / f"{config.database.stem}.ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "alerts": [
                    {
                        "fingerprint": "abc",
                        "rule": "jobs",
                        "trigger_key": "new",
                        "channel": "console",
                        "status": "sent",
                        "reason": "new_match",
                        "price": None,
                        "sent_at": "2026-10-30T12:00:00+00:00",
                    }
                ],
                "price_minima": [],
            }
        ),
        encoding="utf-8",
    )
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        temporary = tempfile.TemporaryDirectory(dir=directory)
        database = webapp._open_dry_run_database(config, temporary)
        try:
            assert len(database.alert_rows()) == 1
        finally:
            database.close()
            temporary.cleanup()
