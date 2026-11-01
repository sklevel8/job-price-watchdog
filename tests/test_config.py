"""Tests for strict configuration validation."""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from config import AppConfig, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def base_data() -> dict:
    return {
        "version": 1,
        "database": "state/test.db",
        "rules": [
            {
                "name": "remote-jobs",
                "kind": "job",
                "source": {"format": "remoteok_json", "url": "https://remoteok.com/api"},
                "match": {"keywords": ["python", "backend"]},
            }
        ],
    }


def write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_load_minimal_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, base_data()))
    assert isinstance(config, AppConfig)
    assert config.version == 1
    assert config.database == Path("state/test.db")
    assert config.http.timeout_seconds == 20.0
    assert config.notifications.alert_burst_warning_threshold == 25
    assert len(config.rules) == 1
    assert len(config.enabled_rules) == 1
    assert config.rules[0].bootstrap == "alert"
    assert config.rules[0].match.keywords == ("python", "backend")
    assert config.rules[0].match.keyword_mode == "any"


def test_full_http_and_notifications(tmp_path: Path) -> None:
    data = base_data()
    data["http"] = {
        "timeout_seconds": 30,
        "max_retries": 5,
        "backoff_factor": 1.5,
        "min_delay_seconds": 2,
        "user_agent": "watchdog-test/2.0",
    }
    data["notifications"] = {
        "discord_webhook_env": "MY_DISCORD",
        "telegram_bot_token_env": "MY_TG_TOKEN",
        "telegram_chat_id_env": "MY_TG_CHAT",
        "dry_run": True,
        "alert_burst_warning_threshold": 10,
        "request_timeout_seconds": 15,
    }
    config = load_config(write_config(tmp_path, data))
    assert config.http.max_retries == 5
    assert config.http.user_agent == "watchdog-test/2.0"
    assert config.notifications.discord_webhook_env == "MY_DISCORD"
    assert config.notifications.dry_run is True
    assert config.notifications.request_timeout_seconds == 15.0


def test_database_env_override(tmp_path: Path) -> None:
    path = write_config(tmp_path, base_data())
    config = load_config(path, environ={"WATCHDOG_DB_PATH": "custom/override.db"})
    assert config.database == Path("custom/override.db")


def test_repository_sample_config_is_valid() -> None:
    config = load_config(REPO_ROOT / "config" / "config.yaml")
    assert {rule.name for rule in config.rules} == {
        "remote-tech-jobs",
        "hn-who-is-hiring",
        "books-price-watch",
        "example-generic-product",
    }
    assert len(config.enabled_rules) == 3


def test_rule_lookup(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, base_data()))
    assert config.rule("remote-jobs").kind == "job"
    with pytest.raises(KeyError):
        config.rule("missing")


def test_hn_params_and_generic_selectors(tmp_path: Path) -> None:
    data = base_data()
    data["rules"] = [
        {
            "name": "hn",
            "kind": "job",
            "source": {
                "format": "hn_algolia_json",
                "url": "https://hn.algolia.com/api/v1/search_by_date",
                "params": {"tags": "job", "query": "python", "hitsPerPage": 50},
            },
            "match": {"keywords": []},
        },
        {
            "name": "product",
            "kind": "price",
            "source": {
                "format": "generic_html",
                "url": "https://example.com/product",
                "item_selector": ".product",
                "title_selector": "h1",
                "price_selector": ".price",
            },
            "match": {"max_price": 20.5, "currency": "gbp"},
        },
    ]
    config = load_config(write_config(tmp_path, data))
    assert config.rules[0].source.params["hitsPerPage"] == 50
    assert config.rules[1].match.max_price == Decimal("20.5")
    assert config.rules[1].match.currency == "GBP"


def test_enabled_false_is_allowed_with_another_rule(tmp_path: Path) -> None:
    data = base_data()
    data["rules"][0]["enabled"] = False
    data["rules"].append(
        {
            "name": "second",
            "kind": "job",
            "source": {"format": "remoteok_json", "url": "https://remoteok.com/api"},
            "match": {},
        }
    )
    config = load_config(write_config(tmp_path, data))
    assert len(config.enabled_rules) == 1


def test_all_rules_disabled(tmp_path: Path) -> None:
    data = base_data()
    data["rules"][0]["enabled"] = False
    with pytest.raises(ConfigError, match="at least one rule must be enabled"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize(
    "mutator",
    [
        lambda data: data.update({"surprise": True}),
        lambda data: data.update({"version": 2}),
        lambda data: data.update({"rules": []}),
        lambda data: data["rules"][0].update({"name": "Bad Name"}),
        lambda data: data["rules"][0].update({"kind": "video"}),
        lambda data: data["rules"][0].update({"bootstrap": "maybe"}),
        lambda data: data["rules"][0].update({"unknown": 1}),
        lambda data: data["rules"][0]["source"].update({"unknown": 1}),
        lambda data: data["rules"][0]["source"].update({"url": "ftp://example.com"}),
        lambda data: data["rules"][0]["source"].update({"url": "http://example.com"}),
        lambda data: data["rules"][0]["source"].update({"url": "http://["}),
        lambda data: data["rules"][0]["match"].update({"keyword_mode": "some"}),
        lambda data: data["rules"][0]["match"].update({"max_age_days": 0}),
        lambda data: data["rules"][0]["match"].update({"max_price": 10}),
        lambda data: data["rules"][0]["match"].update({"keywords": "python"}),
        lambda data: data["rules"][0]["match"].update({"currency": "US"}),
    ],
)
def test_invalid_configurations(tmp_path: Path, mutator) -> None:
    data = base_data()
    mutator(data)
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


def test_duplicate_rule_names(tmp_path: Path) -> None:
    data = base_data()
    data["rules"].append(copy.deepcopy(data["rules"][0]))
    with pytest.raises(ConfigError, match="duplicate rule name"):
        load_config(write_config(tmp_path, data))


def test_duplicate_names_one_disabled_still_rejected(tmp_path: Path) -> None:
    data = base_data()
    duplicate = copy.deepcopy(data["rules"][0])
    duplicate["enabled"] = False
    data["rules"].append(duplicate)
    with pytest.raises(ConfigError, match="duplicate rule name"):
        load_config(write_config(tmp_path, data))


def test_job_rule_rejects_price_source(tmp_path: Path) -> None:
    data = base_data()
    data["rules"][0]["source"] = {
        "format": "books_toscrape",
        "url": "https://books.toscrape.com/",
    }
    with pytest.raises(ConfigError, match="not a job source"):
        load_config(write_config(tmp_path, data))


def test_price_rule_rejects_job_source(tmp_path: Path) -> None:
    data = base_data()
    data["rules"][0]["kind"] = "price"
    with pytest.raises(ConfigError, match="not a price source"):
        load_config(write_config(tmp_path, data))


def test_price_rule_rejects_min_salary(tmp_path: Path) -> None:
    data = base_data()
    data["rules"][0] = {
        "name": "product",
        "kind": "price",
        "source": {
            "format": "books_toscrape",
            "url": "https://books.toscrape.com/",
        },
        "match": {"max_price": 20, "min_salary": 1000},
    }
    with pytest.raises(ConfigError, match="min_salary is only valid for job rules"):
        load_config(write_config(tmp_path, data))


def test_generic_html_requires_selectors(tmp_path: Path) -> None:
    data = base_data()
    data["rules"][0] = {
        "name": "product",
        "kind": "price",
        "source": {
            "format": "generic_html",
            "url": "https://example.com/product",
            "item_selector": ".product",
        },
        "match": {},
    }
    with pytest.raises(ConfigError, match="title_selector is required"):
        load_config(write_config(tmp_path, data))


def test_localhost_http_is_allowed(tmp_path: Path) -> None:
    data = base_data()
    data["rules"][0]["source"]["url"] = "http://localhost:8000/api"
    config = load_config(write_config(tmp_path, data))
    assert config.rules[0].source.url == "http://localhost:8000/api"


def test_invalid_env_name(tmp_path: Path) -> None:
    data = base_data()
    data["notifications"] = {"discord_webhook_env": "not a name"}
    with pytest.raises(ConfigError, match="uppercase environment variable"):
        load_config(write_config(tmp_path, data))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("rules: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not read"):
        load_config(path)


def test_database_cannot_be_directory(tmp_path: Path) -> None:
    data = base_data()
    data["database"] = str(tmp_path)
    with pytest.raises(ConfigError, match="directory"):
        load_config(write_config(tmp_path, data))


def test_http_defaults_do_not_crash(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, base_data()))
    assert config.http.user_agent == "portfolio-watchdog/1.0"
    assert config.notifications.discord_webhook_env == "DISCORD_WEBHOOK_URL"
