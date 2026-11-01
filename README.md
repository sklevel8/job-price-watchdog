# Job / Price Watchdog

A serverless ETL watchdog written in Python. It periodically extracts remote
jobs and product prices, normalizes messy real-world data, stores history in
SQLite, and sends deduplicated alerts to Discord and/or Telegram. GitHub Actions
provides the free cron scheduler — no server required.

The project is intentionally built like a small production pipeline rather than
a demo script: strict configuration validation, retries with backoff, per-host
rate limiting, exact `Decimal` money handling, idempotent alerts, per-channel
retry queues, structured reports, and meaningful CI exit codes.

---

## What it does

```
GitHub Actions cron (every 6 hours)
        │
        ▼
  Extract ──► Normalize ──► Filter ──► Store ──► Decide ──► Alert
  HTTP/JSON      prices        keywords    SQLite     new job     Discord
  HTML           salaries      age/remote  history    new low     Telegram
                 dates         salary      ledger     threshold   console
        │
        ▼
  Portable ledger + JSON/Markdown reports + GitHub job summary
```

Included live sources:

| Source | Type | Format | Notes |
| --- | --- | --- | --- |
| [Remote OK](https://remoteok.com/api) | Jobs | JSON API | Salary fields, tags, remote flag. Attribution required by their API terms. |
| [HN Algolia](https://hn.algolia.com/api) | Jobs | JSON API | Searches Hacker News job stories by query/tag. |
| [Books to Scrape](https://books.toscrape.com/) | Prices | HTML | Public scraping sandbox; demonstrates catalogue pagination and mojibake repair. |
| Any static product page | Prices | HTML + CSS selectors | Configure `generic_html` with item/title/price selectors. |

---

## Engineering highlights

- **Strict YAML config** — unknown keys, unsafe URLs, duplicate rule names, and
  invalid field combinations fail fast with precise errors.
- **Resilient extraction** — timeouts, retries with exponential backoff,
  per-host minimum delay, response-size limits, and a 2,000-listing cap.
- **Real normalization** — currency symbols/codes, thousands separators,
  price ranges, salary annualization, ISO/RFC/relative dates, HTML entities,
  and cp1252→UTF-8 mojibake repair (needed by the Books to Scrape fixture).
- **Exact money** — prices and salaries are stored as decimal text, never
  binary floats.
- **Idempotent alerting** — a SQLite ledger keyed by
  `(listing, rule, trigger, channel)` prevents duplicate alerts across runs.
- **At-least-once delivery with retries** — failed Discord/Telegram deliveries
  are reconstructed from the database and retried on the next run, per channel.
- **Portable ledger** — a compact `*.ledger.json` file survives cache eviction
  and can be committed if you want stronger idempotency guarantees.
- **Safe dry runs** — a disposable database means `--dry-run` never marks real
  listings as seen or sends messages.
- **Fault isolation** — one broken source or webhook does not stop other rules;
  partial runs still export state and reports.
- **Observability** — human or JSON logs, per-rule counters, JSON reports,
  Markdown summaries, and GitHub Actions artifacts.
- **Tested** — 225 offline tests, 88% branch coverage, Ruff lint/format, and
  Actionlint-validated workflows.

---

## Project structure

```
job-price-watchdog/
├── .github/
│   ├── dependabot.yml
│   └── workflows/
│       ├── ci.yml              # lint, format, tests, coverage on 3.11 + 3.13
│       └── watchdog.yml        # scheduled ETL + artifacts + state cache
├── config/
│   └── config.yaml             # sources, filters, thresholds
├── reports/                    # generated JSON/Markdown reports (ignored)
├── src/
│   ├── config.py               # strict YAML loading/validation
│   ├── database.py             # SQLite schema, history, alert ledger
│   ├── main.py                 # CLI entry point
│   ├── models.py               # dataclasses, fingerprints, run reports
│   ├── normalize.py            # text/price/salary/date/keyword helpers
│   ├── notifier.py             # Discord, Telegram, console delivery
│   ├── pipeline.py             # ETL orchestration, matching, retries
│   └── scraper.py              # HTTP client and source parsers
├── state/
│   ├── README.md               # what is stored and what is gitignored
│   └── watchdog.db             # created at runtime (ignored)
├── tests/                      # offline unit/integration tests + fixtures
├── .env.example
├── pyproject.toml              # pytest, coverage, ruff configuration
├── requirements.txt
├── requirements-dev.txt
├── LICENSE
└── README.md
```

The modules use a flat `src/` layout (`from config import ...`) and are run from
the repository root:

```bash
python src/main.py
```

---

## Quick start

### 1. Prerequisites

- Python **3.11+**
- pip
- No database server: SQLite is part of Python.

### 2. Install

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
cp .env.example .env
```

`requirements-dev.txt` includes the runtime dependencies plus pytest, coverage,
and Ruff.

### 3. Preview against live data

```powershell
python src/main.py --dry-run --json-report reports/latest.json
```

A dry run:

- fetches the real sources,
- uses a temporary database,
- never sends Discord/Telegram messages,
- never marks real listings or alerts as seen,
- still prints the full report and can write a JSON artifact.

### 4. Send for real

Add a webhook to `.env` (see below), then run:

```powershell
python src/main.py
```

With no credentials configured, alerts are printed to the log and recorded in
the database so they are not repeated every run.

---

## Alert setup

### Discord

1. In Discord: **Server Settings → Integrations → Webhooks → New Webhook**.
2. Choose a channel, create the webhook, and copy its URL.
3. Add it to `.env`:

```env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/your-token
```

Discord is sent as rich embeds, up to 10 per message, with `@everyone`/role
mentions disabled. HTTP 429 responses are retried using `Retry-After`.

### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Add the bot to your target chat/channel.
3. Get the chat ID (for example via the Bot API `getUpdates` endpoint).
4. Add both values:

```env
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=-1001234567890
```

Telegram messages use HTML parse mode, escape untrusted titles/companies, and
send one message per alert.

Both channels may be configured at once. Each has its own ledger, so if Discord
succeeds and Telegram fails, only Telegram is retried next run.

### Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook. Invalid/non-Discord URLs are ignored. |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token; requires the chat ID too. |
| `TELEGRAM_CHAT_ID` | No | Target Telegram chat/channel. |
| `WATCHDOG_DB_PATH` | No | Overrides the database path from YAML. |
| `WATCHDOG_DRY_RUN` | No | `1/true/yes/on` forces dry-run mode. |
| `WATCHDOG_LOG_LEVEL` | No | Default CLI log level (`INFO`). |

Secrets belong in `.env` locally and in **GitHub repository secrets** in CI.
Never put webhook URLs or tokens in `config/config.yaml`.

---

## Configuration reference

The shipped `config/config.yaml` is a complete, working example. Configuration
is strict: unknown keys are rejected instead of being silently ignored.

### Top level

| Key | Default | Description |
| --- | --- | --- |
| `version` | `1` | Schema version. Only `1` is supported. |
| `database` | `state/watchdog.db` | SQLite path. Parent directories are created. |
| `http` | built-ins | HTTP client settings. |
| `notifications` | built-ins | Channel env-var names and delivery settings. |
| `rules` | required | Non-empty list of job/price watch rules. |

### `http`

| Key | Default | Range | Description |
| --- | --- | --- | --- |
| `timeout_seconds` | `20` | 1–120 | Per-request timeout. |
| `max_retries` | `3` | 0–10 | Transport/status retries via urllib3. |
| `backoff_factor` | `0.5` | 0–10 | Exponential backoff factor. |
| `min_delay_seconds` | `1.0` | 0–60 | Minimum delay between requests to the same host. |
| `user_agent` | `portfolio-watchdog/1.0` | ≥5 chars | Sent on every request. |

`Retry-After` headers are respected. Responses larger than 10 MB are rejected.

### `notifications`

| Key | Default | Description |
| --- | --- | --- |
| `discord_webhook_env` | `DISCORD_WEBHOOK_URL` | Env var holding the webhook. |
| `telegram_bot_token_env` | `TELEGRAM_BOT_TOKEN` | Env var holding the bot token. |
| `telegram_chat_id_env` | `TELEGRAM_CHAT_ID` | Env var holding the chat ID. |
| `dry_run` | `false` | Permanently preview without sending. |
| `alert_burst_warning_threshold` | `25` | Warn when a run produces more alerts than this. Alerts are **never dropped**. |
| `request_timeout_seconds` | `10` | Webhook POST timeout (1–60). |

### Rules

Every rule has these keys:

| Key | Required | Description |
| --- | --- | --- |
| `name` | Yes | Unique ID: lowercase letters/digits/`.`/`_`/`-`, max 64 chars. |
| `kind` | Yes | `job` or `price`. Must match the source format family. |
| `enabled` | No (default `true`) | Disable without deleting the rule. At least one rule must remain enabled. |
| `source` | Yes | Where and how to extract data. |
| `match` | No | Filters and thresholds. |
| `bootstrap` | No (default `alert`) | `alert` or `suppress` on the rule's first run. |

Rule names are also used in the alert ledger, so renaming a rule makes it look
like a new subscription.

### `source`

| `format` | Kind | Allowed keys |
| --- | --- | --- |
| `remoteok_json` | job | `format`, `url` |
| `hn_algolia_json` | job | `format`, `url`, `params` |
| `books_toscrape` | price | `format`, `url`, `max_pages` |
| `generic_html` | price | `format`, `url`, `item_selector`, `title_selector`, `price_selector`, `link_selector` |

- Only absolute `https://` URLs are accepted. Plain `http://` is allowed only
  for `localhost`/`127.0.0.1`/`::1` during local testing.
- `params` must be a flat mapping of scalar query parameters.
- `max_pages` is 1–10 and only applies to `books_toscrape`.
- `generic_html` requires `item_selector`, `title_selector`, and
  `price_selector`; `link_selector` is optional.

### `match`

| Key | Applies to | Description |
| --- | --- | --- |
| `keywords` | both | Case-insensitive keywords matched against title/company/tags. |
| `keyword_mode` | both | `any` (default) or `all`. Empty keywords match everything. |
| `exclude_keywords` | both | Any hit rejects the listing. |
| `include_description` | job | Also search the description (off by default to reduce noise). |
| `remote_only` | job | Reject listings explicitly marked non-remote. Unknown stays eligible. |
| `max_age_days` | job | Reject older postings (1–3650). Undated listings stay eligible. |
| `min_salary` | job | Minimum **annualized** salary; undated/unknown salary behavior controlled by `require_salary`. |
| `require_salary` | job | Reject listings with no parseable salary. |
| `max_price` | price | Alert when price is at or below this value. |
| `currency` | both | ISO-4217 code. Used as the default for generic HTML and as a mismatch filter when a listing has a currency. |

Salaries are annualized with standard full-time assumptions: 2,080 hours/year,
52 weeks/year, 12 months/year, 260 days/year.

### Example: job rule

```yaml
- name: remote-tech-jobs
  kind: job
  enabled: true
  source:
    format: remoteok_json
    url: https://remoteok.com/api
  match:
    keywords: [python, backend, platform, devops, sre, "site reliability"]
    keyword_mode: any
    exclude_keywords: [intern, internship, sales, recruiter]
    include_description: false
    remote_only: true
    max_age_days: 14
  bootstrap: alert
```

### Example: price rule with generic selectors

```yaml
- name: my-product
  kind: price
  enabled: true
  source:
    format: generic_html
    url: https://example.com/products/widget
    item_selector: "div.product_main"
    title_selector: "h1"
    price_selector: ".price_color"
  match:
    max_price: 49.99
    currency: GBP
  bootstrap: suppress
```

For card-based catalogues, point `item_selector` at each card, `title_selector`
at the title inside the card, `price_selector` at the price, and optionally
`link_selector` at the product link. Selector failures are reported per rule,
not treated as empty success.

### Bootstrap behavior

| Value | First run | Later runs |
| --- | --- | --- |
| `alert` | Alert on every current match. | Only new matches/new lows. |
| `suppress` | Store and remember current matches without alerting. | Alert only on subsequent new matches/new lows. |

Suppressed bootstrap alerts are recorded in the ledger with status
`suppressed`, so they cannot fire later. Use `suppress` when adding a rule to a
noisy source so the first run does not flood your channel.

---

## Alert semantics

### Jobs

A job rule alerts once per `(listing, rule)` when a listing first matches that
rule. Because the ledger is per rule, two rules may independently alert on the
same listing; adding a new rule later can alert existing listings that the new
rule has never reported.

A job is not re-alerted merely because it still appears in the feed. If a
delivery failed, the alert is retried under the same key rather than generated
again.

### Prices

Every observed price is stored in `price_history` (deduplicated when unchanged).
A price alert fires when **both** are true:

1. the current price is at or below the rule's `max_price`;
2. it is a new all-time low for that listing.

The first observation below target is reported as `below_target`; later drops
are `new_low`. Prices above target are still tracked so the history is complete.
Multiple price rules share one pre-run baseline, so each rule can independently
alert on the same price drop.

Ranges such as `£10.00 - £20.00` use the lower bound for threshold checks.

### Delivery guarantees

- Delivery is **at least once**, deduplicated per channel.
- Discord messages batch up to 10 embeds; Telegram sends one alert per message.
- HTTP 429 is retried once within the run using `Retry-After` (capped at 15s).
- Other failures are persisted as `failed` and retried on the next run for the
  affected channel only.
- A large burst only logs a warning. No alert is silently discarded.
- Console fallback records alerts so they do not repeat every run.

---

## State and idempotency

### SQLite

Default path: `state/watchdog.db` (override with `database:` or
`WATCHDOG_DB_PATH`). Tables:

- `runs` — run audit trail and totals.
- `rule_state` — first/last attempt per rule, successful-bootstrap marker, run count.
- `listings` — normalized job/product records and last-seen timestamps.
- `price_history` — price observations with exact decimal text.
- `alerts` — per-channel delivery ledger and suppressed bootstrap records.

The database uses WAL mode, a busy timeout, and parameterized SQL throughout.

### Portable ledger

After every run the pipeline exports `<db-stem>.ledger.json` next to the
database (for example `state/watchdog.ledger.json`). It contains terminal alert
rows (`sent`, `console`, and `suppressed`) plus historical price minima. On
startup the ledger is merged back into SQLite, which protects alert idempotency
if the Actions cache was evicted.

The ledger is deliberately small enough to commit. `.gitignore` ignores the
database but **allows** `state/*.ledger.json`. If you choose to commit it, the
workflow's artifact gives you the latest copy; download it and commit it
manually, or add a write-enabled workflow step if your repository policy allows
automated commits. The shipped workflow keeps `contents: read` for safety.

Without a committed ledger, state is best-effort. If both the Actions cache and
all artifacts are lost, previously alerted listings may alert again. That is the
expected trade-off of a serverless, database-free deployment.

### Dry runs

`--dry-run` creates a temporary database and imports the real ledger read-only
for preview purposes. It never writes the configured database, never updates
real price history, and never posts webhooks. Use it before enabling a new rule.

---

## GitHub Actions deployment

### 1. Push the repository

```bash
git init
git add .
git commit -m "feat: serverless job/price watchdog ETL"
git branch -M main
git remote add origin https://github.com/YOUR_HANDLE/job-price-watchdog.git
git push -u origin main
```

### 2. Add secrets

In **Settings → Secrets and variables → Actions**, add any of:

- `DISCORD_WEBHOOK_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Missing secrets are fine: the pipeline falls back to console logging.

### 3. Run it

- Open **Actions → Watchdog ETL → Run workflow**.
- Leave `dry_run` checked for a safe preview, or uncheck it to send alerts.
- Scheduled runs start automatically every 6 hours (`0 */6 * * *`, UTC).

Schedules can start a few minutes late under GitHub load. GitHub may disable
scheduled workflows after prolonged repository inactivity; re-enable them in
the Actions tab if that happens.

### What the workflow does

- Restores `state/` from the Actions cache using a unique key per run and a
  `watchdog-state-` restore prefix.
- Installs runtime dependencies on Python 3.11.
- Validates the YAML before running.
- Runs the ETL with JSON logs, writes `reports/latest.json`, and appends a
  Markdown table to the GitHub job summary.
- Saves the updated state cache even when some rules fail (except dry runs).
- Uploads reports and the portable ledger as a 14-day artifact.
- Uses `concurrency: watchdog-etl` so two runs cannot update state at once.
- Has a 20-minute timeout and read-only repository permissions.

The ETL step exits with the pipeline's status, so failed/partial runs are visible
in the Actions UI and in the job summary.

### CI workflow

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests, and manual
dispatch. It tests Python 3.11 and 3.13, runs Ruff lint/format checks, validates
the sample config, and runs the full suite with an 85% coverage floor. Dependabot
checks pip and GitHub Actions updates weekly.

---

## CLI reference

```text
python src/main.py [options]
```

| Option | Description |
| --- | --- |
| `--config PATH` | YAML config (default `config/config.yaml`). |
| `--db PATH` | Override the SQLite path. |
| `--rule NAME` | Run only this rule; repeat for multiple rules. |
| `--dry-run` | Temporary DB, no messages, no real state changes. |
| `--json-report PATH` | Write the machine-readable report. |
| `--summary-file PATH` | Append a Markdown summary (use `$GITHUB_STEP_SUMMARY`). |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `--log-format FORMAT` | `text` (default) or `json`. |
| `--version` | Print the version. |

Examples:

```powershell
# Preview one rule only
python src/main.py --dry-run --rule remote-tech-jobs

# Use a separate database for an experiment
python src/main.py --db state/experiment.db --dry-run

# JSON logs + both report formats
python src/main.py --log-format json --json-report reports/latest.json --summary-file reports/summary.md
```

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All selected rules succeeded. |
| `1` | Partial success: at least one rule or delivery failed. |
| `2` | Configuration error, unknown rule, database error, or fatal failure. |
| `130` | Interrupted with Ctrl+C. |

On Windows, set UTF-8 for consoles that cannot render emoji:

```powershell
$env:PYTHONUTF8 = "1"
```

`main.py` also reconfigures stdout to UTF-8 with replacement where supported.

---

## Testing and quality

All tests are offline: real source fixtures live in `tests/fixtures/`, and HTTP
calls are replaced with deterministic fakes.

```powershell
# Full suite
.\.venv\Scripts\python.exe -m pytest -q

# Coverage (85% floor configured in pyproject.toml)
.\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing

# Lint and formatting
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
```

Current status:

- **225 tests passing**
- **~88% branch coverage**
- Ruff clean, formatted
- Both workflows validated with Actionlint 1.7.12

The suite covers normalization edge cases, strict config failures, all parsers
and pagination, SQLite round-trips/ledger imports, Discord/Telegram retries and
escaping, pipeline matching/price logic/bootstrap/retries, and the CLI's exit
codes, artifacts, and dry-run isolation.

---

## Design notes

### Why not pandas?

The blueprint lists pandas as a recommended data tool, but this pipeline does
not need a DataFrame. Records are small, typed dataclasses; SQLite performs the
history queries; and `Decimal` provides exact money arithmetic. Pulling pandas
into a cron job would add tens of megabytes and a second representation of the
data without improving correctness. If you later want bulk analytics, the
SQLite tables are easy to load with pandas in a separate notebook or script.

### Why fingerprints instead of URLs?

Listing IDs are preferred when a source provides them. When it does not, the
canonical URL + title + company form a stable identity. URLs are stripped of
fragments and tracking parameters so the same listing does not look new after a
source appends `utm_*` values.

### Why both SQLite and a JSON ledger?

SQLite is excellent for history and joins but awkward to persist in a stateless
runner. The ledger is a small, human-inspectable snapshot of the only two things
needed for idempotency: terminal alert rows and historical price minima. SQLite
is cached between runs; the ledger is the fallback and audit artifact.

---

## Extending the watchdog

### Add another job/price source

1. Add the format name to `SOURCE_FORMATS`, `JOB_FORMATS` or `PRICE_FORMATS`,
   and `ALLOWED_SOURCE_KEYS` in `src/config.py`.
2. Write a pure parser in `src/scraper.py` that returns `Listing` objects and
   raise `ParseError` on schema mismatches.
3. Dispatch to it from `fetch_listings`.
4. Add a fixture plus tests for happy path, malformed payloads, and pagination.
5. Document the new format in this README.

Parsers must not perform network I/O; that keeps them fast and deterministic to
test.

### Change the schedule

Edit the cron expression in `.github/workflows/watchdog.yml`. GitHub cron uses
UTC. Example: every 2 hours is `0 */2 * * *`; weekdays at 08:00 UTC is
`0 8 * * 1-5`.

### Run against a real product page

Confirm the site's terms and `robots.txt` permit the access you need. Set
`format: generic_html`, point `url` at the page, and use browser dev tools to
choose stable selectors. Prefer pages that render prices server-side; this
project intentionally does not use Playwright, so client-rendered prices are not
available.

---

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| No alerts at all | Check keywords, `include_description`, `remote_only`, `max_age_days`, and `bootstrap: suppress`. A listing already present in the ledger is intentionally silent. |
| Discord `HTTP 401/404` | Webhook was revoked or copied incorrectly. Recreate it and update the secret. |
| Telegram `chat not found` | Add the bot to the chat and use the correct numeric chat ID (negative for channels/groups). |
| Telegram `HTTP 400` with HTML error | A title may contain unsupported markup; the notifier escapes HTML, so report a repro if it persists. |
| HN rule matches 0 | The query has no recent hits, or `max_age_days` is too strict. Widen the window or change `query`. |
| Books prices look like `Â£51.77` | The source contains genuine mojibake; `clean_text` repairs cp1252→UTF-8 before parsing. |
| `unknown key` config error | Remove the typo; unknown keys are rejected by design. |
| `url must use https` | Use HTTPS, or `http://localhost` for local tests. |
| State resets in Actions | The cache was evicted or the run was a dry run. Check the workflow's restore/save logs and artifacts. |
| Partial run exit code 1 | Inspect the job summary and JSON report; one source or delivery failed while others succeeded. |
| Need to re-alert a listing | Delete its row from `alerts` (and retry keys) or use a new rule name. |

---

## Security, privacy, and etiquette

- Secrets are never written to config or logs. Notifier errors include HTTP
  status and safe API messages, not webhook URLs or bot tokens.
- Discord webhook URLs are validated against Discord hosts; Telegram tokens are
  format-checked. Invalid credentials fall back to console instead of crashing.
- Telegram text is HTML-escaped. Discord payloads are JSON, and mention parsing
  is disabled.
- Extraction limits response size (10 MB), listing count (2,000/rule), request
  timeouts, retries, and per-host request rates.
- The config is trusted input. Do not let untrusted users edit source URLs or
  selectors, or the workflow could be used for SSRF-style requests.
- Respect each site's terms and `robots.txt`. The included price target is a
  purpose-built scraping sandbox. Remote OK's API terms require linking back to
  the listing and mentioning Remote OK as the source; alerts link to the
  original listing and label the source as **Remote OK**.
- SQLite files and reports may contain public job/product data. Review them
  before sharing artifacts from a private repository.

---

## License

MIT — see [LICENSE](LICENSE).

Remote OK is a trademark of its owner. This project is not affiliated with
Remote OK, Hacker News, Y Combinator, or Books to Scrape.
