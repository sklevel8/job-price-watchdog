# Runtime state

`watchdog.db` and its WAL files are local/SQLite state and are intentionally
ignored by git. The GitHub Actions workflow restores them from the Actions
cache and uploads the portable ledger as a run artifact.

`*.ledger.json` may be committed if you want alert idempotency to survive a
cache eviction; see the README's "State and idempotency" section.
