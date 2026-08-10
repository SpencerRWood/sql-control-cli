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


@dataclass(frozen=True)
class SqlctlConfig:
    storage_path: Path
    managed_root: Path
    normalize_whitespace: bool = True
    validation_profiles: dict[str, ValidationProfile] | None = None


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
    active_env = dict(os.environ if env is None else env)
    state_root = default_state_root()
    values: dict[str, Any] = {
        "storage_path": state_root / "sqlctl.sqlite3",
        "managed_root": state_root / "managed",
        "normalize_whitespace": True,
        "validation_profiles": {},
    }

    paths = [default_user_config_path(), default_project_config_path()]
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
    )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def _flatten_config(data: dict[str, Any]) -> dict[str, Any]:
    storage = data.get("storage") if isinstance(data.get("storage"), dict) else {}
    managed = data.get("managed") if isinstance(data.get("managed"), dict) else {}
    comparison = (
        data.get("comparison") if isinstance(data.get("comparison"), dict) else {}
    )
    validation = (
        data.get("validation") if isinstance(data.get("validation"), dict) else {}
    )
    return {
        "storage_path": data.get("storage_path") or storage.get("path"),
        "managed_root": data.get("managed_root") or managed.get("root"),
        "normalize_whitespace": data.get("normalize_whitespace")
        if "normalize_whitespace" in data
        else comparison.get("normalize_whitespace"),
        "validation_profiles": validation.get("profiles")
        if isinstance(validation.get("profiles"), dict)
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
    )


def _list_value(value: Any, default: list[str]) -> list[Any]:
    if value is None:
        return list(default)
    if isinstance(value, list):
        return value
    return [value]
