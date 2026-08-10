from __future__ import annotations

import json
from pathlib import Path

from sql_control_cli.cli import main
from sql_control_cli.config import load_config
from sql_control_cli.metadata import parse_metadata, source_hash


def test_cli_version() -> None:
    assert main(["--version"]) == 0


def sql_fixture() -> str:
    return """/*
Query_Name: Participant Lookup
Connection_Name: Main Warehouse
App_Name: Defined Benefits
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

    assert main(
        [
            "--storage-path",
            str(config.storage_path),
            "--managed-root",
            str(config.managed_root),
            "--json",
            "capture",
            str(source),
        ]
    ) == 0

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
    base_args = ["--storage-path", str(storage), "--managed-root", str(managed), "--json"]

    assert main([*base_args, "capture", str(source)]) == 0
    capsys.readouterr()
    assert main([*base_args, "status", str(source)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["changed"] is False
    assert output["latest_version"] == 1
