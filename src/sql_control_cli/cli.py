from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from pathlib import Path

from .comparison import (
    compare_application,
    compare_to_production,
    compare_to_rpa_stored_query,
    rpa_compare_parameter_names,
)
from .config import load_config
from .database import (
    execute_query,
    execute_query_source,
    inspect_connection,
    parse_parameters,
)
from .managed import (
    capture,
    diff_source_to_managed,
    identity_from_text_or_key,
    managed_path,
)
from .metadata import (
    MetadataError,
    identity_key,
    metadata_json,
    parse_metadata,
    source_hash,
)
from .parity import audit_parity
from .publishing import deploy_test
from .storage import Repository
from .validation import validate_sql_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlctl")
    parser.add_argument(
        "--version", action="store_true", help="Print the sqlctl version."
    )
    parser.add_argument(
        "--config",
        action="append",
        type=Path,
        default=[],
        help="Load an extra TOML config file.",
    )
    parser.add_argument(
        "--storage-path", type=Path, help="Override the SQLite storage path."
    )
    parser.add_argument(
        "--managed-root", type=Path, help="Override the managed SQL root."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Colorize human-readable terminal output.",
    )
    subparsers = parser.add_subparsers(dest="command")

    metadata_parser = subparsers.add_parser("metadata", help="Parse SQL metadata.")
    metadata_parser.add_argument("sql_file", type=Path)

    hash_parser = subparsers.add_parser(
        "hash", help="Calculate the normalized source hash."
    )
    hash_parser.add_argument("sql_file", type=Path)

    capture_parser = subparsers.add_parser(
        "capture", help="Create or update a managed SQL copy."
    )
    capture_parser.add_argument("sql_file", type=Path)

    check_parser = subparsers.add_parser(
        "check", help="Check SQL metadata and workflow readiness rules."
    )
    check_parser.add_argument("sql_file", type=Path)
    check_parser.add_argument(
        "--profile", default="default", help="Validation profile name."
    )
    check_parser.add_argument(
        "--force-pass",
        action="store_true",
        help="Return success while reporting failures.",
    )

    prepare_parser = subparsers.add_parser(
        "prepare", help="Validate and capture a managed SQL copy."
    )
    prepare_parser.add_argument("sql_file", type=Path)
    prepare_parser.add_argument(
        "--profile", default="default", help="Validation profile name."
    )
    prepare_parser.add_argument(
        "--force-pass", action="store_true", help="Capture even when validation fails."
    )

    find_parser = subparsers.add_parser("find", help="Find managed queries.")
    find_parser.add_argument("term", nargs="?", default="")

    status_parser = subparsers.add_parser(
        "status", help="Show managed-copy status for a SQL file."
    )
    status_parser.add_argument("sql_file", type=Path)

    history_parser = subparsers.add_parser(
        "history", help="Show revision history for a SQL file or identity key."
    )
    history_parser.add_argument("identity")

    pull_parser = subparsers.add_parser(
        "pull", help="Copy a managed SQL file to a destination."
    )
    pull_parser.add_argument("identity")
    pull_parser.add_argument("destination", type=Path)

    diff_parser = subparsers.add_parser(
        "diff", help="Diff a source SQL file against its managed copy."
    )
    diff_parser.add_argument("sql_file", type=Path)

    db_parser = subparsers.add_parser("db", help="Database connection helpers.")
    db_subparsers = db_parser.add_subparsers(dest="db_command")

    db_inspect_parser = db_subparsers.add_parser(
        "inspect", help="Show configured database connection metadata."
    )
    db_inspect_parser.add_argument("connection")

    db_query_parser = db_subparsers.add_parser(
        "query", help="Execute a parameterized SQL query."
    )
    query_target = db_query_parser.add_mutually_exclusive_group(required=True)
    query_target.add_argument("--sql", help="SQL text to execute.")
    query_target.add_argument("--source", help="Configured repository source name.")
    db_query_parser.add_argument("--connection", help="Connection name for --sql.")
    db_query_parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Bind a named parameter as NAME=VALUE.",
    )

    compare_parser = subparsers.add_parser(
        "compare", help="Compare a SQL file to its managed production baseline."
    )
    compare_parser.add_argument("sql_file", type=Path)
    compare_parser.add_argument("--candidate-connection", required=True)
    compare_parser.add_argument("--production-connection", required=True)
    compare_parser.add_argument(
        "--profile", default="default", help="Validation profile name."
    )
    compare_parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Bind a named parameter as NAME=VALUE.",
    )

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate SQL results against the stored RPA query resolved from metadata.",
    )
    _add_rpa_validate_arguments(validate_parser)

    compare_app_parser = subparsers.add_parser(
        "compare-app", help="Compare all managed SQL for an application."
    )
    compare_app_parser.add_argument("app_name")
    compare_app_parser.add_argument("--candidate-connection", required=True)
    compare_app_parser.add_argument("--production-connection", required=True)
    compare_app_parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Bind a named parameter as NAME=VALUE.",
    )

    deploy_test_parser = subparsers.add_parser(
        "deploy-test", help="Publish a managed SQL query to the configured test target."
    )
    deploy_test_parser.add_argument("sql_file", type=Path)
    deploy_test_parser.add_argument(
        "--connection",
        help="Override [publishing.test] connection for this deployment.",
    )
    deploy_test_parser.add_argument(
        "--profile", default="default", help="Validation profile name."
    )

    parity_parser = subparsers.add_parser(
        "parity", help="Audit managed SQL parity against the configured test target."
    )
    parity_parser.add_argument(
        "--app", help="Limit the parity audit to an exact App_Name."
    )
    parity_parser.add_argument(
        "--connection",
        help="Override [publishing.test] connection for this audit.",
    )

    args = parser.parse_args(argv)
    if args.version:
        print("0.1.0")
        return 0
    if not args.command:
        parser.print_help()
        return 0

    config = load_config(
        config_paths=args.config,
        storage_path=args.storage_path,
        managed_root=args.managed_root,
    )
    try:
        return _run(args, config)
    except (MetadataError, OSError, ValueError) as err:
        _emit(
            {"ok": False, "error": str(err)}, json_output=args.json, stream=sys.stderr
        )
        return 2


def _add_rpa_validate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--candidate-connection", default="rpa_mssql")
    parser.add_argument("--store-connection", default="rpa_mssql")
    parser.add_argument("--profile", default="default", help="Validation profile name.")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Bind a named parameter as NAME=VALUE.",
    )
    parser.add_argument(
        "--param-csv",
        type=Path,
        help="Run once per CSV row, mapping columns to input parameter names.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Do not prompt for missing parameter values.",
    )
    parser.add_argument(
        "--store-table",
        default="wirpa_dev.dbo.rpa_SQL_Queries",
        help="RPA query-store table containing stored SQL.",
    )
    parser.add_argument(
        "--store-sql-column",
        default="SQL_Query",
        help="Column in --store-table containing stored SQL text.",
    )


def _run(args: argparse.Namespace, config) -> int:
    repository = Repository(config.storage_path)
    if args.command == "metadata":
        output = json.loads(metadata_json(args.sql_file.read_text(encoding="utf-8")))
    elif args.command == "hash":
        output = {
            "source_hash": source_hash(
                args.sql_file.read_text(encoding="utf-8"),
                normalize_whitespace=config.normalize_whitespace,
            )
        }
    elif args.command == "capture":
        result = capture(args.sql_file, config)
        output = {
            "action": result.action,
            "identity_key": identity_key(result.metadata),
            "source_hash": result.source_hash,
            "managed_path": str(result.managed_path),
            "version": result.revision.version,
        }
    elif args.command == "check":
        result = validate_sql_file(
            args.sql_file,
            config,
            profile_name=args.profile,
            force_pass=args.force_pass,
        )
        if args.json:
            _emit(
                result.to_dict(),
                json_output=True,
                stream=sys.stderr if not result.passed else None,
            )
        else:
            _emit_validation_result(
                result,
                color=args.color,
                stream=sys.stderr if not result.passed else None,
            )
        return 0 if result.passed else 2
    elif args.command == "prepare":
        validation = validate_sql_file(
            args.sql_file,
            config,
            profile_name=args.profile,
            force_pass=args.force_pass,
        )
        if not validation.passed:
            _emit(
                {"validation": validation.to_dict()},
                json_output=args.json,
                stream=sys.stderr,
            )
            return 2
        result = capture(args.sql_file, config)
        output = {
            "validation": validation.to_dict(),
            "capture": {
                "action": result.action,
                "identity_key": identity_key(result.metadata),
                "source_hash": result.source_hash,
                "managed_path": str(result.managed_path),
                "version": result.revision.version,
            },
        }
    elif args.command == "find":
        with repository.connect() as connection:
            rows = (
                repository.find_queries(connection, args.term)
                if args.term
                else repository.all_queries(connection)
            )
        output = {"queries": [_query_row(row) for row in rows]}
    elif args.command == "status":
        sql_text = args.sql_file.read_text(encoding="utf-8")
        metadata = parse_metadata(sql_text)
        path = managed_path(config, metadata)
        digest = source_hash(sql_text, normalize_whitespace=config.normalize_whitespace)
        with repository.connect() as connection:
            latest = repository.latest_revision(connection, metadata)
        output = {
            "identity_key": identity_key(metadata),
            "managed_path": str(path),
            "managed_exists": path.exists(),
            "latest_version": latest.version if latest else None,
            "changed": latest.source_hash != digest if latest else True,
        }
    elif args.command == "history":
        key = identity_from_text_or_key(args.identity)
        with repository.connect() as connection:
            revisions = repository.history(connection, key)
        output = {
            "identity_key": key,
            "revisions": [revision.__dict__ for revision in revisions],
        }
    elif args.command == "pull":
        key = identity_from_text_or_key(args.identity)
        with repository.connect() as connection:
            row = repository.query(connection, key)
        if row is None:
            raise ValueError(f"Managed query not found: {key}")
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(row["managed_path"], args.destination)
        output = {"copied": str(args.destination), "managed_path": row["managed_path"]}
    elif args.command == "diff":
        metadata = parse_metadata(args.sql_file.read_text(encoding="utf-8"))
        path = managed_path(config, metadata)
        if not path.exists():
            raise ValueError(f"Managed copy not found: {path}")
        diff = diff_source_to_managed(args.sql_file, path)
        if not args.json:
            print(diff, end="")
            return 1 if diff else 0
        output = {"diff": diff}
    elif args.command == "db":
        if args.db_command == "inspect":
            output = inspect_connection(config, args.connection).to_dict()
        elif args.db_command == "query":
            parameters = parse_parameters(args.param)
            if args.source:
                output = execute_query_source(
                    config,
                    args.source,
                    parameters=parameters,
                ).to_dict()
            else:
                if not args.connection:
                    raise ValueError("--connection is required with --sql")
                output = execute_query(
                    config,
                    connection_name=args.connection,
                    sql=args.sql,
                    parameters=parameters,
                ).to_dict()
        else:
            raise ValueError("Unsupported db command")
    elif args.command == "compare":
        output = compare_to_production(
            args.sql_file,
            config,
            candidate_connection=args.candidate_connection,
            production_connection=args.production_connection,
            parameters=parse_parameters(args.param),
            profile_name=args.profile,
        )
        if not output["ok"]:
            _emit(output, json_output=args.json, stream=sys.stderr)
            return 2
    elif args.command == "validate":
        parameter_names = rpa_compare_parameter_names(
            args.sql_file,
            config,
            store_connection=args.store_connection,
            profile_name=args.profile,
            store_table=args.store_table,
            store_sql_column=args.store_sql_column,
        )
        if args.param_csv:
            output = _compare_rpa_csv(args, config)
        else:
            parameters = parse_parameters(args.param)
            if not args.no_prompt:
                parameters = _prompt_for_missing_parameters(
                    parameters, parameter_names
                )
            output = _compare_rpa_once(args, config, parameters)
        if not output["ok"]:
            _emit(output, json_output=args.json, stream=sys.stderr)
            return 2
    elif args.command == "compare-app":
        output = compare_application(
            config,
            args.app_name,
            candidate_connection=args.candidate_connection,
            production_connection=args.production_connection,
            parameters=parse_parameters(args.param),
        )
    elif args.command == "deploy-test":
        output = deploy_test(
            args.sql_file,
            config,
            connection_name=args.connection,
            profile_name=args.profile,
        )
        if not output["ok"]:
            _emit(output, json_output=args.json, stream=sys.stderr)
            return 2
    elif args.command == "parity":
        output = audit_parity(
            config,
            app_name=args.app,
            connection_name=args.connection,
        )
        if not output["ok"]:
            _emit(output, json_output=args.json, stream=sys.stderr)
            return 1
    else:
        raise ValueError(f"Unsupported command: {args.command}")
    _emit(output, json_output=args.json)
    return 0


def _query_row(row) -> dict[str, str]:
    return {
        "identity_key": row["identity_key"],
        "query_name": row["query_name"],
        "connection_name": row["connection_name"],
        "app_name": row["app_name"],
        "managed_path": row["managed_path"],
    }


def _prompt_for_missing_parameters(
    parameters: dict[str, object], parameter_names: tuple[str, ...]
) -> dict[str, object]:
    resolved = dict(parameters)
    existing = {name.lower().lstrip("@") for name in resolved}
    for name in parameter_names:
        normalized = name.lower().lstrip("@")
        if normalized in existing:
            continue
        value = input(f"{name}: ")
        resolved.update(parse_parameters([f"{name}={value}"]))
        existing.add(normalized)
    return resolved


def _compare_rpa_once(args, config, parameters: dict[str, object]) -> dict[str, object]:
    return compare_to_rpa_stored_query(
        args.sql_file,
        config,
        candidate_connection=args.candidate_connection,
        store_connection=args.store_connection,
        parameters=parameters,
        profile_name=args.profile,
        store_table=args.store_table,
        store_sql_column=args.store_sql_column,
    )


def _compare_rpa_csv(args, config) -> dict[str, object]:
    base_parameters = parse_parameters(args.param)
    runs = []
    for row_number, row_parameters in _csv_parameter_rows(args.param_csv):
        parameters = {**base_parameters, **row_parameters}
        result = _compare_rpa_once(args, config, parameters)
        runs.append(
            {
                "row": row_number,
                "parameters": parameters,
                "result": result,
            }
        )
    different_count = sum(
        1 for run in runs if run["result"].get("status") == "different"
    )
    return {
        "ok": all(run["result"].get("ok") for run in runs),
        "status": "matched" if different_count == 0 else "different",
        "run_count": len(runs),
        "different_count": different_count,
        "runs": runs,
    }


def _csv_parameter_rows(path: Path) -> list[tuple[int, dict[str, object]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV parameter file has no header row: {path}")
        rows = []
        for row_number, row in enumerate(reader, start=2):
            values = [
                f"{name}={value}"
                for name, value in row.items()
                if name is not None and value is not None and value != ""
            ]
            rows.append((row_number, parse_parameters(values)))
    return rows


def _emit(payload: object, *, json_output: bool, stream=None) -> None:
    active_stream = sys.stdout if stream is None else stream
    if json_output:
        print(json.dumps(payload, indent=2), file=active_stream)
    elif isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}", file=active_stream)
    else:
        print(payload, file=active_stream)


ANSI_COLORS = {
    "reset": "\033[0m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "bold": "\033[1m",
}


def _emit_validation_result(result, *, color: str = "auto", stream=None) -> None:
    active_stream = sys.stdout if stream is None else stream
    use_color = _should_colorize(color, active_stream)
    status = _color_text(result.status, _status_color(result.status), use_color)
    print(f"Validation {status}: {result.sql_file}", file=active_stream)
    print(f"Profile: {result.profile_name}", file=active_stream)
    print(f"Rules: {', '.join(result.enabled_rules)}", file=active_stream)
    if not result.issues:
        print(_color_text("No issues found.", "green", use_color), file=active_stream)
        return
    print(_color_text("Issues:", "bold", use_color), file=active_stream)
    for issue in result.issues:
        severity = _color_text(issue.severity, _severity_color(issue.severity), use_color)
        location = _issue_location(issue)
        location = _color_text(location, "blue", use_color)
        print(
            f"- [{severity}] {issue.rule} ({location}): {issue.message}",
            file=active_stream,
        )


def _issue_location(issue) -> str:
    if issue.line is None:
        return "line n/a"
    if issue.line_end is not None and issue.line_end != issue.line:
        return f"line {issue.line}-{issue.line_end}"
    return f"line {issue.line}"


def _should_colorize(color: str, stream) -> bool:
    if color == "always":
        return True
    if color == "never" or os.environ.get("NO_COLOR") is not None:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _color_text(value: str, color: str, enabled: bool) -> str:
    if not enabled:
        return value
    return f"{ANSI_COLORS[color]}{value}{ANSI_COLORS['reset']}"


def _status_color(status: str) -> str:
    return {
        "passed": "green",
        "warning": "yellow",
        "failed": "red",
        "forced": "yellow",
    }.get(status, "bold")


def _severity_color(severity: str) -> str:
    return {
        "error": "red",
        "warning": "yellow",
    }.get(severity, "bold")
