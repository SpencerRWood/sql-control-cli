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

## Global Editable Install

Install the CLI as a global `uv` tool from the cloned repository:

```bash
uv tool install --editable /path/to/sql-control-cli
```

Install SQL Server support with the optional `mssql` extra:

```bash
uv tool install --editable /path/to/sql-control-cli --with pyodbc
```

For an in-repo development environment, use:

```bash
uv sync --group dev --extra mssql
```

When installed this way, `sqlctl` can be run from any directory. It also loads `.env` from the
project repository automatically, so repo-level settings such as `SQLCTL_CONFIG`,
`SQLCTL_STORAGE_PATH`, and `SQLCTL_MANAGED_ROOT` can travel with the editable install. Environment
variables set in the terminal still take precedence over values in `.env`.

## Check, Validate, And Prepare

Check a SQL file against metadata and workflow rules:

```bash
sqlctl check path/to/query.sql --profile strict --json
```

Validate query results against the stored RPA query resolved from metadata:

```bash
sqlctl validate path/to/query.sql --param active=1 --json
```

Run validation once for each CSV parameter row:

```bash
sqlctl validate path/to/query.sql --param-csv params.csv --json
```

Create a managed copy only after validation passes:

```bash
sqlctl prepare path/to/query.sql --json
```

The active `default` and `strict` profiles are supplied by the project `pyproject.toml` and run the
implemented validation ruleset. Use `sqlctl.toml` only for local profile overrides, such as allowed
team/app lists:

```toml
[validation.profiles.strict]
enabled_rules = [
  "required_metadata",
  "allowed_team",
  "allowed_app",
  "missing_input_parameters",
  "unused_input_parameters",
  "commented_out_sql",
  "select_star",
  "order_by_without_justification",
  "top_without_justification",
  "hard_coded_sensitive_literals",
  "debug_columns",
  "nolock_usage",
  "write_operation",
  "column_compare"
]
allowed_teams = ["Benefits"]
allowed_apps = ["Defined Benefits"]
```

Use `--force-pass` to keep failures visible while allowing an approved prepare to continue.

Implemented validation rules:

- `required_metadata`: requires `Query_Name`, `Connection_Name`, and `App_Name` in a leading SQL comment block.
- `allowed_team`: when `Team` is present and `allowed_teams` is configured, limits it to allowed values.
- `allowed_app`: optionally limits `App_Name` to `allowed_apps`.
- `comparison_keys_required`: implemented for opt-in legacy profiles, but active default profiles do not require `Comparison Keys`.
- `missing_input_parameters`: requires at least one live `@parameter` marker.
- `unused_input_parameters`: flags `@parameter` markers that appear only in comments.
- `commented_out_sql`: flags comments that look like disabled SQL logic.
- `select_star`: flags `SELECT *`, including `SELECT TOP ... *` and `SELECT DISTINCT *`.
- `order_by_without_justification`: requires a reason comment for `ORDER BY`.
- `top_without_justification`: requires a reason comment for `TOP`.
- `hard_coded_sensitive_literals`: flags SSN-shaped values and literal comparisons on sensitive identifier fields.
- `debug_columns`: flags temporary or diagnostic output aliases.
- `nolock_usage`: flags `NOLOCK` and `WITH (NOLOCK)`.
- `write_operation`: flags non-temp-table writes and schema operations; temp tables beginning with `#` are allowed, but `EXEC` and `EXECUTE` are always flagged.
- `column_compare`: compares the output column names and order from the final top-level `SELECT`
  against a reference query from either a configured repository source or an alternate SQL file.
  It reports warnings when columns differ or when compare inputs cannot be resolved; those warnings
  do not fail validation unless another enabled rule reports an error. Repository-source
  comparisons ask the configured database for a zero-row result shape, so SQL Server column aliases
  and expression names come from the driver metadata without fetching report rows.

Configure `column_compare` with an explicit repository source:

```toml
[validation.profiles.columns]
enabled_rules = ["required_metadata", "column_compare"]
column_compare_source = "participant_lookup_reference"

[repository.sources.participant_lookup_reference]
connection = "warehouse"
sql = "select participant_id, name from production_participants"
```

Or configure it with an alternate SQL file:

```toml
[validation.profiles.columns]
enabled_rules = ["required_metadata", "column_compare"]
column_compare_file = "reference/participant_lookup.sql"
```

If `column_compare_source` and `column_compare_file` are omitted, `sqlctl` attempts to find a
repository source using normalized names derived from `Query_Name`, `Connection_Name`, and
`App_Name`. If no matching source is found and the active profile has
`column_compare_connection`, `sqlctl` probes the current SQL against that connection. The built-in
profiles use `rpa_mssql` for this fallback.

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

The project `pyproject.toml` provides an active default `rpa_mssql` connection template. Fill in
the machine-specific values in the repository `.env`:

```env
SQLCTL_RPA_MSSQL_SERVER=your-server-name
SQLCTL_RPA_MSSQL_DATABASE=your_database
SQLCTL_RPA_MSSQL_USERNAME=your_username
SQLCTL_RPA_MSSQL_PASSWORD=your_password
```

The active template is equivalent to:

```toml
[tool.sqlctl.validation.profiles.columns]
enabled_rules = ["required_metadata", "column_compare"]
column_compare_connection = "rpa_mssql"

[tool.sqlctl.database.connection_templates.rpa_mssql]
driver = "mssql"
sql_driver = "ODBC Driver 17 for SQL Server"
server_env = "SQLCTL_RPA_MSSQL_SERVER"
port = 1433
database_env = "SQLCTL_RPA_MSSQL_DATABASE"
username_env = "SQLCTL_RPA_MSSQL_USERNAME"
password_env = "SQLCTL_RPA_MSSQL_PASSWORD"
trust_server_certificate = true
```

Use `sqlctl.toml` only when you need local overrides, validation profiles, or repository sources:

```toml

[validation.profiles.columns]
enabled_rules = ["required_metadata", "column_compare"]
column_compare_source = "participant_lookup_reference"

[repository.sources.participant_lookup_reference]
connection = "rpa_mssql"
sql = """
select participant_id, name
from dbo.Participants
where participant_id = @participant_id
"""
```

SQL Server repository sources may use `@parameter` markers. `sqlctl` translates them to ODBC
positional parameters for `pyodbc`.

The project-level `pyproject.toml` includes a `[tool.sqlctl]` section that supplies default
`strict` and `columns` validation profiles, the active `rpa_mssql` connection template, and the
implemented validation rules, warning-only rules, and required/optional connection fields. Keep
machine-specific values in `.env`, environment variables, or explicit local overrides.

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

## Test Publishing

Configure a test publishing target:

```toml
[database.connections.test]
driver = "sqlite"
path = "test.sqlite3"

[publishing.test]
connection = "test"
table = "published_queries"
```

Publish a validated managed query to test:

```bash
sqlctl deploy-test path/to/query.sql --profile strict --json
```

`deploy-test` validates and captures the query, writes the managed SQL into the configured test
registry, and reports `CREATE`, `UPDATE`, or `NO_CHANGE`. It refuses production-named
connections; production promotion is intentionally outside this command.

## Release Parity

Audit managed SQL against the configured test publishing registry:

```bash
sqlctl parity --app "Defined Benefits" --json
```

`parity` checks that every managed query has a matching test deployment by identity, source hash,
and SQL text. It returns consolidated counts plus per-query issues such as `test_missing`,
`source_hash_mismatch`, and `sql_text_mismatch`.
