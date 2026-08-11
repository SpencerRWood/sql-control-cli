# sql-control-cli

Architecture: Python command-line application.

Implementation stack: Python 3.11+, uv, argparse, pytest, ruff, and pre-commit.

This starter keeps the command entrypoint small, testable, and usable offline.

## Local Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
```

## Validation And Prepare

Validate a SQL file before capture:

```bash
sqlctl validate path/to/query.sql --profile strict --json
```

Create a managed copy only after validation passes:

```bash
sqlctl prepare path/to/query.sql --profile strict --json
```

Validation profiles live in `sqlctl.toml`:

```toml
[validation.profiles.strict]
enabled_rules = ["required_metadata", "allowed_team", "allowed_app", "comparison_keys_required"]
allowed_teams = ["Benefits"]
allowed_apps = ["Defined Benefits"]
```

Use `--force-pass` to keep failures visible while allowing an approved prepare to continue.

## Database And Repository Sources

Configure database connections and named query sources in `sqlctl.toml`:

```toml
[database.connections.warehouse]
driver = "sqlite"
path = "warehouse.sqlite3"

[repository.sources.participant_lookup]
connection = "warehouse"
sql = "select * from participants where participant_id = :participant_id"
```

Inspect connection metadata:

```bash
sqlctl db inspect warehouse --json
```

Execute a parameterized repository source:

```bash
sqlctl db query --source participant_lookup --param participant_id=123 --json
```

## Production Comparison

Capture a production baseline, then compare candidate query output against that managed copy:

```toml
[database.connections.candidate]
driver = "sqlite"
path = "candidate.sqlite3"

[database.connections.production]
driver = "sqlite"
path = "production.sqlite3"
```

```bash
sqlctl capture path/to/query.sql --json
sqlctl compare path/to/query.sql \
  --candidate-connection candidate \
  --production-connection production \
  --param active=1 \
  --json
```

`compare` validates the SQL file, resolves the managed production baseline by normalized query
identity, and compares result rows without depending on database row order. Queries without a
managed baseline return `status: first_time`; existing baselines return `matched` or `different`
with deterministic row differences.

Compare every managed query for an application:

```bash
sqlctl compare-app "Defined Benefits" \
  --candidate-connection candidate \
  --production-connection production \
  --param active=1 \
  --json
```

`compare-app` loads managed queries by exact `App_Name`, compares each query against the same
candidate and production connections, and returns aggregate counts plus per-query comparison
details.
