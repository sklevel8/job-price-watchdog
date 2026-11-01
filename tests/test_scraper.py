"""Tests for HTTP extraction, source parsing, and pagination."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests

from config import HttpSettings, SourceSettings
from normalize import canonical_url
from scraper import (
    FetchError,
    HttpClient,
    ParseError,
    fetch_listings,
    parse_books_catalog_html,
    parse_generic_html_page,
    parse_hn_algolia_json,
    parse_remoteok_json,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    def __init__(
        self,
        content: bytes = b"",
        *,
        status_code: int = 200,
        url: str = "https://example.com/",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        response.url = response.url or url
        return response


def http_client(session: FakeSession, **overrides: Any) -> HttpClient:
    settings = HttpSettings(timeout_seconds=5, max_retries=0, min_delay_seconds=0, **overrides)
    return HttpClient(settings, session=session, sleep=lambda _: None)


# ---------------------------------------------------------------------------
# HttpClient
# ---------------------------------------------------------------------------


def test_http_client_returns_bytes() -> None:
    session = FakeSession([FakeResponse(b'{"ok": true}', url="https://example.com/api")])
    response = http_client(session).get("https://example.com/api")
    assert response.status == 200
    assert response.content == b'{"ok": true}'
    assert response.url == "https://example.com/api"
    assert session.calls[0]["timeout"] == 5


def test_http_client_rejects_error_status() -> None:
    session = FakeSession([FakeResponse(b"blocked", status_code=403)])
    with pytest.raises(FetchError, match="HTTP 403"):
        http_client(session).get("https://example.com/api")


def test_http_client_wraps_timeout() -> None:
    session = FakeSession([requests.Timeout("too slow")])
    with pytest.raises(FetchError, match="failed"):
        http_client(session).get("https://example.com/api")


def test_http_client_enforces_size_limit() -> None:
    session = FakeSession([FakeResponse(b"x", headers={"Content-Length": str(20 * 1024 * 1024)})])
    with pytest.raises(FetchError, match="limit"):
        http_client(session).get("https://example.com/api")


def test_http_client_rate_limits_same_host() -> None:
    session = FakeSession(
        [
            FakeResponse(b"a", url="https://example.com/a"),
            FakeResponse(b"b", url="https://example.com/b"),
        ]
    )
    times = iter([0.0, 0.2])
    sleeps: list[float] = []
    settings = HttpSettings(min_delay_seconds=1.0)
    client = HttpClient(
        settings,
        session=session,
        sleep=sleeps.append,
        monotonic=lambda: next(times),
    )
    client.get("https://example.com/a")
    client.get("https://example.com/b")
    assert sleeps == pytest.approx([0.8])


def test_http_client_rate_limit_is_per_host() -> None:
    session = FakeSession(
        [
            FakeResponse(b"a", url="https://one.example/a"),
            FakeResponse(b"b", url="https://two.example/b"),
        ]
    )
    times = iter([0.0, 0.0, 0.1, 0.1])
    sleeps: list[float] = []
    client = HttpClient(
        HttpSettings(min_delay_seconds=1.0),
        session=session,
        sleep=sleeps.append,
        monotonic=lambda: next(times),
    )
    client.get("https://one.example/a")
    client.get("https://two.example/b")
    assert sleeps == []


# ---------------------------------------------------------------------------
# Remote OK
# ---------------------------------------------------------------------------


def test_parse_remoteok_fixture() -> None:
    listings = parse_remoteok_json(fixture_bytes("remoteok_sample.json"))
    assert len(listings) == 5
    first = listings[0]
    assert first.source == "remoteok"
    assert first.kind == "job"
    assert first.remote is True
    assert first.external_id == "1137603"
    assert first.title == "Competent Wind Turbine Technicians EU UK APAC 2027"
    assert first.company == "GEV Wind Power"
    assert first.url.startswith("https://remoteok.com/")
    assert first.posted_at is not None
    assert "python" in listings[3].tags
    assert listings[3].title == "Cloud Engineer Automation"

    paid = listings[4]
    assert paid.salary_min == Decimal("80000")
    assert paid.salary_max == Decimal("100000")
    assert paid.salary_currency == "USD"
    assert paid.salary_period == "year"


def test_remoteok_html_entities_are_cleaned() -> None:
    listings = parse_remoteok_json(fixture_bytes("remoteok_sample.json"))
    assert listings[2].title == "Client Support & Travel Booking Online"


def test_remoteok_invalid_json() -> None:
    with pytest.raises(ParseError, match="invalid JSON"):
        parse_remoteok_json(b"<html>blocked</html>")


def test_remoteok_wrong_schema() -> None:
    with pytest.raises(ParseError, match="JSON array"):
        parse_remoteok_json(b'{"jobs": []}')


def test_remoteok_skips_legal_and_missing_fields() -> None:
    payload = (
        b'[{"legal":"terms"},{"position":"Valid","url":"https://remoteok.com/job/1",'
        b'"id":"1","tags":null,"salary_min":0,"salary_max":0}]'
    )
    listings = parse_remoteok_json(payload)
    assert len(listings) == 1
    assert listings[0].title == "Valid"
    assert listings[0].tags == ()
    assert listings[0].salary_min is None


# ---------------------------------------------------------------------------
# Hacker News / Algolia
# ---------------------------------------------------------------------------


def test_parse_hn_fixture() -> None:
    listings = parse_hn_algolia_json(fixture_bytes("hn_sample.json"))
    assert len(listings) == 5
    first = listings[0]
    assert first.source == "hn"
    assert first.external_id == "49463361"
    assert first.company == "Adentris (YC P25)"
    assert first.url.startswith("https://www.ycombinator.com/")
    assert first.posted_at is not None
    assert first.tags == ("job",)
    assert listings[1].title == "Reflex (YC W23) Is Hiring Software Engineers – Python"


def test_hn_falls_back_to_item_url() -> None:
    payload = {
        "hits": [
            {
                "objectID": "123",
                "title": "Acme Is Hiring",
                "url": None,
                "created_at": "2026-01-02T03:04:05Z",
            }
        ]
    }
    listings = parse_hn_algolia_json(__import__("json").dumps(payload))
    assert listings[0].url == "https://news.ycombinator.com/item?id=123"
    assert listings[0].company == "Acme"


def test_hn_invalid_payload() -> None:
    with pytest.raises(ParseError, match="hits"):
        parse_hn_algolia_json(b"[]")


# ---------------------------------------------------------------------------
# Books to Scrape
# ---------------------------------------------------------------------------


def test_parse_books_fixture_and_next_page() -> None:
    page_url = "https://books.toscrape.com/catalogue/page-1.html"
    listings, next_url = parse_books_catalog_html(fixture_bytes("books_catalog.html"), page_url)
    assert len(listings) == 20
    first = listings[0]
    assert first.kind == "price"
    assert first.source == "books"
    assert first.title == "A Light in the Attic"
    assert first.price == Decimal("51.77")
    assert first.currency == "GBP"
    assert first.url == (
        "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
    )
    assert first.company == "Books to Scrape"
    assert next_url == "https://books.toscrape.com/catalogue/page-2.html"


def test_books_mojibake_price_is_normalized() -> None:
    listings, _ = parse_books_catalog_html(
        fixture_bytes("books_catalog.html"),
        "https://books.toscrape.com/catalogue/page-1.html",
    )
    assert listings[0].price == Decimal("51.77")
    assert listings[0].raw["price_raw"] == "£51.77"


def test_books_no_cards_is_parse_error() -> None:
    with pytest.raises(ParseError, match="no article"):
        parse_books_catalog_html(b"<html><body>maintenance</body></html>", "https://example.com")


# ---------------------------------------------------------------------------
# Generic HTML
# ---------------------------------------------------------------------------


def test_generic_html_detail_page() -> None:
    source = SourceSettings(
        format="generic_html",
        url="https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
        item_selector="div.product_main",
        title_selector="h1",
        price_selector=".price_color",
    )
    listings = parse_generic_html_page(
        fixture_bytes("books_detail.html"),
        source,
        source.url,
        default_currency="GBP",
    )
    assert len(listings) == 1
    assert listings[0].title == "A Light in the Attic"
    assert listings[0].price == Decimal("51.77")
    assert listings[0].currency == "GBP"
    assert listings[0].url == source.url


def test_generic_html_card_listing() -> None:
    page_url = "https://books.toscrape.com/catalogue/page-1.html"
    source = SourceSettings(
        format="generic_html",
        url=page_url,
        item_selector="article.product_pod",
        title_selector="h3 a",
        price_selector=".price_color",
        link_selector="h3 a",
    )
    listings = parse_generic_html_page(
        fixture_bytes("books_catalog.html"), source, page_url, default_currency="GBP"
    )
    assert len(listings) == 20
    assert listings[0].price == Decimal("51.77")


def test_generic_html_bad_selector() -> None:
    source = SourceSettings(
        format="generic_html",
        url="https://example.com",
        item_selector="div[",
        title_selector="h1",
        price_selector=".price",
    )
    with pytest.raises(ParseError, match="invalid item_selector"):
        parse_generic_html_page(b"<html></html>", source, source.url)


def test_generic_html_no_match() -> None:
    source = SourceSettings(
        format="generic_html",
        url="https://example.com",
        item_selector=".missing",
        title_selector="h1",
        price_selector=".price",
    )
    with pytest.raises(ParseError, match="no elements"):
        parse_generic_html_page(b"<html><body></body></html>", source, source.url)


# ---------------------------------------------------------------------------
# Fetch orchestration
# ---------------------------------------------------------------------------


def test_fetch_remoteok_dispatch() -> None:
    session = FakeSession(
        [FakeResponse(fixture_bytes("remoteok_sample.json"), url="https://remoteok.com/api")]
    )
    listings = fetch_listings(
        SourceSettings(format="remoteok_json", url="https://remoteok.com/api"),
        http_client(session),
    )
    assert len(listings) == 5
    assert canonical_url(session.calls[0]["url"]) == "https://remoteok.com/api"


def test_fetch_hn_passes_params() -> None:
    source = SourceSettings(
        format="hn_algolia_json",
        url="https://hn.algolia.com/api/v1/search_by_date",
        params={"tags": "job", "query": "python"},
    )
    session = FakeSession([FakeResponse(fixture_bytes("hn_sample.json"), url=source.url)])
    listings = fetch_listings(source, http_client(session))
    assert len(listings) == 5
    assert session.calls[0]["params"] == {"tags": "job", "query": "python"}


def test_fetch_books_paginates() -> None:
    page1 = FakeResponse(
        fixture_bytes("books_catalog.html"),
        url="https://books.toscrape.com/catalogue/page-1.html",
    )
    page2 = FakeResponse(
        fixture_bytes("books_catalog_page2.html"),
        url="https://books.toscrape.com/catalogue/page-2.html",
    )
    session = FakeSession([page1, page2])
    source = SourceSettings(
        format="books_toscrape",
        url="https://books.toscrape.com/catalogue/page-1.html",
        max_pages=2,
    )
    listings = fetch_listings(source, http_client(session))
    assert len(listings) == 40
    assert session.calls[1]["url"] == "https://books.toscrape.com/catalogue/page-2.html"


def test_fetch_generic_dispatch() -> None:
    source = SourceSettings(
        format="generic_html",
        url="https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
        item_selector="div.product_main",
        title_selector="h1",
        price_selector=".price_color",
    )
    session = FakeSession([FakeResponse(fixture_bytes("books_detail.html"), url=source.url)])
    listings = fetch_listings(source, http_client(session), default_currency="GBP")
    assert len(listings) == 1
