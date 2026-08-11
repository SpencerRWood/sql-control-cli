from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import SqlctlConfig
from .database import get_connection_config
from .managed import CaptureResult, capture
from .metadata import identity_key, managed_sql, source_hash
from .validation import validate_sql_file


class PublishingError(ValueError):
    pass


@dataclass(frozen=True)
class TestDeployResult:
    action: str
    capture: CaptureResult
    connection_name: str
    table_name: str
    submitted_at: str

    def to_dict(self) -> dict[str, object]:
        metadata = self.capture.metadata
        return {
            "ok": True,
            "action": self.action,
            "identity_key": identity_key(metadata),
            "metadata": metadata.to_dict(),
            "source_hash": self.capture.source_hash,
            "managed_path": str(self.capture.managed_path),
            "version": self.capture.revision.version,
            "test_connection": self.connection_name,
            "table": self.table_name,
            "submitted_at": self.submitted_at,
        }


def deploy_test(
    sql_file: Path,
    config: SqlctlConfig,
    *,
    connection_name: str | None = None,
    profile_name: str = "default",
) -> dict[str, object]:
    validation = validate_sql_file(sql_file, config, profile_name=profile_name)
    if not validation.passed:
        return {"ok": False, "validation": validation.to_dict()}

    active_connection = connection_name or config.test_publishing.connection
    if not active_connection:
        raise PublishingError(
            "Test publishing connection is required. Configure [publishing.test] "
            "connection or pass --connection."
        )
    _validate_test_connection_name(active_connection)

    connection_config = get_connection_config(config, active_connection)
    if connection_config.driver != "sqlite":
        raise PublishingError(
            f"Unsupported test publishing driver for connection '{active_connection}': "
            f"{connection_config.driver}"
        )
    if connection_config.path is None:
        raise PublishingError(
            f"SQLite connection '{active_connection}' requires a path."
        )

    capture_result = capture(sql_file, config)
    submitted_at = datetime.now().astimezone().isoformat(timespec="seconds")
    action = _publish_sqlite(
        database_path=connection_config.path,
        table_name=config.test_publishing.table,
        capture_result=capture_result,
        submitted_at=submitted_at,
    )
    return TestDeployResult(
        action=action,
        capture=capture_result,
        connection_name=active_connection,
        table_name=config.test_publishing.table,
        submitted_at=submitted_at,
    ).to_dict()


def _publish_sqlite(
    *,
    database_path: Path,
    table_name: str,
    capture_result: CaptureResult,
    submitted_at: str,
) -> str:
    _validate_table_name(table_name)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = capture_result.metadata
    key = identity_key(metadata)
    sql_text = managed_sql(
        capture_result.managed_path.read_text(encoding="utf-8"),
        version=capture_result.revision.version,
        date_changed=capture_result.revision.created_at[:10],
    )
    digest = source_hash(sql_text)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                identity_key TEXT PRIMARY KEY,
                query_name TEXT NOT NULL,
                connection_name TEXT NOT NULL,
                app_name TEXT NOT NULL,
                sql_text TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                version INTEGER NOT NULL,
                submitted_at TEXT NOT NULL
            )
            """
        )
        row = connection.execute(
            f"SELECT source_hash FROM {table_name} WHERE identity_key = ?", (key,)
        ).fetchone()
        if row is None:
            action = "CREATE"
        elif row[0] == digest:
            action = "NO_CHANGE"
        else:
            action = "UPDATE"

        if action != "NO_CHANGE":
            connection.execute(
                f"""
                INSERT INTO {table_name}
                    (identity_key, query_name, connection_name, app_name, sql_text,
                     source_hash, version, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO UPDATE SET
                    query_name = excluded.query_name,
                    connection_name = excluded.connection_name,
                    app_name = excluded.app_name,
                    sql_text = excluded.sql_text,
                    source_hash = excluded.source_hash,
                    version = excluded.version,
                    submitted_at = excluded.submitted_at
                """,
                (
                    key,
                    metadata.query_name,
                    metadata.connection_name,
                    metadata.app_name,
                    sql_text,
                    digest,
                    capture_result.revision.version,
                    submitted_at,
                ),
            )
    return action


def _validate_test_connection_name(connection_name: str) -> None:
    normalized = re.sub(r"[^a-z0-9]+", "-", connection_name.lower()).strip("-")
    parts = set(normalized.split("-"))
    if "prod" in parts or "production" in parts:
        raise PublishingError(
            f"deploy-test cannot publish to production connection: {connection_name}"
        )


def _validate_table_name(table_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise PublishingError(f"Invalid test publishing table name: {table_name}")
