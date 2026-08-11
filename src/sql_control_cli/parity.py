from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import SqlctlConfig
from .database import get_connection_config
from .metadata import source_hash
from .publishing import _validate_table_name
from .storage import Repository


class ParityError(ValueError):
    pass


@dataclass(frozen=True)
class ParityItem:
    identity_key: str
    query_name: str
    connection_name: str
    app_name: str
    status: str
    managed_path: str
    test_version: int | None = None
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "identity_key": self.identity_key,
            "query_name": self.query_name,
            "connection_name": self.connection_name,
            "app_name": self.app_name,
            "status": self.status,
            "managed_path": self.managed_path,
            "test_version": self.test_version,
            "issues": list(self.issues),
        }


def audit_parity(
    config: SqlctlConfig,
    *,
    app_name: str | None = None,
    connection_name: str | None = None,
) -> dict[str, object]:
    test_connection = connection_name or config.test_publishing.connection
    if not test_connection:
        raise ParityError(
            "Test publishing connection is required. Configure [publishing.test] "
            "connection or pass --connection."
        )
    connection_config = get_connection_config(config, test_connection)
    if connection_config.driver != "sqlite":
        raise ParityError(
            f"Unsupported parity driver for connection '{test_connection}': "
            f"{connection_config.driver}"
        )
    if connection_config.path is None:
        raise ParityError(f"SQLite connection '{test_connection}' requires a path.")

    repository = Repository(config.storage_path)
    with repository.connect() as connection:
        rows = (
            repository.queries_by_app(connection, app_name)
            if app_name
            else repository.all_queries(connection)
        )

    if not rows:
        target = f" application: {app_name}" if app_name else ""
        raise ParityError(f"Managed queries not found for parity audit{target}.")

    test_rows = _read_test_registry(
        connection_config.path, config.test_publishing.table
    )
    items = tuple(
        _audit_row(row, test_rows.get(str(row["identity_key"]))) for row in rows
    )
    issue_count = sum(1 for item in items if item.status != "matched")
    return {
        "ok": issue_count == 0,
        "status": "matched" if issue_count == 0 else "mismatch",
        "app_name": app_name,
        "test_connection": test_connection,
        "table": config.test_publishing.table,
        "query_count": len(items),
        "matched_count": len(items) - issue_count,
        "mismatch_count": issue_count,
        "queries": [item.to_dict() for item in items],
    }


def _read_test_registry(database_path: Path, table_name: str) -> dict[str, sqlite3.Row]:
    _validate_table_name(table_name)
    if not database_path.exists():
        return {}
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        if not _table_exists(connection, table_name):
            return {}
        rows = connection.execute(
            f"""
            SELECT identity_key, source_hash, sql_text, version
            FROM {table_name}
            """
        ).fetchall()
    return {str(row["identity_key"]): row for row in rows}


def _audit_row(row: sqlite3.Row, test_row: sqlite3.Row | None) -> ParityItem:
    managed_path = Path(str(row["managed_path"]))
    issues: list[str] = []
    test_version: int | None = None

    if not managed_path.exists():
        issues.append("managed_missing")
        managed_hash = None
        managed_sql = ""
    else:
        managed_sql = managed_path.read_text(encoding="utf-8")
        managed_hash = source_hash(managed_sql)

    if test_row is None:
        issues.append("test_missing")
    else:
        test_version = int(test_row["version"])
        test_sql = str(test_row["sql_text"])
        try:
            test_hash = source_hash(test_sql)
        except ValueError:
            test_hash = str(test_row["source_hash"])
            issues.append("test_sql_metadata_invalid")
        if managed_hash is not None and str(test_row["source_hash"]) != managed_hash:
            issues.append("source_hash_mismatch")
        if managed_sql and test_sql != managed_sql:
            issues.append("sql_text_mismatch")
        if managed_hash is not None and test_hash != managed_hash:
            issues.append("test_sql_hash_mismatch")

    status = "matched" if not issues else "mismatch"
    return ParityItem(
        identity_key=str(row["identity_key"]),
        query_name=str(row["query_name"]),
        connection_name=str(row["connection_name"]),
        app_name=str(row["app_name"]),
        status=status,
        managed_path=str(managed_path),
        test_version=test_version,
        issues=tuple(issues),
    )


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None
