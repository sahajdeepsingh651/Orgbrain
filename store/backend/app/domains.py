"""domain_data type validation against schemas/domains/*.json — orgbrain-schema.md §4.0."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DOMAINS_DIR = Path(__file__).resolve().parent.parent.parent / "schemas" / "domains"


class DomainValidationError(Exception):
    def __init__(self, field: str, value: Any, reason: str):
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(reason)


def _load_domain_schema(domain: str) -> dict:
    path = DOMAINS_DIR / f"{domain}.json"
    if not path.exists():
        raise DomainValidationError("domain", domain, f"unknown domain '{domain}'")
    return json.loads(path.read_text())


def _check_type(field: str, value: Any, declared_type: str) -> None:
    if declared_type == "string":
        if not isinstance(value, str):
            raise DomainValidationError(field, value, "expected string")
    elif declared_type == "string[]":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise DomainValidationError(field, value, "expected string[]")
    elif declared_type == "url":
        if not isinstance(value, str) or not urlparse(value).scheme:
            raise DomainValidationError(field, value, "expected a valid URL")
    elif declared_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise DomainValidationError(field, value, "expected number")
    elif declared_type == "boolean":
        if not isinstance(value, bool):
            raise DomainValidationError(field, value, "expected boolean")
    elif declared_type.startswith("enum:"):
        allowed = declared_type[len("enum:"):].split("|")
        if value not in allowed:
            raise DomainValidationError(field, value, f"must be one of {allowed}")
    else:
        raise DomainValidationError(field, value, f"domain schema declares unknown type '{declared_type}'")


def validate_domain_data(domain: str, domain_data: Any) -> None:
    schema = _load_domain_schema(domain)
    fields = schema.get("fields", {})
    if not isinstance(domain_data, dict):
        raise DomainValidationError("domain_data", domain_data, "expected an object")
    for key, value in domain_data.items():
        if key not in fields:
            raise DomainValidationError(f"domain_data.{key}", value, f"field not declared for domain '{domain}'")
        _check_type(f"domain_data.{key}", value, fields[key])
