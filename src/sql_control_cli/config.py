from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ValidationProfile:
    enabled_rules: tuple[str, ...] = ("required_metadata",)
    allowed_teams: tuple[str, ...] = ()
    allowed_apps: tuple[str, ...] = ()
    column_compare_source: str = ""
    column_compare_file: Path | None = None
    column_compare_connection: str = ""


@dataclass(frozen=True)
class DatabaseConnectionConfig:
    driver: str
    path: Path | None = None
    sql_driver: str = ""
    server: str = ""
    port: int | None = None
    database: str = ""
    username: str = ""
    password: str = ""
    sql_driver_env: str = ""
    server_env: str = ""
    port_env: str = ""
    database_env: str = ""
    username_env: str = ""
    password_env: str = ""
    trust_server_certificate_env: str = ""
    trust_server_certificate: bool = False


@dataclass(frozen=True)
class QuerySourceConfig:
    connection: str
    sql: str


@dataclass(frozen=True)
class TestPublishingConfig:
    connection: str = ""
    table: str = "sqlctl_test_queries"


@dataclass(frozen=True)
class SqlctlConfig:
    storage_path: Path
    managed_root: Path
    normalize_whitespace: bool = True
    validation_profiles: dict[str, ValidationProfile] | None = None
    database_connections: dict[str, DatabaseConnectionConfig] | None = None
    query_sources: dict[str, QuerySourceConfig] | None = None
    test_publishing: TestPublishingConfig = TestPublishingConfig()


def default_user_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "sqlctl" / "config.toml"
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "sqlctl"
        / "config.toml"
    )


def default_project_config_path() -> Path:
    return Path.cwd() / "sqlctl.toml"


def default_package_env_path() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent / ".env"
    return None


def default_package_config_path() -> Path | None:
    env_path = default_package_env_path()
    if env_path is None:
        return None
    return env_path.parent / "pyproject.toml"


def default_state_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "sqlctl"
    return (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        / "sqlctl"
    )


def load_config(
    *,
    config_paths: list[Path] | None = None,
    storage_path: Path | None = None,
    managed_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> SqlctlConfig:
    active_env = _active_environment(env)
    state_root = default_state_root()
    values: dict[str, Any] = {
        "storage_path": state_root / "sqlctl.sqlite3",
        "managed_root": state_root / "managed",
        "normalize_whitespace": True,
        "validation_profiles": {},
        "database_connections": {},
        "query_sources": {},
        "test_publishing": {},
    }

    paths = [
        path
        for path in (
            default_package_config_path(),
            default_user_config_path(),
            default_project_config_path(),
        )
        if path is not None
    ]
    if active_env.get("SQLCTL_CONFIG"):
        paths.append(Path(active_env["SQLCTL_CONFIG"]))
    if config_paths:
        paths.extend(config_paths)
    for path in paths:
        values = _merge(values, _flatten_config(_read_toml(path)))

    values = _merge(
        values,
        {
            "storage_path": active_env.get("SQLCTL_STORAGE_PATH"),
            "managed_root": active_env.get("SQLCTL_MANAGED_ROOT"),
            "normalize_whitespace": active_env.get("SQLCTL_NORMALIZE_WHITESPACE"),
        },
    )
    values = _merge(
        values, {"storage_path": storage_path, "managed_root": managed_root}
    )

    normalize = values["normalize_whitespace"]
    if isinstance(normalize, str):
        normalize = normalize.lower() not in {"0", "false", "no", "off"}
    return SqlctlConfig(
        storage_path=Path(values["storage_path"]).expanduser(),
        managed_root=Path(values["managed_root"]).expanduser(),
        normalize_whitespace=bool(normalize),
        validation_profiles={
            name: _validation_profile(raw_profile)
            for name, raw_profile in dict(values["validation_profiles"]).items()
        },
        database_connections={
            name: _database_connection(raw_connection, active_env)
            for name, raw_connection in dict(values["database_connections"]).items()
        },
        query_sources={
            name: _query_source(raw_source)
            for name, raw_source in dict(values["query_sources"]).items()
        },
        test_publishing=_test_publishing(values["test_publishing"]),
    )


def _active_environment(env: dict[str, str] | None) -> dict[str, str]:
    active_env = dict(os.environ if env is None else env)
    package_env_path = default_package_env_path()
    if package_env_path is None:
        return active_env
    file_env = _read_env_file(package_env_path)
    return {**file_env, **active_env}


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _env_value(value)
    return values


def _env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    comment_index = value.find(" #")
    if comment_index >= 0:
        value = value[:comment_index].rstrip()
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def _flatten_config(data: dict[str, Any]) -> dict[str, Any]:
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    sqlctl = tool.get("sqlctl") if isinstance(tool.get("sqlctl"), dict) else {}
    if sqlctl:
        data = _merge(_flatten_tool_sqlctl(sqlctl), data)
    storage = data.get("storage") if isinstance(data.get("storage"), dict) else {}
    managed = data.get("managed") if isinstance(data.get("managed"), dict) else {}
    comparison = (
        data.get("comparison") if isinstance(data.get("comparison"), dict) else {}
    )
    validation = (
        data.get("validation") if isinstance(data.get("validation"), dict) else {}
    )
    database = data.get("database") if isinstance(data.get("database"), dict) else {}
    repository = (
        data.get("repository") if isinstance(data.get("repository"), dict) else {}
    )
    publishing = (
        data.get("publishing") if isinstance(data.get("publishing"), dict) else {}
    )
    return {
        "storage_path": data.get("storage_path") or storage.get("path"),
        "managed_root": data.get("managed_root") or managed.get("root"),
        "normalize_whitespace": data.get("normalize_whitespace")
        if "normalize_whitespace" in data
        else comparison.get("normalize_whitespace"),
        "validation_profiles": data.get("validation_profiles")
        or validation.get("profiles")
        if isinstance(validation.get("profiles"), dict)
        or isinstance(data.get("validation_profiles"), dict)
        else None,
        "database_connections": data.get("database_connections")
        or database.get("connections")
        if isinstance(database.get("connections"), dict)
        or isinstance(data.get("database_connections"), dict)
        else None,
        "query_sources": data.get("query_sources")
        or repository.get("sources")
        if isinstance(repository.get("sources"), dict)
        or isinstance(data.get("query_sources"), dict)
        else None,
        "test_publishing": publishing.get("test")
        if isinstance(publishing.get("test"), dict)
        else None,
    }


def _flatten_tool_sqlctl(sqlctl: dict[str, Any]) -> dict[str, Any]:
    database = sqlctl.get("database") if isinstance(sqlctl.get("database"), dict) else {}
    repository = (
        sqlctl.get("repository") if isinstance(sqlctl.get("repository"), dict) else {}
    )
    validation = (
        sqlctl.get("validation") if isinstance(sqlctl.get("validation"), dict) else {}
    )
    database_connections = database.get("connections")
    if not isinstance(database_connections, dict):
        database_connections = database.get("connection_templates")
    return {
        "validation_profiles": validation.get("profiles")
        if isinstance(validation.get("profiles"), dict)
        else None,
        "database_connections": database_connections
        if isinstance(database_connections, dict)
        else None,
        "query_sources": repository.get("sources")
        if isinstance(repository.get("sources"), dict)
        else None,
    }


def _merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if value is not None and value != "":
            merged[key] = value
    return merged


def _validation_profile(raw_profile: Any) -> ValidationProfile:
    profile = raw_profile if isinstance(raw_profile, dict) else {}
    return ValidationProfile(
        enabled_rules=tuple(
            str(value)
            for value in _list_value(
                profile.get("enabled_rules"), ["required_metadata"]
            )
        ),
        allowed_teams=tuple(
            str(value) for value in _list_value(profile.get("allowed_teams"), [])
        ),
        allowed_apps=tuple(
            str(value) for value in _list_value(profile.get("allowed_apps"), [])
        ),
        column_compare_source=str(profile.get("column_compare_source") or ""),
        column_compare_file=Path(str(profile["column_compare_file"])).expanduser()
        if profile.get("column_compare_file")
        else None,
        column_compare_connection=str(profile.get("column_compare_connection") or ""),
    )


def _list_value(value: Any, default: list[str]) -> list[Any]:
    if value is None:
        return list(default)
    if isinstance(value, list):
        return value
    return [value]


def _database_connection(
    raw_connection: Any, active_env: dict[str, str]
) -> DatabaseConnectionConfig:
    connection = raw_connection if isinstance(raw_connection, dict) else {}
    driver = str(connection.get("driver") or "sqlite")
    path_value = connection.get("path")
    sql_driver_env = str(connection.get("sql_driver_env") or "")
    server_env = str(connection.get("server_env") or "")
    port_env = str(connection.get("port_env") or "")
    database_env = str(connection.get("database_env") or "")
    username_env = str(connection.get("username_env") or "")
    password_env = str(connection.get("password_env") or "")
    trust_server_certificate_env = str(
        connection.get("trust_server_certificate_env") or ""
    )
    return DatabaseConnectionConfig(
        driver=driver,
        path=Path(str(path_value)).expanduser() if path_value else None,
        sql_driver=str(
            connection.get("sql_driver") or active_env.get(sql_driver_env, "")
        ),
        server=str(connection.get("server") or active_env.get(server_env, "")),
        port=_int_value(connection.get("port") or active_env.get(port_env)),
        database=str(connection.get("database") or active_env.get(database_env, "")),
        username=str(connection.get("username") or active_env.get(username_env, "")),
        password=str(connection.get("password") or active_env.get(password_env, "")),
        sql_driver_env=sql_driver_env,
        server_env=server_env,
        port_env=port_env,
        database_env=database_env,
        username_env=username_env,
        password_env=password_env,
        trust_server_certificate_env=trust_server_certificate_env,
        trust_server_certificate=_bool_value(
            connection.get("trust_server_certificate")
            if connection.get("trust_server_certificate") is not None
            else active_env.get(trust_server_certificate_env),
            default=False,
        ),
    )


def _query_source(raw_source: Any) -> QuerySourceConfig:
    source = raw_source if isinstance(raw_source, dict) else {}
    connection = source.get("connection")
    sql = source.get("sql")
    if not connection or not sql:
        return QuerySourceConfig(connection="", sql="")
    return QuerySourceConfig(connection=str(connection), sql=str(sql))


def _test_publishing(raw_publishing: Any) -> TestPublishingConfig:
    publishing = raw_publishing if isinstance(raw_publishing, dict) else {}
    return TestPublishingConfig(
        connection=str(publishing.get("connection") or ""),
        table=str(publishing.get("table") or "sqlctl_test_queries"),
    )


def _int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _bool_value(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return str(value).strip().lower() not in {"0", "false", "no", "off"}
