from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

REQUIRED_FIELDS = ("Query_Name", "Connection_Name", "App_Name")
OPTIONAL_FIELDS = ("Version", "Date Changed", "Comparison Keys")


class MetadataError(ValueError):
    pass


@dataclass(frozen=True)
class QueryMetadata:
    query_name: str
    connection_name: str
    app_name: str
    version: int | None = None
    date_changed: str | None = None
    comparison_keys: tuple[str, ...] = ()

    @property
    def identity(self) -> tuple[str, str, str]:
        return (
            normalize_identity_part(self.query_name),
            normalize_identity_part(self.connection_name),
            normalize_identity_part(self.app_name),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "Query_Name": self.query_name,
            "Connection_Name": self.connection_name,
            "App_Name": self.app_name,
            "Version": self.version,
            "Date Changed": self.date_changed,
            "Comparison Keys": list(self.comparison_keys),
            "identity": {
                "query_name": self.identity[0],
                "connection_name": self.identity[1],
                "app_name": self.identity[2],
            },
        }


def normalize_identity_part(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def identity_key(metadata: QueryMetadata) -> str:
    return "::".join(metadata.identity)


def parse_metadata(sql_text: str) -> QueryMetadata:
    fields: dict[str, str] = {}
    duplicates: list[str] = []
    for raw_line in _leading_comment_lines(sql_text):
        match = re.match(r"^\s*(?:--\s*)?([A-Za-z_][A-Za-z_ ]*):\s*(.*?)\s*$", raw_line)
        if not match:
            continue
        key = _canonical_key(match.group(1))
        if key not in REQUIRED_FIELDS and key not in OPTIONAL_FIELDS:
            continue
        if key in fields:
            duplicates.append(key)
        fields[key] = match.group(2).strip()

    missing = [field for field in REQUIRED_FIELDS if not fields.get(field)]
    if missing or duplicates:
        problems = []
        if missing:
            problems.append(f"missing required fields: {', '.join(missing)}")
        if duplicates:
            problems.append(f"duplicate fields: {', '.join(sorted(set(duplicates)))}")
        raise MetadataError("; ".join(problems))

    version = None
    if fields.get("Version"):
        try:
            version = int(fields["Version"])
        except ValueError as err:
            raise MetadataError("Version must be an integer") from err
        if version < 1:
            raise MetadataError("Version must be at least 1")

    comparison_keys = tuple(
        part.strip()
        for part in re.split(r"[,;\n]+", fields.get("Comparison Keys", ""))
        if part.strip()
    )
    return QueryMetadata(
        query_name=fields["Query_Name"],
        connection_name=fields["Connection_Name"],
        app_name=fields["App_Name"],
        version=version,
        date_changed=fields.get("Date Changed") or None,
        comparison_keys=comparison_keys,
    )


def metadata_json(sql_text: str) -> str:
    return json.dumps(parse_metadata(sql_text).to_dict(), indent=2)


def sql_body(sql_text: str) -> str:
    lines = sql_text.splitlines()
    if lines and lines[0].lstrip().startswith("/*"):
        for index, line in enumerate(lines):
            if "*/" in line:
                return "\n".join(lines[index + 1 :]).strip() + "\n"
    while lines and lines[0].lstrip().startswith("--"):
        lines.pop(0)
    return "\n".join(lines).strip() + "\n"


def source_hash(sql_text: str, *, normalize_whitespace: bool = True) -> str:
    metadata = parse_metadata(sql_text)
    body = sql_body(sql_text)
    if normalize_whitespace:
        body = re.sub(r"\s+", " ", body).strip()
    payload = {
        "identity": metadata.identity,
        "comparison_keys": metadata.comparison_keys,
        "body": body,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def render_metadata_block(metadata: QueryMetadata, *, version: int, date_changed: str) -> str:
    lines = [
        "/*",
        f"Query_Name: {metadata.query_name}",
        f"Connection_Name: {metadata.connection_name}",
        f"App_Name: {metadata.app_name}",
        f"Version: {version}",
        f"Date Changed: {date_changed}",
    ]
    if metadata.comparison_keys:
        lines.append(f"Comparison Keys: {', '.join(metadata.comparison_keys)}")
    lines.append("*/")
    return "\n".join(lines)


def managed_sql(sql_text: str, *, version: int, date_changed: str) -> str:
    metadata = parse_metadata(sql_text)
    return f"{render_metadata_block(metadata, version=version, date_changed=date_changed)}\n{sql_body(sql_text)}"


def _canonical_key(key: str) -> str:
    collapsed = re.sub(r"\s+", " ", key.strip().replace("_", " "))
    lookup = {
        "query name": "Query_Name",
        "connection name": "Connection_Name",
        "app name": "App_Name",
        "version": "Version",
        "date changed": "Date Changed",
        "comparison keys": "Comparison Keys",
    }
    return lookup.get(collapsed.lower(), key.strip())


def _leading_comment_lines(sql_text: str) -> list[str]:
    stripped = sql_text.lstrip()
    if stripped.startswith("/*"):
        end = stripped.find("*/")
        if end == -1:
            raise MetadataError("metadata block is not closed")
        return stripped[2:end].splitlines()
    lines = []
    for line in sql_text.splitlines():
        if line.lstrip().startswith("--"):
            lines.append(line)
            continue
        if not line.strip():
            continue
        break
    return lines
