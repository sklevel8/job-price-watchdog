"""HTTP extraction and source-specific parsers.

Parsers are pure functions over response bytes so they can be tested with
fixtures.  Only :func:`fetch_listings` performs network I/O.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import HttpSettings, SourceSettings
from models import Listing
from normalize import (
    canonical_url,
    clean_text,
    parse_datetime,
    parse_price,
    parse_salary,
    strip_html,
    truncate,
)

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_LISTINGS_PER_RULE = 2_000

_HIRING_RE = re.compile(r"^(?P<company>.+?)\s+(?:is\s+hiring|hiring)\b", re.I)


class FetchError(RuntimeError):
    """A source could not be fetched (network, HTTP status, or size limit)."""


class ParseError(RuntimeError):
    """A source responded but its payload did not match the expected schema."""


@dataclass(frozen=True, slots=True)
class FetchResponse:
    url: str
    status: int
    content: bytes
    headers: Mapping[str, str]

    @property
    def content_length(self) -> int:
        return len(self.content)


class HttpClient:
    """Small, polite HTTP GET client with retries and per-host rate limiting."""

    def __init__(
        self,
        settings: HttpSettings,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: dict[str, float] = {}
        if session is None:
            session = requests.Session()
            retry = Retry(
                total=settings.max_retries,
                connect=settings.max_retries,
                read=settings.max_retries,
                status=settings.max_retries,
                backoff_factor=settings.backoff_factor,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update(
                {
                    "User-Agent": settings.user_agent,
                    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.8",
                }
            )
        self.session = session

    def _wait_for_host(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        now = self._monotonic()
        last = self._last_request.get(host)
        if last is not None:
            remaining = self.settings.min_delay_seconds - (now - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = now

    def get(self, url: str, *, params: Mapping[str, Any] | None = None) -> FetchResponse:
        self._wait_for_host(url)
        try:
            response = self.session.get(
                url,
                params=dict(params or {}),
                timeout=self.settings.timeout_seconds,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise FetchError(f"GET {url} failed: {exc}") from exc

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_RESPONSE_BYTES:
                    raise FetchError(
                        f"GET {url} returned {content_length} bytes (limit {MAX_RESPONSE_BYTES})"
                    )
            except ValueError:
                pass
        content = response.content
        if isinstance(content, str):  # defensive: some test doubles return text
            content = content.encode("utf-8")
        if len(content) > MAX_RESPONSE_BYTES:
            raise FetchError(f"GET {url} exceeded the {MAX_RESPONSE_BYTES} byte limit")
        if response.status_code >= 400:
            raise FetchError(f"GET {url} returned HTTP {response.status_code}")

        return FetchResponse(
            url=str(response.url or url),
            status=int(response.status_code),
            content=content,
            headers=dict(response.headers),
        )

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def _load_json(payload: bytes | str, source: str) -> Any:
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8-sig")
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"{source} returned invalid JSON: {exc}") from exc


def parse_remoteok_json(payload: bytes | str) -> list[Listing]:
    """Parse the Remote OK public API list (the first element is a legal notice)."""

    data = _load_json(payload, "remoteok")
    if not isinstance(data, list):
        raise ParseError("remoteok payload must be a JSON array")

    listings: list[Listing] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict) or "legal" in item or not item.get("position"):
            continue
        title = clean_text(item.get("position"))
        url = canonical_url(
            item.get("url") or item.get("apply_url"), base_url="https://remoteok.com"
        )
        if not title or not url:
            continue
        external_id = clean_text(str(item.get("id") or item.get("slug") or url))
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)

        tags = tuple(
            dict.fromkeys(tag for value in (item.get("tags") or []) if (tag := clean_text(value)))
        )
        salary = parse_salary(
            minimum_raw=item.get("salary_min"),
            maximum_raw=item.get("salary_max"),
            default_currency="USD",
        )
        has_salary = salary.has_value
        listings.append(
            Listing(
                kind="job",
                source="remoteok",
                external_id=external_id,
                title=title,
                url=url,
                company=clean_text(item.get("company")) or None,
                location=clean_text(item.get("location")) or None,
                remote=True,
                description=strip_html(item.get("description")),
                tags=tags,
                posted_at=parse_datetime(item.get("date") or item.get("epoch")),
                salary_min=salary.minimum if has_salary else None,
                salary_max=salary.maximum if has_salary else None,
                salary_currency=salary.currency if has_salary else None,
                salary_period=salary.period if has_salary else None,
                raw={
                    "id": item.get("id"),
                    "slug": item.get("slug"),
                    "date": item.get("date"),
                    "tags": list(tags),
                    "location": item.get("location"),
                    "salary_min": item.get("salary_min"),
                    "salary_max": item.get("salary_max"),
                },
            )
        )
    return listings[:MAX_LISTINGS_PER_RULE]


def parse_hn_algolia_json(payload: bytes | str) -> list[Listing]:
    """Parse Hacker News job stories from the Algolia search API."""

    data = _load_json(payload, "hn_algolia")
    if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
        raise ParseError("hn_algolia payload must contain a 'hits' array")

    listings: list[Listing] = []
    seen: set[str] = set()
    for hit in data["hits"]:
        if not isinstance(hit, dict):
            continue
        title = clean_text(hit.get("title"))
        object_id = clean_text(str(hit.get("objectID") or ""))
        if not title or not object_id or object_id in seen:
            continue
        seen.add(object_id)
        hn_url = f"https://news.ycombinator.com/item?id={object_id}"
        url = canonical_url(hit.get("url")) or hn_url
        hiring_match = _HIRING_RE.match(title)
        company = clean_text(hiring_match.group("company")) if hiring_match else None
        listings.append(
            Listing(
                kind="job",
                source="hn",
                external_id=object_id,
                title=title,
                url=url,
                company=company,
                description=strip_html(hit.get("story_text")),
                tags=("job",),
                posted_at=parse_datetime(hit.get("created_at") or hit.get("created_at_i")),
                raw={
                    "objectID": object_id,
                    "author": hit.get("author"),
                    "created_at": hit.get("created_at"),
                    "hn_url": hn_url,
                },
            )
        )
    return listings[:MAX_LISTINGS_PER_RULE]


def parse_books_catalog_html(
    payload: bytes | str, page_url: str
) -> tuple[list[Listing], str | None]:
    """Parse one books.toscrape.com catalogue page and return the next URL."""

    soup = BeautifulSoup(payload, "html.parser")
    cards = soup.select("article.product_pod")
    if not cards:
        raise ParseError("books catalogue contained no article.product_pod cards")

    listings: list[Listing] = []
    for card in cards:
        link = card.select_one("h3 a")
        price_tag = card.select_one(".price_color")
        if link is None or price_tag is None:
            continue
        title = clean_text(link.get("title") or link.get_text(" ", strip=True))
        url = canonical_url(link.get("href"), base_url=page_url)
        parsed_price = parse_price(price_tag.get_text(" ", strip=True), default_currency="GBP")
        if not title or not url or parsed_price.value is None:
            continue

        rating_tag = card.select_one("p.star-rating")
        rating = None
        if rating_tag is not None:
            classes = [clean_text(value) for value in rating_tag.get("class", [])]
            rating = next((value for value in classes if value not in {"star-rating", ""}), None)
        availability = card.select_one("p.instock.availability")
        listings.append(
            Listing(
                kind="price",
                source="books",
                external_id=url,
                title=title,
                url=url,
                company="Books to Scrape",
                description="",
                tags=("books",),
                price=parsed_price.value,
                currency=parsed_price.currency or "GBP",
                raw={
                    "price_raw": parsed_price.raw,
                    "rating": rating,
                    "availability": clean_text(availability.get_text(" ", strip=True))
                    if availability is not None
                    else None,
                },
            )
        )

    if not listings:
        raise ParseError("books catalogue cards were present but none could be normalized")

    next_link = soup.select_one("li.next a")
    next_url = canonical_url(next_link.get("href"), base_url=page_url) if next_link else None
    return listings, next_url or None


def parse_generic_html_page(
    payload: bytes | str,
    source: SourceSettings,
    page_url: str,
    *,
    default_currency: str | None = None,
) -> list[Listing]:
    """Parse product cards from a page using configured CSS selectors."""

    soup = BeautifulSoup(payload, "html.parser")
    try:
        items = soup.select(source.item_selector)
    except Exception as exc:  # soupsieve raises several selector-specific errors
        raise ParseError(f"invalid item_selector {source.item_selector!r}: {exc}") from exc
    if not items:
        raise ParseError(
            f"generic_html found no elements matching {source.item_selector!r} on {page_url}"
        )

    source_name = urlsplit(page_url).hostname or "generic"
    listings: list[Listing] = []
    for item in items:
        title_tag = item.select_one(source.title_selector)
        price_tag = item.select_one(source.price_selector)
        if title_tag is None or price_tag is None:
            continue
        title = clean_text(title_tag.get("title") or title_tag.get_text(" ", strip=True))
        price_raw = price_tag.get_text(" ", strip=True)
        parsed_price = parse_price(price_raw, default_currency=default_currency)
        if not title or parsed_price.value is None:
            continue
        url = page_url
        if source.link_selector:
            link_tag = item.select_one(source.link_selector)
            if link_tag is not None:
                url = canonical_url(link_tag.get("href"), base_url=page_url) or page_url
        listings.append(
            Listing(
                kind="price",
                source=source_name,
                external_id=url if url != page_url else f"{url}|{title}",
                title=title,
                url=url,
                description=truncate(item.get_text(" ", strip=True), 2_000),
                tags=("price-watch",),
                price=parsed_price.value,
                currency=parsed_price.currency or default_currency,
                raw={"price_raw": price_raw},
            )
        )
    if not listings:
        raise ParseError(
            f"generic_html matched {len(items)} item(s) but none contained a valid title and price"
        )
    return listings[:MAX_LISTINGS_PER_RULE]


# ---------------------------------------------------------------------------
# Network orchestration
# ---------------------------------------------------------------------------


def fetch_listings(
    source: SourceSettings,
    client: HttpClient,
    *,
    default_currency: str | None = None,
) -> list[Listing]:
    """Fetch and parse all listings for a configured source."""

    if source.format == "remoteok_json":
        response = client.get(source.url)
        return parse_remoteok_json(response.content)

    if source.format == "hn_algolia_json":
        response = client.get(source.url, params=source.params)
        return parse_hn_algolia_json(response.content)

    if source.format == "books_toscrape":
        listings: list[Listing] = []
        page_url: str | None = source.url
        visited: set[str] = set()
        for _ in range(source.max_pages):
            if not page_url or page_url in visited:
                break
            visited.add(page_url)
            response = client.get(page_url)
            page_listings, page_url = parse_books_catalog_html(response.content, response.url)
            listings.extend(page_listings)
        return listings[:MAX_LISTINGS_PER_RULE]

    if source.format == "generic_html":
        response = client.get(source.url)
        return parse_generic_html_page(
            response.content,
            source,
            response.url,
            default_currency=default_currency,
        )

    raise ParseError(f"unsupported source format: {source.format}")
