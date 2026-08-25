from __future__ import annotations

import os
import threading
import time
import webbrowser
from dataclasses import dataclass

from .config import Config
from .diagnostics import (
    diagnostic_browser_url,
    diagnostic_record,
    finish_diagnostics_session,
    get_diagnostics_session,
)
from .instance_runtime import (
    InstanceProcessLock,
    InstanceRuntimeInfo,
    build_runtime_info,
    instance_id_for_config,
    instance_lock_path,
    probe_catalog_instance,
    read_runtime_info,
    remove_runtime_info,
    runtime_info_path,
    write_runtime_info,
)
from .server import create_http_server, print_server_banner


DEFAULT_PORT_ATTEMPTS = 100
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0


class LauncherError(RuntimeError):
    """Raised when the catalog launcher cannot start or locate its instance."""


@dataclass(frozen=True)
class LaunchResult:
    action: str
    url: str
    port: int


def launch_catalog(
    config: Config,
    *,
    host: str = "127.0.0.1",
    port_attempts: int = DEFAULT_PORT_ATTEMPTS,
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> LaunchResult:
    """Open the verified running instance or start it on an exclusively bound port."""
    diagnostic_record("backend.launch.start", host=host, preferred_port=config.server_port)
    if port_attempts < 1:
        raise LauncherError("Port attempt count must be at least 1.")

    instance_id = instance_id_for_config(config.config_path)
    lock = InstanceProcessLock(instance_lock_path(config))

    lock_started = time.perf_counter()
    lock_acquired = lock.acquire()
    diagnostic_record(
        "backend.launch.instance_lock",
        acquired=lock_acquired,
        duration_ms=round((time.perf_counter() - lock_started) * 1000.0, 3),
    )

    if not lock_acquired:
        info = _wait_for_existing_instance_or_lock_release(
            config=config,
            lock=lock,
            expected_instance_id=instance_id,
            timeout_seconds=startup_timeout_seconds,
        )
        if info is not None:
            if get_diagnostics_session() is not None:
                raise LauncherError(
                    "Diagnostic launch requires the catalog instance to be fully stopped first. "
                    "Close the running instance and start the diagnostic run again."
                )
            _report_browser_result(info.url)
            print("Catalog 2.0 – existing instance opened")
            print("=" * 70)
            print(f"UI:       {info.url}")
            print(f"instance: {info.instance_id[:16]}")
            return LaunchResult(action="opened_existing", url=info.url, port=info.port)
        # The previous starter released the lock before exposing a verified
        # server. This process acquired the same lock and continues startup.

    runtime_path = runtime_info_path(config)
    runtime_info: InstanceRuntimeInfo | None = None
    server = None
    server_thread: threading.Thread | None = None

    finish_reason = "launcher_finished"
    try:
        bind_started = time.perf_counter()
        server, selected_port = _bind_available_server(
            config=config,
            host=host,
            instance_id=instance_id,
            preferred_port=config.server_port,
            port_attempts=port_attempts,
        )
        diagnostic_record(
            "backend.launch.server_bound",
            selected_port=selected_port,
            duration_ms=round((time.perf_counter() - bind_started) * 1000.0, 3),
        )
        runtime_info = build_runtime_info(
            instance_id=instance_id,
            host=host,
            port=selected_port,
        )
        runtime_write_started = time.perf_counter()
        write_runtime_info(runtime_path, runtime_info)
        diagnostic_record(
            "backend.launch.runtime_info_written",
            duration_ms=round((time.perf_counter() - runtime_write_started) * 1000.0, 3),
            runtime_path=str(runtime_path),
        )

        print_server_banner(
            server,
            host=host,
            port=selected_port,
            launch_mode=True,
        )
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="catalog2-http-server",
            daemon=False,
        )
        server_thread.start()
        diagnostic_record("backend.launch.server_thread_started", thread_name=server_thread.name)

        verify_started = time.perf_counter()
        _wait_for_started_instance(
            info=runtime_info,
            timeout_seconds=startup_timeout_seconds,
        )
        diagnostic_record(
            "backend.launch.instance_verified",
            duration_ms=round((time.perf_counter() - verify_started) * 1000.0, 3),
            url=runtime_info.url,
        )
        browser_url = diagnostic_browser_url(runtime_info.url)
        diagnostic_record("backend.launch.browser_open_requested", url=browser_url)
        _report_browser_result(browser_url)

        try:
            while server_thread.is_alive():
                server_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            finish_reason = "stopped_by_user"
            print("\nServer stopped by user.")
            server.shutdown()
            server_thread.join(timeout=10.0)

        return LaunchResult(action="server_stopped", url=runtime_info.url, port=selected_port)
    except Exception:
        finish_reason = "launcher_error"
        raise
    finally:
        if server is not None:
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
                server_thread.join(timeout=10.0)
            server.server_close()
        if runtime_info is not None:
            remove_runtime_info(runtime_path, expected=runtime_info)
        lock.release()
        diagnostic_record("backend.launch.cleanup_complete", reason=finish_reason)
        finish_diagnostics_session(finish_reason)


def _bind_available_server(
    *,
    config: Config,
    host: str,
    instance_id: str,
    preferred_port: int,
    port_attempts: int,
):
    final_port = min(65535, preferred_port + port_attempts - 1)

    for port in range(preferred_port, final_port + 1):
        try:
            server = create_http_server(
                config,
                host=host,
                port=port,
                instance_id=instance_id,
            )
        except OSError:
            probe = probe_catalog_instance(host=host, port=port, timeout_seconds=0.25)
            if probe.is_catalog2:
                print(f"Port {port} is used by another Catalog 2.0 instance; trying {port + 1}.")
            elif probe.reachable:
                print(f"Port {port} is used by another local service; trying {port + 1}.")
            else:
                print(f"Port {port} cannot be bound exclusively; trying {port + 1}.")
            continue
        return server, port

    attempted_range = (
        str(preferred_port)
        if preferred_port == final_port
        else f"{preferred_port}-{final_port}"
    )
    raise LauncherError(
        f"No exclusively available local port was found in range {attempted_range}. "
        "Close an unused local server or change server_port in the instance config."
    )


def _wait_for_existing_instance_or_lock_release(
    *,
    config: Config,
    lock: InstanceProcessLock,
    expected_instance_id: str,
    timeout_seconds: float,
) -> InstanceRuntimeInfo | None:
    deadline = time.monotonic() + timeout_seconds
    runtime_path = runtime_info_path(config)

    while time.monotonic() < deadline:
        info = read_runtime_info(runtime_path)
        if info is not None and info.instance_id == expected_instance_id:
            probe = probe_catalog_instance(
                host=info.host,
                port=info.port,
                timeout_seconds=1.0,
            )
            if probe.is_catalog2 and probe.instance_id == expected_instance_id:
                return info

        if lock.acquire():
            return None
        time.sleep(0.25)

    raise LauncherError(
        "This catalog instance is locked by another process, but its verified local server "
        "did not become available before the startup timeout. Close stale Catalog 2.0 "
        "processes and try again."
    )


def _wait_for_started_instance(
    *,
    info: InstanceRuntimeInfo,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        probe = probe_catalog_instance(
            host=info.host,
            port=info.port,
            timeout_seconds=1.0,
        )
        if probe.is_catalog2 and probe.instance_id == info.instance_id:
            return
        time.sleep(0.1)
    raise LauncherError(
        f"The server bound port {info.port}, but its instance endpoint was not verified "
        f"within {timeout_seconds:g} seconds."
    )


def _report_browser_result(url: str) -> None:
    if os.environ.get("CATALOG2_NO_BROWSER", "").strip().lower() in {"1", "true", "yes"}:
        print(f"Browser opening disabled for this run. Open manually: {url}")
        return

    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
            print(f"Browser opened: {url}")
            return
        opened = webbrowser.open(url, new=2)
    except Exception as exc:
        print(f"Browser could not be opened automatically: {exc}")
        print(f"Open manually: {url}")
        return

    if opened:
        print(f"Browser opened: {url}")
    else:
        print("Browser could not be opened automatically.")
        print(f"Open manually: {url}")
