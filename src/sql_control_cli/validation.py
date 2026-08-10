from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import SqlctlConfig, ValidationProfile
from .metadata import MetadataError, QueryMetadata, parse_metadata

DEFAULT_RULES = (
    "required_metadata",
    "allowed_team",
    "allowed_app",
    "comparison_keys_required",
)
KNOWN_RULES = set(DEFAULT_RULES)


@dataclass(frozen=True)
class ValidationIssue:
    rule: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "severity": self.severity, "message": self.message}


@dataclass(frozen=True)
class ValidationResult:
    sql_file: Path
    profile_name: str
    enabled_rules: tuple[str, ...]
    metadata: QueryMetadata | None
    issues: tuple[ValidationIssue, ...]
    force_passed: bool = False

    @property
    def passed(self) -> bool:
        return not self.issues or self.force_passed

    @property
    def status(self) -> str:
        if not self.issues:
            return "passed"
        if self.force_passed:
            return "forced"
        return "failed"

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.passed,
            "status": self.status,
            "sql_file": str(self.sql_file),
            "profile": self.profile_name,
            "enabled_rules": list(self.enabled_rules),
            "issues": [issue.to_dict() for issue in self.issues],
        }
        if self.metadata is not None:
            payload["metadata"] = self.metadata.to_dict()
        return payload


def validate_sql_file(
    sql_file: Path,
    config: SqlctlConfig,
    *,
    profile_name: str = "default",
    force_pass: bool = False,
) -> ValidationResult:
    profile = select_profile(config, profile_name)
    enabled_rules = normalize_enabled_rules(profile.enabled_rules)
    issues: list[ValidationIssue] = []
    metadata: QueryMetadata | None = None

    try:
        metadata = parse_metadata(sql_file.read_text(encoding="utf-8"))
    except MetadataError as err:
        issues.append(ValidationIssue("required_metadata", "error", str(err)))
        return ValidationResult(
            sql_file,
            profile_name,
            enabled_rules,
            metadata,
            tuple(issues),
            force_passed=force_pass,
        )

    if "allowed_team" in enabled_rules:
        issues.extend(_validate_allowed_team(metadata, profile))
    if "allowed_app" in enabled_rules:
        issues.extend(_validate_allowed_app(metadata, profile))
    if "comparison_keys_required" in enabled_rules:
        issues.extend(_validate_comparison_keys(metadata))

    return ValidationResult(
        sql_file,
        profile_name,
        enabled_rules,
        metadata,
        tuple(issues),
        force_passed=force_pass and bool(issues),
    )


def select_profile(config: SqlctlConfig, profile_name: str) -> ValidationProfile:
    profiles = config.validation_profiles or {}
    if profile_name in profiles:
        return profiles[profile_name]
    if profile_name == "default":
        return ValidationProfile(enabled_rules=("required_metadata",))
    available = ", ".join(sorted(profiles)) or "default"
    raise ValueError(
        f"Validation profile not found: {profile_name}. Available profiles: {available}"
    )


def normalize_enabled_rules(enabled_rules: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(enabled_rules) - KNOWN_RULES)
    if unknown:
        raise ValueError(f"Unknown validation rule(s): {', '.join(unknown)}")
    return tuple(rule for rule in DEFAULT_RULES if rule in set(enabled_rules))


def _validate_allowed_team(
    metadata: QueryMetadata, profile: ValidationProfile
) -> list[ValidationIssue]:
    if not metadata.team_name:
        return [ValidationIssue("allowed_team", "error", "Team metadata is required.")]
    if profile.allowed_teams and metadata.team_name not in profile.allowed_teams:
        allowed = ", ".join(profile.allowed_teams)
        return [
            ValidationIssue(
                "allowed_team",
                "error",
                f"Team is not allowed: {metadata.team_name}. Expected one of: {allowed}",
            )
        ]
    return []


def _validate_allowed_app(
    metadata: QueryMetadata, profile: ValidationProfile
) -> list[ValidationIssue]:
    if profile.allowed_apps and metadata.app_name not in profile.allowed_apps:
        allowed = ", ".join(profile.allowed_apps)
        return [
            ValidationIssue(
                "allowed_app",
                "error",
                f"App_Name is not allowed: {metadata.app_name}. Expected one of: {allowed}",
            )
        ]
    return []


def _validate_comparison_keys(metadata: QueryMetadata) -> list[ValidationIssue]:
    if metadata.comparison_keys:
        return []
    return [
        ValidationIssue(
            "comparison_keys_required", "error", "Comparison Keys metadata is required."
        )
    ]
