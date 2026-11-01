"""End-to-end ETL tests with deterministic fake sources/notifiers."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, MatchSettings, NotificationSettings, RuleSettings, SourceSettings
from database import Database
from models import Alert, DeliveryResult, Listing
from pipeline import Pipeline, PipelineError
from scraper import FetchError

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


class FakeNotifier:
    def __init__(
        self,
        channels: tuple[str, ...] = ("discord",),
        *,
        status: str = "sent",
        fail_channels: tuple[str, ...] = (),
        fail_first: bool = False,
    ) -> None:
        self.available_channels = channels
        self.status = status
        self.fail_channels = set(fail_channels)
        self.fail_first = fail_first
        self.calls: list[dict[str, Any]] = []
        self.sent: list[Alert] = []

    def send(
        self,
        alerts: Sequence[Alert],
        *,
        channels: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> list[DeliveryResult]:
        channel = (channels or self.available_channels)[0]
        self.calls.append({"channel": channel, "alerts": list(alerts), "now": now})
        failed = (
            channel in self.fail_channels
            or self.status == "failed"
            or (self.fail_first and len(self.calls) == 1)
        )
        results: list[DeliveryResult] = []
        for alert in alerts:
            if failed:
                results.append(DeliveryResult(alert, channel, "failed", "simulated failure"))
            elif self.status == "dry_run":
                results.append(DeliveryResult(alert, channel, "dry_run"))
            elif self.status == "console":
                results.append(DeliveryResult(alert, channel, "console"))
            else:
                self.sent.append(alert)
                results.append(DeliveryResult(alert, channel, "sent"))
        return results


def job_listing(**overrides) -> Listing:
    values = {
        "kind": "job",
        "source": "remoteok",
        "external_id": "job-1",
        "title": "Senior Python Engineer",
        "url": "https://example.com/jobs/1",
        "company": "Acme",
        "location": "Remote",
        "remote": True,
        "description": "Build APIs with Python and Django.",
        "tags": ("python", "backend"),
        "posted_at": NOW - timedelta(hours=2),
        "salary_min": Decimal("120000"),
        "salary_max": Decimal("160000"),
        "salary_currency": "USD",
        "salary_period": "year",
    }
    values.update(overrides)
    return Listing(**values)


def price_listing(price: str = "19.99", **overrides) -> Listing:
    values = {
        "kind": "price",
        "source": "books",
        "external_id": "book-1",
        "title": "A Light in the Attic",
        "url": "https://books.toscrape.com/book-1",
        "company": "Books to Scrape",
        "price": Decimal(price),
        "currency": "GBP",
        "tags": ("books",),
    }
    values.update(overrides)
    return Listing(**values)


def make_rule(
    name: str = "jobs",
    *,
    kind: str = "job",
    match: MatchSettings | None = None,
    enabled: bool = True,
    bootstrap: str = "alert",
    url: str = "https://example.com/jobs",
) -> RuleSettings:
    source = SourceSettings(
        format="remoteok_json" if kind == "job" else "books_toscrape",
        url=url,
    )
    return RuleSettings(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        enabled=enabled,
        source=source,
        match=match or MatchSettings(),
        bootstrap=bootstrap,  # type: ignore[arg-type]
    )


def make_config(*rules: RuleSettings) -> AppConfig:
    return AppConfig(
        database=Path("unused.db"),
        rules=tuple(rules),
        notifications=NotificationSettings(),
    )


def make_fetcher(data: dict[str, list[Listing]] | Exception):
    calls: list[dict[str, Any]] = []

    def fetch(source: SourceSettings, client: object, *, default_currency=None):
        calls.append({"source": source, "default_currency": default_currency})
        if isinstance(data, Exception):
            raise data
        return list(data.get(source.url, []))

    fetch.calls = calls
    return fetch


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "watchdog.db")
    yield database
    database.close()


def run_pipeline(
    db: Database,
    rules: Sequence[RuleSettings],
    listings: dict[str, list[Listing]] | Exception,
    notifier: FakeNotifier | None = None,
    *,
    run_id: str = "run-1",
    selected: Sequence[str] | None = None,
) -> tuple[Any, Database, FakeNotifier, Any]:
    notifier = notifier or FakeNotifier()
    fetcher = make_fetcher(listings)
    pipeline = Pipeline(
        make_config(*rules),
        db,
        notifier,
        client=object(),
        fetcher=fetcher,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    report = pipeline.run(run_id=run_id, rules=selected)
    return report, db, notifier, fetcher


# ---------------------------------------------------------------------------
# Job matching and lifecycle
# ---------------------------------------------------------------------------


def test_new_job_is_stored_alerted_and_not_repeated(db: Database) -> None:
    listing = job_listing()
    report, db, notifier, _ = run_pipeline(
        db, [make_rule()], {"https://example.com/jobs": [listing]}
    )
    assert report.status == "ok"
    assert report.totals == {
        "fetched": 1,
        "matched": 1,
        "new_listings": 1,
        "alerts": 1,
        "deliveries": 1,
        "errors": 0,
    }
    assert len(notifier.calls) == 1
    alert = notifier.calls[0]["alerts"][0]
    assert alert.reason == "new_match"
    assert alert.trigger_key == "new"
    assert db.get_listing(listing.fingerprint) is not None
    assert db.was_alert_sent(listing.fingerprint, "jobs", "new", "discord")

    # The same listing on a later run is not new and must not alert again.
    report2, _, notifier2, _ = run_pipeline(
        db, [make_rule()], {"https://example.com/jobs": [listing]}, run_id="run-2"
    )
    assert report2.totals["new_listings"] == 0
    assert report2.totals["alerts"] == 0
    assert notifier2.calls == []


def test_two_job_rules_alert_for_same_listing(db: Database) -> None:
    listing = job_listing()
    rule_a = make_rule("jobs-a", match=MatchSettings(keywords=("python",)))
    rule_b = make_rule("jobs-b", match=MatchSettings(keywords=("senior",)))
    report, _, notifier, _ = run_pipeline(
        db, [rule_a, rule_b], {"https://example.com/jobs": [listing]}
    )
    assert [stats.alerts for stats in report.rules] == [1, 1]
    assert report.totals["alerts"] == 2
    assert len(notifier.sent) == 2
    assert {alert.rule for alert in notifier.sent} == {"jobs-a", "jobs-b"}


def test_keyword_and_exclusion_filters(db: Database) -> None:
    listings = [
        job_listing(external_id="1", title="Python Backend Engineer"),
        job_listing(external_id="2", title="Sales Engineer", tags=("sales",)),
        job_listing(external_id="3", title="Rust Engineer", tags=("rust",)),
    ]
    rule = make_rule(match=MatchSettings(keywords=("python",), exclude_keywords=("sales",)))
    report, db, _, _ = run_pipeline(db, [rule], {"https://example.com/jobs": listings})
    assert report.rules[0].fetched == 3
    assert report.rules[0].matched == 1
    assert report.totals["new_listings"] == 1


def test_keyword_mode_all(db: Database) -> None:
    listings = [
        job_listing(external_id="1", title="Python Platform Engineer"),
        job_listing(external_id="2", title="Python Data Engineer"),
    ]
    rule = make_rule(match=MatchSettings(keywords=("python", "platform"), keyword_mode="all"))
    report, _, _, _ = run_pipeline(db, [rule], {"https://example.com/jobs": listings})
    assert report.rules[0].matched == 1


def test_description_matching_is_opt_in(db: Database) -> None:
    listing = job_listing(title="Backend Engineer", tags=("backend",))
    base = make_rule(match=MatchSettings(keywords=("django",)))
    report, _, _, _ = run_pipeline(db, [base], {"https://example.com/jobs": [listing]})
    assert report.rules[0].matched == 0

    with_desc = make_rule(
        name="jobs-desc",
        match=MatchSettings(keywords=("django",), include_description=True),
    )
    report2, _, _, _ = run_pipeline(
        db, [with_desc], {"https://example.com/jobs": [listing]}, run_id="run-2"
    )
    assert report2.rules[0].matched == 1


def test_remote_and_age_filters(db: Database) -> None:
    old = job_listing(
        external_id="old",
        title="Python Old",
        posted_at=NOW - timedelta(days=40),
    )
    onsite = job_listing(external_id="onsite", title="Python Onsite", remote=False)
    unknown = job_listing(
        external_id="unknown", title="Python Unknown", remote=None, posted_at=None
    )
    rule = make_rule(match=MatchSettings(keywords=("python",), remote_only=True, max_age_days=30))
    report, _, _, _ = run_pipeline(db, [rule], {"https://example.com/jobs": [old, onsite, unknown]})
    assert report.rules[0].matched == 1


def test_salary_filter(db: Database) -> None:
    low = job_listing(external_id="low", title="Python Low", salary_min=Decimal("70000"))
    high = job_listing(external_id="high", title="Python High", salary_min=Decimal("120000"))
    rule = make_rule(match=MatchSettings(keywords=("python",), min_salary=Decimal("100000")))
    report, _, _, _ = run_pipeline(db, [rule], {"https://example.com/jobs": [low, high]})
    assert report.rules[0].matched == 1


def test_require_salary_excludes_unknown(db: Database) -> None:
    listing = job_listing(
        external_id="no-salary",
        title="Python No Salary",
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        salary_period=None,
    )
    rule = make_rule(
        match=MatchSettings(keywords=("python",), min_salary=Decimal("100000"), require_salary=True)
    )
    report, _, _, _ = run_pipeline(db, [rule], {"https://example.com/jobs": [listing]})
    assert report.rules[0].matched == 0


# ---------------------------------------------------------------------------
# Price logic
# ---------------------------------------------------------------------------


def test_price_drop_alerts_only_on_new_lows(db: Database) -> None:
    rule = make_rule(
        name="books",
        kind="price",
        url="https://example.com/prices",
        match=MatchSettings(max_price=Decimal("20.00"), currency="GBP"),
    )
    notifier = FakeNotifier()
    prices = ["19.99", "18.00", "19.50", "17.00"]
    for index, price in enumerate(prices):
        report, _, notifier, _ = run_pipeline(
            db,
            [rule],
            {"https://example.com/prices": [price_listing(price)]},
            notifier=notifier,
            run_id=f"price-{index}",
        )
        expected_alerts = 1 if price in {"19.99", "18.00", "17.00"} else 0
        assert report.rules[0].alerts == expected_alerts

    assert len(notifier.calls) == 3
    reasons = [call["alerts"][0].reason for call in notifier.calls]
    assert reasons == ["below_target", "new_low", "new_low"]
    assert notifier.calls[0]["alerts"][0].trigger_key == "below:20:19.99"
    assert notifier.calls[1]["alerts"][0].trigger_key == "below:20:18"
    stats = db.get_price_stats(price_listing().fingerprint)
    assert stats.latest == Decimal("17.00")
    assert stats.minimum == Decimal("17.00")
    assert stats.observations == 4


def test_price_above_target_is_tracked_without_alert(db: Database) -> None:
    rule = make_rule(
        name="books",
        kind="price",
        url="https://example.com/prices",
        match=MatchSettings(max_price=Decimal("20.00"), currency="GBP"),
    )
    report, _, notifier, _ = run_pipeline(
        db, [rule], {"https://example.com/prices": [price_listing("25.00")]}
    )
    assert report.rules[0].matched == 1
    assert report.rules[0].alerts == 0
    assert db.get_price_stats(price_listing().fingerprint).latest == Decimal("25.00")
    assert notifier.calls == []


def test_two_price_rules_share_pre_run_baseline(db: Database) -> None:
    url = "https://example.com/prices"
    rule_a = make_rule(
        "prices-wide",
        kind="price",
        url=url,
        match=MatchSettings(max_price=Decimal("20.00"), currency="GBP"),
    )
    rule_b = make_rule(
        "prices-narrow",
        kind="price",
        url=url,
        match=MatchSettings(max_price=Decimal("15.00"), currency="GBP"),
    )
    notifier = FakeNotifier()

    first, _, notifier, _ = run_pipeline(
        db,
        [rule_a, rule_b],
        {url: [price_listing("18.00")]},
        notifier=notifier,
    )
    assert [stats.alerts for stats in first.rules] == [1, 0]
    assert len(notifier.sent) == 1

    second, _, _, _ = run_pipeline(
        db,
        [rule_a, rule_b],
        {url: [price_listing("14.00")]},
        notifier=notifier,
        run_id="run-2",
    )
    assert [stats.alerts for stats in second.rules] == [1, 1]
    assert {alert.rule for alert in notifier.sent[1:]} == {"prices-wide", "prices-narrow"}
    assert all(alert.reason == "new_low" for alert in notifier.sent[1:])


def test_price_currency_mismatch_is_ignored(db: Database) -> None:
    rule = make_rule(
        name="books",
        kind="price",
        url="https://example.com/prices",
        match=MatchSettings(max_price=Decimal("20.00"), currency="GBP"),
    )
    report, _, _, _ = run_pipeline(
        db,
        [rule],
        {"https://example.com/prices": [price_listing("19.99", currency="USD")]},
    )
    assert report.rules[0].matched == 0
    assert db.get_price_stats(price_listing().fingerprint).observations == 0


# ---------------------------------------------------------------------------
# Bootstrap, failures, retries
# ---------------------------------------------------------------------------


def test_bootstrap_suppress_jobs(db: Database) -> None:
    rule = make_rule(bootstrap="suppress")
    first = job_listing(external_id="first", title="Python First")
    report, _, notifier, _ = run_pipeline(db, [rule], {"https://example.com/jobs": [first]})
    assert report.rules[0].new_listings == 1
    assert report.rules[0].alerts == 0
    assert notifier.calls == []

    second = job_listing(external_id="second", title="Python Second")
    report2, _, notifier2, _ = run_pipeline(
        db, [rule], {"https://example.com/jobs": [first, second]}, run_id="run-2"
    )
    assert report2.rules[0].new_listings == 1
    assert report2.rules[0].alerts == 1
    assert len(notifier2.calls) == 1


def test_bootstrap_suppress_survives_failed_first_run(db: Database) -> None:
    rule = make_rule(bootstrap="suppress")
    report, _, notifier, _ = run_pipeline(db, [rule], FetchError("temporary outage"))
    assert report.rules[0].status == "failed"
    assert notifier.calls == []

    first = job_listing(external_id="first", title="Python First")
    report2, _, notifier2, _ = run_pipeline(
        db,
        [rule],
        {"https://example.com/jobs": [first]},
        run_id="run-2",
    )
    assert report2.rules[0].new_listings == 1
    assert report2.rules[0].alerts == 0
    assert notifier2.calls == []

    second = job_listing(external_id="second", title="Python Second")
    report3, _, notifier3, _ = run_pipeline(
        db,
        [rule],
        {"https://example.com/jobs": [first, second]},
        run_id="run-3",
    )
    assert report3.rules[0].alerts == 1
    assert len(notifier3.calls) == 1
    assert notifier3.calls[0]["alerts"][0].listing.fingerprint == second.fingerprint


def test_bootstrap_suppress_price_then_new_low(db: Database) -> None:
    rule = make_rule(
        name="books",
        kind="price",
        bootstrap="suppress",
        url="https://example.com/prices",
        match=MatchSettings(max_price=Decimal("20.00"), currency="GBP"),
    )
    report, _, notifier, _ = run_pipeline(
        db, [rule], {"https://example.com/prices": [price_listing("19.99")]}
    )
    assert report.rules[0].alerts == 0
    assert notifier.calls == []

    report2, _, notifier2, _ = run_pipeline(
        db,
        [rule],
        {"https://example.com/prices": [price_listing("18.00")]},
        run_id="run-2",
    )
    assert report2.rules[0].alerts == 1
    assert notifier2.calls[0]["alerts"][0].reason == "new_low"


def test_rule_failure_is_isolated(db: Database) -> None:
    ok_rule = make_rule("ok", url="https://example.com/jobs")
    bad_rule = make_rule("bad", url="https://example.com/bad")
    fetcher = make_fetcher({"https://example.com/jobs": [job_listing()]})

    def failing(source, client, *, default_currency=None):
        if source.url.endswith("/bad"):
            raise FetchError("upstream down")
        return fetcher(source, client, default_currency=default_currency)

    pipeline = Pipeline(
        make_config(ok_rule, bad_rule),
        db,
        FakeNotifier(),
        client=object(),
        fetcher=failing,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    report = pipeline.run(run_id="run-1")
    assert report.status == "partial"
    assert [stats.status for stats in report.rules] == ["ok", "failed"]
    assert report.totals["errors"] == 1
    assert "upstream down" in report.errors[0]


def test_all_rules_failing_marks_run_failed(db: Database) -> None:
    rule = make_rule()
    report, _, _, _ = run_pipeline(db, [rule], FetchError("everything is down"), run_id="run-1")
    assert report.status == "failed"
    assert report.rules[0].status == "failed"


def test_failed_delivery_is_retried_next_run(db: Database) -> None:
    listing = job_listing()
    failing = FakeNotifier(status="failed")
    report, _, failing, _ = run_pipeline(
        db, [make_rule()], {"https://example.com/jobs": [listing]}, notifier=failing
    )
    assert report.status == "partial"
    assert report.rules[0].errors == 1
    assert db.was_alert_sent(listing.fingerprint, "jobs", "new", "discord") is False

    recovered = FakeNotifier()
    report2, _, recovered, _ = run_pipeline(
        db,
        [make_rule()],
        {"https://example.com/jobs": [listing]},
        notifier=recovered,
        run_id="run-2",
    )
    assert report2.status == "ok"
    assert report2.rules[0].deliveries == 1
    assert len(recovered.calls) == 1
    assert db.was_alert_sent(listing.fingerprint, "jobs", "new", "discord") is True


def test_only_failed_channel_is_retried(db: Database) -> None:
    listing = job_listing()
    first = FakeNotifier(channels=("discord", "telegram"), fail_channels=("telegram",))
    report, _, first, _ = run_pipeline(
        db, [make_rule()], {"https://example.com/jobs": [listing]}, notifier=first
    )
    assert report.status == "partial"
    assert db.was_alert_sent(listing.fingerprint, "jobs", "new", "discord") is True
    assert db.was_alert_sent(listing.fingerprint, "jobs", "new", "telegram") is False

    second = FakeNotifier(channels=("discord", "telegram"))
    report2, _, second, _ = run_pipeline(
        db,
        [make_rule()],
        {"https://example.com/jobs": [listing]},
        notifier=second,
        run_id="run-2",
    )
    assert report2.status == "ok"
    assert len(second.calls) == 1
    assert second.calls[0]["channel"] == "telegram"
    assert db.was_alert_sent(listing.fingerprint, "jobs", "new", "telegram") is True


def test_console_alert_is_later_delivered_to_new_channel(db: Database) -> None:
    listing = job_listing()
    console = FakeNotifier(channels=("console",), status="console")
    report, _, console, _ = run_pipeline(
        db, [make_rule()], {"https://example.com/jobs": [listing]}, notifier=console
    )
    assert report.status == "ok"
    assert report.rules[0].deliveries == 1
    assert db.was_alert_sent(listing.fingerprint, "jobs", "new", "console") is True

    discord = FakeNotifier(channels=("discord",))
    report2, _, discord, _ = run_pipeline(
        db,
        [make_rule()],
        {"https://example.com/jobs": [listing]},
        notifier=discord,
        run_id="run-2",
    )
    assert report2.status == "ok"
    assert report2.rules[0].alerts == 1
    assert report2.rules[0].deliveries == 1
    assert len(discord.calls) == 1
    assert discord.calls[0]["channel"] == "discord"
    assert db.was_alert_sent(listing.fingerprint, "jobs", "new", "discord") is True


def test_ledger_prevents_duplicate_after_cache_loss(db: Database, tmp_path: Path) -> None:
    listing = job_listing()
    report, _, notifier, _ = run_pipeline(
        db, [make_rule()], {"https://example.com/jobs": [listing]}
    )
    assert report.rules[0].alerts == 1
    ledger = db.export_ledger(tmp_path / "shared-ledger.json")

    # Simulate a fresh runner whose SQLite cache was evicted but ledger was committed.
    with Database(tmp_path / "fresh.db", ledger_path=ledger) as fresh:
        report2, _, notifier2, _ = run_pipeline(
            fresh, [make_rule()], {"https://example.com/jobs": [listing]}, run_id="run-2"
        )
    assert report2.rules[0].new_listings == 1
    assert report2.rules[0].alerts == 0
    assert notifier2.calls == []
    assert len(notifier.calls) == 1


def test_unknown_rule_selection(db: Database) -> None:
    pipeline = Pipeline(
        make_config(make_rule()),
        db,
        FakeNotifier(),
        client=object(),
        fetcher=make_fetcher({}),
        clock=lambda: NOW,
    )
    with pytest.raises(PipelineError, match="unknown or disabled"):
        pipeline.run(rules=["missing"], run_id="run-1")


def test_alert_burst_warns_but_does_not_drop(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    listings = [
        job_listing(external_id=f"job-{index}", title=f"Python Job {index}") for index in range(3)
    ]
    config = make_config(make_rule())
    object.__setattr__(
        config,
        "notifications",
        NotificationSettings(alert_burst_warning_threshold=2),
    )
    notifier = FakeNotifier()
    pipeline = Pipeline(
        config,
        db,
        notifier,
        client=object(),
        fetcher=make_fetcher({"https://example.com/jobs": listings}),
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    with caplog.at_level(logging.WARNING, logger="watchdog.pipeline"):
        report = pipeline.run(run_id="run-1")
    assert report.rules[0].alerts == 3
    assert len(notifier.sent) == 3
    assert "Large alert burst" in caplog.text


def test_report_markdown_contains_rule_rows(db: Database) -> None:
    report, _, _, _ = run_pipeline(db, [make_rule()], {"https://example.com/jobs": [job_listing()]})
    markdown = report.to_markdown()
    assert "## Watchdog run" in markdown
    assert "| `jobs` | ok |" in markdown
    assert "**Status:** `ok`" in markdown
