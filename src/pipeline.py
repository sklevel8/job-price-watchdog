"""ETL pipeline orchestration: extract, transform, load, and alert."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from config import AppConfig, RuleSettings
from database import Database
from models import (
    Alert,
    DeliveryResult,
    Listing,
    PriceStats,
    RuleStats,
    RunReport,
    Salary,
    utcnow,
)
from normalize import keyword_matches
from notifier import Notifier
from scraper import FetchError, HttpClient, ParseError, fetch_listings

logger = logging.getLogger("watchdog.pipeline")


class PipelineError(RuntimeError):
    """Raised for invalid pipeline invocations (e.g. unknown rule names)."""


class NotifierLike(Protocol):
    available_channels: tuple[str, ...]

    def send(
        self,
        alerts: Sequence[Alert],
        *,
        channels: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> list[DeliveryResult]: ...


Fetcher = Callable[..., list[Listing]]


class Pipeline:
    """Run configured rules once and return a structured report."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        notifier: NotifierLike | Notifier,
        *,
        client: HttpClient | object | None = None,
        fetcher: Fetcher = fetch_listings,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.db = db
        self.notifier = notifier
        self.client = client
        self.fetcher = fetcher
        self.clock = clock
        self.monotonic = monotonic
        # Price history observed before this run, captured once per listing so
        # several price rules comparing the same listing see the same baseline.
        self._price_baseline: dict[str, PriceStats] = {}

    def run(
        self,
        *,
        rules: Sequence[str] | None = None,
        run_id: str | None = None,
    ) -> RunReport:
        started = self.clock()
        selected = self._select_rules(rules)
        identifier = run_id or f"{started:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        report = RunReport(run_id=identifier, started_at=started)
        self._price_baseline = {}

        imported = self.db.import_ledger()
        if imported:
            logger.info("Merged %d terminal alert row(s) from the portable ledger", imported)

        self.db.start_run(identifier, started)
        client = self.client
        owns_client = client is None
        if client is None:
            client = HttpClient(self.config.http)

        try:
            alerts: list[Alert] = []
            for rule in selected:
                stats, rule_alerts = self._run_rule(rule, client)
                report.rules.append(stats)
                alerts.extend(rule_alerts)

            self._deliver(alerts, report)
            report.status = self._overall_status(report)

            try:
                ledger = self.db.export_ledger()
                logger.info("Exported portable ledger to %s", ledger)
            except Exception as exc:  # pragma: no cover - exercised via mocks
                logger.exception("Could not export ledger")
                report.status = "partial"
                report.errors.append(f"ledger export failed: {exc}")

            try:
                self.db.prune(now=self.clock())
            except Exception:  # pragma: no cover - maintenance must not fail a run
                logger.exception("Retention pruning failed")

            report.finished_at = self.clock()
            self._populate_errors(report)
            self.db.finish_run(
                identifier,
                report.status,
                report.totals,
                report.finished_at,
                error="\n".join(report.errors) or None,
            )
        except BaseException:
            # Record the failed run when possible, then let the CLI handle it.
            try:
                report.status = "failed"
                report.finished_at = self.clock()
                report.errors.append("fatal pipeline error")
                self.db.finish_run(
                    identifier,
                    "failed",
                    report.totals,
                    report.finished_at,
                    error="fatal pipeline error",
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("Could not mark run as failed")
            raise
        finally:
            if owns_client:
                assert isinstance(client, HttpClient)
                client.close()

        logger.info(
            "Run %s finished with status=%s: %s",
            identifier,
            report.status,
            report.totals,
        )
        return report

    # -- rules -------------------------------------------------------------

    def _select_rules(self, names: Sequence[str] | None) -> tuple[RuleSettings, ...]:
        enabled = self.config.enabled_rules
        if names is None:
            selected = enabled
        else:
            requested = list(dict.fromkeys(names))
            available = {rule.name for rule in enabled}
            unknown = [name for name in requested if name not in available]
            if unknown:
                raise PipelineError(f"unknown or disabled rule(s): {', '.join(unknown)}")
            selected = tuple(rule for rule in enabled if rule.name in set(requested))
        if not selected:
            raise PipelineError("no rules selected")
        return selected

    def _run_rule(
        self, rule: RuleSettings, client: HttpClient | object
    ) -> tuple[RuleStats, list[Alert]]:
        stats = RuleStats(rule=rule.name)
        started = self.monotonic()
        first_run = self.db.is_first_run(rule.name)
        alerts: list[Alert] = []
        processed = 0

        try:
            listings = self.fetcher(
                rule.source,
                client,
                default_currency=rule.match.currency,
            )
            stats.fetched = len(listings)
            logger.info("Rule %s fetched %d listing(s)", rule.name, stats.fetched)
            now = self.clock()
            seen_triggers: set[tuple[str, str, str]] = set()

            for listing in listings:
                if not self._matches(rule, listing, now):
                    continue
                stats.matched += 1
                try:
                    is_new = self.db.upsert_listing(listing, now)
                    if is_new:
                        stats.new_listings += 1
                    if listing.kind == "price":
                        new_alerts = self._process_price(rule, listing, now)
                    else:
                        new_alerts = self._process_job(rule, listing)
                    processed += 1
                except Exception as exc:
                    stats.errors += 1
                    stats.detail = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Rule %s could not process listing %r: %s",
                        rule.name,
                        listing.title,
                        exc,
                    )
                    continue

                for alert in new_alerts:
                    key = (alert.fingerprint, alert.rule, alert.trigger_key)
                    if key in seen_triggers:
                        continue
                    seen_triggers.add(key)
                    alerts.append(alert)
                    stats.alerts += 1

            if first_run and rule.bootstrap == "suppress" and alerts:
                logger.info(
                    "Rule %s bootstrap=suppress: withholding %d initial alert(s)",
                    rule.name,
                    len(alerts),
                )
                for alert in alerts:
                    self.db.record_suppressed(alert, now)
                stats.detail = f"bootstrap suppressed {len(alerts)} alert(s)"
                alerts = []
                stats.alerts = 0

            stats.status = "ok"
            if stats.errors and processed == 0:
                stats.status = "failed"
            elif stats.errors:
                stats.status = "partial"
        except (FetchError, ParseError) as exc:
            stats.status = "failed"
            stats.errors += 1
            stats.detail = str(exc)
            logger.warning("Rule %s failed: %s", rule.name, exc)
        except Exception as exc:
            stats.status = "failed"
            stats.errors += 1
            stats.detail = f"{type(exc).__name__}: {exc}"
            logger.exception("Rule %s crashed", rule.name)
        finally:
            stats.duration_ms = int((self.monotonic() - started) * 1000)
            try:
                self.db.record_rule_run(rule.name, stats.status, self.clock())
            except Exception:  # pragma: no cover - DB failures are fatal elsewhere
                logger.exception("Could not record state for rule %s", rule.name)

        logger.info(
            "Rule %s: matched=%d new=%d alerts=%d errors=%d status=%s",
            rule.name,
            stats.matched,
            stats.new_listings,
            stats.alerts,
            stats.errors,
            stats.status,
        )
        return stats, alerts

    # -- transforms --------------------------------------------------------

    def _matches(self, rule: RuleSettings, listing: Listing, now: datetime) -> bool:
        match = rule.match
        if listing.kind != rule.kind:
            return False
        if listing.kind == "price":
            if listing.price is None:
                return False
            if (
                match.currency
                and listing.currency
                and listing.currency.upper() != match.currency.upper()
            ):
                return False

        parts = [listing.title, listing.company or "", " ".join(listing.tags)]
        if match.include_description:
            parts.append(listing.description)
        haystack = " ".join(part for part in parts if part)
        if match.exclude_keywords and keyword_matches(haystack, match.exclude_keywords):
            return False
        if not keyword_matches(haystack, match.keywords, match.keyword_mode):
            return False

        if match.remote_only and listing.remote is False:
            return False

        reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        if match.max_age_days and listing.posted_at:
            posted = (
                listing.posted_at
                if listing.posted_at.tzinfo is not None
                else listing.posted_at.replace(tzinfo=UTC)
            )
            if posted < reference - timedelta(days=match.max_age_days):
                return False

        if listing.kind == "job" and match.min_salary is not None:
            salary = Salary(
                minimum=listing.salary_min,
                maximum=listing.salary_max,
                currency=listing.salary_currency,
                period=listing.salary_period,
            )
            annualized = salary.annualized_minimum()
            if annualized is None:
                if match.require_salary:
                    return False
            elif annualized < match.min_salary:
                return False
        return True

    def _process_job(self, rule: RuleSettings, listing: Listing) -> list[Alert]:
        """Generate one alert per rule until every configured channel has it.

        A console-only run still counts as delivered for the console channel,
        while adding a new channel later causes the alert to be sent there too.
        Failed channels are re-generated and also picked up by the pending queue.
        """

        if self.db.was_alert_suppressed(listing.fingerprint, rule.name, "new"):
            return []
        undelivered = [
            channel
            for channel in self.notifier.available_channels
            if not self.db.was_alert_sent(listing.fingerprint, rule.name, "new", channel)
        ]
        if not undelivered:
            return []
        return [
            Alert(
                listing=listing,
                rule=rule.name,
                reason="new_match",
                trigger_key="new",
            )
        ]

    def _process_price(self, rule: RuleSettings, listing: Listing, now: datetime) -> list[Alert]:
        assert listing.price is not None
        target = rule.match.max_price
        prior = self._price_baseline.get(listing.fingerprint)
        if prior is None:
            prior = self.db.get_price_stats(listing.fingerprint)
            self._price_baseline[listing.fingerprint] = prior
        self.db.record_price(listing.fingerprint, listing.price, listing.currency, now)
        if target is None or listing.price > target:
            return []
        if prior.minimum is not None and listing.price >= prior.minimum:
            return []
        reason = "below_target" if prior.minimum is None else "new_low"
        trigger = f"below:{_decimal_key(target)}:{_decimal_key(listing.price)}"
        return [
            Alert(
                listing=listing,
                rule=rule.name,
                reason=reason,  # type: ignore[arg-type]
                trigger_key=trigger,
                target_price=target,
            )
        ]

    # -- delivery ----------------------------------------------------------

    def _deliver(self, alerts: list[Alert], report: RunReport) -> None:
        stats_by_rule = {stats.rule: stats for stats in report.rules}
        alerts = self._merge_pending(alerts, report)
        if not alerts:
            return
        threshold = self.config.notifications.alert_burst_warning_threshold
        if len(alerts) > threshold:
            logger.warning(
                "Large alert burst: %d alerts (warning threshold %d); sending all",
                len(alerts),
                threshold,
            )

        for channel in self.notifier.available_channels:
            eligible = [
                alert
                for alert in alerts
                if not self.db.was_alert_sent(
                    alert.fingerprint, alert.rule, alert.trigger_key, channel
                )
            ]
            if not eligible:
                continue
            delivery_time = self.clock()
            try:
                results = self.notifier.send(eligible, channels=(channel,), now=delivery_time)
            except Exception as exc:
                logger.exception("Notifier channel %s crashed", channel)
                results = [
                    DeliveryResult(alert, channel, "failed", f"{type(exc).__name__}: {exc}")
                    for alert in eligible
                ]

            for result in results:
                self.db.record_delivery(result, delivery_time)
                stats = stats_by_rule.get(result.alert.rule)
                if stats is None:
                    continue
                if result.status == "failed":
                    stats.errors += 1
                    stats.detail = result.error or "delivery failed"
                    if stats.status == "ok":
                        stats.status = "partial"
                    logger.warning(
                        "Delivery failed for %r on %s: %s",
                        result.alert.listing.title,
                        result.channel,
                        result.error,
                    )
                else:
                    stats.deliveries += 1

    def _merge_pending(self, alerts: list[Alert], report: RunReport) -> list[Alert]:
        keys = {(alert.fingerprint, alert.rule, alert.trigger_key) for alert in alerts}
        allowed_rules = {stats.rule for stats in report.rules}
        merged = list(alerts)
        pending = self.db.pending_alerts()
        for alert in pending:
            if alert.rule not in allowed_rules:
                continue
            key = (alert.fingerprint, alert.rule, alert.trigger_key)
            if key in keys:
                continue
            keys.add(key)
            merged.append(alert)
        if pending:
            logger.info("Retrying %d previously failed alert(s)", len(pending))
        return merged

    # -- reporting ---------------------------------------------------------

    @staticmethod
    def _overall_status(report: RunReport) -> str:
        statuses = [stats.status for stats in report.rules]
        if not statuses or all(status == "failed" for status in statuses):
            return "failed"
        if all(status == "ok" for status in statuses):
            return "ok"
        return "partial"

    @staticmethod
    def _populate_errors(report: RunReport) -> None:
        seen = set(report.errors)
        for stats in report.rules:
            if stats.status in {"failed", "partial"} and stats.detail:
                message = f"{stats.rule}: {stats.detail}"
                if message not in seen:
                    seen.add(message)
                    report.errors.append(message)


def _decimal_key(value: Decimal) -> str:
    return format(value.normalize(), "f")
