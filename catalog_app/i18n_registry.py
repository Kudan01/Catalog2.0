from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


_LOCALE_CODE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REGISTRY_PATH = Path(__file__).resolve().parent / "static" / "i18n" / "locales.json"


class LocaleRegistryError(RuntimeError):
    """Raised when the frontend locale registry is missing or invalid."""


@dataclass(frozen=True)
class LocaleDefinition:
    code: str
    name: str


@dataclass(frozen=True)
class LocaleRegistry:
    default: str
    fallback: str
    locales: tuple[LocaleDefinition, ...]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(locale.code for locale in self.locales)


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LocaleRegistryError(f"Locale registry field {key!r} must be a non-empty string.")
    return value.strip().lower()


@lru_cache(maxsize=1)
def load_locale_registry() -> LocaleRegistry:
    try:
        payload = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LocaleRegistryError(f"Locale registry is missing: {_REGISTRY_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise LocaleRegistryError(
            f"Locale registry contains invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(payload, dict):
        raise LocaleRegistryError("Locale registry root must be a JSON object.")

    default = _required_text(payload, "default")
    fallback = _required_text(payload, "fallback")
    raw_locales = payload.get("locales")
    if not isinstance(raw_locales, list) or not raw_locales:
        raise LocaleRegistryError("Locale registry field 'locales' must be a non-empty array.")

    definitions: list[LocaleDefinition] = []
    seen: set[str] = set()
    for index, raw_definition in enumerate(raw_locales):
        if not isinstance(raw_definition, dict):
            raise LocaleRegistryError(f"Locale registry item {index} must be an object.")
        code = str(raw_definition.get("code") or "").strip().lower()
        name = str(raw_definition.get("name") or "").strip()
        if not _LOCALE_CODE_RE.fullmatch(code):
            raise LocaleRegistryError(f"Locale registry item {index} has invalid code: {code!r}.")
        if not name:
            raise LocaleRegistryError(f"Locale registry item {index} is missing a display name.")
        if code in seen:
            raise LocaleRegistryError(f"Locale registry contains duplicate code: {code!r}.")
        seen.add(code)
        definitions.append(LocaleDefinition(code=code, name=name))

    if default not in seen:
        raise LocaleRegistryError(f"Default locale is not registered: {default!r}.")
    if fallback not in seen:
        raise LocaleRegistryError(f"Fallback locale is not registered: {fallback!r}.")
    if default != "en" or fallback != "en":
        raise LocaleRegistryError("English must be both the default and fallback locale.")

    return LocaleRegistry(default=default, fallback=fallback, locales=tuple(definitions))
