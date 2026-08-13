from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import DatabaseConnectionConfig, SqlctlConfig

SQLCTL_INPUT_PARAMETER_RE = re.compile(r"<\|>\s*([A-Za-z_][A-Za-z0-9_]*)\s*<\|>")


class DatabaseError(ValueError):
    pass


@dataclass(frozen=True)
class QueryResult:
    connection_name: str
    source_name: str | None
    columns: tuple[str, ...]
    rows: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "connection": self.connection_name,
            "source": self.source_name,
            "columns": list(self.columns),
            "row_count": len(self.rows),
            "rows": list(self.rows),
        }


@dataclass(frozen=True)
class ConnectionInfo:
    name: str
    driver: str
    path: str | None
    server: str | None = None
    port: int | None = None
    database: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "driver": self.driver,
            "path": self.path,
        }
        if self.server is not None:
            payload["server"] = self.server
        if self.port is not None:
            payload["port"] = self.port
        if self.database is not None:
            payload["database"] = self.database
        return payload


class DatabaseAdapter(Protocol):
    def execute(self, sql: str, parameters: dict[str, object]) -> QueryResult:
        pass

    def inspect(self) -> ConnectionInfo:
        pass


class SQLiteAdapter:
    def __init__(self, name: str, config: DatabaseConnectionConfig) -> None:
        if config.path is None:
            raise DatabaseError(f"SQLite connection '{name}' requires a path.")
        self.name = name
        self.path = config.path

    def inspect(self) -> ConnectionInfo:
        return ConnectionInfo(name=self.name, driver="sqlite", path=str(self.path))

    def execute(self, sql: str, parameters: dict[str, object]) -> QueryResult:
        if not self.path.exists():
            raise DatabaseError(
                f"SQLite database not found for connection '{self.name}': {self.path}"
            )
        try:
            with sqlite3.connect(self.path) as connection:
                connection.row_factory = sqlite3.Row
                cursor = connection.execute(sql, parameters)
                rows = cursor.fetchall()
        except sqlite3.Error as err:
            raise DatabaseError(
                f"SQLite query failed for connection '{self.name}': {err}"
            ) from err

        columns = tuple(description[0] for description in (cursor.description or ()))
        return QueryResult(
            connection_name=self.name,
            source_name=None,
            columns=columns,
            rows=tuple(dict(row) for row in rows),
        )


class MSSQLAdapter:
    def __init__(self, name: str, config: DatabaseConnectionConfig) -> None:
        self.name = name
        self.config = config
        missing = []
        if not config.sql_driver:
            missing.append("sql_driver")
        if not config.server:
            missing.append("server")
        if not config.database:
            missing.append("database")
        if not config.username:
            missing.append("username or username_env")
        if not config.password:
            missing.append("password or password_env")
        if missing:
            raise DatabaseError(
                f"MSSQL connection '{name}' is missing: {', '.join(missing)}"
            )

    def inspect(self) -> ConnectionInfo:
        return ConnectionInfo(
            name=self.name,
            driver="mssql",
            path=None,
            server=self.config.server,
            port=self.config.port,
            database=self.config.database,
        )

    def execute(self, sql: str, parameters: dict[str, object]) -> QueryResult:
        try:
            import pyodbc
        except ImportError as err:
            raise DatabaseError(
                "MSSQL connections require pyodbc. Install with: "
                "uv tool install --editable /path/to/sql-control-cli --with pyodbc"
            ) from err

        executable_sql, positional_parameters = mssql_named_parameters(
            sql, parameters
        )
        try:
            with pyodbc.connect(self._connection_string()) as connection:
                cursor = connection.cursor()
                cursor.execute(executable_sql, *positional_parameters)
                rows = []
                while cursor.description is None and cursor.nextset():
                    pass
                if cursor.description:
                    rows = cursor.fetchall()
        except pyodbc.Error as err:
            raise DatabaseError(
                f"MSSQL query failed for connection '{self.name}': {err}"
            ) from err

        columns = tuple(description[0] for description in (cursor.description or ()))
        return QueryResult(
            connection_name=self.name,
            source_name=None,
            columns=columns,
            rows=tuple(_row_dict(columns, row) for row in rows),
        )

    def _connection_string(self) -> str:
        server = _mssql_server_endpoint(self.config.server, self.config.port)
        trust = "yes" if self.config.trust_server_certificate else "no"
        return ";".join(
            [
                f"DRIVER={_odbc_value(self.config.sql_driver)}",
                f"SERVER={_odbc_value(server)}",
                f"DATABASE={_odbc_value(self.config.database)}",
                f"UID={_odbc_value(self.config.username)}",
                f"PWD={_odbc_value(self.config.password)}",
                f"TrustServerCertificate={trust}",
            ]
        )


def _mssql_server_endpoint(server: str, port: int | None) -> str:
    if port is None or "\\" in server or "," in server:
        return server
    return f"{server},{port}"


def _odbc_value(value: str) -> str:
    escaped = value.replace("}", "}}")
    return f"{{{escaped}}}"


def get_connection_config(config: SqlctlConfig, name: str) -> DatabaseConnectionConfig:
    connections = config.database_connections or {}
    if name not in connections:
        available = ", ".join(sorted(connections)) or "(none)"
        raise DatabaseError(
            f"Database connection not found: {name}. Available connections: {available}"
        )
    connection = connections[name]
    if not connection.driver:
        raise DatabaseError(f"Database connection '{name}' is missing a driver.")
    return connection


def adapter_for(config: SqlctlConfig, name: str) -> DatabaseAdapter:
    connection = get_connection_config(config, name)
    if connection.driver == "sqlite":
        return SQLiteAdapter(name, connection)
    if connection.driver == "mssql":
        return MSSQLAdapter(name, connection)
    raise DatabaseError(
        f"Unsupported database driver for connection '{name}': {connection.driver}"
    )


def inspect_connection(config: SqlctlConfig, name: str) -> ConnectionInfo:
    return adapter_for(config, name).inspect()


def execute_query(
    config: SqlctlConfig,
    *,
    connection_name: str,
    sql: str,
    parameters: dict[str, object] | None = None,
    source_name: str | None = None,
) -> QueryResult:
    connection = get_connection_config(config, connection_name)
    executable_sql = sqlctl_input_placeholders_to_named_parameters(
        sql, driver=connection.driver
    )
    result = adapter_for(config, connection_name).execute(
        executable_sql, parameters or {}
    )
    if source_name is None:
        return result
    return QueryResult(
        connection_name=result.connection_name,
        source_name=source_name,
        columns=result.columns,
        rows=result.rows,
    )


def execute_query_source(
    config: SqlctlConfig,
    source_name: str,
    *,
    parameters: dict[str, object] | None = None,
) -> QueryResult:
    sources = config.query_sources or {}
    if source_name not in sources:
        available = ", ".join(sorted(sources)) or "(none)"
        raise DatabaseError(
            f"Query source not found: {source_name}. Available sources: {available}"
        )
    source = sources[source_name]
    if not source.connection or not source.sql:
        raise DatabaseError(
            f"Query source '{source_name}' must define connection and sql."
        )
    return execute_query(
        config,
        connection_name=source.connection,
        sql=source.sql,
        parameters=parameters,
        source_name=source_name,
    )


def query_source_columns(config: SqlctlConfig, source_name: str) -> tuple[str, ...]:
    sources = config.query_sources or {}
    if source_name not in sources:
        available = ", ".join(sorted(sources)) or "(none)"
        raise DatabaseError(
            f"Query source not found: {source_name}. Available sources: {available}"
        )
    source = sources[source_name]
    if not source.connection or not source.sql:
        raise DatabaseError(
            f"Query source '{source_name}' must define connection and sql."
        )
    return query_columns(
        config,
        connection_name=source.connection,
        sql=source.sql,
        source_name=source_name,
    )


def query_columns(
    config: SqlctlConfig,
    *,
    connection_name: str,
    sql: str,
    source_name: str | None = None,
) -> tuple[str, ...]:
    driver = get_connection_config(config, connection_name).driver
    query_sql = final_query_select_statement(sql)
    if query_sql is None:
        raise DatabaseError("Could not resolve final query SELECT statement.")
    return execute_query(
        config,
        connection_name=connection_name,
        sql=column_probe_sql(sql if driver == "mssql" else query_sql),
        parameters=probe_parameters(query_sql),
        source_name=source_name,
    ).columns


def column_probe_sql(sql: str) -> str:
    query_sql = final_query_select_statement(sql) or sql
    setup_sql = _setup_sql_before_final_select(sql)
    probe_source = strip_top_level_order_by(query_sql.rstrip().rstrip(";"))
    probe_sql = (
        "select * from (\n"
        f"{probe_source}\n"
        ") as sqlctl_column_probe where 1 = 0"
    )
    if not setup_sql:
        return probe_sql
    return f"{setup_sql.rstrip().rstrip(';')};\n{probe_sql}"


def final_query_select_statement(sql: str) -> str | None:
    bounds = final_query_select_bounds(sql)
    if bounds is None:
        return None
    start, end = bounds
    return sql[start:end].strip()


def final_query_select_bounds(sql: str) -> tuple[int, int] | None:
    lowered = sql.lower()
    depth = 0
    index = 0
    active_with_start: int | None = None
    final_select_start: int | None = None
    final_statement_start: int | None = None
    while index < len(sql):
        char = sql[index]
        if char == "'":
            index = _skip_quoted_string(sql, index)
            continue
        if sql.startswith("--", index):
            index = _skip_line_comment(sql, index)
            continue
        if sql.startswith("/*", index):
            index = _skip_block_comment(sql, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and char == ";":
            active_with_start = None
        elif depth == 0 and _word_at(lowered, index, "with"):
            active_with_start = index
        elif depth == 0 and _word_at(lowered, index, "select"):
            final_select_start = index
            final_statement_start = active_with_start or index
            active_with_start = None
        index += 1
    if final_select_start is None or final_statement_start is None:
        return None

    end = _top_level_statement_end(sql, final_select_start)
    return final_statement_start, end


def _setup_sql_before_final_select(sql: str) -> str:
    bounds = final_query_select_bounds(sql)
    if bounds is None:
        return ""
    final_statement_start, _end = bounds
    return sql[:final_statement_start].strip()


def _top_level_statement_end(sql: str, start: int) -> int:
    depth = 0
    index = start
    while index < len(sql):
        char = sql[index]
        if char == "'":
            index = _skip_quoted_string(sql, index)
            continue
        if sql.startswith("--", index):
            index = _skip_line_comment(sql, index)
            continue
        if sql.startswith("/*", index):
            index = _skip_block_comment(sql, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and (
            char == ";"
            or (index > start and _starts_following_statement(sql, index))
        ):
            return index
        index += 1
    return len(sql)


def _starts_following_statement(sql: str, index: int) -> bool:
    previous = sql[index - 1] if index > 0 else ""
    if previous and not previous.isspace():
        return False
    lowered = sql.lower()
    return any(
        _word_at(lowered, index, word)
        for word in (
            "declare",
            "create",
            "drop",
            "insert",
            "update",
            "delete",
            "merge",
            "exec",
            "execute",
            "set",
        )
    )


def strip_top_level_order_by(sql: str) -> str:
    lowered = sql.lower()
    depth = 0
    index = 0
    final_order_by: int | None = None
    while index < len(sql):
        char = sql[index]
        if char == "'":
            index = _skip_quoted_string(sql, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and _word_at(lowered, index, "order"):
            after_order = index + len("order")
            while after_order < len(sql) and sql[after_order].isspace():
                after_order += 1
            if _word_at(lowered, after_order, "by"):
                final_order_by = index
        index += 1
    if final_order_by is None:
        return sql
    return sql[:final_order_by].rstrip()


def probe_parameters(sql: str) -> dict[str, object]:
    return {name: None for name in named_parameter_names(sql)}


def sqlctl_input_parameter_names(sql: str) -> tuple[str, ...]:
    names = []
    seen = set()
    for match in SQLCTL_INPUT_PARAMETER_RE.finditer(sql):
        name = match.group(1)
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        names.append(name)
    return tuple(names)


def sqlctl_input_placeholders_to_named_parameters(sql: str, *, driver: str) -> str:
    return SQLCTL_INPUT_PARAMETER_RE.sub(
        lambda match: f":{match.group(1)}", sql
    )


def named_parameter_names(sql: str) -> tuple[str, ...]:
    names = []
    seen = set()
    for match in re.finditer(r"(?<![@:\w])[@:]([A-Za-z_][A-Za-z0-9_]*)", sql):
        name = match.group(1)
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        names.append(name)
    return tuple(names)


def mssql_named_parameters(
    sql: str, parameters: dict[str, object]
) -> tuple[str, tuple[object, ...]]:
    normalized_parameters = {
        key.lower().lstrip("@:"): value for key, value in parameters.items()
    }
    positional_parameters: list[object] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        positional_parameters.append(normalized_parameters.get(name.lower()))
        return "?"

    return (
        re.sub(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)", replace, sql),
        tuple(positional_parameters),
    )


def parse_parameters(values: list[str]) -> dict[str, object]:
    parameters: dict[str, object] = {}
    for value in values:
        if "=" not in value:
            raise DatabaseError(f"Parameter must be NAME=VALUE: {value}")
        name, raw_value = value.split("=", 1)
        name = name.strip()
        if not name:
            raise DatabaseError(f"Parameter name cannot be empty: {value}")
        parameters[name] = _coerce_parameter(raw_value)
    return parameters


def _coerce_parameter(value: str) -> object:
    if value == "":
        return ""
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def resolve_path(base_path: Path, configured_path: Path) -> Path:
    if configured_path.is_absolute():
        return configured_path
    return base_path / configured_path


def _row_dict(columns: tuple[str, ...], row: Any) -> dict[str, object]:
    return {column: row[index] for index, column in enumerate(columns)}


def _word_at(value: str, index: int, word: str) -> bool:
    if not value.startswith(word, index):
        return False
    before = value[index - 1] if index > 0 else " "
    after_index = index + len(word)
    after = value[after_index] if after_index < len(value) else " "
    return not (before.isalnum() or before == "_") and not (
        after.isalnum() or after == "_"
    )


def _skip_quoted_string(value: str, start: int) -> int:
    index = start + 1
    while index < len(value):
        if value[index] == "'":
            if index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    return len(value)


def _skip_line_comment(value: str, start: int) -> int:
    newline = value.find("\n", start + 2)
    return len(value) if newline < 0 else newline + 1


def _skip_block_comment(value: str, start: int) -> int:
    end = value.find("*/", start + 2)
    return len(value) if end < 0 else end + 2
