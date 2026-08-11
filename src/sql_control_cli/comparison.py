from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import SqlctlConfig
from .database import QueryResult, execute_query
from .metadata import identity_key, parse_metadata, sql_body
from .storage import Repository
from .validation import validate_sql_file


class ComparisonError(ValueError):
    pass


@dataclass(frozen=True)
class RowComparison:
    status: str
    candidate_row_count: int
    production_row_count: int
    missing_from_candidate: tuple[dict[str, object], ...] = ()
    unexpected_in_candidate: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "candidate_row_count": self.candidate_row_count,
            "production_row_count": self.production_row_count,
            "missing_from_candidate": list(self.missing_from_candidate),
            "unexpected_in_candidate": list(self.unexpected_in_candidate),
        }


def compare_to_production(
    sql_file: Path,
    config: SqlctlConfig,
    *,
    candidate_connection: str,
    production_connection: str,
    parameters: dict[str, object] | None = None,
    profile_name: str = "default",
) -> dict[str, object]:
    validation = validate_sql_file(sql_file, config, profile_name=profile_name)
    if not validation.passed:
        return {"ok": False, "validation": validation.to_dict()}

    sql_text = sql_file.read_text(encoding="utf-8")
    metadata = parse_metadata(sql_text)
    key = identity_key(metadata)
    repository = Repository(config.storage_path)
    with repository.connect() as connection:
        baseline = repository.query(connection, key)
        latest = repository.latest_revision(connection, metadata)

    payload: dict[str, object] = {
        "ok": True,
        "status": "first_time" if baseline is None else "compared",
        "identity_key": key,
        "metadata": metadata.to_dict(),
        "validation": validation.to_dict(),
        "baseline": None,
    }
    if baseline is None:
        return payload

    baseline_path = Path(str(baseline["managed_path"]))
    if not baseline_path.exists():
        raise ComparisonError(f"Managed production baseline not found: {baseline_path}")

    active_parameters = parameters or {}
    candidate = execute_query(
        config,
        connection_name=candidate_connection,
        sql=sql_body(sql_text),
        parameters=active_parameters,
    )
    production = execute_query(
        config,
        connection_name=production_connection,
        sql=sql_body(baseline_path.read_text(encoding="utf-8")),
        parameters=active_parameters,
    )
    comparison = compare_rows(candidate, production)
    payload.update(
        {
            "status": comparison.status,
            "baseline": {
                "managed_path": str(baseline_path),
                "version": latest.version if latest else None,
            },
            "candidate": _result_summary(candidate),
            "production": _result_summary(production),
            "comparison": comparison.to_dict(),
        }
    )
    return payload


def compare_rows(candidate: QueryResult, production: QueryResult) -> RowComparison:
    candidate_counter = Counter(_canonical_row(row) for row in candidate.rows)
    production_counter = Counter(_canonical_row(row) for row in production.rows)
    missing = production_counter - candidate_counter
    unexpected = candidate_counter - production_counter
    status = "matched" if not missing and not unexpected else "different"
    return RowComparison(
        status=status,
        candidate_row_count=len(candidate.rows),
        production_row_count=len(production.rows),
        missing_from_candidate=tuple(_decode_rows(missing)),
        unexpected_in_candidate=tuple(_decode_rows(unexpected)),
    )


def _result_summary(result: QueryResult) -> dict[str, object]:
    return {
        "connection": result.connection_name,
        "columns": list(result.columns),
        "row_count": len(result.rows),
    }


def _canonical_row(row: dict[str, object]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def _decode_rows(counter: Counter[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for encoded in sorted(counter):
        rows.extend(json.loads(encoded) for _index in range(counter[encoded]))
    return rows
