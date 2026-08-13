from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

from sql_control_cli import config as config_module
from sql_control_cli.cli import main
from sql_control_cli.config import load_config
from sql_control_cli.database import (
    DatabaseConnectionConfig,
    MSSQLAdapter,
    column_probe_sql,
    execute_query,
    final_query_select_statement,
    inspect_connection,
    mssql_named_parameters,
    probe_parameters,
    strip_top_level_order_by,
)
from sql_control_cli.metadata import parse_metadata, source_hash
from sql_control_cli.validation import DEFAULT_RULES

ACTIVE_RULES = tuple(
    rule
    for rule in DEFAULT_RULES
    if rule not in {"allowed_team", "comparison_keys_required"}
)


def test_cli_version() -> None:
    assert main(["--version"]) == 0


def test_pyproject_documents_sqlctl_runtime_contract() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    sqlctl = pyproject["tool"]["sqlctl"]

    assert tuple(sqlctl["validation"]["implemented_rules"]) == DEFAULT_RULES
    assert sqlctl["validation"]["default_rules"] == ["required_metadata"]
    assert sqlctl["validation"]["warning_rules"] == ["column_compare"]
    assert "column_compare" not in sqlctl["validation"]["error_rules"]
    assert "comparison_keys_required" not in sqlctl["validation"]["error_rules"]
    assert tuple(sqlctl["validation"]["profiles"]["default"]["enabled_rules"]) == (
        ACTIVE_RULES
    )
    assert tuple(sqlctl["validation"]["profiles"]["strict"]["enabled_rules"]) == (
        ACTIVE_RULES
    )
    assert sqlctl["validation"]["profiles"]["columns"]["enabled_rules"] == [
        "required_metadata",
        "column_compare",
    ]
    assert (
        sqlctl["validation"]["profiles"]["columns"]["column_compare_connection"]
        == "rpa_mssql"
    )

    mssql_fields = sqlctl["database"]["connection_fields"]["mssql"]
    assert mssql_fields["required"] == [
        "driver",
        "sql_driver",
        "server_env",
        "database_env",
        "username_env",
        "password_env",
    ]
    assert mssql_fields["secret_fields"] == ["username", "password"]
    assert mssql_fields["secret_env_fields"] == ["username_env", "password_env"]

    template = sqlctl["database"]["connection_templates"]["rpa_mssql"]
    assert template["driver"] == "mssql"
    assert template["server_env"] == "SQLCTL_RPA_MSSQL_SERVER"
    assert template["database_env"] == "SQLCTL_RPA_MSSQL_DATABASE"
    assert template["username_env"] == "SQLCTL_RPA_MSSQL_USERNAME"
    assert template["password_env"] == "SQLCTL_RPA_MSSQL_PASSWORD"


def sql_fixture() -> str:
    return """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
Team: Benefits
Comparison Keys: participant_id
*/

select participant_id, name
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


def test_parse_metadata_from_star_prefixed_block_comment() -> None:
    metadata = parse_metadata(
        """/*
 * Query_Name: DB_Validation_7
 * Connection_Name: DBCS_AzureConnection
 * App_Name: DBCS
 */

select 1;
"""
    )

    assert metadata.query_name == "DB_Validation_7"
    assert metadata.connection_name == "DBCS_AzureConnection"
    assert metadata.app_name == "DBCS"


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


def test_load_config_reads_package_env_file(
    tmp_path: Path, monkeypatch
) -> None:
    package_env = tmp_path / ".env"
    storage_path = tmp_path / "from-env.sqlite3"
    managed_root = tmp_path / "from-env-managed"
    package_env.write_text(
        f"""
SQLCTL_STORAGE_PATH={storage_path}
SQLCTL_MANAGED_ROOT="{managed_root}"
SQLCTL_NORMALIZE_WHITESPACE=false
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "default_package_env_path", lambda: package_env)

    config = load_config(env={})

    assert config.storage_path == storage_path
    assert config.managed_root == managed_root
    assert config.normalize_whitespace is False


def test_real_environment_overrides_package_env_file(
    tmp_path: Path, monkeypatch
) -> None:
    package_env = tmp_path / ".env"
    package_env.write_text(
        f"SQLCTL_STORAGE_PATH={tmp_path / 'from-env.sqlite3'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "default_package_env_path", lambda: package_env)

    config = load_config(env={"SQLCTL_STORAGE_PATH": str(tmp_path / "from-shell.sqlite3")})

    assert config.storage_path == tmp_path / "from-shell.sqlite3"


def test_load_config_uses_package_pyproject_sqlctl_connection_template(
    tmp_path: Path, monkeypatch
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    package_env = package_root / ".env"
    package_env.write_text(
        """
SQLCTL_RPA_MSSQL_SERVER=rpa-server
SQLCTL_RPA_MSSQL_DATABASE=RPA
SQLCTL_RPA_MSSQL_USERNAME=rpa-user
SQLCTL_RPA_MSSQL_PASSWORD=rpa-password
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (package_root / "pyproject.toml").write_text(
        """
[tool.sqlctl.validation.profiles.strict]
enabled_rules = ["required_metadata", "comparison_keys_required"]
column_compare_connection = "rpa_mssql"

[tool.sqlctl.database.connection_templates.rpa_mssql]
driver = "mssql"
sql_driver = "ODBC Driver 17 for SQL Server"
server_env = "SQLCTL_RPA_MSSQL_SERVER"
port = 1433
database_env = "SQLCTL_RPA_MSSQL_DATABASE"
username_env = "SQLCTL_RPA_MSSQL_USERNAME"
password_env = "SQLCTL_RPA_MSSQL_PASSWORD"
trust_server_certificate = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "default_package_env_path", lambda: package_env)

    config = load_config(env={})

    connection = config.database_connections["rpa_mssql"]
    assert config.validation_profiles["strict"].enabled_rules == (
        "required_metadata",
        "comparison_keys_required",
    )
    assert config.validation_profiles["strict"].column_compare_connection == "rpa_mssql"
    assert connection.driver == "mssql"
    assert connection.sql_driver == "ODBC Driver 17 for SQL Server"
    assert connection.server == "rpa-server"
    assert connection.port == 1433
    assert connection.database == "RPA"
    assert connection.username == "rpa-user"
    assert connection.password == "rpa-password"
    assert connection.trust_server_certificate is True


def test_load_config_parses_mssql_connection_with_env_secrets(tmp_path: Path) -> None:
    config_path = tmp_path / "sqlctl.toml"
    config_path.write_text(
        """
[database.connections.rpa_mssql]
driver = "mssql"
sql_driver = "ODBC Driver 17 for SQL Server"
server = "rpa-server"
port = 1433
database = "RPA"
username_env = "SQLCTL_RPA_MSSQL_USERNAME"
password_env = "SQLCTL_RPA_MSSQL_PASSWORD"
trust_server_certificate = true
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(
        config_paths=[config_path],
        env={
            "SQLCTL_RPA_MSSQL_USERNAME": "rpa-user",
            "SQLCTL_RPA_MSSQL_PASSWORD": "rpa-password",
        },
    )

    connection = config.database_connections["rpa_mssql"]
    assert connection.driver == "mssql"
    assert connection.sql_driver == "ODBC Driver 17 for SQL Server"
    assert connection.server == "rpa-server"
    assert connection.port == 1433
    assert connection.database == "RPA"
    assert connection.username == "rpa-user"
    assert connection.password == "rpa-password"
    assert connection.trust_server_certificate is True


def test_mssql_named_parameters_translate_for_pyodbc() -> None:
    sql, parameters = mssql_named_parameters(
        "select * from dbo.Participants where participant_id = @participant_id "
        "and plan_id = @plan_id or fallback_id = @participant_id",
        {"participant_id": 123, "plan_id": "011"},
    )

    assert sql == (
        "select * from dbo.Participants where participant_id = ? "
        "and plan_id = ? or fallback_id = ?"
    )
    assert parameters == (123, "011", 123)


def test_mssql_connection_string_keeps_named_instance_without_port() -> None:
    adapter = MSSQLAdapter(
        "rpa_mssql",
        DatabaseConnectionConfig(
            driver="mssql",
            sql_driver="ODBC Driver 17 for SQL Server",
            server=r"rpa-server\sqlinst",
            port=1433,
            database="wirpa_dev",
            username="rpa-user",
            password="rpa-password",
            trust_server_certificate=True,
        ),
    )

    connection_string = adapter._connection_string()

    assert "SERVER={rpa-server\\sqlinst}" in connection_string
    assert "SERVER={rpa-server\\sqlinst,1433}" not in connection_string


def test_mssql_connection_string_appends_port_for_plain_server() -> None:
    adapter = MSSQLAdapter(
        "rpa_mssql",
        DatabaseConnectionConfig(
            driver="mssql",
            sql_driver="ODBC Driver 17 for SQL Server",
            server="rpa-server",
            port=1433,
            database="wirpa_dev",
            username="rpa-user",
            password="rpa;password",
            trust_server_certificate=True,
        ),
    )

    connection_string = adapter._connection_string()

    assert "SERVER={rpa-server,1433}" in connection_string
    assert "PWD={rpa;password}" in connection_string


def test_column_probe_sql_and_parameters_are_zero_row_safe() -> None:
    sql = "select participant_id, name from dbo.Participants where participant_id = @participant_id;"

    assert column_probe_sql(sql) == (
        "select * from (\n"
        "select participant_id, name from dbo.Participants where participant_id = @participant_id\n"
        ") as sqlctl_column_probe where 1 = 0"
    )
    assert probe_parameters(sql) == {"participant_id": None}


def test_column_probe_strips_only_final_top_level_order_by() -> None:
    sql = (
        "select participant_id, "
        "(select max(plan_id) from plans order by plan_id) as latest_plan "
        "from participants order by participant_id"
    )

    assert strip_top_level_order_by(sql) == (
        "select participant_id, "
        "(select max(plan_id) from plans order by plan_id) as latest_plan "
        "from participants"
    )


def test_final_query_select_statement_uses_last_select_from_multistatement_script() -> None:
    sql = """
DECLARE @p1 char(50)
DECLARE @p2 char(50)

create table #planexclude (clnt_id_n int, db_plan_n char(5))
insert into #planexclude values (722846, '011')

select participant_id, name
from #results
where participant_id = @participant_id
order by participant_id;
"""

    assert final_query_select_statement(sql) == (
        "select participant_id, name\n"
        "from #results\n"
        "where participant_id = @participant_id\n"
        "order by participant_id"
    )


def test_final_query_select_statement_keeps_cte_for_last_select() -> None:
    sql = """
DECLARE @p1 char(50)

with final_rows as (
    select participant_id, name from participants
)
select participant_id, name
from final_rows;
"""

    assert final_query_select_statement(sql) == (
        "with final_rows as (\n"
        "    select participant_id, name from participants\n"
        ")\n"
        "select participant_id, name\n"
        "from final_rows"
    )


def test_final_query_select_statement_stops_before_cleanup_without_semicolon() -> None:
    sql = """
DECLARE @p1 char(50)

select participant_id, name
from #results
where participant_id = @participant_id
drop table #results
"""

    assert final_query_select_statement(sql) == (
        "select participant_id, name\n"
        "from #results\n"
        "where participant_id = @participant_id"
    )


def test_final_query_select_statement_ignores_intermediate_selects() -> None:
    sql = """
select setup_id
from #setup;

insert into #results
select participant_id, name
from participants;

select participant_id, name, status
from #results;
"""

    assert final_query_select_statement(sql) == (
        "select participant_id, name, status\n"
        "from #results"
    )


def write_validation_config(path: Path) -> Path:
    config = path / "sqlctl.toml"
    config.write_text(
        """
[validation.profiles.strict]
enabled_rules = ["required_metadata", "allowed_team", "allowed_app"]
allowed_teams = ["Benefits"]
allowed_apps = ["Defined Benefits"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def write_static_validation_config(path: Path) -> Path:
    config = path / "sqlctl.toml"
    config.write_text(
        """
[validation.profiles.static]
enabled_rules = [
  "required_metadata",
  "missing_input_parameters",
  "unused_input_parameters",
  "commented_out_sql",
  "select_star",
  "order_by_without_justification",
  "top_without_justification",
  "hard_coded_sensitive_literals",
  "debug_columns",
  "nolock_usage",
  "write_operation"
]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def write_column_compare_source_config(path: Path, *, reference_sql: str) -> Path:
    database_path = path / "reference.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE production_participants (participant_id INTEGER, full_name TEXT, name TEXT)"
        )
    config = path / "sqlctl.toml"
    config.write_text(
        f"""
[validation.profiles.columns]
enabled_rules = ["required_metadata", "column_compare"]
column_compare_source = "participant_lookup_reference"

[database.connections.warehouse]
driver = "sqlite"
path = "{database_path}"

[repository.sources.participant_lookup_reference]
connection = "warehouse"
sql = {reference_sql!r}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def write_column_compare_file_config(path: Path, reference_path: Path) -> Path:
    config = path / "sqlctl.toml"
    config.write_text(
        f"""
[validation.profiles.columns]
enabled_rules = ["required_metadata", "column_compare"]
column_compare_file = {str(reference_path)!r}
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


def test_validate_allows_missing_team_metadata(tmp_path: Path, capsys) -> None:
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
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert output["issues"] == []


def test_validate_allows_missing_comparison_keys(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        sql_fixture().replace("Comparison Keys: participant_id\n", ""),
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
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert output["issues"] == []


def test_validate_static_rules_pass_clean_parameterized_select(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
 * Query_Name: Participant Lookup
 * Connection_Name: Main Warehouse
 * App_Name: Defined Benefits
 * Comparison Keys: participant_id
 * order by required for stable validation output
 */

select participant_id, name
from participants
where participant_id = @participant_id
order by participant_id;
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"


def test_validate_static_rules_report_sql_quality_failures(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
Comparison Keys: participant_id
-- where participant_id = @commented_parameter
-- select disabled_column from old_table
*/

select top 10 *, count(*) as row_count
from participants with (nolock)
where participant_id = '123456789'
order by participant_id;
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert {issue["rule"] for issue in output["issues"]} == {
        "missing_input_parameters",
        "unused_input_parameters",
        "commented_out_sql",
        "select_star",
        "order_by_without_justification",
        "top_without_justification",
        "hard_coded_sensitive_literals",
        "debug_columns",
        "nolock_usage",
    }
    assert all("line" in issue for issue in output["issues"])


def test_validate_comment_with_plain_english_and_is_not_disabled_sql(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from participants
where participant_id = @participant_id;

-- Both 1003 and 1013 exist
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert output["issues"] == []


def test_validate_business_rule_comment_with_condition_words_is_not_disabled_sql(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from participants
where participant_id = @participant_id;

--Fail1 bcd is les than process date (not deceased and benf_calc_c = 2) THEN ben_elig_d is no more than 6 months ago and is no more than 30 days in the future (between -6 months and + 30 days)
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert output["issues"] == []


def test_validate_business_rule_comment_with_select_word_is_not_disabled_sql(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from participants
where participant_id = @participant_id;

--Fail2 Select ATT Plans/Executive (must wait 6 months after term to receive a payment)
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert output["issues"] == []


def test_validate_business_rule_comment_with_duration_comparison_is_not_disabled_sql(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from participants
where participant_id = @participant_id;

--bcd <= 60 days from process date then ben_elig_d must be between 30 and 60 days from process date
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert output["issues"] == []


def test_validate_commented_predicate_still_flags_disabled_sql(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from participants
where participant_id = @participant_id;

-- and plan_id = @plan_id
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert [issue["rule"] for issue in output["issues"]] == [
        "unused_input_parameters",
        "commented_out_sql",
    ]
    assert output["issues"][1]["line"] == 11


def test_validate_text_output_is_readable_with_line_numbers(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select *
from participants
where participant_id = '123456789';
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 2
    )

    output = capsys.readouterr().err
    assert "Validation failed:" in output
    assert "Issues:" in output
    assert "- [error] select_star (line 7):" in output
    assert "- [error] hard_coded_sensitive_literals (line 9):" in output


def test_validate_write_operation_allows_temp_tables(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Temp Table Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
Comparison Keys: participant_id
*/

create table #participants (participant_id int);
insert into #participants values (@participant_id);
select participant_id from #participants;
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"


def test_validate_write_operation_flags_non_temp_writes(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Update
Connection_Name: Main Warehouse
App_Name: Defined Benefits
Comparison Keys: participant_id
*/

update participants
set name = 'Test'
where participant_id = @participant_id;
""",
        encoding="utf-8",
    )
    config = write_static_validation_config(tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "static",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert [issue["rule"] for issue in output["issues"]] == ["write_operation"]


def test_validate_column_compare_matches_repository_source(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select p.participant_id, p.name as participant_name
from participants p
where p.participant_id = @participant_id;
""",
        encoding="utf-8",
    )
    config = write_column_compare_source_config(
        tmp_path,
        reference_sql=(
            "select participant_id, full_name as participant_name "
            "from production_participants"
        ),
    )

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "columns",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"


def test_validate_column_compare_reports_order_mismatch(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from participants;
""",
        encoding="utf-8",
    )
    config = write_column_compare_source_config(
        tmp_path,
        reference_sql="select name, participant_id from production_participants",
    )

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "columns",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["status"] == "warning"
    assert output["issues"][0]["rule"] == "column_compare"
    assert output["issues"][0]["severity"] == "warning"
    assert "Expected: name, participant_id" in output["issues"][0]["message"]
    assert "Actual: participant_id, name" in output["issues"][0]["message"]


def test_validate_column_compare_uses_reference_file(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from participants;
""",
        encoding="utf-8",
    )
    reference = tmp_path / "reference.sql"
    reference.write_text(
        """/*
Query_Name: Participant Lookup Reference
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from production_participants;
""",
        encoding="utf-8",
    )
    config = write_column_compare_file_config(tmp_path, reference)

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "columns",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"


def test_validate_column_compare_uses_fallback_connection(
    tmp_path: Path, capsys
) -> None:
    database_path = tmp_path / "reference.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE participants (participant_id INTEGER, name TEXT)")
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from participants;
""",
        encoding="utf-8",
    )
    config = tmp_path / "sqlctl.toml"
    config.write_text(
        f"""
[validation.profiles.columns]
enabled_rules = ["required_metadata", "column_compare"]
column_compare_connection = "warehouse"

[database.connections.warehouse]
driver = "sqlite"
path = "{database_path}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "columns",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert output["issues"] == []


def test_validate_column_compare_reports_unresolved_reference(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    config = tmp_path / "sqlctl.toml"
    config.write_text(
        """
[validation.profiles.columns]
enabled_rules = ["required_metadata", "column_compare"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "columns",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["status"] == "warning"
    assert output["issues"][0]["rule"] == "column_compare"
    assert output["issues"][0]["severity"] == "warning"
    assert "no reference query source, SQL file, or fallback connection" in output[
        "issues"
    ][0]["message"]


def test_validate_column_compare_warning_does_not_hide_errors(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
*/

select participant_id, name
from participants;
""",
        encoding="utf-8",
    )
    reference = tmp_path / "reference.sql"
    reference.write_text(
        "select name, participant_id from production_participants;\n",
        encoding="utf-8",
    )
    config = tmp_path / "sqlctl.toml"
    config.write_text(
        f"""
[validation.profiles.columns]
enabled_rules = ["required_metadata", "missing_input_parameters", "column_compare"]
column_compare_file = {str(reference)!r}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--config",
                str(config),
                "--json",
                "validate",
                str(source),
                "--profile",
                "columns",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert output["ok"] is False
    assert output["status"] == "failed"
    assert {issue["severity"] for issue in output["issues"]} == {"error", "warning"}
    assert {issue["rule"] for issue in output["issues"]} == {
        "missing_input_parameters",
        "column_compare",
    }


def test_prepare_refuses_to_capture_on_validation_failure(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        sql_fixture().replace("App_Name: Defined Benefits", "App_Name: Other App"),
        encoding="utf-8",
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
    assert output["validation"]["issues"][0]["rule"] == "allowed_app"
    assert not managed.exists()


def test_prepare_force_pass_captures_with_validation_context(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        sql_fixture().replace("App_Name: Defined Benefits", "App_Name: Other App"),
        encoding="utf-8",
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
    assert output["validation"]["issues"][0]["rule"] == "allowed_app"
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
    order_by_comment = (
        "-- order by required for deterministic comparison output\n" if order_by else ""
    )
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
{order_by_comment}\
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


def write_publishing_config(
    path: Path,
    *,
    test_database: Path,
    connection_name: str = "test",
) -> Path:
    config = path / "sqlctl.toml"
    config.write_text(
        f"""
[database.connections.{connection_name}]
driver = "sqlite"
path = "{test_database}"

[publishing.test]
connection = "{connection_name}"
table = "published_queries"
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
-- order by required for deterministic comparison output
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
-- order by required for deterministic comparison output
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


def test_deploy_test_creates_and_no_changes_published_query(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    test_db = tmp_path / "test.sqlite3"
    config_path = write_publishing_config(tmp_path, test_database=test_db)
    base_args = [
        "--config",
        str(config_path),
        "--storage-path",
        str(tmp_path / "state.sqlite3"),
        "--managed-root",
        str(tmp_path / "managed"),
        "--json",
        "deploy-test",
        str(source),
    ]

    assert main(base_args) == 0
    create_output = json.loads(capsys.readouterr().out)
    assert create_output["action"] == "CREATE"
    assert create_output["test_connection"] == "test"
    assert create_output["table"] == "published_queries"

    with sqlite3.connect(test_db) as connection:
        row = connection.execute(
            "SELECT query_name, version FROM published_queries WHERE identity_key = ?",
            (create_output["identity_key"],),
        ).fetchone()
    assert row == ("Participant Lookup", 1)

    assert main(base_args) == 0
    no_change_output = json.loads(capsys.readouterr().out)
    assert no_change_output["action"] == "NO_CHANGE"
    assert no_change_output["version"] == 1


def test_deploy_test_updates_changed_query(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    test_db = tmp_path / "test.sqlite3"
    config_path = write_publishing_config(tmp_path, test_database=test_db)
    base_args = [
        "--config",
        str(config_path),
        "--storage-path",
        str(tmp_path / "state.sqlite3"),
        "--managed-root",
        str(tmp_path / "managed"),
        "--json",
        "deploy-test",
        str(source),
    ]

    assert main(base_args) == 0
    capsys.readouterr()
    source.write_text(
        sql_fixture().replace(
            "select participant_id, name", "select participant_id, name, active"
        ),
        encoding="utf-8",
    )

    assert main(base_args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "UPDATE"
    assert output["version"] == 2

    with sqlite3.connect(test_db) as connection:
        row = connection.execute(
            "SELECT version, sql_text FROM published_queries WHERE identity_key = ?",
            (output["identity_key"],),
        ).fetchone()
    assert row[0] == 2
    assert "select participant_id, name, active" in row[1]


def test_deploy_test_refuses_validation_failure(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        sql_fixture().replace("Query_Name: Participant Lookup\n", ""), encoding="utf-8"
    )
    test_db = tmp_path / "test.sqlite3"
    config_path = write_publishing_config(tmp_path, test_database=test_db)
    validation_config = tmp_path / "validation.toml"
    validation_config.write_text(
        """
[validation.profiles.strict]
enabled_rules = ["required_metadata"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--config",
                str(validation_config),
                "--json",
                "deploy-test",
                str(source),
                "--profile",
                "strict",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert output["validation"]["status"] == "failed"
    assert output["validation"]["issues"][0]["rule"] == "required_metadata"
    assert not test_db.exists()


def test_deploy_test_prohibits_production_connection(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    production_db = tmp_path / "production.sqlite3"
    config_path = write_publishing_config(
        tmp_path, test_database=production_db, connection_name="production"
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--json",
                "deploy-test",
                str(source),
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().err)
    assert output["ok"] is False
    assert (
        output["error"]
        == "deploy-test cannot publish to production connection: production"
    )


def test_parity_reports_matched_test_registry(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    test_db = tmp_path / "test.sqlite3"
    config_path = write_publishing_config(tmp_path, test_database=test_db)
    base_args = [
        "--config",
        str(config_path),
        "--storage-path",
        str(tmp_path / "state.sqlite3"),
        "--managed-root",
        str(tmp_path / "managed"),
        "--json",
    ]

    assert main([*base_args, "deploy-test", str(source)]) == 0
    capsys.readouterr()

    assert main([*base_args, "parity", "--app", "Defined Benefits"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "matched"
    assert output["query_count"] == 1
    assert output["matched_count"] == 1
    assert output["mismatch_count"] == 0
    assert output["queries"][0]["status"] == "matched"
    assert output["queries"][0]["issues"] == []


def test_parity_reports_missing_test_deployment(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    test_db = tmp_path / "test.sqlite3"
    config_path = write_publishing_config(tmp_path, test_database=test_db)
    base_args = [
        "--config",
        str(config_path),
        "--storage-path",
        str(tmp_path / "state.sqlite3"),
        "--managed-root",
        str(tmp_path / "managed"),
        "--json",
    ]

    assert main([*base_args, "capture", str(source)]) == 0
    capsys.readouterr()

    assert main([*base_args, "parity"]) == 1
    output = json.loads(capsys.readouterr().err)
    assert output["ok"] is False
    assert output["status"] == "mismatch"
    assert output["mismatch_count"] == 1
    assert output["queries"][0]["issues"] == ["test_missing"]


def test_parity_reports_test_sql_drift(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.sql"
    source.write_text(sql_fixture(), encoding="utf-8")
    test_db = tmp_path / "test.sqlite3"
    config_path = write_publishing_config(tmp_path, test_database=test_db)
    base_args = [
        "--config",
        str(config_path),
        "--storage-path",
        str(tmp_path / "state.sqlite3"),
        "--managed-root",
        str(tmp_path / "managed"),
        "--json",
    ]

    assert main([*base_args, "deploy-test", str(source)]) == 0
    deploy_output = json.loads(capsys.readouterr().out)
    with sqlite3.connect(test_db) as connection:
        connection.execute(
            """
            UPDATE published_queries
            SET sql_text = replace(sql_text, 'select participant_id, name', 'select participant_id')
            WHERE identity_key = ?
            """,
            (deploy_output["identity_key"],),
        )

    assert main([*base_args, "parity"]) == 1
    output = json.loads(capsys.readouterr().err)
    assert output["status"] == "mismatch"
    assert output["queries"][0]["issues"] == [
        "sql_text_mismatch",
        "test_sql_hash_mismatch",
    ]
