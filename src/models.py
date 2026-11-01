"""Typed domain models shared by the ETL pipeline.

The models are deliberately storage-agnostic: parsers produce :class:`Listing`
objects, the database persists them, and the notifier consumes :class:`Alert`
objects.  Keeping these boundaries explicit makes each layer independently
testable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal

Kind = Literal["job", "price"]
AlertReason = Literal["new_match", "below_target", "new_low"]
DeliveryStatus = Literal["sent", "failed", "dry_run", "console"]
RuleStatus = Literal["ok", "partial", "failed", "skipped"]


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp (patchable in tests)."""

    return datetime.now(UTC)


def isoformat(value: datetime | None) -> str | None:
    """Serialize a datetime as a stable UTC ISO-8601 string."""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def json_default(value: object) -> object:
    """JSON encoder fallback for datetimes and Decimals."""

    if isinstance(value, datetime):
        return isoformat(value)
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass(frozen=True, slots=True)
class ParsedPrice:
    """A price extracted from messy source text.

    ``value`` is the lowest price in a range so threshold checks are
    conservative; ``low``/``high`` preserve the original range.
    """

    value: Decimal | None
    raw: str
    currency: str | None = None
    low: Decimal | None = None
    high: Decimal | None = None

    @property
    def is_range(self) -> bool:
        return self.low is not None and self.high is not None and self.low != self.high


@dataclass(frozen=True, slots=True)
class Salary:
    """A normalized salary range, with the period kept as published."""

    minimum: Decimal | None = None
    maximum: Decimal | None = None
    currency: str | None = None
    period: str | None = None
    raw: str = ""

    @property
    def has_value(self) -> bool:
        return self.minimum is not None or self.maximum is not None

    def annualized_minimum(self) -> Decimal | None:
        """Return the minimum salary annualized for easy comparison.

        Standard full-time assumptions are used: 40 hours/week (2,080/year)
        and 260 working days/year.
        """

        return _annualize(self.minimum, self.period)

    def annualized_maximum(self) -> Decimal | None:
        return _annualize(self.maximum, self.period)


def _annualize(value: Decimal | None, period: str | None) -> Decimal | None:
    if value is None:
        return None
    factors = {
        "year": Decimal("1"),
        "month": Decimal("12"),
        "week": Decimal("52"),
        "day": Decimal("260"),
        "hour": Decimal("2080"),
    }
    factor = factors.get((period or "year").lower())
    if factor is None:
        return None
    return (value * factor).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class Listing:
    """A normalized job or product record emitted by a scraper."""

    kind: Kind
    source: str
    external_id: str
    title: str
    url: str
    company: str | None = None
    location: str | None = None
    remote: bool | None = None
    description: str = ""
    tags: tuple[str, ...] = ()
    posted_at: datetime | None = None
    price: Decimal | None = None
    currency: str | None = None
    salary_min: Decimal | None = None
    salary_max: Decimal | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def fingerprint(self) -> str:
        """Stable identity used for deduplication and the alert ledger.

        Source IDs are preferred (e.g. a Remote OK job id) because URLs often
        change by adding tracking parameters.  Falling back to the canonical
        URL plus title keeps sources without IDs stable enough.
        """

        identity = self.external_id.strip()
        if not identity:
            identity = f"{self.url}|{self.title}|{self.company or ''}"
        digest = sha256(f"{self.kind}|{self.source}|{identity}".encode())
        return digest.hexdigest()

    @property
    def price_display(self) -> str:
        if self.price is None:
            return ""
        return f"{self.currency or ''} {self.price:,.2f}".strip()


@dataclass(frozen=True, slots=True)
class Alert:
    """A notification candidate produced by a matching rule."""

    listing: Listing
    rule: str
    reason: AlertReason
    trigger_key: str
    target_price: Decimal | None = None

    @property
    def fingerprint(self) -> str:
        return self.listing.fingerprint


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Outcome of attempting to deliver one alert to one channel."""

    alert: Alert
    channel: str
    status: DeliveryStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PriceStats:
    """Price history summary for one listing."""

    latest: Decimal | None
    previous: Decimal | None
    minimum: Decimal | None
    observations: int


@dataclass
class RuleStats:
    """Per-rule counters included in the machine-readable run report."""

    rule: str
    status: RuleStatus = "ok"
    fetched: int = 0
    matched: int = 0
    new_listings: int = 0
    alerts: int = 0
    deliveries: int = 0
    errors: int = 0
    duration_ms: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "status": self.status,
            "fetched": self.fetched,
            "matched": self.matched,
            "new_listings": self.new_listings,
            "alerts": self.alerts,
            "deliveries": self.deliveries,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


@dataclass
class RunReport:
    """Top-level ETL run report written to stdout, JSON, and CI summaries."""

    run_id: str
    status: RuleStatus = "ok"
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    rules: list[RuleStats] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "fetched": sum(rule.fetched for rule in self.rules),
            "matched": sum(rule.matched for rule in self.rules),
            "new_listings": sum(rule.new_listings for rule in self.rules),
            "alerts": sum(rule.alerts for rule in self.rules),
            "deliveries": sum(rule.deliveries for rule in self.rules),
            "errors": sum(rule.errors for rule in self.rules),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "started_at": isoformat(self.started_at),
            "finished_at": isoformat(self.finished_at),
            "duration_seconds": round(
                ((self.finished_at or utcnow()) - self.started_at).total_seconds(), 2
            ),
            "totals": self.totals,
            "rules": [rule.to_dict() for rule in self.rules],
            "errors": self.errors,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=json_default)

    def to_markdown(self) -> str:
        """Render a compact GitHub Actions job summary."""

        totals = self.totals
        lines = [
            f"## Watchdog run `{self.run_id}`",
            "",
            f"**Status:** `{self.status}`  ",
            f"**Duration:** {self.to_dict()['duration_seconds']}s  ",
            f"**Fetched:** {totals['fetched']} | **Matched:** {totals['matched']} | "
            f"**New:** {totals['new_listings']} | **Alerts:** {totals['alerts']} | "
            f"**Errors:** {totals['errors']}",
            "",
            "| Rule | Status | Fetched | Matched | New | Alerts | Error(s) |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for rule in self.rules:
            lines.append(
                f"| `{rule.rule}` | {rule.status} | {rule.fetched} | {rule.matched} | "
                f"{rule.new_listings} | {rule.alerts} | {rule.errors} |"
            )
        if self.errors:
            lines.extend(["", "### Errors", ""])
            lines.extend(f"- {error}" for error in self.errors)
        return "\n".join(lines) + "\n"
