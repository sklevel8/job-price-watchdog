"""SQLite repository tests, including idempotency and ledger round-trips."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from database import Database, DatabaseError
from models import Alert, DeliveryResult, Listing

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def make_listing(**overrides) -> Listing:
    values = {
        "kind": "job",
        "source": "test",
        "external_id": "job-1",
        "title": "Senior Python Engineer",
        "url": "https://example.com/jobs/1",
        "company": "Acme",
        "location": "Remote",
        "remote": True,
        "description": "Build things",
        "tags": ("python", "backend"),
        "posted_at": datetime(2026, 10, 29, 8, 0, tzinfo=UTC),
        "salary_min": Decimal("120000"),
        "salary_max": Decimal("160000"),
        "salary_currency": "USD",
        "salary_period": "year",
        "raw": {"id": "job-1"},
    }
    values.update(overrides)
    return Listing(**values)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "watchdog.db")
    yield database
    database.close()


def test_schema_and_memory_mode(tmp_path: Path) -> None:
    path = tmp_path / "schema.db"
    with Database(path) as database:
        version = database.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1
        tables = {
            row[0]
            for row in database.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"runs", "rule_state", "listings", "price_history", "alerts"} <= tables
    with Database(":memory:") as memory:
        assert memory.count_listings() == 0


def test_run_lifecycle(db: Database) -> None:
    db.start_run("run-1", NOW)
    db.finish_run("run-1", "ok", {"fetched": 3, "alerts": 1}, NOW + timedelta(seconds=2))
    summary = db.summary()
    assert summary["last_run"]["run_id"] == "run-1"
    assert summary["last_run"]["status"] == "ok"


def test_rule_state(db: Database) -> None:
    assert db.is_first_run("jobs") is True

    # A failed first attempt must not consume bootstrap suppression.
    db.record_rule_run("jobs", "failed", NOW)
    assert db.is_first_run("jobs") is True

    db.record_rule_run("jobs", "ok", NOW + timedelta(minutes=1))
    assert db.is_first_run("jobs") is False

    # Later failures keep the previously recorded successful bootstrap.
    db.record_rule_run("jobs", "failed", NOW + timedelta(hours=1))
    assert db.is_first_run("jobs") is False

    row = db.conn.execute(
        "SELECT run_count, last_status, first_success_at FROM rule_state WHERE rule = 'jobs'"
    ).fetchone()
    assert row["run_count"] == 3
    assert row["last_status"] == "failed"
    assert row["first_success_at"] is not None


def test_upsert_listing_roundtrip(db: Database) -> None:
    listing = make_listing()
    assert db.upsert_listing(listing, NOW) is True
    assert db.upsert_listing(listing, NOW + timedelta(hours=1)) is False
    assert db.count_listings() == 1

    loaded = db.get_listing(listing.fingerprint)
    assert loaded is not None
    assert loaded.title == listing.title
    assert loaded.company == "Acme"
    assert loaded.remote is True
    assert loaded.tags == ("python", "backend")
    assert loaded.price is None
    assert loaded.salary_min == Decimal("120000")
    assert loaded.salary_currency == "USD"
    assert loaded.raw == {"id": "job-1"}

    row = db.conn.execute(
        "SELECT first_seen_at, last_seen_at FROM listings WHERE fingerprint = ?",
        (listing.fingerprint,),
    ).fetchone()
    assert row["first_seen_at"] != row["last_seen_at"]


def test_upsert_updates_mutable_fields(db: Database) -> None:
    listing = make_listing()
    db.upsert_listing(listing, NOW)
    updated = make_listing(title="Staff Python Engineer", salary_max=Decimal("180000"))
    db.upsert_listing(updated, NOW + timedelta(hours=1))
    loaded = db.get_listing(listing.fingerprint)
    assert loaded.title == "Staff Python Engineer"
    assert loaded.salary_max == Decimal("180000")


def test_price_history_and_stats(db: Database) -> None:
    listing = make_listing(
        kind="price",
        source="shop",
        external_id="sku-1",
        title="Keyboard",
        url="https://shop.example/keyboard",
        price=Decimal("99.95"),
        currency="GBP",
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        salary_period=None,
    )
    db.upsert_listing(listing, NOW)
    assert db.record_price(listing.fingerprint, Decimal("99.95"), "GBP", NOW) is True
    assert (
        db.record_price(listing.fingerprint, Decimal("99.95"), "GBP", NOW + timedelta(hours=1))
        is False
    )
    assert (
        db.record_price(listing.fingerprint, Decimal("89.95"), "GBP", NOW + timedelta(hours=2))
        is True
    )
    assert (
        db.record_price(listing.fingerprint, Decimal("79.95"), "GBP", NOW + timedelta(hours=3))
        is True
    )

    stats = db.get_price_stats(listing.fingerprint)
    assert stats.latest == Decimal("79.95")
    assert stats.previous == Decimal("89.95")
    assert stats.minimum == Decimal("79.95")
    assert stats.observations == 3
    assert db.price_observation_count() == 3
    assert db.latest_price(listing.fingerprint) == Decimal("79.95")


def test_price_history_requires_exact_decimal(db: Database) -> None:
    db.record_price("fp", Decimal("0.10"), "USD", NOW)
    db.record_price("fp", Decimal("0.20"), "USD", NOW + timedelta(seconds=1))
    stats = db.get_price_stats("fp")
    assert stats.minimum == Decimal("0.10")
    assert db.latest_price("fp") == Decimal("0.20")


def make_alert(listing: Listing | None = None, **overrides) -> Alert:
    values = {
        "listing": listing or make_listing(),
        "rule": "test-rule",
        "reason": "new_match",
        "trigger_key": "new",
        "target_price": None,
    }
    values.update(overrides)
    return Alert(**values)


def test_alert_ledger_per_channel(db: Database) -> None:
    alert = make_alert()
    assert db.was_alert_sent(alert.fingerprint, alert.rule, alert.trigger_key, "discord") is False
    db.record_delivery(
        DeliveryResult(alert, "discord", "sent"),
        NOW,
    )
    db.record_delivery(
        DeliveryResult(alert, "telegram", "failed", "rate limited"),
        NOW,
    )
    assert db.was_alert_sent(alert.fingerprint, alert.rule, alert.trigger_key, "discord") is True
    assert db.was_alert_sent(alert.fingerprint, alert.rule, alert.trigger_key, "telegram") is False

    # A later successful retry replaces the failed row.
    db.record_delivery(DeliveryResult(alert, "telegram", "sent"), NOW + timedelta(hours=1))
    assert db.was_alert_sent(alert.fingerprint, alert.rule, alert.trigger_key, "telegram") is True
    rows = db.alert_rows()
    assert len(rows) == 2
    assert {row["channel"] for row in rows} == {"discord", "telegram"}


def test_pending_alerts_are_rebuilt_for_retry(db: Database) -> None:
    listing = make_listing()
    db.upsert_listing(listing, NOW)
    alert = make_alert(
        listing, reason="below_target", trigger_key="below:20:9.99", target_price=Decimal("20")
    )
    db.record_delivery(DeliveryResult(alert, "discord", "failed", "timeout"), NOW)
    pending = db.pending_alerts()
    assert len(pending) == 1
    assert pending[0].fingerprint == listing.fingerprint
    assert pending[0].target_price == Decimal("20")
    assert pending[0].listing.title == listing.title

    db.record_delivery(DeliveryResult(alert, "discord", "sent"), NOW + timedelta(minutes=1))
    assert db.pending_alerts() == []


def test_pending_alerts_skip_missing_listings(db: Database) -> None:
    alert = make_alert()
    db.record_delivery(DeliveryResult(alert, "discord", "failed", "timeout"), NOW)
    assert db.pending_alerts() == []


def test_suppressed_bootstrap_alert_is_terminal(db: Database) -> None:
    listing = make_listing()
    db.upsert_listing(listing, NOW)
    alert = make_alert(listing)
    db.record_suppressed(alert, NOW)

    assert db.was_alert_suppressed(alert.fingerprint, alert.rule, alert.trigger_key) is True
    assert db.was_alert_sent(alert.fingerprint, alert.rule, alert.trigger_key, "bootstrap") is False
    assert db.pending_alerts() == []

    db.record_suppressed(alert, NOW)
    assert len(db.alert_rows()) == 1


def test_console_delivery_counts_as_delivered(db: Database) -> None:
    alert = make_alert()
    db.record_delivery(DeliveryResult(alert, "console", "console"), NOW)
    assert db.was_alert_sent(alert.fingerprint, alert.rule, alert.trigger_key, "console") is True
    assert db.summary()["alerts_sent"] == 1


def test_price_alert_ledger_includes_target(db: Database) -> None:
    alert = make_alert(
        reason="below_target", trigger_key="below:20:19.99", target_price=Decimal("20")
    )
    db.record_delivery(DeliveryResult(alert, "discord", "sent"), NOW)
    row = db.alert_rows()[0]
    assert row["price"] == "20"
    assert row["reason"] == "below_target"


def test_export_and_import_ledger(db: Database, tmp_path: Path) -> None:
    alert = make_alert()
    console_alert = make_alert(rule="console-rule")
    suppressed_alert = make_alert(rule="suppress-rule")
    db.record_delivery(DeliveryResult(alert, "discord", "sent"), NOW)
    db.record_delivery(DeliveryResult(console_alert, "console", "console"), NOW)
    db.record_suppressed(suppressed_alert, NOW)
    db.record_price("fp-price", Decimal("10.00"), "GBP", NOW)
    db.record_price("fp-price", Decimal("8.00"), "GBP", NOW + timedelta(hours=1))
    ledger = db.export_ledger(tmp_path / "ledger.json")

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["alerts"]) == 3
    assert payload["price_minima"][0]["price"] == "8.00"

    with Database(tmp_path / "fresh.db", ledger_path=ledger) as fresh:
        assert fresh.import_ledger(ledger) == 3
        assert fresh.was_alert_sent(alert.fingerprint, alert.rule, alert.trigger_key, "discord")
        assert fresh.was_alert_sent(
            console_alert.fingerprint, console_alert.rule, console_alert.trigger_key, "console"
        )
        assert fresh.was_alert_suppressed(
            suppressed_alert.fingerprint, suppressed_alert.rule, suppressed_alert.trigger_key
        )
        assert fresh.get_price_stats("fp-price").minimum == Decimal("8.00")
        # Importing twice is safe.
        assert fresh.import_ledger(ledger) == 3
        assert fresh.get_price_stats("fp-price").observations == 1


def test_import_missing_ledger_is_zero(tmp_path: Path) -> None:
    with Database(tmp_path / "db.sqlite") as database:
        assert database.import_ledger(tmp_path / "missing.json") == 0


def test_import_rejects_unknown_version(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"version": 99}), encoding="utf-8")
    with (
        Database(tmp_path / "db.sqlite") as database,
        pytest.raises(DatabaseError, match="unsupported ledger version"),
    ):
        database.import_ledger(ledger)


def test_import_rejects_malformed_json(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text("{not json", encoding="utf-8")
    with (
        Database(tmp_path / "db.sqlite") as database,
        pytest.raises(DatabaseError, match="could not read ledger"),
    ):
        database.import_ledger(ledger)


def test_prune_old_runs_and_alerts(db: Database) -> None:
    alert = make_alert()
    db.start_run("old", NOW - timedelta(days=400))
    db.finish_run("old", "ok", {}, NOW - timedelta(days=400))
    db.record_delivery(
        DeliveryResult(alert, "discord", "sent"),
        NOW - timedelta(days=400),
    )
    db.start_run("recent", NOW)
    db.finish_run("recent", "ok", {}, NOW)
    db.prune(now=NOW, runs_days=180, alerts_days=365)
    assert db.alert_rows() == []
    assert db.summary()["last_run"]["run_id"] == "recent"


def test_transaction_rolls_back(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status) VALUES ('rollback', ?, 'running')",
            (NOW.isoformat(),),
        )
        raise RuntimeError("boom")
    assert db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
