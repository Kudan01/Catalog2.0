from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize and compare Catalog 2.0 diagnostic sessions."
    )
    parser.add_argument(
        "path",
        help="Diagnostics directory, one *_summary.json file, or one .jsonl events file.",
    )
    parser.add_argument(
        "--latest",
        type=int,
        default=2,
        help="Number of latest sessions to show when PATH is a directory; default is 2.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    target = Path(args.path).expanduser()
    summaries = find_summaries(target, latest=max(1, args.latest))
    if not summaries:
        raise SystemExit(f"No diagnostic summary files found: {target}")

    print("Catalog 2.0 – diagnostic summary")
    print("=" * 72)
    loaded = []
    for index, summary_path in enumerate(summaries, start=1):
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        events = read_events(Path(summary.get("events_file", "")))
        loaded.append((summary, events))
        print_session(index, summary_path, summary, events)
        if index != len(summaries):
            print()

    if len(loaded) >= 2:
        print()
        print_comparison(loaded[-2][0], loaded[-2][1], loaded[-1][0], loaded[-1][1])
    return 0


def find_summaries(target: Path, *, latest: int) -> list[Path]:
    if target.is_dir():
        candidates = sorted(
            target.glob("catalog2_diagnostics_*_summary.json"),
            key=lambda path: path.stat().st_mtime,
        )
        return candidates[-latest:]
    if target.name.endswith("_summary.json") and target.is_file():
        return [target]
    if target.suffix == ".jsonl" and target.is_file():
        candidate = target.with_name(target.stem + "_summary.json")
        return [candidate] if candidate.exists() else []
    return []


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def event_value(events: list[dict[str, Any]], name: str, field: str) -> Any:
    for event in events:
        if event.get("event") == name:
            return event.get(field)
    return None


def print_session(
    index: int,
    summary_path: Path,
    summary: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    verified_ms = event_value(events, "backend.launch.instance_verified", "elapsed_ms")
    db_check_ms = event_value(events, "backend.server.database_checked", "duration_ms")
    browser_request_ms = event_value(events, "backend.launch.browser_open_requested", "elapsed_ms")
    process_elapsed_ms = event_value(events, "backend.cli.ready", "process_elapsed_ms")
    import_before_main_ms = event_value(events, "backend.cli.ready", "import_before_main_ms")
    script_to_verified_ms = add_numbers(process_elapsed_ms, verified_ms)
    script_to_browser_ms = add_numbers(process_elapsed_ms, browser_request_ms)
    frontend = summary.get("frontend_durations", {})

    print(f"Session {index}: {summary_path.name}")
    print(f"started:            {summary.get('started_at', '-')}")
    print(f"finish reason:      {summary.get('finish_reason', '-')}")
    print(f"session duration:   {number(summary.get('duration_ms'))} ms")
    print(f"imports before CLI: {number(import_before_main_ms)} ms")
    print(f"server verified:    {number(script_to_verified_ms)} ms from script entry")
    print(f"runtime DB check:   {number(db_check_ms)} ms")
    print(f"browser requested:  {number(script_to_browser_ms)} ms from script entry")
    print(f"SQL plans:          {'yes' if summary.get('query_plans_enabled') else 'no'}")

    for event_name, label in (
        ("frontend.app.main.end", "frontend initial load"),
        ("frontend.navigation.folder.end", "folder navigation"),
        ("frontend.tree.load.end", "tree rebuild"),
        ("frontend.locale.save.end", "language change"),
    ):
        value = frontend.get(event_name)
        if value:
            print(
                f"{label + ':':20} avg {number(value.get('average_ms'))} ms, "
                f"max {number(value.get('max_ms'))} ms, count {value.get('count', 0)}"
            )

    print("slowest HTTP:")
    for item in summary.get("slowest_http_requests", [])[:8]:
        print(
            f"- {item.get('request')}: max {number(item.get('max_ms'))} ms, "
            f"avg {number(item.get('average_ms'))} ms, SQL {number(item.get('sql_total_ms'))} ms"
        )

    print("SQL by total time:")
    for item in summary.get("sql_by_total_time", [])[:8]:
        sql = str(item.get("sql", "")).replace("\n", " ")
        print(
            f"- {number(item.get('total_ms'))} ms / {item.get('count', 0)}×: {sql[:140]}"
        )


def print_comparison(
    first: dict[str, Any],
    first_events: list[dict[str, Any]],
    second: dict[str, Any],
    second_events: list[dict[str, Any]],
) -> None:
    print("Latest two sessions – direct comparison")
    print("=" * 72)
    print_delta(
        "imports before CLI",
        event_value(first_events, "backend.cli.ready", "import_before_main_ms"),
        event_value(second_events, "backend.cli.ready", "import_before_main_ms"),
    )
    print_delta(
        "server verified",
        add_numbers(
            event_value(first_events, "backend.cli.ready", "process_elapsed_ms"),
            event_value(first_events, "backend.launch.instance_verified", "elapsed_ms"),
        ),
        add_numbers(
            event_value(second_events, "backend.cli.ready", "process_elapsed_ms"),
            event_value(second_events, "backend.launch.instance_verified", "elapsed_ms"),
        ),
    )
    print_delta(
        "database check",
        event_value(first_events, "backend.server.database_checked", "duration_ms"),
        event_value(second_events, "backend.server.database_checked", "duration_ms"),
    )
    print_delta(
        "browser requested",
        add_numbers(
            event_value(first_events, "backend.cli.ready", "process_elapsed_ms"),
            event_value(first_events, "backend.launch.browser_open_requested", "elapsed_ms"),
        ),
        add_numbers(
            event_value(second_events, "backend.cli.ready", "process_elapsed_ms"),
            event_value(second_events, "backend.launch.browser_open_requested", "elapsed_ms"),
        ),
    )

    first_frontend = first.get("frontend_durations", {})
    second_frontend = second.get("frontend_durations", {})
    for event_name, label in (
        ("frontend.app.main.end", "frontend initial load"),
        ("frontend.navigation.folder.end", "folder navigation"),
        ("frontend.tree.load.end", "tree rebuild"),
        ("frontend.locale.save.end", "language change"),
    ):
        first_value = first_frontend.get(event_name, {}).get("max_ms")
        second_value = second_frontend.get(event_name, {}).get("max_ms")
        if first_value is not None or second_value is not None:
            print_delta(label, first_value, second_value)


def print_delta(label: str, first: Any, second: Any) -> None:
    try:
        first_number = float(first)
        second_number = float(second)
    except (TypeError, ValueError):
        print(f"{label}: insufficient data")
        return
    delta = second_number - first_number
    percent = (delta / first_number * 100.0) if first_number else 0.0
    print(
        f"{label}: {first_number:.3f} → {second_number:.3f} ms "
        f"({delta:+.3f} ms, {percent:+.1f} %)"
    )


def add_numbers(first: Any, second: Any) -> float | None:
    try:
        return float(first) + float(second)
    except (TypeError, ValueError):
        return None


def number(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "-"


if __name__ == "__main__":
    raise SystemExit(main())
