from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .metadata import QueryMetadata, identity_key


@dataclass(frozen=True)
class Revision:
    id: int
    identity_key: str
    version: int
    source_hash: str
    source_path: str
    managed_path: str
    created_at: str


class Repository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        self._initialize(connection)
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS queries (
                identity_key TEXT PRIMARY KEY,
                query_name TEXT NOT NULL,
                connection_name TEXT NOT NULL,
                app_name TEXT NOT NULL,
                managed_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_key TEXT NOT NULL REFERENCES queries(identity_key),
                version INTEGER NOT NULL,
                source_hash TEXT NOT NULL,
                source_path TEXT NOT NULL,
                managed_path TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(identity_key, version),
                UNIQUE(identity_key, source_hash)
            );
            """
        )

    def upsert_query(
        self,
        connection: sqlite3.Connection,
        metadata: QueryMetadata,
        *,
        managed_path: Path,
    ) -> None:
        connection.execute(
            """
            INSERT INTO queries (identity_key, query_name, connection_name, app_name, managed_path)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO UPDATE SET
                query_name = excluded.query_name,
                connection_name = excluded.connection_name,
                app_name = excluded.app_name,
                managed_path = excluded.managed_path,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                identity_key(metadata),
                metadata.query_name,
                metadata.connection_name,
                metadata.app_name,
                str(managed_path),
            ),
        )

    def latest_revision(
        self,
        connection: sqlite3.Connection,
        metadata: QueryMetadata,
    ) -> Revision | None:
        row = connection.execute(
            """
            SELECT * FROM revisions
            WHERE identity_key = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (identity_key(metadata),),
        ).fetchone()
        return _revision(row) if row else None

    def add_revision(
        self,
        connection: sqlite3.Connection,
        metadata: QueryMetadata,
        *,
        version: int,
        source_hash: str,
        source_path: Path,
        managed_path: Path,
    ) -> Revision:
        cursor = connection.execute(
            """
            INSERT INTO revisions
                (identity_key, version, source_hash, source_path, managed_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                identity_key(metadata),
                version,
                source_hash,
                str(source_path),
                str(managed_path),
            ),
        )
        row = connection.execute(
            "SELECT * FROM revisions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _revision(row)

    def find_queries(
        self, connection: sqlite3.Connection, term: str
    ) -> list[sqlite3.Row]:
        like = f"%{term}%"
        return list(
            connection.execute(
                """
                SELECT * FROM queries
                WHERE query_name LIKE ? OR connection_name LIKE ? OR app_name LIKE ?
                ORDER BY query_name, connection_name, app_name
                """,
                (like, like, like),
            )
        )

    def all_queries(self, connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(connection.execute("SELECT * FROM queries ORDER BY query_name"))

    def queries_by_app(
        self, connection: sqlite3.Connection, app_name: str
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT * FROM queries
                WHERE app_name = ?
                ORDER BY query_name, connection_name
                """,
                (app_name,),
            )
        )

    def history(self, connection: sqlite3.Connection, key: str) -> list[Revision]:
        rows = connection.execute(
            "SELECT * FROM revisions WHERE identity_key = ? ORDER BY version",
            (key,),
        ).fetchall()
        return [_revision(row) for row in rows]

    def query(self, connection: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM queries WHERE identity_key = ?", (key,)
        ).fetchone()


def _revision(row: sqlite3.Row) -> Revision:
    return Revision(
        id=int(row["id"]),
        identity_key=str(row["identity_key"]),
        version=int(row["version"]),
        source_hash=str(row["source_hash"]),
        source_path=str(row["source_path"]),
        managed_path=str(row["managed_path"]),
        created_at=str(row["created_at"]),
    )
