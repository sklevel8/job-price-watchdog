"""Notification delivery for Discord, Telegram, and local console output.

The notifier never logs webhook URLs or bot tokens.  Network errors are
returned as failed delivery results so one bad channel cannot crash the ETL.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from config import NotificationSettings
from models import Alert, DeliveryResult, Listing, isoformat, utcnow
from normalize import truncate

logger = logging.getLogger("watchdog.notifier")

DISCORD_MAX_EMBEDS = 10
DISCORD_TITLE_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 1_000
DISCORD_FIELD_VALUE_LIMIT = 1_024
TELEGRAM_MESSAGE_LIMIT = 4_096
MAX_RETRY_AFTER_SECONDS = 15.0

_CHANNELS = ("discord", "telegram", "console")

# Human-readable attribution. Remote OK's API terms require mentioning it as
# the source and linking back to the listing URL, which every alert does.
_SOURCE_LABELS = {
    "remoteok": "Remote OK",
    "hn": "Hacker News",
    "books": "Books to Scrape",
}


def _source_label(source: str) -> str:
    return _SOURCE_LABELS.get(source, source)


@dataclass(frozen=True, slots=True)
class _PostResponse:
    status_code: int
    text: str
    json_data: Any = None


class Notifier:
    """Send alerts to every configured channel, with a console fallback."""

    def __init__(
        self,
        settings: NotificationSettings,
        *,
        environ: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        dry_run: bool | None = None,
    ) -> None:
        self.settings = settings
        self._environ = os.environ if environ is None else environ
        self._sleep = sleep
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.session = session or requests.Session()

        self.discord_url = self._environ.get(settings.discord_webhook_env, "").strip()
        self.telegram_token = self._environ.get(settings.telegram_bot_token_env, "").strip()
        self.telegram_chat_id = self._environ.get(settings.telegram_chat_id_env, "").strip()

        channels: list[str] = []
        if self.discord_url:
            if _valid_discord_webhook(self.discord_url):
                channels.append("discord")
            else:
                logger.warning(
                    "Ignoring %s: not a valid Discord webhook URL",
                    settings.discord_webhook_env,
                )
        if self.telegram_token and self.telegram_chat_id:
            if _valid_telegram_token(self.telegram_token):
                channels.append("telegram")
            else:
                logger.warning(
                    "Ignoring %s: invalid Telegram bot token format",
                    settings.telegram_bot_token_env,
                )
        elif self.telegram_token or self.telegram_chat_id:
            logger.warning(
                "Telegram needs both %s and %s; falling back",
                settings.telegram_bot_token_env,
                settings.telegram_chat_id_env,
            )
        if not channels:
            channels.append("console")
        self.channels = tuple(channels)

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()

    @property
    def available_channels(self) -> tuple[str, ...]:
        return self.channels

    def send(
        self,
        alerts: Sequence[Alert],
        *,
        channels: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> list[DeliveryResult]:
        """Deliver alerts to the requested channels (default: all configured)."""

        selected = tuple(channels) if channels is not None else self.channels
        results: list[DeliveryResult] = []
        if not alerts:
            return results
        for channel in selected:
            if channel not in _CHANNELS:
                for alert in alerts:
                    results.append(
                        DeliveryResult(alert, channel, "failed", f"unknown channel: {channel}")
                    )
                continue
            if channel == "discord" and "discord" not in self.channels:
                results.extend(
                    DeliveryResult(alert, channel, "failed", "Discord is not configured")
                    for alert in alerts
                )
                continue
            if channel == "telegram" and "telegram" not in self.channels:
                results.extend(
                    DeliveryResult(alert, channel, "failed", "Telegram is not configured")
                    for alert in alerts
                )
                continue
            if self.dry_run:
                for alert in alerts:
                    logger.info("[dry-run] %s", _console_line(alert))
                    results.append(DeliveryResult(alert, channel, "dry_run"))
                continue
            if channel == "console":
                for alert in alerts:
                    logger.info(_console_line(alert))
                    results.append(DeliveryResult(alert, "console", "console"))
                continue
            if channel == "discord":
                results.extend(self._send_discord(alerts, now=now))
            elif channel == "telegram":
                results.extend(self._send_telegram(alerts))
        return results

    # -- Discord -----------------------------------------------------------

    def _send_discord(
        self, alerts: Sequence[Alert], *, now: datetime | None = None
    ) -> list[DeliveryResult]:
        results: list[DeliveryResult] = []
        for start in range(0, len(alerts), DISCORD_MAX_EMBEDS):
            batch = list(alerts[start : start + DISCORD_MAX_EMBEDS])
            error = self._post_discord_batch(batch, now=now)
            status = "failed" if error else "sent"
            results.extend(DeliveryResult(alert, "discord", status, error) for alert in batch)
        return results

    def _post_discord_batch(
        self, alerts: Sequence[Alert], *, now: datetime | None = None
    ) -> str | None:
        payload = {
            "username": "Job / Price Watchdog",
            "content": f"🚨 **{len(alerts)} new watchdog alert(s)**",
            "embeds": [_discord_embed(alert, now=now) for alert in alerts],
            "allowed_mentions": {"parse": []},
        }
        response = self._post_with_retry(self.discord_url, payload)
        if isinstance(response, str):
            return response
        return None

    # -- Telegram ----------------------------------------------------------

    def _send_telegram(self, alerts: Sequence[Alert]) -> list[DeliveryResult]:
        results: list[DeliveryResult] = []
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        for alert in alerts:
            payload = {
                "chat_id": self.telegram_chat_id,
                "text": _telegram_text(alert),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            response = self._post_with_retry(url, payload)
            if isinstance(response, str):
                results.append(DeliveryResult(alert, "telegram", "failed", response))
                continue
            assert response is not None
            try:
                body = (
                    response.json_data
                    if response.json_data is not None
                    else json.loads(response.text)
                )
            except (TypeError, ValueError):
                body = {}
            if response.status_code == 200 and isinstance(body, dict) and body.get("ok") is True:
                results.append(DeliveryResult(alert, "telegram", "sent"))
            else:
                detail = body.get("description") if isinstance(body, dict) else None
                results.append(
                    DeliveryResult(
                        alert,
                        "telegram",
                        "failed",
                        f"Telegram HTTP {response.status_code}: {truncate(str(detail or ''), 200)}",
                    )
                )
        return results

    # -- shared HTTP -------------------------------------------------------

    def _post_with_retry(
        self,
        url: str,
        payload: Mapping[str, Any],
    ) -> _PostResponse | str | None:
        """Return a response, a failure string, or None. Never logs secrets."""

        for attempt in range(2):
            try:
                response = self.session.post(
                    url,
                    json=dict(payload),
                    timeout=self.settings.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                return f"request failed: {type(exc).__name__}"
            post = _PostResponse(
                status_code=int(response.status_code),
                text=response.text or "",
                json_data=self._safe_json(response),
            )
            if post.status_code in {200, 204}:
                return post
            if post.status_code == 429 and attempt == 0:
                retry_after = _retry_after(post)
                logger.warning("Rate limited; retrying in %.1fs", retry_after)
                self._sleep(retry_after)
                continue
            detail = (
                post.json_data.get("message") or post.json_data.get("description")
                if isinstance(post.json_data, dict)
                else truncate(post.text, 200)
            )
            return f"HTTP {post.status_code}: {truncate(str(detail or ''), 200)}"
        return "rate limited after retry"

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        try:
            return response.json()
        except (ValueError, TypeError):
            return None


def _valid_discord_webhook(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and (host == "discord.com" or host.endswith(".discord.com") or host == "discordapp.com")
        and parts.path.startswith("/api/webhooks/")
        and len(parts.path.strip("/").split("/")) >= 3
    )


def _valid_telegram_token(token: str) -> bool:
    return bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token))


def _retry_after(response: _PostResponse) -> float:
    data = response.json_data
    value: Any = None
    if isinstance(data, dict):
        value = data.get("retry_after")
        if value is None and isinstance(data.get("parameters"), dict):
            value = data["parameters"].get("retry_after")
    try:
        seconds = float(value) if value is not None else 1.0
    except (TypeError, ValueError):
        seconds = 1.0
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _discord_embed(alert: Alert, *, now: datetime | None = None) -> dict[str, Any]:
    listing = alert.listing
    prefix = {
        "new_match": "🆕 New match",
        "below_target": "🎯 Below target",
        "new_low": "📉 New low",
    }[alert.reason]
    description_parts: list[str] = []
    if listing.kind == "price":
        description_parts.append(f"**Current price:** {_price_display(listing, alert)}")
        if alert.target_price is not None:
            description_parts.append(
                f"**Target:** {listing.currency or ''} {alert.target_price:,.2f}"
            )
    else:
        if listing.company:
            description_parts.append(f"**Company:** {listing.company}")
        if listing.location:
            description_parts.append(f"**Location:** {listing.location}")
        salary = _salary_display(listing)
        if salary:
            description_parts.append(f"**Salary:** {salary}")
        if listing.description:
            description_parts.append(truncate(listing.description, 400))

    fields: list[dict[str, Any]] = []
    if listing.kind == "job" and listing.posted_at:
        fields.append(
            {"name": "Posted", "value": isoformat(listing.posted_at)[:10], "inline": True}
        )
    if listing.source:
        fields.append({"name": "Source", "value": _source_label(listing.source), "inline": True})
    fields.append({"name": "Rule", "value": alert.rule, "inline": True})

    return {
        "title": truncate(f"{prefix}: {listing.title}", DISCORD_TITLE_LIMIT),
        "url": listing.url,
        "description": truncate("\n".join(description_parts), DISCORD_DESCRIPTION_LIMIT) or None,
        "color": 0xE74C3C if listing.kind == "price" else 0x2ECC71,
        "fields": [
            {
                "name": truncate(field["name"], 256),
                "value": truncate(field["value"], DISCORD_FIELD_VALUE_LIMIT) or "-",
                "inline": field.get("inline", False),
            }
            for field in fields
        ],
        "footer": {"text": f"watchdog • {alert.rule}"},
        "timestamp": isoformat(now or utcnow()),
    }


def _price_display(listing: Listing, alert: Alert) -> str:
    value = listing.price
    if value is None:
        return "unknown"
    suffix = ""
    if alert.reason == "new_low":
        suffix = " (new historical low)"
    return f"{listing.currency or ''} {value:,.2f}{suffix}".strip()


def _salary_display(listing: Listing) -> str:
    if listing.salary_min is None and listing.salary_max is None:
        return ""
    currency = listing.salary_currency or ""
    period = f"/{listing.salary_period}" if listing.salary_period else ""
    if listing.salary_min is not None and listing.salary_max is not None:
        if listing.salary_min == listing.salary_max:
            amount = f"{listing.salary_min:,.0f}"
        else:
            amount = f"{listing.salary_min:,.0f} - {listing.salary_max:,.0f}"
    elif listing.salary_min is not None:
        amount = f"{listing.salary_min:,.0f}+"
    else:
        amount = f"up to {listing.salary_max:,.0f}"  # type: ignore[union-attr]
    return f"{currency} {amount}{period}".strip()


def _telegram_text(alert: Alert) -> str:
    listing = alert.listing
    heading = {
        "new_match": "New matching listing",
        "below_target": "Price below target",
        "new_low": "New historical price low",
    }[alert.reason]
    lines = [
        f"🚨 <b>{html.escape(heading)}</b>",
        "",
        f"<b>{html.escape(truncate(listing.title, 500))}</b>",
    ]
    if listing.company:
        lines.append(f"Company: {html.escape(truncate(listing.company, 200))}")
    lines.append(f"Source: {html.escape(_source_label(listing.source))}")
    if listing.kind == "price" and listing.price is not None:
        lines.append(f"Price: {html.escape(listing.currency or '')} {listing.price:,.2f}")
        if alert.target_price is not None:
            lines.append(f"Target: {html.escape(listing.currency or '')} {alert.target_price:,.2f}")
    else:
        salary = _salary_display(listing)
        if salary:
            lines.append(f"Salary: {html.escape(salary)}")
        if listing.location:
            lines.append(f"Location: {html.escape(truncate(listing.location, 200))}")
    lines.append(f"Rule: {html.escape(alert.rule)}")
    lines.append("")
    lines.append(f'<a href="{html.escape(listing.url, quote=True)}">Open listing</a>')
    message = "\n".join(lines)
    return (
        message
        if len(message) <= TELEGRAM_MESSAGE_LIMIT
        else message[: TELEGRAM_MESSAGE_LIMIT - 1] + "…"
    )


def _console_line(alert: Alert) -> str:
    listing = alert.listing
    price = f" [{listing.price_display}]" if listing.price is not None else ""
    return (
        f"ALERT [{listing.kind}/{alert.reason}] {listing.title} "
        f"({listing.company or _source_label(listing.source)}){price} -> {listing.url}"
    )


# Re-export for callers that only import notifier.
__all__ = ["Notifier"]
