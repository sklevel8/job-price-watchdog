"""Normalization helpers for messy scraped data.

Every helper is pure and side-effect free so the tricky parsing rules can be
tested exhaustively without network access.  The parsers intentionally handle
the formats actually seen in job boards and shops, not arbitrary prose.
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from models import ParsedPrice, Salary, utcnow

_CURRENCY_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("US$", "USD"),
    ("CA$", "CAD"),
    ("AU$", "AUD"),
    ("NZ$", "NZD"),
    ("R$", "BRL"),
    ("£", "GBP"),
    ("€", "EUR"),
    ("¥", "JPY"),
    ("₹", "INR"),
    ("₦", "NGN"),
    ("₽", "RUB"),
    ("₩", "KRW"),
    ("$", "USD"),
)

_CURRENCY_CODES = (
    "USD",
    "EUR",
    "GBP",
    "ZAR",
    "NGN",
    "INR",
    "CAD",
    "AUD",
    "NZD",
    "JPY",
    "BRL",
    "RUB",
    "KRW",
    "CHF",
    "SEK",
    "NOK",
    "DKK",
    "PLN",
)

_CURRENCY_WORDS = {
    "euro": "EUR",
    "euros": "EUR",
    "dollar": "USD",
    "dollars": "USD",
    "pound": "GBP",
    "pounds": "GBP",
    "rand": "ZAR",
    "yen": "JPY",
    "rupee": "INR",
    "rupees": "INR",
}

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
    "gh_src",
    "lever-source",
    "lever-origin",
    "trk",
    "trackingid",
}

_NUMBER_RE = re.compile(r"\d(?:[\d\s.,'’]*\d)?")
_SALARY_NUMBER_RE = re.compile(r"\d(?:[\d\s.,'’]*\d)?\s*[kKmM]?\+?")
_RANGE_RE = re.compile(
    r"(?P<left>(?:[^\d\s]{0,4}\s*)?\d[\d\s.,'’]*?)\s*(?:-|–|—|\bto\b)\s*"
    r"(?P<right>(?:[^\d\s]{0,4}\s*)?\d[\d\s.,'’]*)",
    re.IGNORECASE,
)
_RELATIVE_RE = re.compile(
    r"(?P<count>\d+|a|an|one)\s+(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
_DATE_PREFIX_RE = re.compile(r"^(?:posted|updated|published|listed)\s*(?:on|:)?\s*", re.I)
_ORDINAL_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.I)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


_MOJIBAKE_MARKERS = ("Â", "Ã", "â€", "ðŸ")


def _repair_mojibake(text: str) -> str:
    """Repair common UTF-8-decoded-as-cp1252 mojibake (e.g. ``Â£`` -> ``£``)."""

    if not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def clean_text(value: object | None) -> str:
    """Collapse HTML entities and all whitespace into a single-line string."""

    if value is None:
        return ""
    text = html.unescape(str(value))
    text = _repair_mojibake(text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u200b", "").replace("\u200e", "").replace("\u200f", "")
    return re.sub(r"\s+", " ", text).strip()


def strip_html(value: str | None, *, limit: int = 20_000) -> str:
    """Convert an HTML fragment to clean text without executing anything."""

    if not value:
        return ""
    fragment = value[:limit]
    if "<" in fragment and ">" in fragment:
        fragment = BeautifulSoup(fragment, "html.parser").get_text(" ")
    return clean_text(fragment)


def truncate(value: str | None, limit: int) -> str:
    """Trim a string to ``limit`` characters, adding an ellipsis when cut."""

    text = clean_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def canonical_url(raw_url: str | None, *, base_url: str | None = None) -> str:
    """Return a stable http(s) URL without fragments or tracking parameters."""

    if not raw_url:
        return ""
    candidate = clean_text(raw_url)
    if base_url:
        candidate = urljoin(base_url, candidate)
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    if port and port not in {80, 443}:
        host = f"{host}:{port}"
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs))
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, host, path, query, ""))


# ---------------------------------------------------------------------------
# Currency and prices
# ---------------------------------------------------------------------------


def detect_currency(text: str, default: str | None = None) -> str | None:
    """Detect an ISO-4217 currency from a symbol, code, or word.

    ``$`` is treated as USD because that is overwhelmingly the convention on
    the sources this project targets; pass an explicit default for others.
    """

    cleaned = clean_text(text)
    upper = cleaned.upper()
    for code in _CURRENCY_CODES:
        if re.search(rf"(?<![A-Z]){code}(?![A-Z])", upper):
            return code
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in cleaned:
            return code
    # "R 1 299,00" (South African rand). Check after R$ to avoid a false hit.
    if re.search(r"(?<![A-Za-z])R\s?(?=\d)", cleaned):
        return "ZAR"
    words = re.findall(r"[a-z]+", cleaned.casefold())
    for word in words:
        if word in _CURRENCY_WORDS:
            return _CURRENCY_WORDS[word]
    if default:
        normalized = clean_text(default).upper()
        return normalized or None
    return None


def _parse_grouped_number(raw: str) -> Decimal | None:
    """Parse ``1,299.00``, ``1.299,00``, ``1 299,00`` or ``1299``."""

    text = re.sub(r"[^\d.,]", "", raw)
    if not text or not any(char.isdigit() for char in text):
        return None
    try:
        if "." in text and "," in text:
            decimal_sep = "." if text.rfind(".") > text.rfind(",") else ","
            thousands_sep = "," if decimal_sep == "." else "."
            text = text.replace(thousands_sep, "")
            if decimal_sep == ",":
                text = text.replace(",", ".")
            return Decimal(text)

        separator = "." if "." in text else ("," if "," in text else None)
        if separator is None:
            return Decimal(text)

        parts = text.split(separator)
        if len(parts) > 2:
            if all(len(part) == 3 for part in parts[1:]):
                return Decimal("".join(parts))
            decimal_part = parts[-1]
            integer_part = "".join(parts[:-1])
            return Decimal(f"{integer_part or '0'}.{decimal_part}")

        left, right = parts
        if not right:
            return Decimal(left or "0")
        # A single separator followed by exactly three digits is a thousands
        # separator (1,299 / 1.299) unless the integer part is zero-padded.
        if len(right) == 3 and 1 <= len(left) <= 3 and not left.startswith("0"):
            return Decimal(left + right)
        return Decimal(f"{left or '0'}.{right}")
    except (InvalidOperation, ValueError):
        return None


def _extract_number_candidates(text: str) -> list[tuple[Decimal, int, int]]:
    candidates: list[tuple[Decimal, int, int]] = []
    for match in _NUMBER_RE.finditer(text):
        value = _parse_grouped_number(match.group(0))
        if value is not None:
            candidates.append((value, match.start(), match.end()))
    return candidates


def _currency_distance(text: str, start: int, end: int) -> int | None:
    """Return the character distance from a number to the nearest currency token."""

    distances: list[int] = []
    for symbol, _ in _CURRENCY_SYMBOLS:
        search_from = 0
        while (index := text.find(symbol, search_from)) != -1:
            token_start, token_end = index, index + len(symbol)
            if token_end <= start:
                distances.append(start - token_end)
            elif token_start >= end:
                distances.append(token_start - end)
            search_from = index + 1
    upper = text.upper()
    for code in _CURRENCY_CODES:
        for match in re.finditer(rf"(?<![A-Z]){code}(?![A-Z])", upper):
            if match.end() <= start:
                distances.append(start - match.end())
            elif match.start() >= end:
                distances.append(match.start() - end)
    if re.search(r"(?<![A-Za-z])R\s?(?=\d)", text):
        for match in re.finditer(r"(?<![A-Za-z])R\s?(?=\d)", text):
            if match.end() <= start:
                distances.append(start - match.end())
            elif match.start() >= end:
                distances.append(match.start() - end)
    return min(distances) if distances else None


def _parse_single_price(text: str) -> tuple[Decimal | None, str]:
    stripped = clean_text(text)
    if not stripped:
        return None, ""
    if re.fullmatch(r"(?:free|gratis|no charge)", stripped, re.I):
        return Decimal("0"), stripped
    candidates = _extract_number_candidates(stripped)
    if not candidates:
        return None, stripped
    scored = [
        (distance, index, value)
        for index, (value, start, end) in enumerate(candidates)
        if (distance := _currency_distance(stripped, start, end)) is not None
    ]
    if scored:
        # Closest to a currency marker wins; ties resolve to the earliest number.
        return min(scored, key=lambda item: (item[0], item[1]))[2], stripped
    return candidates[0][0], stripped


def parse_price(raw: str | None, *, default_currency: str | None = None) -> ParsedPrice:
    """Extract a Decimal price and currency from a messy string.

    Supports ``$1,299.00``, ``1.299,00 €``, ``£10 - £20``, ``From $99`` and
    ``Free``.  For ranges, ``value`` is the lower bound.
    """

    text = clean_text(raw)
    if not text:
        return ParsedPrice(value=None, raw="", currency=default_currency)

    currency = detect_currency(text, default_currency)
    range_match = None
    if not re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        range_match = _RANGE_RE.search(text)
    if range_match:
        left, left_raw = _parse_single_price(range_match.group("left"))
        right, right_raw = _parse_single_price(range_match.group("right"))
        if left is not None and right is not None:
            low, high = sorted((left, right))
            if low == high:
                return ParsedPrice(
                    value=low, raw=clean_text(f"{left_raw} - {right_raw}"), currency=currency
                )
            return ParsedPrice(
                value=low,
                raw=clean_text(f"{left_raw} - {right_raw}"),
                currency=currency,
                low=low,
                high=high,
            )

    value, single_raw = _parse_single_price(text)
    return ParsedPrice(value=value, raw=single_raw, currency=currency)


# ---------------------------------------------------------------------------
# Salaries
# ---------------------------------------------------------------------------


def _salary_amount(token: str) -> Decimal | None:
    stripped = clean_text(token).replace("+", "")
    multiplier = Decimal("1")
    if re.search(r"\d\s*[kK]\b", stripped) or re.search(r"\d\s*[kK]$", stripped):
        multiplier = Decimal("1000")
        stripped = re.sub(r"[kK]", "", stripped)
    elif re.search(r"\d\s*[mM]\b", stripped) or re.search(r"\d\s*[mM]$", stripped):
        multiplier = Decimal("1_000_000")
        stripped = re.sub(r"[mM]", "", stripped)
    value = _parse_grouped_number(stripped)
    if value is None:
        return None
    return value * multiplier


def _detect_period(text: str) -> str | None:
    patterns = (
        ("hour", r"\b(?:per\s+hour|hourly|/\s*hr|an?\s+hour)\b"),
        ("month", r"\b(?:per\s+month|monthly|/\s*mo|a\s+month)\b"),
        ("week", r"\b(?:per\s+week|weekly|/\s*wk|a\s+week)\b"),
        ("day", r"\b(?:per\s+day|daily|/\s*day|a\s+day)\b"),
        ("year", r"\b(?:per\s+(?:year|annum)|yearly|annual(?:ly)?|/\s*yr|a\s+year)\b"),
    )
    for period, pattern in patterns:
        if re.search(pattern, text, re.I):
            return period
    return None


def _extract_salary_candidates(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for match in _SALARY_NUMBER_RE.finditer(text):
        value = _salary_amount(match.group(0))
        if value is not None and value > 0:
            values.append(value)
    return values


def parse_salary(
    raw: str | None = None,
    *,
    minimum_raw: str | int | float | Decimal | None = None,
    maximum_raw: str | int | float | Decimal | None = None,
    default_currency: str | None = None,
) -> Salary:
    """Normalize salary text or explicit API min/max values.

    Examples: ``$120k - $160k``, ``€80k+``, ``R600k - R900k per annum`` and
    ``$50 - $70 per hour``.  A missing period defaults to hourly for small
    amounts and annual otherwise.
    """

    text = clean_text(raw)
    currency = detect_currency(text, default_currency) if text else (default_currency or None)

    explicit_min = _salary_amount(str(minimum_raw)) if minimum_raw not in (None, "", 0) else None
    explicit_max = _salary_amount(str(maximum_raw)) if maximum_raw not in (None, "", 0) else None

    minimum: Decimal | None = None
    maximum: Decimal | None = None
    if explicit_min is not None or explicit_max is not None:
        minimum = explicit_min
        maximum = explicit_max
    elif text:
        range_match = None
        if not re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
            range_match = _RANGE_RE.search(text)
        if range_match:
            left = _salary_amount(range_match.group("left"))
            right = _salary_amount(range_match.group("right"))
            if left is not None and right is not None:
                minimum, maximum = sorted((left, right))
        if minimum is None:
            values = _extract_salary_candidates(text)
            if values:
                minimum = values[0]
                maximum = values[1] if len(values) > 1 else None
                if re.search(r"(?:up\s+to|max(?:imum)?)\b", text, re.I):
                    minimum, maximum = None, values[0]
    if minimum is not None and maximum is None and explicit_max is not None:
        maximum = explicit_max

    period = _detect_period(text) if text else None
    if period is None and (minimum is not None or maximum is not None):
        largest = max(value for value in (minimum, maximum) if value is not None)
        period = "hour" if largest < 1000 else "year"

    return Salary(
        minimum=minimum,
        maximum=maximum,
        currency=currency,
        period=period,
        raw=text,
    )


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def parse_datetime(
    value: str | int | float | datetime | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Parse ISO/RFC 2822/relative dates into an aware UTC datetime.

    Unparseable values return ``None`` rather than crashing a whole ETL run.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:  # milliseconds
            timestamp /= 1000
        result = datetime.fromtimestamp(timestamp, tz=UTC)
    else:
        text = clean_text(value)
        if not text:
            return None
        if re.fullmatch(r"\d{10}(?:\d{3})?", text):
            return parse_datetime(int(text), now=now)
        reference = now or utcnow()
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)

        lowered = text.casefold()
        if lowered in {"just now", "now", "today"}:
            return reference
        if lowered == "yesterday":
            return reference - timedelta(days=1)

        stripped = _DATE_PREFIX_RE.sub("", text)
        relative = _RELATIVE_RE.search(stripped)
        if relative:
            count_raw = relative.group("count").casefold()
            count = 1 if count_raw in {"a", "an", "one"} else int(count_raw)
            unit = relative.group("unit").casefold()
            delta = {
                "second": timedelta(seconds=count),
                "minute": timedelta(minutes=count),
                "hour": timedelta(hours=count),
                "day": timedelta(days=count),
                "week": timedelta(weeks=count),
                "month": timedelta(days=30 * count),
                "year": timedelta(days=365 * count),
            }[unit]
            return reference - delta

        iso_candidate = stripped.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            result = datetime.fromisoformat(iso_candidate)
        except ValueError:
            try:
                result = parsedate_to_datetime(stripped)
            except (TypeError, ValueError):
                cleaned = _ORDINAL_RE.sub("", stripped)
                formats = (
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d",
                    "%Y/%m/%d",
                    "%d %b %Y",
                    "%d %B %Y",
                    "%b %d, %Y",
                    "%B %d, %Y",
                    "%d/%m/%Y",
                    "%m/%d/%Y",
                    "%d-%m-%Y",
                    "%m-%d-%Y",
                )
                result = None
                for date_format in formats:
                    try:
                        result = datetime.strptime(cleaned, date_format)
                        break
                    except ValueError:
                        continue
                if result is None:
                    return None

    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def keyword_matches(text: str, keywords: tuple[str, ...] | list[str], mode: str = "any") -> bool:
    """Case-insensitive keyword matching with alphanumeric word boundaries."""

    cleaned_keywords = [
        clean_text(keyword).casefold() for keyword in keywords if clean_text(keyword)
    ]
    if not cleaned_keywords:
        return True
    haystack = clean_text(text).casefold()
    hits = []
    for keyword in cleaned_keywords:
        pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
        hits.append(bool(re.search(pattern, haystack)))
    return all(hits) if mode == "all" else any(hits)


def salary_annualized_minimum(salary: Salary) -> Decimal | None:
    return salary.annualized_minimum()
