from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import SqlctlConfig
from .metadata import (
    QueryMetadata,
    identity_key,
    managed_sql,
    parse_metadata,
    source_hash,
)
from .storage import Repository, Revision


@dataclass(frozen=True)
class CaptureResult:
    action: str
    metadata: QueryMetadata
    source_hash: str
    managed_path: Path
    revision: Revision


def managed_path(config: SqlctlConfig, metadata: QueryMetadata) -> Path:
    query, connection, app = metadata.identity
    return config.managed_root / app / connection / f"{query}.sql"


def capture(source_path: Path, config: SqlctlConfig, *, today: date | None = None) -> CaptureResult:
    sql_text = source_path.read_text(encoding="utf-8")
    metadata = parse_metadata(sql_text)
    digest = source_hash(sql_text, normalize_whitespace=config.normalize_whitespace)
    target_path = managed_path(config, metadata)
    repository = Repository(config.storage_path)
    active_date = today or datetime.now().astimezone().date()
    with repository.connect() as connection:
        latest = repository.latest_revision(connection, metadata)
        if latest and latest.source_hash == digest:
            repository.upsert_query(connection, metadata, managed_path=target_path)
            return CaptureResult("unchanged", metadata, digest, target_path, latest)

        version = 1 if latest is None else latest.version + 1
        rendered = managed_sql(sql_text, version=version, date_changed=active_date.isoformat())
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        temp_path.write_text(rendered, encoding="utf-8")
        temp_path.replace(target_path)
        repository.upsert_query(connection, metadata, managed_path=target_path)
        revision = repository.add_revision(
            connection,
            metadata,
            version=version,
            source_hash=digest,
            source_path=source_path,
            managed_path=target_path,
        )
        action = "created" if version == 1 else "updated"
        return CaptureResult(action, metadata, digest, target_path, revision)


def diff_source_to_managed(source_path: Path, managed_file: Path) -> str:
    source_lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
    managed_lines = managed_file.read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            managed_lines,
            source_lines,
            fromfile=str(managed_file),
            tofile=str(source_path),
        )
    )


def identity_from_text_or_key(value: str) -> str:
    candidate = Path(value)
    if candidate.exists():
        return identity_key(parse_metadata(candidate.read_text(encoding="utf-8")))
    return value
