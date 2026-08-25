from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_APP = PROJECT_ROOT / "catalog_app"
APP_JS = CATALOG_APP / "static" / "app.js"
BASELINE_PATH = Path(__file__).with_name("backend_message_legacy_baseline.json")

_MESSAGE_CODE_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
_PHRASE_RE = re.compile(r"\s|[^\x00-\x7f]")

LEGACY_FLAT_FIELDS = {
    "legacy_message",
    "message_code",
    "message_default",
    "message_params",
    "message_severity",
}
LEGACY_HELPERS = {"_backend_message", "_message_fields"}
JS_LEGACY_CONSTANTS = {
    "BACKEND_MESSAGE_TRANSLATION_KEYS",
    "SCAN_MODE_TRANSLATION_KEYS",
    "SCAN_TIMING_TRANSLATION_KEYS",
    "SCAN_TIMING_TRANSLATION_PATTERNS",
    "AFFECTED_SCOPE_LEGACY_LABEL_KEYS",
}

TECHNICAL_ERROR_FIELD_SCOPES = {
    ("catalog_app/jobs.py", "<module>.RuntimeJob.to_dict"),
    ("catalog_app/jobs.py", "<module>._run_media_preview_maintenance_for_branches"),
    ("catalog_app/video_tools.py", "<module>.VideoToolProbe.to_dict"),
}

JS_LEGACY_FUNCTIONS = {
    "localizeBackendMessage",
    "messageObjectFromFlatFields",
    "preferredBackendMessages",
    "localizedScanMode",
    "localizedScanTimingStepLabel",
    "localizedAffectedScopeLabel",
}


@dataclass(frozen=True)
class Finding:
    category: str
    path: str
    scope: str
    detail: str

    @property
    def signature(self) -> str:
        return "|".join((self.category, self.path, self.scope, self.detail))


class PythonInventoryVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path.relative_to(PROJECT_ROOT).as_posix()
        self.scope: list[str] = ["<module>"]
        self.findings: list[Finding] = []

    def _scope_name(self) -> str:
        return ".".join(self.scope)

    def _add(self, category: str, detail: str) -> None:
        self.findings.append(
            Finding(category, self.path, self._scope_name(), detail)
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name in LEGACY_HELPERS:
            self._add("legacy-helper", node.name)
        if any(arg.arg == "legacy_message" for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)):
            self._add("legacy-parameter", f"{node.name}:legacy_message")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg == "legacy_message":
                self._add("legacy-keyword", "legacy_message")

        function_name = ""
        if isinstance(node.func, ast.Name):
            function_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            function_name = node.func.attr

        if function_name in {"ApiError", "JobBusyError"}:
            self._add("plain-ui-exception", function_name)
        if function_name == "UpdatePlanPhase":
            for keyword in node.keywords:
                if keyword.arg in {"label", "current_behavior_note"}:
                    self._add("update-plan-ui-text", keyword.arg)

        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value in LEGACY_FLAT_FIELDS:
                    self._add("legacy-field", key.value)
                elif key.value == "message":
                    self._add("direct-message-field", "message")
                elif key.value == "warnings":
                    self._add("direct-warning-field", "warnings")
                elif key.value == "error":
                    if (self.path, self._scope_name()) not in TECHNICAL_ERROR_FIELD_SCOPES:
                        self._add("direct-error-field", "error")
                elif key.value == "label":
                    self._add("direct-label-field", "label")
                elif key.value == "current_behavior_note":
                    self._add("direct-note-field", "current_behavior_note")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        self._inspect_mapping_assignment(names, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        names = [node.target.id] if isinstance(node.target, ast.Name) else []
        if node.value is not None:
            self._inspect_mapping_assignment(names, node.value)
        self.generic_visit(node)

    def _inspect_mapping_assignment(self, names: list[str], value: ast.AST) -> None:
        if not names or not isinstance(value, (ast.Dict, ast.Tuple, ast.List)):
            return

        strings = [
            item.value
            for item in ast.walk(value)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
        codes = [item for item in strings if _MESSAGE_CODE_RE.fullmatch(item)]
        phrases = [
            item
            for item in strings
            if _PHRASE_RE.search(item) and not _MESSAGE_CODE_RE.fullmatch(item)
        ]
        if codes and phrases:
            assignment_name = ",".join(names)
            for code in codes:
                self._add("sentence-code-map", f"{assignment_name}:{code}")


def collect_python_findings() -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(CATALOG_APP.glob("*.py")):
        if path.name == "message_contract.py":
            continue
        source = path.read_text(encoding="utf-8")
        visitor = PythonInventoryVisitor(path)
        visitor.visit(ast.parse(source, filename=str(path)))
        findings.extend(visitor.findings)
    return findings


def _line_scope(source: str, position: int) -> str:
    return f"line-{source.count(chr(10), 0, position) + 1}"


def collect_javascript_findings() -> list[Finding]:
    source = APP_JS.read_text(encoding="utf-8")
    path = APP_JS.relative_to(PROJECT_ROOT).as_posix()
    findings: list[Finding] = []

    for name in sorted(JS_LEGACY_CONSTANTS):
        pattern = re.compile(rf"\bconst\s+{re.escape(name)}\s*=")
        for match in pattern.finditer(source):
            findings.append(Finding("js-legacy-constant", path, "<module>", name))

    for name in sorted(JS_LEGACY_FUNCTIONS):
        pattern = re.compile(rf"\bfunction\s+{re.escape(name)}\s*\(")
        for match in pattern.finditer(source):
            findings.append(Finding("js-legacy-function", path, "<module>", name))

    for field in sorted(LEGACY_FLAT_FIELDS):
        pattern = re.compile(rf"\b{re.escape(field)}\b")
        for _match in pattern.finditer(source):
            findings.append(Finding("js-legacy-field", path, "<module>", field))

    sentence_code_pairs = re.findall(
        r'\[\s*["\']([^"\']*(?:\s|[^\x00-\x7f])[^"\']*)["\']\s*,\s*["\']backendMessage\.([a-z0-9_.]+)["\']\s*\]',
        source,
    )
    for phrase, code in sentence_code_pairs:
        findings.append(
            Finding(
                "js-sentence-code-map",
                path,
                "<module>",
                f"{phrase}=>{code}",
            )
        )

    return findings


def collect_findings() -> list[Finding]:
    return collect_python_findings() + collect_javascript_findings()


def finding_counts(findings: Iterable[Finding]) -> Counter[str]:
    return Counter(finding.signature for finding in findings)


def load_baseline() -> dict[str, object]:
    try:
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing backend message baseline: {BASELINE_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid backend message baseline JSON: {exc}") from exc

    if payload.get("schema_version") != 1:
        raise RuntimeError("Unsupported backend message baseline schema.")
    sites = payload.get("legacy_sites")
    if not isinstance(sites, dict) or not all(
        isinstance(key, str) and isinstance(value, int) and value >= 0
        for key, value in sites.items()
    ):
        raise RuntimeError("Backend message baseline has invalid legacy_sites.")
    return payload


def validate_canonical_contract() -> list[str]:
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from catalog_app.message_contract import (  # pylint: disable=import-outside-toplevel
            build_backend_message,
            validate_backend_message,
        )
    finally:
        sys.path.pop(0)

    errors: list[str] = []
    sample = build_backend_message(
        "scan.update.completed",
        severity="success",
        params={"media_count": 25},
    )
    if list(sample.keys()) != ["code", "params", "severity"]:
        errors.append(f"Canonical message keys are incorrect: {list(sample.keys())}")
    if sample != {
        "code": "scan.update.completed",
        "params": {"media_count": 25},
        "severity": "success",
    }:
        errors.append(f"Canonical message payload is incorrect: {sample!r}")

    for invalid in (
        {"code": "scan.update.completed", "params": {}, "severity": "info", "message": "text"},
        {"code": "scan.update.completed", "params": {}, "severity": "info", "legacy_message": "text"},
        {"code": "scan.update.completed", "params": {}},
    ):
        try:
            validate_backend_message(invalid)
        except ValueError:
            pass
        else:
            errors.append(f"Invalid payload was accepted: {invalid!r}")

    for invalid_code in ("", "Scan.Update", "scan update", "scan"):
        try:
            build_backend_message(invalid_code)
        except ValueError:
            pass
        else:
            errors.append(f"Invalid message code was accepted: {invalid_code!r}")

    return errors


def print_inventory(findings: list[Finding]) -> None:
    category_counts = Counter(finding.category for finding in findings)
    file_counts = Counter(finding.path for finding in findings)

    print("Backend message contract inventory")
    print("=" * 42)
    for category, count in sorted(category_counts.items()):
        print(f"{category}: {count}")
    print("\nFiles containing transition debt:")
    for path, count in sorted(file_counts.items()):
        print(f"- {path}: {count}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the canonical backend message contract and freeze current legacy debt."
    )
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="Print the current transition-debt inventory even when validation passes.",
    )
    args = parser.parse_args()

    errors = validate_canonical_contract()
    findings = collect_findings()
    current = finding_counts(findings)

    try:
        baseline = load_baseline()
    except RuntimeError as exc:
        errors.append(str(exc))
        baseline = {"legacy_sites": {}}

    allowed = Counter(baseline.get("legacy_sites", {}))
    for signature, count in sorted(current.items()):
        allowed_count = allowed.get(signature, 0)
        if count > allowed_count:
            errors.append(
                "New or expanded legacy backend-message site: "
                f"{signature} (current={count}, baseline={allowed_count})."
            )

    if args.inventory or errors:
        print_inventory(findings)

    if errors:
        print("\nBackend message contract check FAILED:")
        for error in errors:
            print(f"- {error}")
        return 1

    removed = sum(allowed.values()) - sum(current.values())
    print(
        "Backend message contract check passed: "
        f"canonical envelope is valid; transition debt did not increase; "
        f"removed sites since baseline={max(removed, 0)}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
