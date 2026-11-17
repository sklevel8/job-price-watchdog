# Job / Price Watchdog

A serverless ETL watchdog in Python. Every six hours it pulls South African tech jobs and
rand-denominated product prices, normalises the mess, stores history in SQLite, and sends only
**new** matches to Discord and/or Telegram. GitHub Actions is the scheduler; there is no server.

**Live dashboard demo:** https://sklevel8-job-price-watchdog.netlify.app
**Stack:** Python 3.11+ · GitHub Actions · SQLite · Discord/Telegram · optional Flask UI

---

## Why this exists

Two chores are identical in shape and both are done badly by hand: watching job boards, and
watching prices. You refresh a page, you miss something, you check again the next day and cannot
remember whether the listing is new or one you already rejected.

A cron job should solve this. What makes it a real project rather than a script is everything a
naive version gets wrong:

- **Duplicates.** Running every six hours against the same listings would alert on the same job
  forever. Alerts need to be idempotent across runs, across retries, and across a CI runner that
  starts with a fresh filesystem.
- **Money.** Prices and salaries must not be added, compared or summed as binary floats.
- **Upstreams fail.** Job APIs time out, product pages change markup, and hosts rate limit. One
  broken source must not take the run down.
- **Cost.** GitHub Actions minutes and free API tiers are finite, so state has to be cheap to
  persist and the run has to be resumable.

The project is deliberately shaped like a small production pipeline rather than a demo script:
strict configuration validation, retries with backoff, per-host pacing, exact `Decimal` money,
an alert ledger, per-channel retry queues, structured reports and CI exit codes that mean something.

## How it works

```
GitHub Actions cron (every 6 hours)
        │
        ├── Extract ──► Normalize ──► Filter ──► Store ──► Decide ──► Alert
        │   HTTP/JSON     prices        keywords    SQLite    new job    Discord
        │   HTML          salaries      age/remote  history   new low    Telegram
        │                 dates         salary      ledger    threshold  console
        │
        └── portable ledger + JSON/Markdown reports + job summary artifact
```

**Extract.** Each rule in `config/config.yaml` names a source format — `greenhouse_json`,
`steam_json`, or `generic_html` with CSS selectors. The HTTP client applies timeouts, retries with
exponential backoff, a minimum delay per host, a response-size cap and a 2,000-listing ceiling.

**Normalize.** This is where most of the domain knowledge lives: currency symbols and codes,
thousands separators, price ranges, annualisation of salary figures, ISO/RFC/relative dates, HTML
entities, and repair of cp1252→UTF-8 mojibake. Money is parsed into `Decimal` and stored as decimal
text.

**Decide.** A fingerprint identifies a listing across runs. The SQLite ledger is keyed by
`(listing, rule, trigger, channel)`, so an alert can only fire once per trigger per channel. A
compact `*.ledger.json` file is also exported, which survives GitHub Actions cache eviction and can
be committed if you want stronger guarantees.

**Alert.** Delivery is at-least-once with retries: a failed Discord or Telegram call is recorded and
reconstructed from the database on the next run, per channel, so one dead webhook never blocks the
other channel.

**Isolate.** A failing rule is logged and skipped; the rest of the run completes and still exports
state and reports. `--dry-run` uses a disposable database, so it can never mark real listings as
seen.

### What it watches (defaults)

| Source | Type | Format | Notes |
| --- | --- | --- | --- |
| Greenhouse job boards | Jobs | JSON | Keyless public APIs (Ozow, Entersekt, Luno) |
| Steam app details | Prices | JSON | ZAR store prices (`cc=za`), discounts, F2P detection |
| Any product page | Prices | HTML + selectors | `generic_html` with item/title/price selectors |

## Features

- Strict YAML validation: unknown keys, unsafe URLs, duplicate rule names and invalid combinations
  all fail fast with precise messages
- Exact decimal money handling, including price ranges and salary annualisation
- Idempotent alert ledger keyed by listing, rule, trigger and channel
- Per-channel retry queues reconstructed from the database
- Portable ledger file that survives CI cache eviction
- Fault isolation per rule, with partial-run reporting
- JSON and Markdown reports plus a GitHub job summary
- 254 offline tests at roughly 88% branch coverage, Ruff lint/format, Actionlint-validated workflows
- Optional token-protected dashboard (Flask + Waitress) with live logs, searchable listings, price
  history, the alert ledger and a validated YAML editor — vanilla front end, no build step

## Run it locally

```bash
python -m venv .venv && . .venv/Scripts/activate     # Windows
pip install -r requirements-dev.txt
cp .env.example .env                                 # add webhook URLs / bot token if you want real sends
python -m src.main --dry-run                         # full pipeline, nothing sent, nothing marked seen
```

Real send:

```bash
python -m src.main
```

Dashboard:

```bash
pip install -r requirements-web.txt
python -m src.webapp          # http://127.0.0.1:5000
```

| Variable | Purpose |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | Discord delivery |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram delivery |
| `WATCHDOG_DASHBOARD_TOKEN` | Dashboard/API auth |

Secrets are referenced by **environment variable name** in `config.yaml`; the file itself never
contains them.

## Scheduling

`.github/workflows/watchdog.yml` runs on a six-hour cron, restores the SQLite database from the
Actions cache, runs the pipeline, uploads reports as artifacts, and saves the cache again. The
workflow is the deployment: no host, no container, no always-on process.

## The deployed demo

Netlify publishes `web/` — a static dashboard over the real report data committed in
`reports/latest.json`. It shows the same KPIs, rule table, price chart and alert feed the Flask UI
renders, plus a "simulate a run" control that appends a synthetic alert so the feed can be seen
moving. The Flask application remains the real interface to live state; the static page is there so
the pipeline's output is inspectable without running Python.

## Project layout

```
config/config.yaml     sources, filters, thresholds
src/config.py          strict YAML loading and validation
src/scraper.py         HTTP client and source parsers
src/normalize.py       price, salary, date, text and keyword helpers
src/models.py          dataclasses, fingerprints, run reports
src/pipeline.py        ETL orchestration, matching, retries
src/database.py        SQLite schema, history, alert ledger, read models
src/notifier.py        Discord, Telegram and console delivery
src/webapp.py          optional Flask dashboard and JSON API
web/                   static dashboard (the deployed demo)
tests/                 offline unit and integration suites with fixtures
scripts/               demo and helper utilities
```

## Tests

```bash
pytest            # 254 tests, no network access required
ruff check . && ruff format --check .
```

## Licence

MIT
