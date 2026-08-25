"""Validate external frontend locale files for Catalog 2.0.

The English locale is the reference key set and the runtime fallback. Locale
metadata lives in locales.json; functional JavaScript must not embed translation
data.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

STATIC_ROOT = Path("catalog_app/static")
I18N_ROOT = STATIC_ROOT / "i18n"
REGISTRY_PATH = I18N_ROOT / "locales.json"
APP_JS_PATH = STATIC_ROOT / "app.js"
INDEX_HTML_PATH = STATIC_ROOT / "index.html"
LOCALE_CODE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


class DuplicateKeyError(ValueError):
    pass


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        fail(f"missing JSON file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except DuplicateKeyError as exc:
        fail(f"{path}: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}")
    if not isinstance(payload, dict):
        fail(f"JSON root must be an object: {path}")
    return payload


def validate_registry(payload: dict[str, Any]) -> list[dict[str, str]]:
    default = payload.get("default")
    fallback = payload.get("fallback")
    definitions = payload.get("locales")

    if default != "en":
        fail("locales.json default must be 'en'")
    if fallback != "en":
        fail("locales.json fallback must be 'en'")
    if not isinstance(definitions, list) or not definitions:
        fail("locales.json locales must be a non-empty array")

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(definitions):
        if not isinstance(item, dict):
            fail(f"locales.json locales[{index}] must be an object")
        code = item.get("code")
        name = item.get("name")
        if not isinstance(code, str) or not LOCALE_CODE_RE.fullmatch(code):
            fail(f"invalid locale code at locales[{index}]: {code!r}")
        if not isinstance(name, str) or not name.strip():
            fail(f"missing locale display name for {code!r}")
        if code in seen:
            fail(f"duplicate locale code in locales.json: {code!r}")
        seen.add(code)
        normalized.append({"code": code, "name": name.strip()})

    if default not in seen:
        fail("default locale is not registered")
    if fallback not in seen:
        fail("fallback locale is not registered")
    return normalized


def validate_locale_file(path: Path, code: str) -> dict[str, str]:
    payload = load_json_object(path)
    translations: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            fail(f"{path}: translation keys must be non-empty strings")
        if not isinstance(value, str):
            fail(f"{path}: value for {key!r} must be a string")
        translations[key] = value
    if not translations:
        fail(f"{path}: locale {code!r} has no translations")
    return translations


def placeholders(value: str) -> set[str]:
    return set(PLACEHOLDER_RE.findall(value))


def validate_app_structure(locale_codes: set[str]) -> None:
    app_source = APP_JS_PATH.read_text(encoding="utf-8")
    if "const TRANSLATIONS" in app_source or "let TRANSLATIONS" in app_source or "var TRANSLATIONS" in app_source:
        fail("app.js must not embed a TRANSLATIONS object")
    if '"/static/i18n/locales.json"' not in app_source:
        fail("app.js does not reference the locale registry")

    index_source = INDEX_HTML_PATH.read_text(encoding="utf-8")
    for code in locale_codes:
        if re.search(rf'<option\b[^>]*\bvalue=["\']{re.escape(code)}["\']', index_source):
            fail(f"index.html hard-codes locale option {code!r}; options must come from locales.json")


def main() -> int:
    registry = load_json_object(REGISTRY_PATH)
    definitions = validate_registry(registry)
    locale_codes = {item["code"] for item in definitions}

    expected_files = {f"{code}.json" for code in locale_codes} | {"locales.json"}
    actual_files = {path.name for path in I18N_ROOT.glob("*.json")}
    missing_files = sorted(expected_files - actual_files)
    unregistered_files = sorted(actual_files - expected_files)
    if missing_files:
        fail(f"missing registered locale file(s): {', '.join(missing_files)}")
    if unregistered_files:
        fail(f"unregistered locale file(s): {', '.join(unregistered_files)}")

    translations = {
        code: validate_locale_file(I18N_ROOT / f"{code}.json", code)
        for code in sorted(locale_codes)
    }
    reference = translations["en"]
    reference_keys = set(reference)

    for code, locale in translations.items():
        locale_keys = set(locale)
        missing = sorted(reference_keys - locale_keys)
        extra = sorted(locale_keys - reference_keys)
        if missing:
            fail(f"locale {code!r} is missing {len(missing)} key(s): {', '.join(missing[:10])}")
        if extra:
            fail(f"locale {code!r} has {len(extra)} extra key(s): {', '.join(extra[:10])}")
        for key in reference:
            expected = placeholders(reference[key])
            actual = placeholders(locale[key])
            if actual != expected:
                fail(
                    f"placeholder mismatch for {key!r} in locale {code!r}: "
                    f"expected {sorted(expected)}, got {sorted(actual)}"
                )

    validate_app_structure(locale_codes)

    print("OK: external i18n guardrail passed")
    print(f"locales: {', '.join(sorted(locale_codes))}")
    print(f"reference keys: {len(reference_keys)}")
    print("default: en")
    print("fallback: en")
    return 0


if __name__ == "__main__":
    sys.exit(main())
