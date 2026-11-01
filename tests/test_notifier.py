"""Notifier tests with fake HTTP responses (no real webhooks)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import requests

from config import NotificationSettings
from models import Alert, Listing
from notifier import Notifier

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
DISCORD_URL = "https://discord.com/api/webhooks/123456789/abcdefTOKEN"
TELEGRAM_TOKEN = "123456789:" + "a" * 35
DISCORD_ENV = {"DISCORD_WEBHOOK_URL": DISCORD_URL}
TELEGRAM_ENV = {"TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN, "TELEGRAM_CHAT_ID": "42"}


class FakeResponse:
    def __init__(self, status_code: int = 204, *, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected POST")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def settings(*, dry_run: bool = False, timeout: float = 5.0) -> NotificationSettings:
    return NotificationSettings(
        dry_run=dry_run,
        request_timeout_seconds=timeout,
        alert_burst_warning_threshold=50,
    )


def make_listing(**overrides) -> Listing:
    values = {
        "kind": "job",
        "source": "remoteok",
        "external_id": "42",
        "title": "Senior Python Engineer",
        "url": "https://example.com/jobs/42",
        "company": "Acme",
        "location": "Remote",
        "remote": True,
        "description": "Build reliable systems.",
        "tags": ("python",),
        "posted_at": datetime(2026, 10, 29, tzinfo=UTC),
        "salary_min": Decimal("120000"),
        "salary_max": Decimal("160000"),
        "salary_currency": "USD",
        "salary_period": "year",
    }
    values.update(overrides)
    return Listing(**values)


def make_alert(**overrides) -> Alert:
    values = {
        "listing": make_listing(),
        "rule": "remote-tech-jobs",
        "reason": "new_match",
        "trigger_key": "new",
    }
    values.update(overrides)
    return Alert(**values)


def test_console_fallback(caplog: pytest.LogCaptureFixture) -> None:
    session = FakeSession([])
    notifier = Notifier(settings(), environ={}, session=session)
    assert notifier.channels == ("console",)
    alerts = [make_alert()]
    with caplog.at_level(logging.INFO, logger="watchdog.notifier"):
        results = notifier.send(alerts)
    assert [r.status for r in results] == ["console"]
    assert "Senior Python Engineer" in caplog.text
    assert session.calls == []


def test_invalid_discord_url_falls_back_to_console() -> None:
    notifier = Notifier(
        settings(),
        environ={"DISCORD_WEBHOOK_URL": "https://evil.example.com/api/webhooks/1/x"},
        session=FakeSession([]),
    )
    assert notifier.channels == ("console",)


def test_dry_run_does_not_post() -> None:
    session = FakeSession([])
    notifier = Notifier(settings(dry_run=True), environ=DISCORD_ENV, session=session)
    results = notifier.send([make_alert()])
    assert [r.status for r in results] == ["dry_run"]
    assert session.calls == []


def test_discord_batches_embeds() -> None:
    session = FakeSession([FakeResponse(204), FakeResponse(204)])
    notifier = Notifier(settings(), environ=DISCORD_ENV, session=session)
    alerts = [make_alert(rule=f"rule-{index}") for index in range(12)]
    results = notifier.send(alerts, now=NOW)
    assert len(results) == 12
    assert all(result.status == "sent" for result in results)
    assert len(session.calls) == 2
    first_payload = session.calls[0]["json"]
    second_payload = session.calls[1]["json"]
    assert len(first_payload["embeds"]) == 10
    assert len(second_payload["embeds"]) == 2
    assert first_payload["allowed_mentions"] == {"parse": []}
    assert first_payload["embeds"][0]["url"] == "https://example.com/jobs/42"
    assert first_payload["embeds"][0]["timestamp"].startswith("2026-10-30T12:00:00")
    source_fields = [
        field for field in first_payload["embeds"][0]["fields"] if field["name"] == "Source"
    ]
    assert source_fields[0]["value"] == "Remote OK"


def test_discord_retries_429() -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            FakeResponse(429, json_data={"retry_after": 2.5, "message": "rate limited"}),
            FakeResponse(204),
        ]
    )
    notifier = Notifier(settings(), environ=DISCORD_ENV, session=session, sleep=sleeps.append)
    results = notifier.send([make_alert()], now=NOW)
    assert [r.status for r in results] == ["sent"]
    assert sleeps == [2.5]
    assert len(session.calls) == 2


def test_discord_failure_is_reported_without_secret() -> None:
    session = FakeSession([FakeResponse(500, json_data={"message": "server error"})])
    notifier = Notifier(settings(), environ=DISCORD_ENV, session=session)
    results = notifier.send([make_alert()])
    assert results[0].status == "failed"
    assert "HTTP 500" in (results[0].error or "")
    assert "abcdefTOKEN" not in (results[0].error or "")


def test_discord_timeout_is_reported() -> None:
    session = FakeSession([requests.Timeout("slow")])
    notifier = Notifier(settings(), environ=DISCORD_ENV, session=session)
    results = notifier.send([make_alert()])
    assert results[0].status == "failed"
    assert "Timeout" in (results[0].error or "")


def test_telegram_message_and_escaping() -> None:
    session = FakeSession([FakeResponse(200, json_data={"ok": True})])
    notifier = Notifier(settings(), environ=TELEGRAM_ENV, session=session)
    assert notifier.channels == ("telegram",)
    alert = make_alert(listing=make_listing(title="<script>alert(1)</script> Python"))
    results = notifier.send([alert])
    assert results[0].status == "sent"
    call = session.calls[0]
    assert TELEGRAM_TOKEN in call["url"]
    assert call["json"]["chat_id"] == "42"
    assert call["json"]["parse_mode"] == "HTML"
    assert "&lt;script&gt;" in call["json"]["text"]
    assert "<script>" not in call["json"]["text"]
    assert "Source: Remote OK" in call["json"]["text"]


def test_telegram_api_error() -> None:
    session = FakeSession(
        [FakeResponse(400, json_data={"ok": False, "description": "chat not found"})]
    )
    notifier = Notifier(settings(), environ=TELEGRAM_ENV, session=session)
    results = notifier.send([make_alert()])
    assert results[0].status == "failed"
    assert "chat not found" in (results[0].error or "")


def test_telegram_retries_429() -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            FakeResponse(429, json_data={"ok": False, "parameters": {"retry_after": 1}}),
            FakeResponse(200, json_data={"ok": True}),
        ]
    )
    notifier = Notifier(settings(), environ=TELEGRAM_ENV, session=session, sleep=sleeps.append)
    results = notifier.send([make_alert()])
    assert results[0].status == "sent"
    assert sleeps == [1.0]


def test_invalid_telegram_token_falls_back() -> None:
    notifier = Notifier(
        settings(),
        environ={"TELEGRAM_BOT_TOKEN": "not-a-token", "TELEGRAM_CHAT_ID": "42"},
        session=FakeSession([]),
    )
    assert notifier.channels == ("console",)


def test_channel_filter_only_posts_requested_channel() -> None:
    session = FakeSession([FakeResponse(204)])
    notifier = Notifier(settings(), environ={**DISCORD_ENV, **TELEGRAM_ENV}, session=session)
    assert set(notifier.channels) == {"discord", "telegram"}
    results = notifier.send([make_alert()], channels=("discord",))
    assert len(results) == 1
    assert results[0].channel == "discord"
    assert len(session.calls) == 1
    assert "discord.com" in session.calls[0]["url"]


def test_unknown_channel() -> None:
    notifier = Notifier(settings(), environ={}, session=FakeSession([]))
    results = notifier.send([make_alert()], channels=("email",))
    assert results[0].status == "failed"
    assert "unknown channel" in (results[0].error or "")


def test_no_alerts_no_calls() -> None:
    session = FakeSession([])
    notifier = Notifier(settings(), environ=DISCORD_ENV, session=session)
    assert notifier.send([]) == []
    assert session.calls == []


def test_price_embed_and_truncation() -> None:
    session = FakeSession([FakeResponse(204)])
    notifier = Notifier(settings(), environ=DISCORD_ENV, session=session)
    listing = make_listing(
        kind="price",
        source="books",
        title="A " * 400,
        price=Decimal("9.99"),
        currency="GBP",
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        salary_period=None,
        description="",
    )
    alert = make_alert(
        listing=listing,
        reason="below_target",
        trigger_key="below:20:9.99",
        target_price=Decimal("20.00"),
    )
    results = notifier.send([alert], now=NOW)
    assert results[0].status == "sent"
    embed = session.calls[0]["json"]["embeds"][0]
    assert len(embed["title"]) <= 256
    assert "GBP 9.99" in embed["description"]
    assert "Target" in embed["description"]
    assert embed["color"] == 0xE74C3C


def test_notifier_close() -> None:
    session = FakeSession([])
    notifier = Notifier(settings(), environ={}, session=session)
    notifier.close()
    assert session.closed is True
