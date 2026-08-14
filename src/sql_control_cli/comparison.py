from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import SqlctlConfig
from .database import (
    QueryResult,
    execute_query,
    sqlctl_input_parameter_names,
)
from .metadata import identity_key, parse_metadata, sql_body
from .query_store import stored_query_sql
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

    candidate, production, comparison = _compare_sql_texts(
        config,
        candidate_connection=candidate_connection,
        production_connection=production_connection,
        candidate_sql_text=sql_text,
        production_sql_text=baseline_path.read_text(encoding="utf-8"),
        parameters=parameters,
    )
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


def compare_application(
    config: SqlctlConfig,
    app_name: str,
    *,
    candidate_connection: str,
    production_connection: str,
    parameters: dict[str, object] | None = None,
) -> dict[str, object]:
    repository = Repository(config.storage_path)
    query_outputs: list[dict[str, object]] = []
    with repository.connect() as connection:
        rows = repository.queries_by_app(connection, app_name)
        if not rows:
            raise ComparisonError(
                f"Managed queries not found for application: {app_name}"
            )

        for row in rows:
            baseline_path = Path(str(row["managed_path"]))
            if not baseline_path.exists():
                raise ComparisonError(
                    f"Managed production baseline not found: {baseline_path}"
                )
            sql_text = baseline_path.read_text(encoding="utf-8")
            metadata = parse_metadata(sql_text)
            latest = repository.latest_revision(connection, metadata)
            candidate, production, comparison = _compare_sql_texts(
                config,
                candidate_connection=candidate_connection,
                production_connection=production_connection,
                candidate_sql_text=sql_text,
                production_sql_text=sql_text,
                parameters=parameters,
            )
            query_outputs.append(
                {
                    "identity_key": identity_key(metadata),
                    "query_name": metadata.query_name,
                    "connection_name": metadata.connection_name,
                    "app_name": metadata.app_name,
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

    different_count = sum(
        1 for query_output in query_outputs if query_output["status"] == "different"
    )
    return {
        "ok": True,
        "status": "matched" if different_count == 0 else "different",
        "app_name": app_name,
        "query_count": len(query_outputs),
        "matched_count": len(query_outputs) - different_count,
        "different_count": different_count,
        "queries": query_outputs,
    }


def compare_to_stored_query(
    sql_file: Path,
    config: SqlctlConfig,
    *,
    candidate_connection: str,
    store_connection: str,
    parameters: dict[str, object] | None = None,
    profile_name: str = "default",
    store_table: str = "query_store.dbo.sql_queries",
    store_sql_column: str = "Query_Value",
) -> dict[str, object]:
    validation = validate_sql_file(sql_file, config, profile_name=profile_name)
    if not validation.passed:
        return {"ok": False, "validation": validation.to_dict()}

    sql_text = sql_file.read_text(encoding="utf-8")
    metadata = parse_metadata(sql_text)
    reference_sql = stored_query_sql(
        config,
        connection_name=store_connection,
        table=store_table,
        sql_column=store_sql_column,
        query_name=metadata.query_name,
        connection_name_value=metadata.connection_name,
        app_name=metadata.app_name,
    )
    if reference_sql is None:
        raise ComparisonError(
            "Stored query not found for metadata: "
            f"Query_Name={metadata.query_name}, "
            f"Connection_Name={metadata.connection_name}, App_Name={metadata.app_name}"
        )

    candidate, reference, comparison = _compare_sql_texts(
        config,
        candidate_connection=candidate_connection,
        production_connection=store_connection,
        candidate_sql_text=sql_text,
        production_sql_text=reference_sql,
        parameters=parameters,
    )
    return {
        "ok": True,
        "status": comparison.status,
        "metadata": metadata.to_dict(),
        "validation": validation.to_dict(),
        "store": {
            "connection": store_connection,
            "table": store_table,
            "sql_column": store_sql_column,
        },
        "candidate": _result_summary(candidate),
        "reference": _result_summary(reference),
        "comparison": comparison.to_dict(),
    }


def query_store_compare_parameter_names(
    sql_file: Path,
    config: SqlctlConfig,
    *,
    store_connection: str,
    profile_name: str = "default",
    store_table: str = "query_store.dbo.sql_queries",
    store_sql_column: str = "Query_Value",
) -> tuple[str, ...]:
    validation = validate_sql_file(sql_file, config, profile_name=profile_name)
    if not validation.passed:
        return sqlctl_input_parameter_names(sql_file.read_text(encoding="utf-8"))
    sql_text = sql_file.read_text(encoding="utf-8")
    metadata = parse_metadata(sql_text)
    reference_sql = stored_query_sql(
        config,
        connection_name=store_connection,
        table=store_table,
        sql_column=store_sql_column,
        query_name=metadata.query_name,
        connection_name_value=metadata.connection_name,
        app_name=metadata.app_name,
    )
    names = list(sqlctl_input_parameter_names(sql_text))
    if reference_sql is not None:
        for name in sqlctl_input_parameter_names(reference_sql):
            if name.lower() not in {existing.lower() for existing in names}:
                names.append(name)
    return tuple(names)


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


def _compare_sql_texts(
    config: SqlctlConfig,
    *,
    candidate_connection: str,
    production_connection: str,
    candidate_sql_text: str,
    production_sql_text: str,
    parameters: dict[str, object] | None,
) -> tuple[QueryResult, QueryResult, RowComparison]:
    active_parameters = parameters or {}
    candidate = execute_query(
        config,
        connection_name=candidate_connection,
        sql=sql_body(candidate_sql_text),
        parameters=active_parameters,
    )
    production = execute_query(
        config,
        connection_name=production_connection,
        sql=sql_body(production_sql_text),
        parameters=active_parameters,
    )
    return candidate, production, compare_rows(candidate, production)


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
