from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import DatabaseConnectionConfig, SqlctlConfig


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

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "driver": self.driver, "path": self.path}


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
    result = adapter_for(config, connection_name).execute(sql, parameters or {})
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
