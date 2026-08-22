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

Validate query results against the stored query resolved from metadata:

```bash
sqlctl validate path/to/query.sql --param active=1 --json
```

`validate` runs both the proposed SQL and the stored query. By default, the proposed SQL runs
against the database registered for the file's `App_Name`; use `--candidate-connection` only when
you need an explicit override.

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
- `missing_input_parameters`: retained for legacy profiles; active validation reports missing SQLCTL markers through `unused_input_parameters`.
- `unused_input_parameters`: warns when the full SQL text has no SQLCTL input marker like `<|>ClientID<|>`.
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
`column_compare_connection`, `sqlctl` uses that connection to read the existing SQL from the query
store and compares the stored SQL's final output columns to the proposed SQL. It does not run the
proposed query or fetch report rows during `check`. The built-in profiles use `query_store_mssql`
for this query store lookup.

Configure the query store table once in `sqlctl.toml`:

```toml
[query_store]
table = "wirpa_dev.dbo.rpa_SQL_Queries"
sql_column = "Query_Value"
```

## Database And Repository Sources

Configure database connections and named query sources in `sqlctl.toml`:

```toml
[database.connections.warehouse]
driver = "sqlite"
path = "warehouse.sqlite3"

[database.app_connections]
"Defined Benefits" = "warehouse"
DBCS = "dbcs_database"

[repository.sources.participant_lookup]
connection = "warehouse"
sql = "select * from participants where participant_id = :participant_id"
```

The project `sqlctl.toml` provides the active default `query_store_mssql` connection. Fill in the
machine-specific values in the repository `.env`:

```env
SQLCTL_QUERY_STORE_MSSQL_SERVER=your-server-name
SQLCTL_QUERY_STORE_MSSQL_DATABASE=your_database
SQLCTL_QUERY_STORE_MSSQL_USERNAME=your_username
SQLCTL_QUERY_STORE_MSSQL_PASSWORD=your_password
```

`[database.app_connections]` maps SQL metadata `App_Name` values to database connection names.
For example, `App_Name: DBCS` can map to a connection named `dbcs_database`; the App_Name and
connection name do not need to match. If `sqlctl validate` sees an App_Name with no registration, it
stops with an explicit error naming that App_Name and the available registrations.

For databases that use Microsoft Entra ID / Active Directory interactive login, configure the MSSQL
connection with `authentication = "ActiveDirectoryInteractive"` and omit `password_env`:

```toml
[database.connections.dbcs_database]
driver = "mssql"
sql_driver = "ODBC Driver 17 for SQL Server"
server_env = "SQLCTL_DBCS_SERVER"
port = 1433
database_env = "SQLCTL_DBCS_DATABASE"
schema = "dbcs"
authentication = "ActiveDirectoryInteractive"
username_env = "SQLCTL_DBCS_USERNAME"
trust_server_certificate = true

[database.app_connections]
DBCS = "dbcs_database"
```

For MSSQL connections, optional `schema` is applied by sqlctl before execution: common
unqualified object references such as `from Participants` or `join [Plans]` become
`from [dbcs].Participants` and `join [dbcs].[Plans]`. Already qualified names such as
`dbo.Participants`, temp tables, table variables, and CTE references are left alone. SQL Server
does not expose a general per-connection default schema setting through the ODBC connection string.

The project `sqlctl.toml` includes this query-store connection:

```toml
[query_store]
table = "wirpa_dev.dbo.rpa_SQL_Queries"
sql_column = "Query_Value"

[database.connections.query_store_mssql]
driver = "mssql"
sql_driver = "ODBC Driver 17 for SQL Server"
server_env = "SQLCTL_QUERY_STORE_MSSQL_SERVER"
port = 1433
database_env = "SQLCTL_QUERY_STORE_MSSQL_DATABASE"
username_env = "SQLCTL_QUERY_STORE_MSSQL_USERNAME"
password_env = "SQLCTL_QUERY_STORE_MSSQL_PASSWORD"
trust_server_certificate = true
```

Use `sqlctl.toml` for local overrides, database connections, App_Name mappings, validation profiles,
or repository sources:

```toml

[validation.profiles.columns]
enabled_rules = ["required_metadata", "column_compare"]
column_compare_source = "participant_lookup_reference"

[repository.sources.participant_lookup_reference]
connection = "query_store_mssql"
sql = """
select participant_id, name
from dbo.Participants
where participant_id = <|>participant_id<|>
"""
```

Use `<|>name<|>` for parameters that `sqlctl validate` should prompt for or map from `--param`
and `--param-csv`. Normal SQL variables such as `@population` are not interactive input markers.
Quoted markers are supported when the whole quoted value is the marker, such as
`where vs.ssn_n = '<|>SSN<|>'`; `sqlctl` binds that as a parameter and prompts for `SSN`.
SQL Server repository sources may still use native `@parameter` markers; `sqlctl` translates them
to ODBC positional parameters for `pyodbc`.

The project-level `pyproject.toml` includes a `[tool.sqlctl]` section that supplies default
`strict` and `columns` validation profiles plus the implemented validation rules, warning-only
rules, and required/optional connection fields. Keep concrete connection registrations in
`sqlctl.toml`, with machine-specific values in `.env`, environment variables, or explicit local
overrides.

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
