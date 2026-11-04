"""Windows-only process identity, single-instance lock and detached launch primitives."""

import ctypes
import hashlib
import json
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from ctypes import wintypes as wt
from pathlib import Path

from .config import Config, child_environment
from .model import BridgeError


def require_windows():
    if os.name != "nt":
        raise BridgeError("windows_required")


def _kernel():
    require_windows()
    return ctypes.WinDLL("kernel32", use_last_error=True)


@contextmanager
def single_instance(name: str):
    kernel = _kernel()
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    kernel.CreateMutexW.restype = wt.HANDLE
    handle = kernel.CreateMutexW(None, False, "Local\\SlackCodexBridge-" + name)
    if not handle:
        raise BridgeError("single_instance_lock_failed")
    try:
        if ctypes.get_last_error() == 183:
            raise BridgeError("another_instance_is_running")
        yield
    finally:
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.CloseHandle(handle)


def process_identity(pid: int) -> dict | None:
    kernel = _kernel()
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, int(pid))
    if not handle:
        if ctypes.get_last_error() in {87, 1168}:
            return None
        raise BridgeError("process_identity_unavailable")
    try:
        # An exited process can remain openable while a parent retains a handle.
        # Check the same kernel object before reading its now-unavailable path.
        kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        kernel.WaitForSingleObject.restype = wt.DWORD
        result = kernel.WaitForSingleObject(handle, 0)
        if result == 0:
            return None
        if result != 258:
            raise BridgeError("process_liveness_unavailable")
        try:
            return _handle_identity(kernel, handle, pid)
        except BridgeError as error:
            # The process may exit after the first liveness check. Only the same
            # signaled kernel handle proves exit; a live unreadable process stays unknown.
            if (error.code in {"process_path_unavailable", "process_start_time_unavailable"}
                    and kernel.WaitForSingleObject(handle, 0) == 0):
                return None
            raise
    finally:
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.CloseHandle(handle)


def _handle_identity(kernel, handle, pid):
    buf = ctypes.create_unicode_buffer(32768)
    size = wt.DWORD(len(buf))
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        raise BridgeError("process_path_unavailable")
    times = [wt.FILETIME() for _ in range(4)]
    kernel.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise BridgeError("process_start_time_unavailable")
    created = times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)
    return {"pid": int(pid), "created": created, "path": str(Path(buf.value).resolve())}


def processes() -> list[dict]:
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("pid", wt.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wt.DWORD), ("threads", wt.DWORD),
                    ("parent", wt.DWORD), ("priority", wt.LONG), ("flags", wt.DWORD),
                    ("exe", wt.WCHAR * 260)]
    kernel = _kernel()
    kernel.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wt.HANDLE
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise BridgeError("process_inventory_unavailable")
    kernel.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(Entry)]
    entry = Entry()
    entry.dwSize = ctypes.sizeof(entry)
    rows = []
    try:
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            rows.append({"pid": entry.pid, "parent": entry.parent, "name": entry.exe})
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
        return rows
    finally:
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.CloseHandle(handle)


def file_version(path: Path) -> str:
    require_windows()
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
    ignored = wt.DWORD()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size:
        raise BridgeError("desktop_version_unavailable")
    buf = ctypes.create_string_buffer(size)
    version.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    if not version.GetFileVersionInfoW(str(path), 0, size, buf):
        raise BridgeError("desktop_version_unavailable")
    pointer, length = ctypes.c_void_p(), wt.UINT()
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                     ctypes.POINTER(wt.UINT)]
    if not version.VerQueryValueW(buf, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise BridgeError("desktop_version_unavailable")
    values = ctypes.cast(pointer, ctypes.POINTER(wt.DWORD))
    return ".".join(map(str, [values[2] >> 16, values[2] & 0xFFFF, values[3] >> 16, values[3] & 0xFFFF]))


def binary_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_versions(cfg: Config):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        value = subprocess.run([str(cfg.backend_exe), "--version"], capture_output=True, text=True,
                               timeout=10, check=True, creationflags=flags, env=child_environment(cfg))
        if value.stdout.strip() != cfg.backend_version or file_version(cfg.desktop_exe) != cfg.desktop_version:
            raise BridgeError("version_mismatch_writes_disabled")
    except (OSError, subprocess.SubprocessError):
        raise BridgeError("version_check_failed") from None


def detached(args: list[str], env: dict[str, str]) -> dict:
    require_windows()
    try:
        # A venv console launcher may spawn a fresh console in its runtime child.
        # Use its matching GUI-subsystem launcher for background Python helpers.
        executable = Path(args[0])
        if executable.name.casefold() == "python.exe":
            windowless = executable.with_name("pythonw.exe")
            if not windowless.is_file():
                raise BridgeError("windowless_python_required")
            args = [str(windowless), *args[1:]]
        # Breakaway failure is reported. Never silently inherit an agent's kill-on-close job.
        child = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True,
                                 creationflags=0x01000000 | subprocess.CREATE_NEW_PROCESS_GROUP |
                                 subprocess.DETACHED_PROCESS)
        identity = process_identity(child.pid)
        if not identity:
            raise BridgeError("detached_process_exited")
        return identity
    except OSError:
        raise BridgeError("detached_launch_failed_use_normal_terminal") from None


def tcp_rows():
    require_windows()
    result = subprocess.run([str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32/netstat.exe"),
                             "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    return [s for line in result.stdout.splitlines() if len(s := line.split()) >= 5 and s[0] == "TCP"]


def correlate(record: dict):
    port = record["port"]
    rows = tcp_rows()
    parents = {p["pid"]: p["parent"] for p in processes()}
    for server in rows:
        if (server[1] != f"127.0.0.1:{port}" or server[3] != "ESTABLISHED"
                or server[4] != str(record["backend"]["pid"])):
            continue
        client = next((r for r in rows if r[1] == server[2] and r[2] == server[1] and r[3] == "ESTABLISHED"), None)
        if not client:
            continue
        peer = int(client[4])
        ancestor = peer
        for _ in range(16):
            if ancestor == record["desktop"]["pid"]:
                identity = process_identity(peer)
                if identity:
                    return identity
            ancestor = parents.get(ancestor)
            if not ancestor:
                break
    raise BridgeError("desktop_peer_not_owned")


def record_path(cfg):
    return cfg.data_dir / "desktop-session.json"


def save_record(cfg, record):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    temp = record_path(cfg).with_suffix(".tmp")
    temp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    temp.replace(record_path(cfg))


def read_record(cfg):
    try:
        value = json.loads(record_path(cfg).read_text(encoding="utf-8"))
        if value["host_id"] != cfg.host_id or value["endpoint"] != f"ws://127.0.0.1:{value['port']}":
            raise BridgeError("session_identity_mismatch")
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise BridgeError("controlled_session_record_unavailable") from None


def verify_record(cfg: Config, *, require_desktop=True, check_binaries=True):
    record = read_record(cfg)
    if record.get("status") != "verified":
        raise BridgeError("controlled_session_not_verified")
    if (record.get("backend_version") != cfg.backend_version
            or record.get("desktop_version") != cfg.desktop_version):
        raise BridgeError("session_version_changed")
    if record["desktop"]["path"].lower() != str(cfg.desktop_exe.resolve()).lower():
        raise BridgeError("desktop_path_changed")
    if record["backend"]["path"].lower() != str(cfg.backend_exe.resolve()).lower():
        raise BridgeError("backend_path_changed")
    if process_identity(record["backend"]["pid"]) != record["backend"]:
        raise BridgeError("backend_process_identity_changed")
    if check_binaries:
        verify_versions(cfg)
        if binary_hash(cfg.backend_exe) != record["backend_hash"] or binary_hash(cfg.desktop_exe) != record["desktop_hash"]:
            raise BridgeError("binary_hash_changed")
    if require_desktop:
        desktop = process_identity(record["desktop"]["pid"])
        if desktop != record["desktop"] or correlate(record) != record["peer"]:
            raise BridgeError("desktop_process_identity_changed")
    return record


def start(cfg: Config):
    require_windows()
    with single_instance(cfg.host_id + "-launcher"):
        verify_versions(cfg)
        if any(p["name"].lower() == cfg.desktop_exe.name.lower() for p in processes()):
            raise BridgeError("desktop_already_open_close_it_manually")
        if record_path(cfg).exists():
            old = read_record(cfg)
            if old.get("backend") and process_identity(old["backend"]["pid"]) == old["backend"]:
                raise BridgeError("owned_backend_exists_use_status_or_stop")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        endpoint = f"ws://127.0.0.1:{port}"
        record = {"host_id": cfg.host_id, "status": "starting", "port": port, "endpoint": endpoint,
                  "backend_version": cfg.backend_version, "desktop_version": cfg.desktop_version,
                  "backend_hash": binary_hash(cfg.backend_exe), "desktop_hash": binary_hash(cfg.desktop_exe)}
        record["backend"] = detached([str(cfg.backend_exe), "app-server", "--listen", endpoint], child_environment(cfg))
        save_record(cfg, record)
        for _ in range(30):
            if any(r[1] == f"127.0.0.1:{port}" and r[3] == "LISTENING" and
                   r[4] == str(record["backend"]["pid"]) for r in tcp_rows()):
                break
            time.sleep(0.2)
        else:
            raise BridgeError("backend_not_listening_owned_process_retained")
        env = child_environment(cfg)
        env["CODEX_APP_SERVER_WS_URL"] = endpoint
        record["desktop"] = detached([str(cfg.desktop_exe)], env)
        save_record(cfg, record)
        for _ in range(45):
            time.sleep(1)
            try:
                record["peer"] = correlate(record)
                record["status"] = "verified"
                save_record(cfg, record)
                return record
            except BridgeError:
                continue
        raise BridgeError("desktop_not_correlated_owned_processes_retained")


def restore_desktop(cfg: Config):
    """Explicitly restore a closed desktop to its original verified live backend."""
    with single_instance(cfg.host_id + "-launcher"):
        record = verify_record(cfg, require_desktop=False)
        if any(p["name"].casefold() == cfg.desktop_exe.name.casefold() for p in processes()):
            # An existing unrelated desktop must never be silently attached/replaced.
            raise BridgeError("supervisor_unrelated_desktop_close_or_reconnect_manually")
        if not any(r[1] == f"127.0.0.1:{record['port']}" and r[3] == "LISTENING"
                   and r[4] == str(record["backend"]["pid"]) for r in tcp_rows()):
            raise BridgeError("supervisor_original_backend_not_listening")
        env = child_environment(cfg)
        env["CODEX_APP_SERVER_WS_URL"] = record["endpoint"]
        record["desktop"] = detached([str(cfg.desktop_exe)], env)
        # Retain the new identity even if correlation fails. Never launch a second desktop.
        record["status"] = "restoring"
        save_record(cfg, record)
        for _ in range(45):
            time.sleep(1)
            try:
                record["peer"] = correlate(record)
                record["status"] = "verified"
                save_record(cfg, record)
                return record
            except BridgeError:
                continue
        raise BridgeError("desktop_not_correlated_owned_processes_retained")


async def stop(cfg: Config):
    from .desktop import RPC

    with single_instance(cfg.host_id + "-launcher"), single_instance(cfg.host_id + "-run"):
        record = read_record(cfg)
        if record.get("desktop") and process_identity(record["desktop"]["pid"]) == record["desktop"]:
            raise BridgeError("desktop_still_open_stop_refused")
        if any(p["name"].lower() == cfg.desktop_exe.name.lower() for p in processes()):
            raise BridgeError("desktop_still_open_stop_refused")
        if process_identity(record["backend"]["pid"]) != record["backend"]:
            raise BridgeError("backend_process_identity_changed")
        if (record["backend"]["path"].lower() != str(cfg.backend_exe.resolve()).lower()
                or binary_hash(cfg.backend_exe) != record["backend_hash"]):
            raise BridgeError("backend_binary_changed")
        rpc = RPC(record["endpoint"], cfg.request_timeout, lambda *_: None)
        await rpc.connect()
        try:
            cursor = None
            while True:
                page = await rpc.call("thread/loaded/list", {"limit": 100, "cursor": cursor})
                for tid in page["data"]:
                    t = (await rpc.call("thread/read", {"threadId": tid, "includeTurns": False}))["thread"]
                    status = t.get("status", {})
                    if status.get("type") != "idle" or status.get("activeFlags"):
                        raise BridgeError("active_or_untrusted_thread_stop_refused")
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        finally:
            await rpc.close()
        if process_identity(record["backend"]["pid"]) != record["backend"]:
            raise BridgeError("backend_process_identity_changed")
        kernel = _kernel()
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.OpenProcess.restype = wt.HANDLE
        handle = kernel.OpenProcess(1 | 0x1000 | 0x100000, False, record["backend"]["pid"])
        if not handle:
            raise BridgeError("owned_backend_stop_failed")
        try:
            if _handle_identity(kernel, handle, record["backend"]["pid"]) != record["backend"]:
                raise BridgeError("backend_process_identity_changed")
            kernel.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
            if not kernel.TerminateProcess(handle, 0):
                raise BridgeError("owned_backend_stop_failed")
            # TerminateProcess initiates termination; it does not wait for exit.
            kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
            kernel.WaitForSingleObject.restype = wt.DWORD
            if kernel.WaitForSingleObject(handle, 10000) != 0:
                raise BridgeError("owned_backend_exit_unproven")
        finally:
            kernel.CloseHandle(handle)
        record["status"] = "stopped"
        save_record(cfg, record)
