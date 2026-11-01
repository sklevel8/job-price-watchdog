"""Strict YAML configuration loading and validation.

Configuration mistakes in a scheduled workflow are expensive: nobody is
watching the terminal.  This module therefore rejects unknown keys and
cross-field mistakes early with precise error messages.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml

RULE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

SOURCE_FORMATS = {
    "remoteok_json",
    "hn_algolia_json",
    "books_toscrape",
    "generic_html",
}
JOB_FORMATS = {"remoteok_json", "hn_algolia_json"}
PRICE_FORMATS = {"books_toscrape", "generic_html"}
ALLOWED_SOURCE_KEYS = {
    "remoteok_json": {"format", "url"},
    "hn_algolia_json": {"format", "url", "params"},
    "books_toscrape": {"format", "url", "max_pages"},
    "generic_html": {
        "format",
        "url",
        "item_selector",
        "title_selector",
        "price_selector",
        "link_selector",
    },
}

RULE_KEYS = {"name", "kind", "enabled", "source", "match", "bootstrap"}
MATCH_KEYS = {
    "keywords",
    "keyword_mode",
    "exclude_keywords",
    "include_description",
    "remote_only",
    "max_age_days",
    "max_price",
    "min_salary",
    "require_salary",
    "currency",
}
HTTP_KEYS = {
    "timeout_seconds",
    "max_retries",
    "backoff_factor",
    "min_delay_seconds",
    "user_agent",
}
NOTIFICATION_KEYS = {
    "discord_webhook_env",
    "telegram_bot_token_env",
    "telegram_chat_id_env",
    "dry_run",
    "alert_burst_warning_threshold",
    "request_timeout_seconds",
}
TOP_LEVEL_KEYS = {"version", "database", "http", "notifications", "rules"}


class ConfigError(ValueError):
    """Raised when the YAML configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class HttpSettings:
    timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_factor: float = 0.5
    min_delay_seconds: float = 1.0
    user_agent: str = "portfolio-watchdog/1.0"


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    discord_webhook_env: str = "DISCORD_WEBHOOK_URL"
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"
    dry_run: bool = False
    alert_burst_warning_threshold: int = 25
    request_timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class SourceSettings:
    format: str
    url: str
    params: dict[str, Any] = field(default_factory=dict)
    max_pages: int = 1
    item_selector: str = ""
    title_selector: str = ""
    price_selector: str = ""
    link_selector: str = ""


@dataclass(frozen=True, slots=True)
class MatchSettings:
    keywords: tuple[str, ...] = ()
    keyword_mode: str = "any"
    exclude_keywords: tuple[str, ...] = ()
    include_description: bool = False
    remote_only: bool = False
    max_age_days: int | None = None
    max_price: Decimal | None = None
    min_salary: Decimal | None = None
    require_salary: bool = False
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class RuleSettings:
    name: str
    kind: Literal["job", "price"]
    enabled: bool
    source: SourceSettings
    match: MatchSettings = field(default_factory=MatchSettings)
    bootstrap: Literal["alert", "suppress"] = "alert"


@dataclass(frozen=True, slots=True)
class AppConfig:
    database: Path
    http: HttpSettings = field(default_factory=HttpSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    rules: tuple[RuleSettings, ...] = ()
    version: int = 1
    path: Path | None = None

    @property
    def enabled_rules(self) -> tuple[RuleSettings, ...]:
        return tuple(rule for rule in self.rules if rule.enabled)

    def rule(self, name: str) -> RuleSettings:
        for rule in self.rules:
            if rule.name == name:
                return rule
        raise KeyError(name)


# ---------------------------------------------------------------------------
# Scalar coercion helpers
# ---------------------------------------------------------------------------


def _require_mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{context} must be a mapping/object, got {type(value).__name__}")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        joined = ", ".join(repr(key) for key in unknown)
        raise ConfigError(f"{context} has unknown key(s): {joined}")


def _string(
    value: object,
    context: str,
    *,
    default: str | None = None,
    allow_empty: bool = False,
) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{context} must be a string")
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise ConfigError(f"{context} must not be empty")
    return cleaned


def _bool(value: object, context: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be true or false")
    return value


def _int(
    value: object,
    context: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{context} must be between {minimum} and {maximum}")
    return value


def _float(
    value: object,
    context: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{context} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigError(f"{context} must be between {minimum} and {maximum}")
    return result


def _decimal(
    value: object,
    context: str,
    *,
    minimum: Decimal = Decimal("0"),
) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"{context} must be a number")
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{context} must be a valid number") from exc
    if not result.is_finite() or result < minimum:
        raise ConfigError(f"{context} must be at least {minimum}")
    return result


def _string_list(value: object, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{context} must be a list of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        items.append(_string(item, f"{context}[{index}]"))
    return tuple(items)


def _validate_source_url(url: str, context: str) -> None:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ConfigError(f"{context} must be an absolute http(s) URL") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"{context} must be an absolute http(s) URL")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigError(f"{context} must use https (http is only allowed for localhost)")


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _parse_http(raw: object) -> HttpSettings:
    if raw is None:
        return HttpSettings()
    mapping = _require_mapping(raw, "http")
    _reject_unknown(mapping, HTTP_KEYS, "http")
    user_agent = _string(
        mapping.get("user_agent"),
        "http.user_agent",
        default="portfolio-watchdog/1.0",
    )
    if len(user_agent) < 5:
        raise ConfigError("http.user_agent must be at least 5 characters")
    return HttpSettings(
        timeout_seconds=_float(
            mapping.get("timeout_seconds"),
            "http.timeout_seconds",
            default=20.0,
            minimum=1.0,
            maximum=120.0,
        ),
        max_retries=_int(
            mapping.get("max_retries"),
            "http.max_retries",
            default=3,
            minimum=0,
            maximum=10,
        ),
        backoff_factor=_float(
            mapping.get("backoff_factor"),
            "http.backoff_factor",
            default=0.5,
            minimum=0.0,
            maximum=10.0,
        ),
        min_delay_seconds=_float(
            mapping.get("min_delay_seconds"),
            "http.min_delay_seconds",
            default=1.0,
            minimum=0.0,
            maximum=60.0,
        ),
        user_agent=user_agent,
    )


def _parse_notifications(raw: object) -> NotificationSettings:
    if raw is None:
        return NotificationSettings()
    mapping = _require_mapping(raw, "notifications")
    _reject_unknown(mapping, NOTIFICATION_KEYS, "notifications")
    env_values = {}
    for key, default in (
        ("discord_webhook_env", "DISCORD_WEBHOOK_URL"),
        ("telegram_bot_token_env", "TELEGRAM_BOT_TOKEN"),
        ("telegram_chat_id_env", "TELEGRAM_CHAT_ID"),
    ):
        env_name = _string(mapping.get(key), f"notifications.{key}", default=default)
        if not ENV_NAME_RE.match(env_name):
            raise ConfigError(f"notifications.{key} must be an uppercase environment variable name")
        env_values[key] = env_name
    return NotificationSettings(
        **env_values,
        dry_run=_bool(mapping.get("dry_run"), "notifications.dry_run", default=False),
        alert_burst_warning_threshold=_int(
            mapping.get("alert_burst_warning_threshold"),
            "notifications.alert_burst_warning_threshold",
            default=25,
            minimum=1,
            maximum=200,
        ),
        request_timeout_seconds=_float(
            mapping.get("request_timeout_seconds"),
            "notifications.request_timeout_seconds",
            default=10.0,
            minimum=1.0,
            maximum=60.0,
        ),
    )


def _parse_source(raw: object, context: str) -> SourceSettings:
    mapping = _require_mapping(raw, context)
    source_format = _string(mapping.get("format"), f"{context}.format")
    if source_format not in SOURCE_FORMATS:
        allowed = ", ".join(sorted(SOURCE_FORMATS))
        raise ConfigError(f"{context}.format must be one of: {allowed}")
    _reject_unknown(mapping, ALLOWED_SOURCE_KEYS[source_format], context)
    url = _string(mapping.get("url"), f"{context}.url")
    _validate_source_url(url, f"{context}.url")

    params_raw = mapping.get("params")
    params: dict[str, Any] = {}
    if params_raw is not None:
        params_mapping = _require_mapping(params_raw, f"{context}.params")
        for key, value in params_mapping.items():
            if not isinstance(key, str) or not key.strip():
                raise ConfigError(f"{context}.params keys must be non-empty strings")
            if isinstance(value, (dict, list)):
                raise ConfigError(f"{context}.params.{key} must be a scalar value")
            params[key.strip()] = value

    source = SourceSettings(
        format=source_format,
        url=url,
        params=params,
        max_pages=_int(
            mapping.get("max_pages"),
            f"{context}.max_pages",
            default=1,
            minimum=1,
            maximum=10,
        ),
        item_selector=_string(
            mapping.get("item_selector"),
            f"{context}.item_selector",
            default="",
            allow_empty=True,
        ),
        title_selector=_string(
            mapping.get("title_selector"),
            f"{context}.title_selector",
            default="",
            allow_empty=True,
        ),
        price_selector=_string(
            mapping.get("price_selector"),
            f"{context}.price_selector",
            default="",
            allow_empty=True,
        ),
        link_selector=_string(
            mapping.get("link_selector"),
            f"{context}.link_selector",
            default="",
            allow_empty=True,
        ),
    )
    if source_format == "generic_html":
        for field_name in ("item_selector", "title_selector", "price_selector"):
            if not getattr(source, field_name):
                raise ConfigError(f"{context}.{field_name} is required for generic_html")
    return source


def _parse_match(raw: object, context: str, kind: str) -> MatchSettings:
    if raw is None:
        mapping: Mapping[str, Any] = {}
    else:
        mapping = _require_mapping(raw, context)
    _reject_unknown(mapping, MATCH_KEYS, context)

    mode = _string(mapping.get("keyword_mode"), f"{context}.keyword_mode", default="any").lower()
    if mode not in {"any", "all"}:
        raise ConfigError(f"{context}.keyword_mode must be 'any' or 'all'")

    max_age = mapping.get("max_age_days")
    max_age_days = (
        None
        if max_age is None
        else _int(max_age, f"{context}.max_age_days", default=0, minimum=1, maximum=3650)
    )
    max_price = _decimal(mapping.get("max_price"), f"{context}.max_price")
    min_salary = _decimal(mapping.get("min_salary"), f"{context}.min_salary")
    currency = mapping.get("currency")
    currency_value = None
    if currency is not None:
        currency_value = _string(currency, f"{context}.currency").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency_value):
            raise ConfigError(f"{context}.currency must be a 3-letter ISO currency code")

    if kind == "price" and min_salary is not None:
        raise ConfigError(f"{context}.min_salary is only valid for job rules")
    if kind == "job" and max_price is not None:
        raise ConfigError(f"{context}.max_price is only valid for price rules")

    return MatchSettings(
        keywords=_string_list(mapping.get("keywords"), f"{context}.keywords"),
        keyword_mode=mode,
        exclude_keywords=_string_list(
            mapping.get("exclude_keywords"), f"{context}.exclude_keywords"
        ),
        include_description=_bool(
            mapping.get("include_description"),
            f"{context}.include_description",
            default=False,
        ),
        remote_only=_bool(mapping.get("remote_only"), f"{context}.remote_only", default=False),
        max_age_days=max_age_days,
        max_price=max_price,
        min_salary=min_salary,
        require_salary=_bool(
            mapping.get("require_salary"),
            f"{context}.require_salary",
            default=False,
        ),
        currency=currency_value,
    )


def _parse_rule(raw: object, index: int) -> RuleSettings:
    context = f"rules[{index}]"
    mapping = _require_mapping(raw, context)
    _reject_unknown(mapping, RULE_KEYS, context)

    name = _string(mapping.get("name"), f"{context}.name")
    if not RULE_NAME_RE.match(name):
        raise ConfigError(
            f"{context}.name must contain only lowercase letters, digits, '.', '_' or '-'"
        )
    kind = _string(mapping.get("kind"), f"{context}.kind").lower()
    if kind not in {"job", "price"}:
        raise ConfigError(f"{context}.kind must be 'job' or 'price'")

    source = _parse_source(mapping.get("source"), f"{context}.source")
    if kind == "job" and source.format not in JOB_FORMATS:
        raise ConfigError(f"{context}.source.format is not a job source")
    if kind == "price" and source.format not in PRICE_FORMATS:
        raise ConfigError(f"{context}.source.format is not a price source")

    bootstrap = _string(mapping.get("bootstrap"), f"{context}.bootstrap", default="alert").lower()
    if bootstrap not in {"alert", "suppress"}:
        raise ConfigError(f"{context}.bootstrap must be 'alert' or 'suppress'")

    return RuleSettings(
        name=name,
        kind=kind,
        enabled=_bool(mapping.get("enabled"), f"{context}.enabled", default=True),
        source=source,
        match=_parse_match(mapping.get("match"), f"{context}.match", kind),
        bootstrap=bootstrap,
    )


def _parse_rules(raw: object) -> tuple[RuleSettings, ...]:
    if raw is None:
        raise ConfigError("rules must be a non-empty list")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("rules must be a non-empty list")
    rules = tuple(_parse_rule(item, index) for index, item in enumerate(raw))
    names = [rule.name for rule in rules]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(f"duplicate rule name(s): {', '.join(duplicates)}")
    if not any(rule.enabled for rule in rules):
        raise ConfigError("at least one rule must be enabled")
    return rules


def load_config(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load and validate a watchdog YAML file.

    Relative database paths are resolved from the current working directory,
    which is the repository root for both local and GitHub Actions runs.
    """

    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read {config_path}: {exc}") from exc

    mapping = _require_mapping(raw, "configuration")
    _reject_unknown(mapping, TOP_LEVEL_KEYS, "configuration")
    version = _int(mapping.get("version"), "version", default=1, minimum=1, maximum=1)

    database_raw = _string(mapping.get("database"), "database", default="state/watchdog.db")
    environment = os.environ if environ is None else environ
    database_override = environment.get("WATCHDOG_DB_PATH")
    database = Path(database_override.strip() if database_override else database_raw)
    if database.exists() and database.is_dir():
        raise ConfigError(f"database path points to a directory: {database}")

    return AppConfig(
        version=version,
        database=database,
        http=_parse_http(mapping.get("http")),
        notifications=_parse_notifications(mapping.get("notifications")),
        rules=_parse_rules(mapping.get("rules")),
        path=config_path.resolve(),
    )
