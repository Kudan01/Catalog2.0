from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .config import Config
from .models import (
    FolderRecord,
    MediaRecord,
    ScanErrorRecord,
    ScanSkipRecord,
)
from .scanner import scan_data_root, validate_scan_scope


ScanActionKind = Literal[
    "begin_scan",
    "folder",
    "media",
    "error",
    "skip",
    "complete_scan",
]
ScanPayload: TypeAlias = (
    FolderRecord | MediaRecord | ScanErrorRecord | ScanSkipRecord | None
)


@dataclass(frozen=True)
class ScanAction:
    """One action in the shared, streaming scan plan."""

    kind: ScanActionKind
    payload: ScanPayload = None


@dataclass(frozen=True)
class ScanPreviewSummary:
    folders: int
    images: int
    gifs: int
    videos: int
    other: int
    errors: int
    skipped_links: int
    skipped_ignored_directories: int
    skipped_other: int
    duration_seconds: float
    completed: bool


def build_scan_plan(
    config: Config,
    *,
    branch_rel_path: str = "",
) -> Iterator[ScanAction]:
    """
    Build the shared scan plan as a one-pass iterator.

    The same stream will later be consumed by database execution. This step
    only provides read-only preview output.
    """
    branch_rel_path = validate_scan_scope(config, branch_rel_path)

    yield ScanAction("begin_scan")

    for record in scan_data_root(config, branch_rel_path=branch_rel_path):
        if isinstance(record, FolderRecord):
            yield ScanAction("folder", record)
        elif isinstance(record, MediaRecord):
            yield ScanAction("media", record)
        elif isinstance(record, ScanErrorRecord):
            yield ScanAction("error", record)
        elif isinstance(record, ScanSkipRecord):
            yield ScanAction("skip", record)
        else:
            raise TypeError(f"Unknown scan record type: {type(record)!r}")

    yield ScanAction("complete_scan")


def print_scan_preview(
    actions: Iterable[ScanAction],
    *,
    sample_limit: int = 30,
) -> ScanPreviewSummary:
    """Consume and print a read-only scan plan without storing it in memory."""
    started = time.monotonic()
    counts = {
        "folders": 0,
        "images": 0,
        "gifs": 0,
        "videos": 0,
        "other": 0,
        "errors": 0,
        "skipped_links": 0,
        "skipped_ignored_directories": 0,
        "skipped_other": 0,
    }
    samples: list[str] = []
    completed = False

    for action in actions:
        if action.kind == "folder":
            record = _expect_payload(action, FolderRecord)
            counts["folders"] += 1
            _add_sample(
                samples,
                sample_limit,
                f"FOLDER  {record.rel_path or '[root]'}",
            )

        elif action.kind == "media":
            record = _expect_payload(action, MediaRecord)
            count_key = {
                "image": "images",
                "gif": "gifs",
                "video": "videos",
                "other": "other",
            }[record.media_type]
            counts[count_key] += 1
            _add_sample(
                samples,
                sample_limit,
                f"{record.media_type.upper():7} {record.rel_path}",
            )

        elif action.kind == "error":
            record = _expect_payload(action, ScanErrorRecord)
            counts["errors"] += 1
            _add_sample(
                samples,
                sample_limit,
                f"ERROR   {record.rel_path or '[root]'}: "
                f"{record.operation}: {record.message}",
            )

        elif action.kind == "skip":
            record = _expect_payload(action, ScanSkipRecord)
            if record.reason == "link_or_junction":
                counts["skipped_links"] += 1
            elif record.reason == "ignored_directory":
                counts["skipped_ignored_directories"] += 1
            else:
                counts["skipped_other"] += 1

            _add_sample(
                samples,
                sample_limit,
                f"SKIPPED {record.rel_path}: {record.reason}",
            )

        elif action.kind == "complete_scan":
            completed = True

    duration = time.monotonic() - started
    summary = ScanPreviewSummary(
        folders=counts["folders"],
        images=counts["images"],
        gifs=counts["gifs"],
        videos=counts["videos"],
        other=counts["other"],
        errors=counts["errors"],
        skipped_links=counts["skipped_links"],
        skipped_ignored_directories=counts["skipped_ignored_directories"],
        skipped_other=counts["skipped_other"],
        duration_seconds=duration,
        completed=completed,
    )

    print("Catalog 2.0 – read-only scan preview")
    print("=" * 70)
    print(f"Folders: {summary.folders}")
    print(f"Photos: {summary.images}")
    print(f"GIFy: {summary.gifs}")
    print(f"Videos: {summary.videos}")
    print(f"Other: {summary.other}")
    print(f"Errors: {summary.errors}")
    print(f"Skipped links/junctions: {summary.skipped_links}")
    print(
        "Skipped technical directories: "
        f"{summary.skipped_ignored_directories}"
    )
    print(f"Other skipped items: {summary.skipped_other}")
    print(f"Time: {summary.duration_seconds:.3f} s")
    print(f"Plan completed: {'yes' if summary.completed else 'no'}")
    print()
    print(f"First {sample_limit} item actions:")

    if samples:
        for sample in samples:
            print(f"- {sample}")
    else:
        print("- none")

    print()
    print("The scan was read-only. The database and files were not changed.")
    return summary


def _expect_payload(action: ScanAction, expected_type: type):
    if not isinstance(action.payload, expected_type):
        raise TypeError(
            f"Action {action.kind} does not have expected payload {expected_type.__name__}."
        )
    return action.payload


def _add_sample(samples: list[str], limit: int, text: str) -> None:
    if len(samples) < limit:
        samples.append(text)
