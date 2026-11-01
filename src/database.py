"""SQLite persistence for listings, prices, runs, and alert idempotency.

Money is stored as text so Decimal values round-trip without binary floating
point drift.  The alert ledger is exported to a small JSON file after every
run; that file can be committed even though the full SQLite database is
restored from the GitHub Actions cache.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from models import (
    Alert,
    DeliveryResult,
    Listing,
    PriceStats,
    isoformat,
    utcnow,
)

SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    totals_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE TABLE IF NOT EXISTS rule_state (
    rule TEXT PRIMARY KEY,
    first_run_at TEXT NOT NULL,
    last_run_at TEXT NOT NULL,
    run_count INTEGER NOT NULL DEFAULT 1,
    last_status TEXT,
    first_success_at TEXT
);

CREATE TABLE IF NOT EXISTS listings (
    fingerprint TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('job', 'price')),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    remote INTEGER CHECK (remote IN (0, 1) OR remote IS NULL),
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    posted_at TEXT,
    price TEXT,
    currency TEXT,
    salary_min TEXT,
    salary_max TEXT,
    salary_currency TEXT,
    salary_period TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source);
CREATE INDEX IF NOT EXISTS idx_listings_kind ON listings(kind);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings(last_seen_at);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    price TEXT NOT NULL,
    currency TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_lookup
    ON price_history(fingerprint, observed_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_unique
    ON price_history(fingerprint, observed_at, price);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    rule TEXT NOT NULL,
    reason TEXT NOT NULL,
    trigger_key TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    price TEXT,
    sent_at TEXT NOT NULL,
    error TEXT,
    UNIQUE (fingerprint, rule, trigger_key, channel)
);
CREATE INDEX IF NOT EXISTS idx_alerts_sent
    ON alerts(fingerprint, rule, trigger_key, channel, status);
"""


class DatabaseError(RuntimeError):
    """Raised for schema or ledger integrity problems."""


class Database:
    """Thin, explicit SQLite repository. All SQL is parameterized."""

    def __init__(
        self,
        path: str | Path,
        *,
        now: datetime | None = None,
        ledger_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self._is_memory = str(path) == ":memory:"
        if not self._is_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or utcnow()
        if ledger_path is None:
            ledger_path = (
                self.path.parent / f"{self.path.stem}.ledger.json"
                if not self._is_memory
                else Path(":memory:")
            )
        self.ledger_path = Path(ledger_path)
        self.conn = sqlite3.connect(
            str(self.path) if not self._is_memory else ":memory:",
            timeout=30,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        if not self._is_memory:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    # -- lifecycle ---------------------------------------------------------

    def _migrate(self) -> None:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise DatabaseError(
                f"database schema v{version} is newer than supported v{SCHEMA_VERSION}"
            )
        if version < 1:
            self.conn.executescript(SCHEMA_V1)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.execute("PRAGMA user_version")
        # Additive migration: older v1 databases predate the bootstrap marker.
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(rule_state)").fetchall()
        }
        if "first_success_at" not in columns:
            self.conn.execute("ALTER TABLE rule_state ADD COLUMN first_success_at TEXT")
            self.conn.execute(
                "UPDATE rule_state SET first_success_at = first_run_at "
                "WHERE first_success_at IS NULL"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- runs and rules ----------------------------------------------------

    def start_run(self, run_id: str, started_at: datetime) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, status) VALUES (?, ?, 'running')",
                (run_id, isoformat(started_at)),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        totals: dict[str, int],
        finished_at: datetime,
        *,
        error: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, totals_json = ?, error = ? "
                "WHERE run_id = ?",
                (
                    isoformat(finished_at),
                    status,
                    json.dumps(totals, sort_keys=True),
                    error,
                    run_id,
                ),
            )

    def is_first_run(self, rule: str) -> bool:
        """Return True until this rule has completed at least one run.

        A failed attempt (for example, an upstream outage) does not count as
        the bootstrap run.  Otherwise a transient first-run failure would
        silently disable ``bootstrap: suppress`` on the retry.
        """

        row = self.conn.execute(
            "SELECT first_success_at FROM rule_state WHERE rule = ? LIMIT 1", (rule,)
        ).fetchone()
        return row is None or row["first_success_at"] is None

    def record_rule_run(self, rule: str, status: str, now: datetime) -> None:
        timestamp = isoformat(now)
        first_success_at = None if status == "failed" else timestamp
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO rule_state (
                    rule, first_run_at, last_run_at, run_count, last_status, first_success_at
                )
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(rule) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    run_count = rule_state.run_count + 1,
                    last_status = excluded.last_status,
                    first_success_at = COALESCE(
                        rule_state.first_success_at, excluded.first_success_at
                    )
                """,
                (rule, timestamp, timestamp, status, first_success_at),
            )

    # -- listings ----------------------------------------------------------

    def upsert_listing(self, listing: Listing, now: datetime) -> bool:
        """Insert or refresh a listing. Returns True only when it is new."""

        timestamp = isoformat(now)
        tags_json = json.dumps(list(listing.tags), ensure_ascii=False)
        raw_json = json.dumps(listing.raw, ensure_ascii=False, default=str)
        remote = None if listing.remote is None else int(listing.remote)
        values = (
            listing.fingerprint,
            listing.kind,
            listing.source,
            listing.external_id,
            listing.title,
            listing.company,
            listing.location,
            remote,
            listing.url,
            listing.description,
            tags_json,
            isoformat(listing.posted_at),
            str(listing.price) if listing.price is not None else None,
            listing.currency,
            str(listing.salary_min) if listing.salary_min is not None else None,
            str(listing.salary_max) if listing.salary_max is not None else None,
            listing.salary_currency,
            listing.salary_period,
            raw_json,
            timestamp,
            timestamp,
        )
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO listings (
                    fingerprint, kind, source, external_id, title, company, location,
                    remote, url, description, tags_json, posted_at, price, currency,
                    salary_min, salary_max, salary_currency, salary_period, raw_json,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                values,
            )
            is_new = cursor.rowcount == 1
            if not is_new:
                conn.execute(
                    """
                    UPDATE listings SET
                        title = ?, company = ?, location = ?, remote = ?, url = ?,
                        description = ?, tags_json = ?, posted_at = COALESCE(?, posted_at),
                        price = ?, currency = ?, salary_min = ?, salary_max = ?,
                        salary_currency = ?, salary_period = ?, raw_json = ?,
                        last_seen_at = ?
                    WHERE fingerprint = ?
                    """,
                    (
                        listing.title,
                        listing.company,
                        listing.location,
                        remote,
                        listing.url,
                        listing.description,
                        tags_json,
                        isoformat(listing.posted_at),
                        values[12],
                        listing.currency,
                        values[14],
                        values[15],
                        listing.salary_currency,
                        listing.salary_period,
                        raw_json,
                        timestamp,
                        listing.fingerprint,
                    ),
                )
        return is_new

    def get_listing(self, fingerprint: str) -> Listing | None:
        row = self.conn.execute(
            "SELECT * FROM listings WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return _row_to_listing(row) if row is not None else None

    def count_listings(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0])

    # -- prices ------------------------------------------------------------

    def record_price(
        self,
        fingerprint: str,
        price: Decimal,
        currency: str | None,
        observed_at: datetime,
    ) -> bool:
        """Store a price observation only when it differs from the latest one."""

        latest = self.latest_price(fingerprint)
        if latest is not None and latest == price:
            return False
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO price_history (fingerprint, price, currency, observed_at) "
                "VALUES (?, ?, ?, ?)",
                (fingerprint, str(price), currency, isoformat(observed_at)),
            )
        return True

    def latest_price(self, fingerprint: str) -> Decimal | None:
        row = self.conn.execute(
            "SELECT price FROM price_history WHERE fingerprint = ? "
            "ORDER BY observed_at DESC, id DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        return _decimal_or_none(row["price"]) if row else None

    def get_price_stats(self, fingerprint: str) -> PriceStats:
        rows = self.conn.execute(
            "SELECT price FROM price_history WHERE fingerprint = ? "
            "ORDER BY observed_at DESC, id DESC",
            (fingerprint,),
        ).fetchall()
        values = [value for row in rows if (value := _decimal_or_none(row["price"])) is not None]
        return PriceStats(
            latest=values[0] if values else None,
            previous=values[1] if len(values) > 1 else None,
            minimum=min(values) if values else None,
            observations=len(values),
        )

    def price_observation_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0])

    # -- alerts ------------------------------------------------------------

    def was_alert_sent(
        self,
        fingerprint: str,
        rule: str,
        trigger_key: str,
        channel: str,
    ) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM alerts WHERE fingerprint = ? AND rule = ? AND trigger_key = ? "
            "AND channel = ? AND status IN ('sent', 'console') LIMIT 1",
            (fingerprint, rule, trigger_key, channel),
        ).fetchone()
        return row is not None

    def was_alert_suppressed(
        self,
        fingerprint: str,
        rule: str,
        trigger_key: str,
    ) -> bool:
        """Return True if a bootstrap run intentionally suppressed this alert."""

        row = self.conn.execute(
            "SELECT 1 FROM alerts WHERE fingerprint = ? AND rule = ? AND trigger_key = ? "
            "AND status = 'suppressed' LIMIT 1",
            (fingerprint, rule, trigger_key),
        ).fetchone()
        return row is not None

    def record_suppressed(self, alert: Alert, recorded_at: datetime) -> None:
        """Remember a bootstrap-suppressed alert so it never fires later.

        Suppressed rows are not deliveries: they use the reserved ``bootstrap``
        channel and are ignored by ``was_alert_sent`` and ``pending_alerts``.
        """

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO alerts (
                    fingerprint, rule, reason, trigger_key, channel, status, price,
                    sent_at, error
                ) VALUES (?, ?, ?, ?, 'bootstrap', 'suppressed', ?, ?, NULL)
                ON CONFLICT(fingerprint, rule, trigger_key, channel) DO NOTHING
                """,
                (
                    alert.fingerprint,
                    alert.rule,
                    alert.reason,
                    alert.trigger_key,
                    str(alert.target_price) if alert.target_price is not None else None,
                    isoformat(recorded_at),
                ),
            )

    def record_delivery(self, delivery: DeliveryResult, sent_at: datetime) -> None:
        alert = delivery.alert
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO alerts (
                    fingerprint, rule, reason, trigger_key, channel, status, price,
                    sent_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint, rule, trigger_key, channel) DO UPDATE SET
                    reason = excluded.reason,
                    status = excluded.status,
                    price = excluded.price,
                    sent_at = excluded.sent_at,
                    error = excluded.error
                """,
                (
                    alert.fingerprint,
                    alert.rule,
                    alert.reason,
                    alert.trigger_key,
                    delivery.channel,
                    delivery.status,
                    str(alert.target_price) if alert.target_price is not None else None,
                    isoformat(sent_at),
                    delivery.error,
                ),
            )

    def alert_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT fingerprint, rule, reason, trigger_key, channel, status, price, "
                "sent_at, error FROM alerts ORDER BY id"
            )
        ]

    def pending_alerts(self) -> list[Alert]:
        """Rebuild alerts with failed deliveries so a later run can retry them."""

        rows = self.conn.execute(
            """
            SELECT a.rule AS alert_rule, a.reason AS alert_reason,
                   a.trigger_key AS alert_trigger, a.price AS target_price, l.*
            FROM alerts a
            JOIN listings l ON l.fingerprint = a.fingerprint
            WHERE a.status = 'failed'
            GROUP BY a.fingerprint, a.rule, a.trigger_key
            ORDER BY a.id
            """
        ).fetchall()
        alerts: list[Alert] = []
        for row in rows:
            try:
                listing = _row_to_listing(row)
            except (KeyError, json.JSONDecodeError, TypeError):
                continue
            alerts.append(
                Alert(
                    listing=listing,
                    rule=str(row["alert_rule"]),
                    reason=str(row["alert_reason"]),  # type: ignore[arg-type]
                    trigger_key=str(row["alert_trigger"]),
                    target_price=_decimal_or_none(row["target_price"]),
                )
            )
        return alerts

    # -- retention and reporting ------------------------------------------

    def prune(
        self, *, now: datetime | None = None, runs_days: int = 180, alerts_days: int = 365
    ) -> None:
        reference = now or utcnow()
        run_cutoff = isoformat(reference - timedelta(days=runs_days))
        alert_cutoff = isoformat(reference - timedelta(days=alerts_days))
        with self.transaction() as conn:
            conn.execute("DELETE FROM runs WHERE started_at < ?", (run_cutoff,))
            conn.execute("DELETE FROM alerts WHERE sent_at < ?", (alert_cutoff,))

    def summary(self) -> dict[str, Any]:
        listings = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN kind = 'price' THEN 1 ELSE 0 END) AS prices FROM listings"
        ).fetchone()
        alerts = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status IN ('sent', 'console') THEN 1 ELSE 0 END) AS sent "
            "FROM alerts"
        ).fetchone()
        last_run = self.conn.execute(
            "SELECT run_id, started_at, status FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return {
            "listings": int(listings["total"] or 0),
            "price_listings": int(listings["prices"] or 0),
            "alerts": int(alerts["total"] or 0),
            "alerts_sent": int(alerts["sent"] or 0),
            "price_observations": self.price_observation_count(),
            "last_run": dict(last_run) if last_run else None,
        }

    # -- portable ledger ---------------------------------------------------

    def export_ledger(self, path: str | Path | None = None, *, now: datetime | None = None) -> Path:
        target = Path(path) if path is not None else self.ledger_path
        if str(target) == ":memory:":
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        minima = self._price_minima()
        payload = {
            "version": SCHEMA_VERSION,
            "exported_at": isoformat(now or utcnow()),
            "alerts": self.alert_rows(),
            "price_minima": minima,
        }
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        temporary.replace(target)
        return target

    def import_ledger(self, path: str | Path | None = None) -> int:
        """Merge terminal alert rows and price baselines from the ledger.

        ``sent``, ``console``, and ``suppressed`` rows are terminal for
        idempotency purposes and must survive a cache eviction.  Failed rows
        are intentionally skipped: they are regenerated from the source or
        retried through the pending queue when their listing is still present.

        Returns the number of alert rows merged.  Malformed entries raise
        :class:`DatabaseError` rather than silently corrupting idempotency.
        """

        source = Path(path) if path is not None else self.ledger_path
        if not source.is_file():
            return 0
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DatabaseError(f"could not read ledger {source}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
            raise DatabaseError(f"unsupported ledger version in {source}")

        alerts = payload.get("alerts", [])
        minima = payload.get("price_minima", [])
        if not isinstance(alerts, list) or not isinstance(minima, list):
            raise DatabaseError(f"ledger {source} has an invalid shape")

        merged = 0
        with self.transaction() as conn:
            for row in alerts:
                if not isinstance(row, dict):
                    continue
                status = row.get("status")
                if status not in {"sent", "console", "suppressed"}:
                    continue
                try:
                    fingerprint = str(row["fingerprint"])
                    rule = str(row["rule"])
                    trigger_key = str(row["trigger_key"])
                    channel = str(row["channel"])
                    sent_at = str(row["sent_at"])
                except KeyError as exc:
                    raise DatabaseError(f"ledger alert is missing {exc}") from exc
                conn.execute(
                    """
                    INSERT INTO alerts (
                        fingerprint, rule, reason, trigger_key, channel, status, price,
                        sent_at, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(fingerprint, rule, trigger_key, channel) DO UPDATE SET
                        reason = excluded.reason,
                        status = excluded.status,
                        price = excluded.price,
                        sent_at = excluded.sent_at,
                        error = NULL
                    """,
                    (
                        fingerprint,
                        rule,
                        str(row.get("reason") or "new_match"),
                        trigger_key,
                        channel,
                        status,
                        row.get("price"),
                        sent_at,
                    ),
                )
                merged += 1
            for point in minima:
                if not isinstance(point, dict):
                    continue
                try:
                    fingerprint = str(point["fingerprint"])
                    price = Decimal(str(point["price"]))
                    observed_at = str(point["observed_at"])
                except (KeyError, InvalidOperation, ValueError) as exc:
                    raise DatabaseError(f"ledger price point is invalid: {exc}") from exc
                conn.execute(
                    "INSERT OR IGNORE INTO price_history "
                    "(fingerprint, price, currency, observed_at) VALUES (?, ?, ?, ?)",
                    (fingerprint, str(price), point.get("currency"), observed_at),
                )
        return merged

    def _price_minima(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT fingerprint, price, currency, observed_at FROM price_history "
            "ORDER BY observed_at, id"
        ).fetchall()
        minima: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = _decimal_or_none(row["price"])
            if value is None:
                continue
            current = minima.get(row["fingerprint"])
            if current is None or value < Decimal(current["price"]):
                minima[row["fingerprint"]] = {
                    "fingerprint": row["fingerprint"],
                    "price": str(value),
                    "currency": row["currency"],
                    "observed_at": row["observed_at"],
                }
        return list(minima.values())


def _row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        kind=row["kind"],
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        url=row["url"],
        company=row["company"],
        location=row["location"],
        remote=None if row["remote"] is None else bool(row["remote"]),
        description=row["description"],
        tags=tuple(json.loads(row["tags_json"])),
        posted_at=_parse_stored_datetime(row["posted_at"]),
        price=_decimal_or_none(row["price"]),
        currency=row["currency"],
        salary_min=_decimal_or_none(row["salary_min"]),
        salary_max=_decimal_or_none(row["salary_max"]),
        salary_currency=row["salary_currency"],
        salary_period=row["salary_period"],
        raw=json.loads(row["raw_json"]),
    )


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_stored_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    from normalize import parse_datetime

    return parse_datetime(value)


# Keep the annotation import used by type checkers without a runtime cost.
__all__ = ["SCHEMA_VERSION", "Database", "DatabaseError"]
