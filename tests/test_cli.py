from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sql_control_cli.cli import main
from sql_control_cli.config import load_config
from sql_control_cli.database import execute_query, inspect_connection
from sql_control_cli.metadata import parse_metadata, source_hash


def test_cli_version() -> None:
    assert main(["--version"]) == 0


def sql_fixture() -> str:
    return """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
Team: Benefits
Comparison Keys: participant_id
*/

select *
from participants
where participant_id = @participant_id;
"""


def test_parse_required_metadata() -> None:
    metadata = parse_metadata(sql_fixture())

    assert metadata.query_name == "Participant Lookup"
    assert metadata.connection_name == "Main Warehouse"
    assert metadata.app_name == "Defined Benefits"
    assert metadata.team_name == "Benefits"
    assert metadata.identity == (
        "participant-lookup",
        "main-warehouse",
        "defined-benefits",
    )
    assert metadata.comparison_keys == ("participant_id",)


def test_hash_excludes_managed_version_fields() -> None:
    unmanaged = sql_fixture()
    managed = unmanaged.replace(
        "Comparison Keys: participant_id",
        "Version: 3\nDate Changed: 2026-08-10\nComparison Keys: participant_id",
    )

    assert source_hash(unmanaged) == source_hash(managed)


def test_capture_creates_managed_copy_and_revision(tmp_path: Path) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    config = load_config(
        storage_path=tmp_path / "state.sqlite3",
        managed_root=tmp_path / "managed",
        env={},
    )

    assert (
        main(
            [
                "--storage-path",
                str(config.storage_path),
                "--managed-root",
                str(config.managed_root),
                "--json",
                "capture",
                str(source),
            ]
        )
        == 0
    )

    managed = (
        tmp_path
        / "managed"
        / "defined-benefits"
        / "main-warehouse"
        / "participant-lookup.sql"
    )
    assert managed.exists()
    assert "Version: 1" in managed.read_text(encoding="utf-8")


def test_status_reports_unchanged_after_capture(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    storage = tmp_path / "state.sqlite3"
    managed = tmp_path / "managed"
    base_args = [
        "--storage-path",
        str(storage),
        "--managed-root",
        str(managed),
        "--json",
    ]

    assert main([*base_args, "capture", str(source)]) == 0
    capsys.readouterr()
    assert main([*base_args, "status", str(source)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["changed"] is False
    assert output["latest_version"] == 1


def write_validation_config(path: Path) -> Path:
    config = path / "sqlctl.toml"
    config.write_text(
        """
[validation.profiles.strict]
enabled_rules = ["required_metadata", "allowed_team", "allowed_app", "comparison_keys_required"]
allowed_teams = ["Benefits"]
allowed_apps = ["Defined Benefits"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def test_validate_uses_profile_rules(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    config = write_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "strict",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert output["enabled_rules"] == [
        "required_metadata",
        "allowed_team",
        "allowed_app",
        "comparison_keys_required",
    ]
    assert output["issues"] == []


def test_validate_reports_application_and_team_failures(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        sql_fixture()
        .replace("App_Name: Defined Benefits", "App_Name: Other App")
        .replace("Team: Benefits", "Team: Finance"),
        encoding="utf-8",
    )
    config = write_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "strict",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert output["status"] == "failed"
    assert [issue["rule"] for issue in output["issues"]] == [
        "allowed_team",
        "allowed_app",
    ]


def test_validate_force_pass_reports_forced_success(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture().replace("Team: Benefits\n", ""), encoding="utf-8")
    config = write_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "strict",
                "--force-pass",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "forced"
    assert [issue["rule"] for issue in output["issues"]] == ["allowed_team"]


def test_prepare_refuses_to_capture_on_validation_failure(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        sql_fixture().replace("Comparison Keys: participant_id\n", ""), encoding="utf-8"
    )
    config_path = write_validation_config(tmp_path)
    storage = tmp_path / "state.sqlite3"
    managed = tmp_path / "managed"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--storage-path",
                str(storage),
                "--managed-root",
                str(managed),
                "--json",
                "prepare",
                str(source),
                "--profile",
                "strict",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert output["validation"]["status"] == "failed"
    assert not managed.exists()


def test_prepare_force_pass_captures_with_validation_context(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        sql_fixture().replace("Comparison Keys: participant_id\n", ""), encoding="utf-8"
    )
    config_path = write_validation_config(tmp_path)
    storage = tmp_path / "state.sqlite3"
    managed = tmp_path / "managed"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--storage-path",
                str(storage),
                "--managed-root",
                str(managed),
                "--json",
                "prepare",
                str(source),
                "--profile",
                "strict",
                "--force-pass",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["validation"]["status"] == "forced"
    assert output["capture"]["action"] == "created"
    assert Path(output["capture"]["managed_path"]).exists()


def create_participant_database(
    path: Path,
    rows: list[tuple[int, str, int]] | None = None,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE participants (
                participant_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                active INTEGER NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO participants (participant_id, name, active) VALUES (?, ?, ?)",
            rows or [(1, "Ada", 1), (2, "Grace", 0)],
        )


def write_database_config(path: Path, database_path: Path) -> Path:
    config = path / "sqlctl.toml"
    config.write_text(
        f"""
[database.connections.warehouse]
driver = "sqlite"
path = "{database_path}"

[repository.sources.participant_lookup]
connection = "warehouse"
sql = "select participant_id, name from participants where participant_id = :participant_id"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def test_database_adapter_inspects_and_executes_parameterized_query(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "warehouse.sqlite3"
    create_participant_database(database_path)
    config_path = write_database_config(tmp_path, database_path)
    config = load_config(config_paths=[config_path], env={})

    info = inspect_connection(config, "warehouse")
    result = execute_query(
        config,
        connection_name="warehouse",
        sql="select name, active from participants where participant_id = :participant_id",
        parameters={"participant_id": 1},
    )

    assert info.to_dict() == {
        "name": "warehouse",
        "driver": "sqlite",
        "path": str(database_path),
    }
    assert result.to_dict() == {
        "connection": "warehouse",
        "source": None,
        "columns": ["name", "active"],
        "row_count": 1,
        "rows": [{"name": "Ada", "active": 1}],
    }


def test_db_query_runs_configured_repository_source(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "warehouse.sqlite3"
    create_participant_database(database_path)
    config_path = write_database_config(tmp_path, database_path)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--json",
                "db",
                "query",
                "--source",
                "participant_lookup",
                "--param",
                "participant_id=2",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["connection"] == "warehouse"
    assert output["source"] == "participant_lookup"
    assert output["columns"] == ["participant_id", "name"]
    assert output["rows"] == [{"participant_id": 2, "name": "Grace"}]


def test_db_query_requires_connection_for_inline_sql(tmp_path: Path, capsys) -> None:
    config_path = write_database_config(tmp_path, tmp_path / "warehouse.sqlite3")

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--json",
                "db",
                "query",
                "--sql",
                "select 1",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert output["ok"] is False
    assert output["error"] == "--connection is required with --sql"


def test_db_query_reports_missing_source_deterministically(
    tmp_path: Path, capsys
) -> None:
    database_path = tmp_path / "warehouse.sqlite3"
    create_participant_database(database_path)
    config_path = write_database_config(tmp_path, database_path)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--json",
                "db",
                "query",
                "--source",
                "missing",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert output["ok"] is False
    assert "Query source not found: missing" in output["error"]


def comparison_fixture(order_by: str = "") -> str:
    return f"""/*
Query_Name: Participant Active Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
Team: Benefits
Comparison Keys: participant_id
*/

select participant_id, name
from participants
where active = :active
{order_by};
"""


def application_comparison_fixture(query_name: str, select_sql: str) -> str:
    return f"""/*
Query_Name: {query_name}
Connection_Name: Main Warehouse
App_Name: Defined Benefits
Team: Benefits
Comparison Keys: participant_id
*/

{select_sql}
"""


def write_comparison_config(
    path: Path,
    *,
    candidate_database: Path,
    production_database: Path,
) -> Path:
    config = path / "sqlctl.toml"
    config.write_text(
        f"""
[database.connections.candidate]
driver = "sqlite"
path = "{candidate_database}"

[database.connections.production]
driver = "sqlite"
path = "{production_database}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def compare_base_args(tmp_path: Path, config_path: Path) -> list[str]:
    return [
        "--config",
        str(config_path),
        "--storage-path",
        str(tmp_path / "state.sqlite3"),
        "--managed-root",
        str(tmp_path / "managed"),
        "--json",
    ]


def test_compare_reports_first_time_without_baseline(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(comparison_fixture(), encoding="utf-8")
    candidate_db = tmp_path / "candidate.sqlite3"
    production_db = tmp_path / "production.sqlite3"
    create_participant_database(candidate_db)
    create_participant_database(production_db)
    config_path = write_comparison_config(
        tmp_path,
        candidate_database=candidate_db,
        production_database=production_db,
    )

    assert (
        main(
            [
                *compare_base_args(tmp_path, config_path),
                "compare",
                str(source),
                "--candidate-connection",
                "candidate",
                "--production-connection",
                "production",
                "--param",
                "active=1",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "first_time"
    assert (
        output["identity_key"]
        == "participant-active-lookup::main-warehouse::defined-benefits"
    )
    assert output["baseline"] is None


def test_compare_matches_independent_of_row_order(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        comparison_fixture("order by participant_id asc"), encoding="utf-8"
    )
    candidate_db = tmp_path / "candidate.sqlite3"
    production_db = tmp_path / "production.sqlite3"
    rows = [(1, "Ada", 1), (3, "Alan", 1), (2, "Grace", 0)]
    create_participant_database(candidate_db, rows=list(reversed(rows)))
    create_participant_database(production_db, rows=rows)
    config_path = write_comparison_config(
        tmp_path,
        candidate_database=candidate_db,
        production_database=production_db,
    )
    base_args = compare_base_args(tmp_path, config_path)

    assert main([*base_args, "capture", str(source)]) == 0
    capsys.readouterr()
    source.write_text(
        comparison_fixture("order by participant_id desc"), encoding="utf-8"
    )

    assert (
        main(
            [
                *base_args,
                "compare",
                str(source),
                "--candidate-connection",
                "candidate",
                "--production-connection",
                "production",
                "--param",
                "active=1",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "matched"
    assert output["baseline"]["version"] == 1
    assert output["comparison"] == {
        "status": "matched",
        "candidate_row_count": 2,
        "production_row_count": 2,
        "missing_from_candidate": [],
        "unexpected_in_candidate": [],
    }


def test_compare_reports_different_rows_deterministically(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(comparison_fixture(), encoding="utf-8")
    candidate_db = tmp_path / "candidate.sqlite3"
    production_db = tmp_path / "production.sqlite3"
    create_participant_database(candidate_db, rows=[(1, "Ada Changed", 1)])
    create_participant_database(production_db, rows=[(1, "Ada", 1)])
    config_path = write_comparison_config(
        tmp_path,
        candidate_database=candidate_db,
        production_database=production_db,
    )
    base_args = compare_base_args(tmp_path, config_path)

    assert main([*base_args, "capture", str(source)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                *base_args,
                "compare",
                str(source),
                "--candidate-connection",
                "candidate",
                "--production-connection",
                "production",
                "--param",
                "active=1",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "different"
    assert output["comparison"]["missing_from_candidate"] == [
        {"name": "Ada", "participant_id": 1}
    ]
    assert output["comparison"]["unexpected_in_candidate"] == [
        {"name": "Ada Changed", "participant_id": 1}
    ]


def test_compare_app_matches_all_managed_queries(tmp_path: Path, capsys) -> None:
    lookup = tmp_path / "lookup.sql"
    names = tmp_path / "names.sql"
    lookup.write_text(
        application_comparison_fixture(
            "Participant Active Lookup",
            """
select participant_id, name
from participants
where active = :active
order by participant_id asc;
""".strip(),
        ),
        encoding="utf-8",
    )
    names.write_text(
        application_comparison_fixture(
            "Participant Active Names",
            """
select participant_id, name
from participants
where active = :active
order by participant_id desc;
""".strip(),
        ),
        encoding="utf-8",
    )
    candidate_db = tmp_path / "candidate.sqlite3"
    production_db = tmp_path / "production.sqlite3"
    rows = [(1, "Ada", 1), (3, "Alan", 1), (2, "Grace", 0)]
    create_participant_database(candidate_db, rows=list(reversed(rows)))
    create_participant_database(production_db, rows=rows)
    config_path = write_comparison_config(
        tmp_path,
        candidate_database=candidate_db,
        production_database=production_db,
    )
    base_args = compare_base_args(tmp_path, config_path)

    assert main([*base_args, "capture", str(lookup)]) == 0
    assert main([*base_args, "capture", str(names)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                *base_args,
                "compare-app",
                "Defined Benefits",
                "--candidate-connection",
                "candidate",
                "--production-connection",
                "production",
                "--param",
                "active=1",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "matched"
    assert output["query_count"] == 2
    assert output["matched_count"] == 2
    assert output["different_count"] == 0
    assert [query["query_name"] for query in output["queries"]] == [
        "Participant Active Lookup",
        "Participant Active Names",
    ]
    assert {query["status"] for query in output["queries"]} == {"matched"}


def test_compare_app_reports_different_aggregate(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(comparison_fixture(), encoding="utf-8")
    candidate_db = tmp_path / "candidate.sqlite3"
    production_db = tmp_path / "production.sqlite3"
    create_participant_database(candidate_db, rows=[(1, "Ada Changed", 1)])
    create_participant_database(production_db, rows=[(1, "Ada", 1)])
    config_path = write_comparison_config(
        tmp_path,
        candidate_database=candidate_db,
        production_database=production_db,
    )
    base_args = compare_base_args(tmp_path, config_path)

    assert main([*base_args, "capture", str(source)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                *base_args,
                "compare-app",
                "Defined Benefits",
                "--candidate-connection",
                "candidate",
                "--production-connection",
                "production",
                "--param",
                "active=1",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "different"
    assert output["matched_count"] == 0
    assert output["different_count"] == 1
    assert output["queries"][0]["comparison"]["missing_from_candidate"] == [
        {"name": "Ada", "participant_id": 1}
    ]
    assert output["queries"][0]["comparison"]["unexpected_in_candidate"] == [
        {"name": "Ada Changed", "participant_id": 1}
    ]


def test_compare_app_reports_missing_application(tmp_path: Path, capsys) -> None:
    candidate_db = tmp_path / "candidate.sqlite3"
    production_db = tmp_path / "production.sqlite3"
    create_participant_database(candidate_db)
    create_participant_database(production_db)
    config_path = write_comparison_config(
        tmp_path,
        candidate_database=candidate_db,
        production_database=production_db,
    )

    assert (
        main(
            [
                *compare_base_args(tmp_path, config_path),
                "compare-app",
                "Defined Benefits",
                "--candidate-connection",
                "candidate",
                "--production-connection",
                "production",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert output["ok"] is False
    assert (
        output["error"] == "Managed queries not found for application: Defined Benefits"
    )
