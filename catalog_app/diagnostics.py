from __future__ import annotations

import contextvars
import ctypes
import hashlib
import json
import math
import os
import platform
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


_DIAGNOSTIC_ENV = "CATALOG2_DIAGNOSTICS"
_QUERY_PLAN_ENV = "CATALOG2_DIAGNOSTICS_SQL_PLANS"
_ENABLED_VALUES = {"1", "true", "yes", "on"}
_MAX_FRONTEND_BATCH_BYTES = 512 * 1024
_MAX_FRONTEND_EVENTS_PER_BATCH = 500
_SQL_SPACE_RE = re.compile(r"\s+")
_SQL_LITERAL_RE = re.compile(r"'(?:''|[^'])*'|\b\d+(?:\.\d+)?\b")


@dataclass
class RequestSqlMetrics:
    count: int = 0
    total_ms: float = 0.0
    slowest: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestTrace:
    started: float
    method: str
    path: str
    query: dict[str, list[str]]
    context_token: contextvars.Token[RequestSqlMetrics | None]
    metrics: RequestSqlMetrics


_current_request_metrics: contextvars.ContextVar[RequestSqlMetrics | None] = contextvars.ContextVar(
    "catalog2_diagnostic_request_metrics",
    default=None,
)
_active_session: DiagnosticsSession | None = None
_active_session_lock = threading.Lock()


def diagnostics_requested() -> bool:
    return os.environ.get(_DIAGNOSTIC_ENV, "").strip().lower() in _ENABLED_VALUES


def query_plans_requested() -> bool:
    return os.environ.get(_QUERY_PLAN_ENV, "").strip().lower() in _ENABLED_VALUES


def get_diagnostics_session() -> DiagnosticsSession | None:
    return _active_session


def start_diagnostics_session(
    *,
    config_path: Path,
    output_root: Path,
    command: str,
    cli_elapsed_ms: float,
    config_load_ms: float,
    process_elapsed_ms: float,
) -> DiagnosticsSession:
    global _active_session

    with _active_session_lock:
        if _active_session is not None:
            return _active_session

        session = DiagnosticsSession.create(
            config_path=config_path,
            output_root=output_root,
            command=command,
            query_plans_enabled=query_plans_requested(),
        )
        _active_session = session

    session.record(
        "backend.cli.ready",
        cli_elapsed_ms=round(cli_elapsed_ms, 3),
        config_load_ms=round(config_load_ms, 3),
        process_elapsed_ms=round(process_elapsed_ms, 3),
        import_before_main_ms=round(max(0.0, process_elapsed_ms - cli_elapsed_ms), 3),
        argv=sys.argv,
    )
    return session


def finish_diagnostics_session(reason: str) -> None:
    global _active_session

    with _active_session_lock:
        session = _active_session
        _active_session = None

    if session is not None:
        session.finish(reason=reason)


def diagnostic_browser_url(base_url: str) -> str:
    session = get_diagnostics_session()
    if session is None:
        return base_url

    split = urlsplit(base_url)
    query = parse_qs(split.query, keep_blank_values=True)
    query["catalog2_diagnostics"] = [session.session_id]
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query, doseq=True), split.fragment))


def diagnostics_output_lines() -> list[str]:
    session = get_diagnostics_session()
    if session is None:
        return []
    return [
        "Diagnostics: enabled for this run",
        f"events:      {session.log_path}",
        f"summary:     {session.summary_path}",
        f"SQL plans:   {'enabled' if session.query_plans_enabled else 'disabled'}",
    ]


def diagnostic_record(event: str, **fields: Any) -> None:
    session = get_diagnostics_session()
    if session is not None:
        session.record(event, **fields)


def begin_http_request(method: str, raw_path: str) -> RequestTrace | None:
    session = get_diagnostics_session()
    if session is None:
        return None

    parsed = urlsplit(raw_path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.pop("catalog2_diagnostics", None)
    metrics = RequestSqlMetrics()
    token = _current_request_metrics.set(metrics)
    return RequestTrace(
        started=time.perf_counter(),
        method=method,
        path=parsed.path or "/",
        query=_safe_query(query),
        context_token=token,
        metrics=metrics,
    )


def finish_http_request(
    trace: RequestTrace | None,
    *,
    status_code: int,
    response_bytes: int,
) -> None:
    if trace is None:
        return

    try:
        session = get_diagnostics_session()
        if session is None:
            return
        elapsed_ms = (time.perf_counter() - trace.started) * 1000.0
        session.record_http_request(
            method=trace.method,
            path=trace.path,
            query=trace.query,
            status_code=status_code,
            response_bytes=response_bytes,
            elapsed_ms=elapsed_ms,
            sql_metrics=trace.metrics,
        )
    finally:
        _current_request_metrics.reset(trace.context_token)


def diagnostic_sql_connection_factory() -> type[sqlite3.Connection] | None:
    return DiagnosticConnection if get_diagnostics_session() is not None else None


def diagnostic_request_active() -> bool:
    """Return whether the current call is inside an instrumented HTTP request."""
    return get_diagnostics_session() is not None and _current_request_metrics.get() is not None


def diagnostic_request_sql_snapshot() -> tuple[int, float]:
    metrics = _current_request_metrics.get()
    if metrics is None:
        return (0, 0.0)
    return (metrics.count, metrics.total_ms)


def diagnostic_set_request_detail(name: str, value: Mapping[str, Any]) -> None:
    metrics = _current_request_metrics.get()
    if metrics is not None and get_diagnostics_session() is not None:
        metrics.details[name] = _json_safe(value)


def record_sql(statement: str, elapsed_ms: float, *, operation: str) -> None:
    session = get_diagnostics_session()
    if session is None:
        return

    normalized = normalize_sql(statement)
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    session.record_sql(
        normalized_sql=normalized,
        fingerprint=fingerprint,
        elapsed_ms=elapsed_ms,
        operation=operation,
    )

    request_metrics = _current_request_metrics.get()
    if request_metrics is not None:
        request_metrics.count += 1
        request_metrics.total_ms += elapsed_ms
        item = {
            "fingerprint": fingerprint,
            "elapsed_ms": round(elapsed_ms, 3),
            "sql": normalized[:500],
        }
        request_metrics.slowest.append(item)
        request_metrics.slowest.sort(key=lambda value: value["elapsed_ms"], reverse=True)
        del request_metrics.slowest[5:]


def normalize_sql(statement: str) -> str:
    compact = _SQL_SPACE_RE.sub(" ", str(statement or "").strip())
    return compact[:2000]


def _sql_template(statement: str) -> str:
    return _SQL_LITERAL_RE.sub("?", normalize_sql(statement))


class DiagnosticCursor(sqlite3.Cursor):
    def execute(self, sql: str, parameters: Any = (), /) -> DiagnosticCursor:
        started = time.perf_counter()
        try:
            result = super().execute(sql, parameters)
        finally:
            record_sql(sql, (time.perf_counter() - started) * 1000.0, operation="execute")
        session = get_diagnostics_session()
        if session is not None and session.query_plans_enabled:
            session.capture_query_plan(self.connection, sql, parameters)
        return result

    def executemany(self, sql: str, seq_of_parameters: Iterable[Any], /) -> DiagnosticCursor:
        started = time.perf_counter()
        try:
            return super().executemany(sql, seq_of_parameters)
        finally:
            record_sql(sql, (time.perf_counter() - started) * 1000.0, operation="executemany")

    def executescript(self, sql_script: str, /) -> DiagnosticCursor:
        started = time.perf_counter()
        try:
            return super().executescript(sql_script)
        finally:
            record_sql(sql_script, (time.perf_counter() - started) * 1000.0, operation="executescript")


class DiagnosticConnection(sqlite3.Connection):
    def cursor(self, factory: type[sqlite3.Cursor] | None = None) -> sqlite3.Cursor:
        return super().cursor(factory=factory or DiagnosticCursor)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Iterable[Any], /) -> sqlite3.Cursor:
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self.cursor().executescript(sql_script)


@dataclass
class DiagnosticsSession:
    session_id: str
    started_at: str
    started_perf: float
    log_path: Path
    summary_path: Path
    config_path: Path
    output_root: Path
    command: str
    query_plans_enabled: bool
    process_rss_bytes_at_start: int | None
    lock: threading.Lock = field(default_factory=threading.Lock)
    event_counts: Counter[str] = field(default_factory=Counter)
    request_aggregates: dict[str, dict[str, Any]] = field(default_factory=dict)
    sql_aggregates: dict[str, dict[str, Any]] = field(default_factory=dict)
    frontend_event_counts: Counter[str] = field(default_factory=Counter)
    frontend_duration_aggregates: dict[str, dict[str, float | int]] = field(default_factory=dict)
    query_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    finished: bool = False

    @classmethod
    def create(
        cls,
        *,
        config_path: Path,
        output_root: Path,
        command: str,
        query_plans_enabled: bool,
    ) -> DiagnosticsSession:
        diagnostics_dir = output_root / "_state" / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        stamp = now.astimezone().strftime("%Y%m%d_%H%M%S")
        session_id = uuid.uuid4().hex
        base_name = f"catalog2_diagnostics_{stamp}_{os.getpid()}_{session_id[:8]}"
        session = cls(
            session_id=session_id,
            started_at=now.isoformat(),
            started_perf=time.perf_counter(),
            log_path=diagnostics_dir / f"{base_name}.jsonl",
            summary_path=diagnostics_dir / f"{base_name}_summary.json",
            config_path=config_path.resolve(),
            output_root=output_root.resolve(),
            command=command,
            query_plans_enabled=query_plans_enabled,
            process_rss_bytes_at_start=process_rss_bytes(),
        )
        session.record(
            "diagnostics.session.started",
            command=command,
            pid=os.getpid(),
            python=sys.version,
            executable=sys.executable,
            platform=platform.platform(),
            config_path=str(session.config_path),
            output_root=str(session.output_root),
            query_plans_enabled=query_plans_enabled,
            process_rss_bytes=session.process_rss_bytes_at_start,
        )
        return session

    def record(self, event: str, **fields: Any) -> None:
        item = {
            "event": event,
            "session_id": self.session_id,
            "wall_time": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((time.perf_counter() - self.started_perf) * 1000.0, 3),
            **_json_safe(fields),
        }
        line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            self.event_counts[event] += 1
            with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")

    def record_http_request(
        self,
        *,
        method: str,
        path: str,
        query: dict[str, list[str]],
        status_code: int,
        response_bytes: int,
        elapsed_ms: float,
        sql_metrics: RequestSqlMetrics,
    ) -> None:
        is_transport = path == "/api/diagnostics/events"
        event = "backend.http.diagnostic_transport" if is_transport else "backend.http.request"
        current_rss_bytes = process_rss_bytes()
        details = dict(sql_metrics.details)
        folder_browse = details.get("folder_browse")
        if isinstance(folder_browse, dict):
            folder_browse = dict(folder_browse)
            preview_sql_ms = max(0.0, float(folder_browse.pop("preview_sql_duration_ms", 0.0)))
            # Preview wall time already contains its SQL; subtract only SQL from
            # the remaining request phases to keep the "other" bucket disjoint.
            tracked_ms = sum(
                max(0.0, float(folder_browse.get(field, 0.0)))
                for field in (
                    "source_root_status_ms",
                    "folder_fs_status_ms",
                    "root_enumeration_ms",
                    "preview_metadata_ms",
                )
            )
            tracked_ms += max(0.0, sql_metrics.total_ms - preview_sql_ms)
            folder_browse["total_ms"] = round(elapsed_ms, 3)
            folder_browse["sql_ms"] = round(sql_metrics.total_ms, 3)
            folder_browse["other_ms"] = round(max(0.0, elapsed_ms - tracked_ms), 3)
            details["folder_browse"] = folder_browse

        thumbnail = details.get("thumbnail")
        if isinstance(thumbnail, dict):
            thumbnail = dict(thumbnail)
            tracked_ms = sum(
                max(0.0, float(thumbnail.get(field, 0.0)))
                for field in (
                    "media_lookup_ms",
                    "thumbnail_lookup_ms",
                    "cache_file_check_ms",
                )
            )
            thumbnail["http_status"] = int(status_code)
            thumbnail["total_ms"] = round(elapsed_ms, 3)
            thumbnail["other_ms"] = round(max(0.0, elapsed_ms - tracked_ms), 3)
            details["thumbnail"] = thumbnail

        self.record(
            event,
            method=method,
            path=path,
            query=query,
            status_code=status_code,
            response_bytes=response_bytes,
            duration_ms=round(elapsed_ms, 3),
            sql_count=sql_metrics.count,
            sql_duration_ms=round(sql_metrics.total_ms, 3),
            slowest_sql=sql_metrics.slowest,
            process_rss_bytes=current_rss_bytes,
            **details,
        )
        if is_transport:
            return

        key = f"{method} {path}"
        with self.lock:
            aggregate = self.request_aggregates.setdefault(
                key,
                {
                    "count": 0,
                    "total_ms": 0.0,
                    "max_ms": 0.0,
                    "response_bytes": 0,
                    "sql_count": 0,
                    "sql_total_ms": 0.0,
                    "statuses": defaultdict(int),
                    "max_process_rss_bytes": 0,
                },
            )
            aggregate["count"] += 1
            aggregate["total_ms"] += elapsed_ms
            aggregate["max_ms"] = max(aggregate["max_ms"], elapsed_ms)
            aggregate["response_bytes"] += max(0, int(response_bytes))
            aggregate["sql_count"] += sql_metrics.count
            aggregate["sql_total_ms"] += sql_metrics.total_ms
            aggregate["statuses"][str(status_code)] += 1
            aggregate["max_process_rss_bytes"] = max(
                int(aggregate["max_process_rss_bytes"]),
                int(current_rss_bytes or 0),
            )

    def record_sql(
        self,
        *,
        normalized_sql: str,
        fingerprint: str,
        elapsed_ms: float,
        operation: str,
    ) -> None:
        with self.lock:
            aggregate = self.sql_aggregates.setdefault(
                fingerprint,
                {
                    "sql": _sql_template(normalized_sql)[:1000],
                    "operations": defaultdict(int),
                    "count": 0,
                    "total_ms": 0.0,
                    "max_ms": 0.0,
                },
            )
            aggregate["operations"][operation] += 1
            aggregate["count"] += 1
            aggregate["total_ms"] += elapsed_ms
            aggregate["max_ms"] = max(aggregate["max_ms"], elapsed_ms)

    def capture_query_plan(self, connection: sqlite3.Connection, sql: str, parameters: Any) -> None:
        normalized = normalize_sql(sql)
        upper = normalized.lstrip().upper()
        if not (upper.startswith("SELECT ") or upper.startswith("WITH ")):
            return
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        with self.lock:
            if fingerprint in self.query_plans:
                return
            self.query_plans[fingerprint] = {"status": "pending", "sql": _sql_template(normalized)[:1000]}

        try:
            cursor = sqlite3.Connection.cursor(connection, factory=sqlite3.Cursor)
            try:
                sqlite3.Cursor.execute(cursor, "EXPLAIN QUERY PLAN " + sql, parameters)
                rows = cursor.fetchall()
            finally:
                cursor.close()
            plan = [
                {
                    "id": int(row[0]),
                    "parent": int(row[1]),
                    "detail": str(row[3]),
                }
                for row in rows
            ]
            value = {"status": "ok", "sql": _sql_template(normalized)[:1000], "plan": plan}
        except Exception as exc:
            value = {
                "status": "error",
                "sql": _sql_template(normalized)[:1000],
                "technical_detail": f"{exc.__class__.__name__}: {exc}",
            }
        with self.lock:
            self.query_plans[fingerprint] = value
        self.record("backend.sql.query_plan", fingerprint=fingerprint, **value)

    def accept_frontend_events(self, *, token: str, raw_body: bytes) -> int:
        if token != self.session_id:
            raise ValueError("Diagnostic session token does not match the active session.")
        if len(raw_body) > _MAX_FRONTEND_BATCH_BYTES:
            raise ValueError("Diagnostic frontend event batch is too large.")
        payload = json.loads(raw_body.decode("utf-8"))
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            raise ValueError("Diagnostic frontend payload must contain an events array.")
        if len(events) > _MAX_FRONTEND_EVENTS_PER_BATCH:
            raise ValueError("Diagnostic frontend event batch contains too many events.")

        accepted = 0
        for event in events:
            if not isinstance(event, dict):
                continue
            name = str(event.get("event") or "frontend.unknown")[:160]
            fields = {key: value for key, value in event.items() if key != "event"}
            self.record(name, source="frontend", **fields)
            with self.lock:
                self.frontend_event_counts[name] += 1
                duration_value = event.get("duration_ms")
                if isinstance(duration_value, (int, float)) and math.isfinite(float(duration_value)):
                    aggregate = self.frontend_duration_aggregates.setdefault(
                        name,
                        {"count": 0, "total_ms": 0.0, "max_ms": 0.0},
                    )
                    aggregate["count"] = int(aggregate["count"]) + 1
                    aggregate["total_ms"] = float(aggregate["total_ms"]) + float(duration_value)
                    aggregate["max_ms"] = max(float(aggregate["max_ms"]), float(duration_value))
            accepted += 1
        return accepted

    def finish(self, *, reason: str) -> None:
        with self.lock:
            if self.finished:
                return
            self.finished = True
        self.record("diagnostics.session.finished", reason=reason)
        summary = self._summary(reason=reason)
        temp_path = self.summary_path.with_name(self.summary_path.name + ".tmp")
        temp_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.summary_path)

    def _summary(self, *, reason: str) -> dict[str, Any]:
        with self.lock:
            requests = {}
            for key, aggregate in self.request_aggregates.items():
                count = int(aggregate["count"])
                requests[key] = {
                    "count": count,
                    "total_ms": round(aggregate["total_ms"], 3),
                    "average_ms": round(aggregate["total_ms"] / count, 3) if count else 0.0,
                    "max_ms": round(aggregate["max_ms"], 3),
                    "response_bytes": int(aggregate["response_bytes"]),
                    "sql_count": int(aggregate["sql_count"]),
                    "sql_total_ms": round(aggregate["sql_total_ms"], 3),
                    "statuses": dict(aggregate["statuses"]),
                    "max_process_rss_bytes": int(aggregate["max_process_rss_bytes"]),
                }

            sql = {}
            for fingerprint, aggregate in self.sql_aggregates.items():
                count = int(aggregate["count"])
                sql[fingerprint] = {
                    "sql": aggregate["sql"],
                    "count": count,
                    "total_ms": round(aggregate["total_ms"], 3),
                    "average_ms": round(aggregate["total_ms"] / count, 3) if count else 0.0,
                    "max_ms": round(aggregate["max_ms"], 3),
                    "operations": dict(aggregate["operations"]),
                }

            event_counts = dict(self.event_counts)
            frontend_counts = dict(self.frontend_event_counts)
            frontend_durations = {
                name: {
                    "count": int(value["count"]),
                    "total_ms": round(float(value["total_ms"]), 3),
                    "average_ms": round(float(value["total_ms"]) / int(value["count"]), 3)
                    if int(value["count"])
                    else 0.0,
                    "max_ms": round(float(value["max_ms"]), 3),
                }
                for name, value in self.frontend_duration_aggregates.items()
            }
            query_plans = dict(self.query_plans)

        slow_requests = sorted(requests.items(), key=lambda item: item[1]["max_ms"], reverse=True)[:20]
        slow_sql = sorted(sql.items(), key=lambda item: item[1]["total_ms"], reverse=True)[:30]
        return {
            "schema_version": 1,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.perf_counter() - self.started_perf) * 1000.0, 3),
            "finish_reason": reason,
            "command": self.command,
            "config_path": str(self.config_path),
            "output_root": str(self.output_root),
            "query_plans_enabled": self.query_plans_enabled,
            "process_rss_bytes_at_start": self.process_rss_bytes_at_start,
            "event_counts": event_counts,
            "frontend_event_counts": frontend_counts,
            "frontend_durations": frontend_durations,
            "http_requests": requests,
            "slowest_http_requests": [{"request": key, **value} for key, value in slow_requests],
            "sql": sql,
            "sql_by_total_time": [{"fingerprint": key, **value} for key, value in slow_sql],
            "query_plans": query_plans,
            "process_rss_bytes_at_finish": process_rss_bytes(),
            "events_file": str(self.log_path),
        }


def process_rss_bytes() -> int | None:
    """Return current process resident memory when the platform exposes it."""
    if os.name == "nt":
        try:
            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                process,
                ctypes.byref(counters),
                counters.cb,
            )
            return int(counters.WorkingSetSize) if ok else None
        except Exception:
            return None

    try:
        statm = Path("/proc/self/statm")
        if statm.exists():
            resident_pages = int(statm.read_text(encoding="ascii").split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
    except Exception:
        pass

    try:
        import resource

        maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return maximum if sys.platform == "darwin" else maximum * 1024
    except Exception:
        return None


def _safe_query(query: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, values in query.items():
        safe_values = []
        for value in values:
            text = str(value)
            safe_values.append(text[:300])
        result[str(key)[:100]] = safe_values
    return result


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def diagnostic_http_handler(method: str):
    """Wrap one BaseHTTPRequestHandler method without changing normal request behavior."""
    def decorator(function):
        def wrapped(handler, *args, **kwargs):
            handler._diagnostic_status_code = 500
            handler._diagnostic_response_bytes = 0
            trace = begin_http_request(method, handler.path)
            try:
                return function(handler, *args, **kwargs)
            finally:
                finish_http_request(
                    trace,
                    status_code=int(getattr(handler, "_diagnostic_status_code", 500)),
                    response_bytes=int(getattr(handler, "_diagnostic_response_bytes", 0)),
                )
        return wrapped
    return decorator
