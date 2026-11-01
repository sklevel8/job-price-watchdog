"""Exhaustive unit tests for the normalization layer."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from normalize import (
    canonical_url,
    clean_text,
    detect_currency,
    keyword_matches,
    parse_datetime,
    parse_price,
    parse_salary,
    strip_html,
    truncate,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("  Hello\n\tworld  ", "Hello world"),
        ("Tom &amp; Jerry", "Tom & Jerry"),
        ("non\u00a0breaking\u200b space", "non breaking space"),
        ("Â£51.77", "£51.77"),
        ("smarter â€™ quotes", "smarter ’ quotes"),
        ("  ", ""),
    ],
)
def test_clean_text(raw: str | None, expected: str) -> None:
    assert clean_text(raw) == expected


def test_strip_html() -> None:
    assert (
        strip_html("<p>Hello <strong>world</strong> &amp; friends</p>") == "Hello world & friends"
    )


def test_strip_html_is_bounded() -> None:
    result = strip_html("<p>" + "a" * 100 + "</p>", limit=20)
    assert len(result) <= 20


def test_truncate() -> None:
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 4) == "abc"


@pytest.mark.parametrize(
    ("raw", "base", "expected"),
    [
        (
            "https://Example.com/jobs/?utm_source=x&b=2&a=1#apply",
            None,
            "https://example.com/jobs?a=1&b=2",
        ),
        ("/jobs/123", "https://example.com/base/", "https://example.com/jobs/123"),
        ("https://example.com:443/x/", None, "https://example.com/x"),
        ("ftp://example.com/file", None, ""),
        ("https://example.com:bad/x", None, ""),
        ("javascript:alert(1)", None, ""),
        ("", None, ""),
        ("https://example.com/", None, "https://example.com/"),
    ],
)
def test_canonical_url(raw: str, base: str | None, expected: str) -> None:
    assert canonical_url(raw, base_url=base) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,299.00", "USD"),
        ("€ 49,99", "EUR"),
        ("£45", "GBP"),
        ("R 1 299,00", "ZAR"),
        ("R$ 100", "BRL"),
        ("ZAR 500", "ZAR"),
        ("120 dollars", "USD"),
        ("no currency", None),
    ],
)
def test_detect_currency(raw: str, expected: str | None) -> None:
    assert detect_currency(raw) == expected


def test_detect_currency_default() -> None:
    assert detect_currency("99", default="GBP") == "GBP"


@pytest.mark.parametrize(
    ("raw", "value", "currency", "low", "high"),
    [
        ("$1,299.00", "1299.00", "USD", None, None),
        ("£45", "45", "GBP", None, None),
        ("1.299,00 €", "1299.00", "EUR", None, None),
        ("1\u00a0299,00 €", "1299.00", "EUR", None, None),
        ("R 1 299,00", "1299.00", "ZAR", None, None),
        ("USD 99.95", "99.95", "USD", None, None),
        ("From $99", "99", "USD", None, None),
        ("£10.00 - £20.00", "10.00", "GBP", "10.00", "20.00"),
        ("$1,200–$1,500", "1200", "USD", "1200", "1500"),
        ("10 to 20", "10", None, "10", "20"),
        ("Now £49.99 (was £99.99)", "49.99", "GBP", None, None),
        ("2 for $10", "10", "USD", None, None),
        ("Free", "0", None, None, None),
        ("1,299", "1299", None, None, None),
        ("12,999", "12999", None, None, None),
        ("89,9", "89.9", None, None, None),
        ("0.999", "0.999", None, None, None),
        ("1,234,567.89", "1234567.89", None, None, None),
        ("1.234.567,89", "1234567.89", None, None, None),
        ("£1,299.00 GBP", "1299.00", "GBP", None, None),
        ("Â£51.77", "51.77", "GBP", None, None),
        ("", None, None, None, None),
        ("Price on request", None, None, None, None),
    ],
)
def test_parse_price(
    raw: str,
    value: str | None,
    currency: str | None,
    low: str | None,
    high: str | None,
) -> None:
    parsed = parse_price(raw)
    assert parsed.value == (Decimal(value) if value is not None else None)
    assert parsed.currency == currency
    assert parsed.low == (Decimal(low) if low is not None else None)
    assert parsed.high == (Decimal(high) if high is not None else None)


def test_parse_price_default_currency() -> None:
    assert parse_price("99.00", default_currency="GBP").currency == "GBP"


def test_parse_price_range_flag() -> None:
    assert parse_price("$10 - $20").is_range is True
    assert parse_price("$10 - $10").is_range is False


@pytest.mark.parametrize(
    ("raw", "minimum", "maximum", "currency", "period"),
    [
        ("$120k - $160k", "120000", "160000", "USD", "year"),
        ("€80k+", "80000", None, "EUR", "year"),
        ("R600k - R900k per annum", "600000", "900000", "ZAR", "year"),
        ("$50 - $70 per hour", "50", "70", "USD", "hour"),
        ("120000 USD", "120000", None, "USD", "year"),
        ("£45,000 per year", "45000", None, "GBP", "year"),
        ("$1.2m", "1200000", None, "USD", "year"),
        ("up to $160k", None, "160000", "USD", "year"),
        ("Competitive", None, None, None, None),
        ("", None, None, None, None),
    ],
)
def test_parse_salary(
    raw: str,
    minimum: str | None,
    maximum: str | None,
    currency: str | None,
    period: str | None,
) -> None:
    salary = parse_salary(raw)
    assert salary.minimum == (Decimal(minimum) if minimum is not None else None)
    assert salary.maximum == (Decimal(maximum) if maximum is not None else None)
    assert salary.currency == currency
    assert salary.period == period


def test_parse_salary_from_api_fields() -> None:
    salary = parse_salary(minimum_raw=80000, maximum_raw=100000, default_currency="USD")
    assert salary.minimum == Decimal("80000")
    assert salary.maximum == Decimal("100000")
    assert salary.currency == "USD"
    assert salary.period == "year"


def test_parse_salary_ignores_zero_api_fields() -> None:
    salary = parse_salary(minimum_raw=0, maximum_raw=0)
    assert salary.has_value is False
    assert salary.period is None


def test_salary_annualization() -> None:
    hourly = parse_salary("$50 per hour")
    assert hourly.annualized_minimum() == Decimal("104000.00")
    monthly = parse_salary("$5,000 per month")
    assert monthly.annualized_minimum() == Decimal("60000.00")
    assert parse_salary("Competitive").annualized_minimum() is None


def test_salary_explicit_max_only() -> None:
    salary = parse_salary(minimum_raw=None, maximum_raw="160k", default_currency="USD")
    assert salary.minimum is None
    assert salary.maximum == Decimal("160000")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-30T12:34:56Z", datetime(2026, 10, 30, 12, 34, 56, tzinfo=UTC)),
        ("2026-10-30T14:34:56+02:00", datetime(2026, 10, 30, 12, 34, 56, tzinfo=UTC)),
        ("2026-10-30", datetime(2026, 10, 30, tzinfo=UTC)),
        ("2026/10/30", datetime(2026, 10, 30, tzinfo=UTC)),
        ("30 Oct 2026", datetime(2026, 10, 30, tzinfo=UTC)),
        ("October 30, 2026", datetime(2026, 10, 30, tzinfo=UTC)),
        ("30/10/2026", datetime(2026, 10, 30, tzinfo=UTC)),
        ("2026-10-30 12:00:00", datetime(2026, 10, 30, 12, tzinfo=UTC)),
        (
            "Fri, 30 Oct 2026 12:00:00 GMT",
            datetime(2026, 10, 30, 12, tzinfo=UTC),
        ),
    ],
)
def test_parse_datetime_formats(raw: str, expected: datetime) -> None:
    assert parse_datetime(raw) == expected


def test_parse_datetime_epoch() -> None:
    assert parse_datetime(1793318417) == datetime(2026, 10, 30, 0, 0, 17, tzinfo=UTC)
    assert parse_datetime("1793318417000") == datetime(2026, 10, 30, 0, 0, 17, tzinfo=UTC)


def test_parse_datetime_relative() -> None:
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    assert parse_datetime("3 days ago", now=now) == datetime(2026, 10, 27, 12, tzinfo=UTC)
    assert parse_datetime("Posted 2 hours ago", now=now) == datetime(2026, 10, 30, 10, tzinfo=UTC)
    assert parse_datetime("a week ago", now=now) == datetime(2026, 10, 23, 12, tzinfo=UTC)
    assert parse_datetime("yesterday", now=now) == datetime(2026, 10, 29, 12, tzinfo=UTC)
    assert parse_datetime("just now", now=now) == now


def test_parse_datetime_naive_assumed_utc() -> None:
    result = parse_datetime(datetime(2026, 1, 1, 9, 30))
    assert result == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["", None, "not a date", "sometime soon"])
def test_parse_datetime_invalid(raw: str | None) -> None:
    assert parse_datetime(raw) is None


@pytest.mark.parametrize(
    ("text", "keywords", "mode", "expected"),
    [
        ("Senior Python Engineer", ["python"], "any", True),
        ("Pythonic tooling", ["python"], "any", False),
        ("Senior Python Engineer", ["python", "engineer"], "all", True),
        ("Senior Python Engineer", ["python", "rust"], "all", False),
        ("Backend / Platform role", ["platform"], "any", True),
        ("SRE - Site Reliability", ["site reliability"], "any", True),
        ("C++ developer", ["c++"], "any", True),
        ("Anything", [], "any", True),
        ("Machine Learning", ["machine", "learning"], "all", True),
    ],
)
def test_keyword_matches(text: str, keywords: list[str], mode: str, expected: bool) -> None:
    assert keyword_matches(text, keywords, mode=mode) is expected
