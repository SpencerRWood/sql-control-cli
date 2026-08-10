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
