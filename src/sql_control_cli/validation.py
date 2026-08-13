from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import SqlctlConfig, ValidationProfile
from .database import DatabaseError, query_source_columns
from .metadata import (
    MetadataError,
    QueryMetadata,
    normalize_identity_part,
    parse_metadata,
)

DEFAULT_RULES = (
    "required_metadata",
    "allowed_team",
    "allowed_app",
    "comparison_keys_required",
    "missing_input_parameters",
    "unused_input_parameters",
    "commented_out_sql",
    "select_star",
    "order_by_without_justification",
    "top_without_justification",
    "hard_coded_sensitive_literals",
    "debug_columns",
    "nolock_usage",
    "write_operation",
    "column_compare",
)
KNOWN_RULES = set(DEFAULT_RULES)

INPUT_PARAMETER_RE = re.compile(r"(?<![@:\w])[@:][A-Za-z_][A-Za-z0-9_]*")
REASON_WORDS = (
    "intentional",
    "required",
    "business",
    "deterministic",
    "stable",
    "presentation",
    "expected",
)
SENSITIVE_FIELDS = (
    "ssn",
    "client_id",
    "clientid",
    "clnt_id",
    "calcrefnum",
    "calc_ref",
    "beneficiary_id",
    "participant_id",
    "participantid",
    "plan_id",
)


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
        return not self.error_issues or self.force_passed

    @property
    def error_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def status(self) -> str:
        if not self.issues:
            return "passed"
        if not self.error_issues:
            return "warning"
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

    sql_text = sql_file.read_text(encoding="utf-8")
    live_sql, comments = _split_sql_sections(sql_text)

    try:
        metadata = parse_metadata(sql_text)
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
    if "missing_input_parameters" in enabled_rules:
        issues.extend(_validate_missing_input_parameters(live_sql))
    if "unused_input_parameters" in enabled_rules:
        issues.extend(_validate_unused_input_parameters(live_sql, comments))
    if "commented_out_sql" in enabled_rules:
        issues.extend(_validate_commented_out_sql(comments))
    if "select_star" in enabled_rules:
        issues.extend(_validate_select_star(live_sql))
    if "order_by_without_justification" in enabled_rules:
        issues.extend(_validate_order_by_without_justification(live_sql, comments))
    if "top_without_justification" in enabled_rules:
        issues.extend(_validate_top_without_justification(live_sql, comments))
    if "hard_coded_sensitive_literals" in enabled_rules:
        issues.extend(_validate_hard_coded_sensitive_literals(live_sql))
    if "debug_columns" in enabled_rules:
        issues.extend(_validate_debug_columns(live_sql))
    if "nolock_usage" in enabled_rules:
        issues.extend(_validate_nolock_usage(live_sql))
    if "write_operation" in enabled_rules:
        issues.extend(_validate_write_operation(live_sql))
    if "column_compare" in enabled_rules:
        issues.extend(_validate_column_compare(sql_text, metadata, config, profile))

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


def _validate_missing_input_parameters(live_sql: str) -> list[ValidationIssue]:
    if _input_parameters(live_sql):
        return []
    return [
        ValidationIssue(
            "missing_input_parameters",
            "error",
            "At least one live input parameter marker is required.",
        )
    ]


def _validate_unused_input_parameters(
    live_sql: str, comments: tuple[str, ...]
) -> list[ValidationIssue]:
    live_parameters = _input_parameters(live_sql)
    comment_parameters = _input_parameters("\n".join(comments))
    unused = sorted(comment_parameters - live_parameters)
    return [
        ValidationIssue(
            "unused_input_parameters",
            "error",
            f"Input parameter appears only in comments: {parameter}",
        )
        for parameter in unused
    ]


def _validate_commented_out_sql(comments: tuple[str, ...]) -> list[ValidationIssue]:
    issues = []
    for comment in _comment_logic_lines(comments):
        normalized = _normalize_sql(comment)
        if re.search(
            r"\b(select|join|where|and|or|insert|update|delete|merge)\b",
            normalized,
        ):
            issues.append(
                ValidationIssue(
                    "commented_out_sql",
                    "error",
                    "Comment appears to contain disabled SQL logic.",
                )
            )
            break
    return issues


def _validate_select_star(live_sql: str) -> list[ValidationIssue]:
    normalized = _normalize_sql(live_sql)
    if re.search(
        r"\bselect\s+(?:distinct\s+)?(?:top\s*\(?\s*\d+\s*\)?\s+)?\*",
        normalized,
    ):
        return [
            ValidationIssue(
                "select_star",
                "error",
                "SELECT * is not allowed; name output columns explicitly.",
            )
        ]
    return []


def _validate_order_by_without_justification(
    live_sql: str, comments: tuple[str, ...]
) -> list[ValidationIssue]:
    if not re.search(r"\border\s+by\b", _normalize_sql(live_sql)):
        return []
    if _has_justification(comments, "order by"):
        return []
    return [
        ValidationIssue(
            "order_by_without_justification",
            "error",
            "ORDER BY requires a justification comment.",
        )
    ]


def _validate_top_without_justification(
    live_sql: str, comments: tuple[str, ...]
) -> list[ValidationIssue]:
    if not re.search(r"\btop\s*\(?\s*\d+\s*\)?", _normalize_sql(live_sql)):
        return []
    if _has_justification(comments, "top"):
        return []
    return [
        ValidationIssue(
            "top_without_justification",
            "error",
            "TOP requires a justification comment.",
        )
    ]


def _validate_hard_coded_sensitive_literals(live_sql: str) -> list[ValidationIssue]:
    normalized = _normalize_sql(live_sql)
    if re.search(r"'?\b\d{3}-\d{2}-\d{4}\b'?", normalized) or re.search(
        r"'?\b\d{9}\b'?", normalized
    ):
        return [
            ValidationIssue(
                "hard_coded_sensitive_literals",
                "error",
                "Hard-coded sensitive literal detected.",
            )
        ]
    sensitive_field_pattern = "|".join(re.escape(field) for field in SENSITIVE_FIELDS)
    if re.search(
        rf"\b(?:{sensitive_field_pattern})\b\s*(?:=|<>|!=|in\s*\()\s*'?[A-Za-z0-9-]+'?",
        normalized,
    ):
        return [
            ValidationIssue(
                "hard_coded_sensitive_literals",
                "error",
                "Sensitive field is compared to a hard-coded literal.",
            )
        ]
    return []


def _validate_debug_columns(live_sql: str) -> list[ValidationIssue]:
    normalized = _normalize_sql(live_sql)
    if re.search(
        r"\bas\s+\[?(?:debug\w*|row_count|error\w*|test\w*|tmp\w*)\]?\b",
        normalized,
    ):
        return [
            ValidationIssue(
                "debug_columns",
                "error",
                "Output alias appears temporary or diagnostic.",
            )
        ]
    return []


def _validate_nolock_usage(live_sql: str) -> list[ValidationIssue]:
    if re.search(r"\bwith\s*\(\s*nolock\s*\)|\bnolock\b", _normalize_sql(live_sql)):
        return [
            ValidationIssue(
                "nolock_usage",
                "error",
                "NOLOCK usage is not allowed.",
            )
        ]
    return []


def _validate_write_operation(live_sql: str) -> list[ValidationIssue]:
    normalized = _normalize_sql(live_sql)
    if re.search(r"\bexec(?:ute)?\b", normalized):
        return [
            ValidationIssue(
                "write_operation",
                "error",
                "EXEC and EXECUTE operations are not allowed.",
            )
        ]
    patterns = (
        r"\binsert\s+into\s+(?!#)",
        r"\bupdate\s+(?!#)",
        r"\bdelete\s+(?:from\s+)?(?!#)",
        r"\bmerge\s+(?!#)",
        r"\btruncate\s+table\s+(?!#)",
        r"\bdrop\s+table\s+(?!#)",
        r"\balter\s+table\s+(?!#)",
        r"\bcreate\s+table\s+(?!#)",
    )
    if any(re.search(pattern, normalized) for pattern in patterns):
        return [
            ValidationIssue(
                "write_operation",
                "error",
                "Non-temp-table write or schema operation detected.",
            )
        ]
    return []


def _validate_column_compare(
    sql_text: str,
    metadata: QueryMetadata,
    config: SqlctlConfig,
    profile: ValidationProfile,
) -> list[ValidationIssue]:
    candidate_columns = _final_select_columns(sql_text)
    if candidate_columns is None:
        return [
            ValidationIssue(
                "column_compare",
                "warning",
                "Could not resolve final top-level SELECT columns for candidate SQL.",
            )
        ]

    reference_columns, reference_error = _resolve_column_compare_reference_columns(
        metadata, config, profile
    )
    if reference_error:
        return [
            ValidationIssue(
                "column_compare",
                "warning",
                reference_error,
            )
        ]
    if reference_columns is None:
        return [
            ValidationIssue(
                "column_compare",
                "warning",
                "Could not resolve final top-level SELECT columns for reference SQL.",
            )
        ]

    if candidate_columns == reference_columns:
        return []
    return [
        ValidationIssue(
            "column_compare",
            "warning",
            "Output columns do not match reference. "
            f"Expected: {', '.join(reference_columns)}. "
            f"Actual: {', '.join(candidate_columns)}.",
        )
    ]


def _split_sql_sections(sql_text: str) -> tuple[str, tuple[str, ...]]:
    comments: list[str] = []

    def replace_block(match: re.Match[str]) -> str:
        comments.append(match.group(0))
        return " "

    without_blocks = re.sub(r"/\*.*?\*/", replace_block, sql_text, flags=re.DOTALL)
    live_lines = []
    for line in without_blocks.splitlines():
        comment_start = line.find("--")
        if comment_start >= 0:
            comments.append(line[comment_start:])
            line = line[:comment_start]
        live_lines.append(line)
    return "\n".join(live_lines), tuple(comments)


def _normalize_sql(sql_text: str) -> str:
    return re.sub(r"\s+", " ", sql_text).strip().lower()


def _input_parameters(sql_text: str) -> set[str]:
    return {match.group(0).lower() for match in INPUT_PARAMETER_RE.finditer(sql_text)}


def _has_justification(comments: tuple[str, ...], subject: str) -> bool:
    for comment in comments:
        normalized = _normalize_sql(comment)
        if subject in normalized and any(word in normalized for word in REASON_WORDS):
            return True
    return False


def _comment_logic_lines(comments: tuple[str, ...]) -> tuple[str, ...]:
    lines = []
    for comment in comments:
        for line in comment.splitlines():
            cleaned = re.sub(r"^\s*(?:/\*+|\*/|--|\*)\s*", "", line).strip()
            if not cleaned:
                continue
            if re.match(r"^[A-Za-z_][A-Za-z_ ]*:\s*", cleaned):
                continue
            lines.append(cleaned)
    return tuple(lines)


def _resolve_column_compare_reference_columns(
    metadata: QueryMetadata,
    config: SqlctlConfig,
    profile: ValidationProfile,
) -> tuple[tuple[str, ...] | None, str | None]:
    if profile.column_compare_file is not None:
        if not profile.column_compare_file.exists():
            return (
                None,
                f"Column compare reference SQL file was not found: {profile.column_compare_file}",
            )
        return _final_select_columns(
            profile.column_compare_file.read_text(encoding="utf-8")
        ), None

    sources = config.query_sources or {}
    if profile.column_compare_source:
        return _reference_source_columns(config, profile.column_compare_source)

    source_names = _candidate_reference_source_names(metadata)
    for source_name in source_names:
        source = sources.get(source_name)
        if source and source.sql:
            return _reference_source_columns(config, source_name)
    return (
        None,
        "Column compare was requested but no reference query source or SQL file could be resolved.",
    )


def _reference_source_columns(
    config: SqlctlConfig, source_name: str
) -> tuple[tuple[str, ...] | None, str | None]:
    try:
        return query_source_columns(config, source_name), None
    except DatabaseError as err:
        return None, f"Column compare reference could not be resolved: {err}"


def _candidate_reference_source_names(metadata: QueryMetadata) -> tuple[str, ...]:
    normalized_query = normalize_identity_part(metadata.query_name)
    normalized_connection = normalize_identity_part(metadata.connection_name)
    normalized_app = normalize_identity_part(metadata.app_name)
    names = [
        normalized_query,
        normalized_query.replace("-", "_"),
        f"{normalized_app}_{normalized_connection}_{normalized_query}".replace("-", "_"),
        f"{normalized_query}_{normalized_connection}_{normalized_app}".replace("-", "_"),
    ]
    return tuple(dict.fromkeys(names))


def _final_select_columns(sql_text: str) -> tuple[str, ...] | None:
    live_sql, _comments = _split_sql_sections(sql_text)
    select_bounds = _final_top_level_select_bounds(live_sql)
    if select_bounds is None:
        return None
    start, end = select_bounds
    select_list = live_sql[start:end].strip()
    select_list = re.sub(
        r"^(?:distinct\s+)?(?:top\s*\(?\s*\d+\s*\)?\s+)?",
        "",
        select_list,
        flags=re.IGNORECASE,
    ).strip()
    columns = tuple(
        _column_name(expression)
        for expression in _split_top_level_commas(select_list)
        if expression.strip()
    )
    return columns or None


def _final_top_level_select_bounds(sql_text: str) -> tuple[int, int] | None:
    lowered = sql_text.lower()
    depth = 0
    final_select: int | None = None
    index = 0
    while index < len(sql_text):
        char = sql_text[index]
        if char == "'":
            index = _skip_quoted_string(sql_text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and _word_at(lowered, index, "select"):
            final_select = index + len("select")
            index += len("select")
            continue
        index += 1
    if final_select is None:
        return None

    depth = 0
    index = final_select
    while index < len(sql_text):
        char = sql_text[index]
        if char == "'":
            index = _skip_quoted_string(sql_text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and _word_at(lowered, index, "from"):
            return final_select, index
        index += 1
    return final_select, len(sql_text)


def _split_top_level_commas(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(value):
        char = value[index]
        if char == "'":
            index = _skip_quoted_string(value, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
        index += 1
    parts.append(value[start:].strip())
    return tuple(parts)


def _column_name(expression: str) -> str:
    cleaned = expression.strip().rstrip(";")
    alias = re.search(
        r"\bas\s+(\[[^\]]+\]|\"[^\"]+\"|'[^']+'|[A-Za-z_][A-Za-z0-9_]*)\s*$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if alias:
        return _clean_identifier(alias.group(1))

    tokens = re.findall(r"\[[^\]]+\]|\"[^\"]+\"|'[^']+'|[A-Za-z_][A-Za-z0-9_]*|\*", cleaned)
    if not tokens:
        return cleaned
    if len(tokens) >= 2 and tokens[-2].lower() not in {
        "then",
        "else",
        "end",
        "over",
    }:
        return _clean_identifier(tokens[-1])
    return _clean_identifier(tokens[-1].split(".")[-1])


def _clean_identifier(identifier: str) -> str:
    value = identifier.strip()
    if (value.startswith("[") and value.endswith("]")) or (
        value.startswith('"') and value.endswith('"')
    ) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return value


def _word_at(value: str, index: int, word: str) -> bool:
    if not value.startswith(word, index):
        return False
    before = value[index - 1] if index > 0 else " "
    after_index = index + len(word)
    after = value[after_index] if after_index < len(value) else " "
    return not (before.isalnum() or before == "_") and not (
        after.isalnum() or after == "_"
    )


def _skip_quoted_string(value: str, start: int) -> int:
    index = start + 1
    while index < len(value):
        if value[index] == "'":
            if index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    return len(value)
