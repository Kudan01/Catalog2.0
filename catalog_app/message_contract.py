from __future__ import annotations

import re
from typing import Any, Literal, Mapping, TypedDict, cast


MessageSeverity = Literal["success", "info", "warning", "blocker", "error"]
MESSAGE_SEVERITIES: frozenset[str] = frozenset(
    {"success", "info", "warning", "blocker", "error"}
)

_MESSAGE_CODE_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")


class BackendMessage(TypedDict):
    """Language-neutral message envelope sent from the backend to the UI."""

    code: str
    params: dict[str, Any]
    severity: MessageSeverity


def build_backend_message(
    code: str,
    *,
    severity: MessageSeverity = "info",
    params: Mapping[str, Any] | None = None,
) -> BackendMessage:
    """Build the only supported UI-facing backend message structure.

    Localized text and English fallback strings do not belong in this payload.
    The frontend resolves ``code`` through the active locale and ``en.json``.
    """
    normalized_code = str(code or "").strip()
    if not _MESSAGE_CODE_RE.fullmatch(normalized_code):
        raise ValueError(
            "Backend message code must use lowercase dot-separated identifiers: "
            f"{normalized_code!r}."
        )
    if severity not in MESSAGE_SEVERITIES:
        raise ValueError(f"Unsupported backend message severity: {severity!r}.")

    return {
        "code": normalized_code,
        "params": dict(params or {}),
        "severity": cast(MessageSeverity, severity),
    }


def validate_backend_message(value: object) -> BackendMessage:
    """Validate and normalize an existing UI-facing message envelope."""
    if not isinstance(value, dict):
        raise ValueError("Backend message must be a JSON object.")

    allowed_keys = {"code", "params", "severity"}
    actual_keys = set(value)
    if actual_keys != allowed_keys:
        missing = sorted(allowed_keys - actual_keys)
        extra = sorted(actual_keys - allowed_keys)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        raise ValueError(
            "Backend message must contain exactly code, params and severity"
            + (f" ({', '.join(details)})" if details else "")
            + "."
        )

    params = value["params"]
    if not isinstance(params, dict):
        raise ValueError("Backend message params must be a JSON object.")

    return build_backend_message(
        str(value["code"]),
        severity=cast(MessageSeverity, value["severity"]),
        params=params,
    )
