from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Config


INSTANCE_ENDPOINT_PATH = "/api/instance"
INSTANCE_PROTOCOL_VERSION = 1
RUNTIME_INFO_FILENAME = "server_runtime.json"
INSTANCE_LOCK_FILENAME = "server.lock"


class InstanceRuntimeError(RuntimeError):
    """Raised when one catalog instance cannot coordinate its server process."""


@dataclass(frozen=True)
class InstanceRuntimeInfo:
    instance_id: str
    pid: int
    host: str
    port: int
    url: str
    started_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": INSTANCE_PROTOCOL_VERSION,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "url": self.url,
            "started_at": self.started_at,
        }


@dataclass(frozen=True)
class InstanceProbe:
    reachable: bool
    is_catalog2: bool
    instance_id: str | None
    port: int


class InstanceProcessLock:
    """Cross-platform process lock held for the complete server lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None
        self._locked = False

    def acquire(self) -> bool:
        if self._handle is not None:
            raise InstanceRuntimeError(f"Instance lock is already open: {self.path}")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    handle.close()
                    return False
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    handle.close()
                    return False
        except Exception:
            handle.close()
            raise

        self._handle = handle
        self._locked = True
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return

        try:
            if self._locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            self._handle = None
            handle.close()


def instance_id_for_config(config_path: Path) -> str:
    """Return a stable runtime identity for one installed config path."""
    resolved = config_path.expanduser().resolve()
    normalized = os.path.normcase(os.path.normpath(str(resolved)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def instance_lock_path(config: Config) -> Path:
    return config.state_dir / INSTANCE_LOCK_FILENAME


def runtime_info_path(config: Config) -> Path:
    return config.state_dir / RUNTIME_INFO_FILENAME


def build_runtime_info(*, instance_id: str, host: str, port: int) -> InstanceRuntimeInfo:
    return InstanceRuntimeInfo(
        instance_id=instance_id,
        pid=os.getpid(),
        host=host,
        port=port,
        url=f"http://{host}:{port}/",
        started_at=time.time(),
    )


def write_runtime_info(path: Path, info: InstanceRuntimeInfo) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    content = json.dumps(info.to_dict(), ensure_ascii=False, indent=2) + "\n"
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def read_runtime_info(path: Path) -> InstanceRuntimeInfo | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    if not isinstance(raw, dict) or raw.get("protocol_version") != INSTANCE_PROTOCOL_VERSION:
        return None

    try:
        instance_id = str(raw["instance_id"])
        pid = int(raw["pid"])
        host = str(raw["host"])
        port = int(raw["port"])
        url = str(raw["url"])
        started_at = float(raw["started_at"])
    except (KeyError, TypeError, ValueError):
        return None

    if not instance_id or not host or not (1 <= port <= 65535):
        return None
    if url != f"http://{host}:{port}/":
        return None

    return InstanceRuntimeInfo(
        instance_id=instance_id,
        pid=pid,
        host=host,
        port=port,
        url=url,
        started_at=started_at,
    )


def remove_runtime_info(path: Path, *, expected: InstanceRuntimeInfo) -> None:
    current = read_runtime_info(path)
    if current is None:
        return
    if current.instance_id != expected.instance_id or current.pid != expected.pid:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def probe_catalog_instance(
    *,
    host: str,
    port: int,
    timeout_seconds: float = 1.0,
) -> InstanceProbe:
    url = f"http://{host}:{port}{INSTANCE_ENDPOINT_PATH}"
    request = Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if int(response.status) < 200 or int(response.status) >= 300:
                return InstanceProbe(True, False, None, port)
            raw = json.loads(response.read().decode("utf-8"))
    except HTTPError:
        return InstanceProbe(True, False, None, port)
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return InstanceProbe(False, False, None, port)

    if not isinstance(raw, dict):
        return InstanceProbe(True, False, None, port)
    if raw.get("application") != "Catalog 2.0":
        return InstanceProbe(True, False, None, port)
    if raw.get("protocol_version") != INSTANCE_PROTOCOL_VERSION:
        return InstanceProbe(True, False, None, port)

    instance_id = raw.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        return InstanceProbe(True, False, None, port)

    return InstanceProbe(True, True, instance_id, port)
