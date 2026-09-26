"""Response schema validation + pattern-based injection scanning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import ResponseSchema

INJECTION_PATTERNS = [
    re.compile(r"ignore (all )?previous instructions", re.IGNORECASE),
    re.compile(r"ignore the above", re.IGNORECASE),
    re.compile(r"disregard all prior", re.IGNORECASE),
    re.compile(r"you are now", re.IGNORECASE),
    re.compile(r"\[SYSTEM\]", re.IGNORECASE),
    re.compile(r"###\s*INSTRUCTION", re.IGNORECASE),
    re.compile(r"```system"),
    re.compile(r"<\|im_start\|>\s*system"),
    re.compile(r"new instructions:", re.IGNORECASE),
]

# Crude heuristic for URLs carrying long base64-looking payloads (exfiltration).
EXFIL_PATTERN = re.compile(r"https?://\S+[?&]\w+=[A-Za-z0-9+/]{24,}={0,2}")


@dataclass
class InjectionResult:
    detected: bool
    matched_pattern: str | None = None


@dataclass
class SchemaResult:
    valid: bool
    missing_fields: list[str] | None = None


def scan_for_injection(payload: Any, enabled: bool = True) -> InjectionResult:
    if not enabled:
        return InjectionResult(detected=False)

    text = _flatten_to_text(payload)

    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            return InjectionResult(detected=True, matched_pattern=pattern.pattern)

    match = EXFIL_PATTERN.search(text)
    if match:
        return InjectionResult(detected=True, matched_pattern="exfiltration_url")

    return InjectionResult(detected=False)


def validate_schema(data: dict | None, schema: ResponseSchema | None) -> SchemaResult:
    if schema is None or not schema.required_fields:
        return SchemaResult(valid=True)
    if data is None:
        return SchemaResult(valid=False, missing_fields=list(schema.required_fields))

    missing = [f for f in schema.required_fields if f not in data]
    if missing:
        return SchemaResult(valid=False, missing_fields=missing)
    return SchemaResult(valid=True)


def _flatten_to_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return " ".join(_flatten_to_text(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return " ".join(_flatten_to_text(v) for v in payload)
    return str(payload)
