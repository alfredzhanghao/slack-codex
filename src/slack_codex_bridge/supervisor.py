"""Manually launched Windows bridge supervisor. No login hooks or task replay."""

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import windows
from .artifacts import handle_path
from .config import load
from .model import BridgeError


def fixed_code(error):
    code = getattr(error, "code", "supervisor_operation_failed")
    return code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,100}", code) else "supervisor_operation_failed"


def instance_description(candidate):
    desktop = candidate["desktop"]
    if candidate.get("transport") == "desktop_ipc" and candidate["connectable"]:
        detail = "Existing desktop IPC — loaded chats; native processes stay running"
    elif candidate["connectable"]:
        detail = f"backend {candidate['backend']['pid']}, port {candidate['port']} — ready to confirm"
    else:
        detail = {
            "no_shared_endpoint": "No verified shared endpoint; this desktop will stay running",
            "instance_executable_not_configured": "Executable differs from the configured desktop",
            "version_mismatch_writes_disabled": "Version is incompatible with this bridge profile",
            "binary_hash_changed": "Binary changed; verify the deployment before connecting",
            "instance_previous_start_unconfirmed": "An earlier native start is unconfirmed; inspect it first",
            "instance_different_user_or_session": "Instance belongs to a different Windows user or login session",
        }.get(candidate["reason"], "Identity or connection unavailable; refresh or verify the profile")
    return f"Desktop {desktop['pid']} | {detail}"


def physical_file(value):
    path = Path(value)
    if not path.is_absolute():
        raise BridgeError("supervisor_requires_canonical_absolute_paths")
    try:
        with path.open("rb") as stream:
            if handle_path(stream) != path.resolve():
                raise BridgeError("supervisor_requires_canonical_absolute_paths")
        return path.resolve()
    except OSError:
        raise BridgeError("supervisor_deployment_path_unavailable") from None


class OperatorAdapter:
    def __init__(self, deployment, profile, operator):
        self.deployment, self.operator = physical_file(deployment), physical_file(operator)
        self.profile = profile
        self.manifest = json.loads(self.deployment.read_text(encoding="utf-8"))
        self.value = self.manifest["profiles"][profile]
        if hashlib.sha256(self.operator.read_bytes()).hexdigest() != self.manifest["operator_sha256"]:
            raise BridgeError("operator_source_changed")
        # Validate paths in the outside-container process, including the actual module bytes.
        for key in ("python", "expected_worker_exe", "config"):
            physical_file(self.value[key])
        for name in self.value["module_sha256"]:
            physical_file(Path(self.value["runtime"]) / "slack_codex_bridge" / name)
        self.cfg = load(Path(self.value["config"]))
        physical_file(self.cfg.backend_exe)
        physical_file(self.cfg.desktop_exe)
        self.record = None

    def worker(self):
        result = self.command("status")
        if result.get("running"):
            if not result.get("process") or result.get("version") != self.value["version"] or (
                    Path(result["process"]["path"]).resolve() != Path(self.value["expected_worker_exe"]).resolve()):
                raise BridgeError("supervisor_worker_identity_unverified")
            expected = {k: self.record[k] for k in ("endpoint", "desktop", "backend", "peer")} if self.record else None
            if not expected or result.get("native_chain") != expected:
                raise BridgeError("supervisor_worker_backend_unverified")
        return result

    def command(self, action):
        try:
            result = subprocess.run([self.value["python"], str(self.operator), "--deployment", str(self.deployment),
                "--profile", self.profile, action], capture_output=True, text=True, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            value = json.loads(result.stdout)
            if result.returncode:
                code = value.get("code", "supervisor_operator_failed")
                raise BridgeError(code if re.fullmatch(r"[a-z0-9_]{1,100}", code) else "supervisor_operator_failed")
            return value
        except (OSError, ValueError, subprocess.SubprocessError):
            raise BridgeError("supervisor_operator_unconfirmed_inspect_before_retry") from None

    def native(self):
        physical_file(windows.record_path(self.cfg))
        self.record = windows.verify_record(self.cfg, check_binaries=False)
        return True

    def restore(self):
        try:
            return windows.verify_record(self.cfg)
        except BridgeError as error:
            if error.code == "desktop_process_identity_changed":
                return windows.restore_desktop(self.cfg)
            if error.code in {"controlled_session_record_unavailable", "backend_process_identity_changed",
                              "controlled_session_not_verified"}:
                return windows.start(self.cfg)
            raise

    def discover(self):
        return windows.discover_instances(self.cfg)

    def attach(self, candidate):
        record = windows.validate_instance(self.cfg, candidate)
        worker = self.command("status")
        selected = {k: candidate[k] for k in ("desktop", "backend", "peer")}
        selected["endpoint"] = record["endpoint"]
        if worker.get("running"):
            self.native()
            if worker.get("native_chain") != selected:
                raise BridgeError("supervisor_stop_bridge_before_switch")
            return self.record
        # A TCP peer alone does not prove that this listener speaks native RPC.
        # Probe only initialize; do not persist a new identity on protocol failure.
        asyncio.run(self.probe(record["endpoint"]))
        self.record = windows.adopt_instance(self.cfg, candidate)
        return self.record

    async def probe(self, endpoint):
        from .desktop import Desktop
        rpc = Desktop(self.cfg, endpoint)
        try:
            await rpc.connect()
        finally:
            await rpc.close()

    def start(self):
        self.command("start")
        self.native()
        return self.worker()

    def stop(self, run_id):
        worker = self.command("status")
        if worker.get("running") and worker.get("run_id") != run_id:
            raise BridgeError("supervisor_worker_ownership_changed")
        return self.command("stop")


class Supervisor:
    """Pure lifecycle policy; adapter effects are serialized by the UI worker."""
    def __init__(self, adapter, clock=time.monotonic):
        self.adapter, self.clock = adapter, clock
        self.enabled = False
        self.managed_run = None
        self.retries = 0
        self.next_retry = 0
        self.healthy_since = None
        self.phase = "stopped"
        self.code = None
        self.last_action_code = None
        self.last_worker = {}
        self.native_verified = False
        self.candidates = []
        self.selection_pending = False

    def inspect(self):
        self.native_verified = bool(self.adapter.native())
        self.last_worker = self.adapter.worker()
        return self.last_worker

    def start(self):
        self.code = None
        # Detect first. Selecting an existing desktop never invokes native restore.
        if hasattr(self.adapter, "discover"):
            self.scan_instances()
            if self.candidates:
                return
        # Start and Restore are explicit; monitoring never reopens the desktop.
        self.adapter.restore()
        self._start_verified()

    def scan_instances(self):
        self.candidates = []
        self.selection_pending = False
        self.candidates = self.adapter.discover()
        self.selection_pending = bool(self.candidates)
        if not self.enabled and not self.managed_run:
            self.phase = "choose an existing instance" if self.candidates else "no running desktop detected"
            self.code = None

    def connect_instance(self, candidate_id):
        candidate = next((c for c in self.candidates if c["id"] == candidate_id), None)
        if not candidate:
            raise BridgeError("instance_selection_expired_refresh")
        if not candidate["connectable"]:
            raise BridgeError("instance_not_connectable")
        # Consume before effects. An uncertain operator result cannot reuse a click.
        self.candidates = []
        self.selection_pending = False
        self.adapter.attach(candidate)
        self._start_verified()

    def _start_verified(self):
        worker = self.inspect()
        if worker.get("running"):
            if not worker.get("fresh") or not worker.get("run_id"):
                raise BridgeError("supervisor_existing_worker_unconfirmed")
            # Explicit Start/Restore may manage the verified pinned worker; polling never adopts it.
            self.managed_run = worker["run_id"]
            self.enabled = True
            self.retries = 0
            self.next_retry = 0
            self.healthy_since = self.clock()
            self.phase = "running" if worker.get("ready") else "connecting"
            return
        self.last_worker = self.adapter.start()
        self.managed_run = self.last_worker.get("run_id")
        if not self.managed_run or not self.last_worker.get("running"):
            raise BridgeError("supervisor_start_unconfirmed")
        self.enabled = True
        self.retries = 0
        self.next_retry = 0
        self.healthy_since = self.clock()
        self.phase = "running"

    def stop(self):
        self.enabled = False
        self.next_retry = 0
        self.healthy_since = None
        if self.managed_run:
            self.adapter.stop(self.managed_run)
            self.managed_run = None
        self.phase = "stopped"

    def poll(self):
        if self.selection_pending and not self.enabled and not self.managed_run:
            return
        try:
            worker = self.inspect()
        except BridgeError as error:
            self.native_verified = False
            self.code = fixed_code(error)
            self.enabled = False
            # Graceful operator stop only, never kill the desktop/backend or tasks.
            if self.managed_run:
                self.adapter.stop(self.managed_run)
                self.managed_run = None
            self.phase = "suspended"
            return
        if not self.enabled:
            self.phase = "external worker (observe only)" if worker.get("running") else self.phase
            return
        if worker.get("running"):
            if worker.get("run_id") != self.managed_run:
                self.enabled = False
                self.phase, self.code = "suspended", "supervisor_worker_ownership_changed"
                return
            if not worker.get("fresh"):
                self.phase, self.code = "attention required", "supervisor_status_stale_no_duplicate_start"
                return
            self.phase = "running" if worker.get("ready") else "connecting"
            self.code = worker.get("fault")
            if worker.get("ready") and worker.get("healthy"):
                if self.healthy_since is None:
                    self.healthy_since = self.clock()
                if self.clock() - self.healthy_since >= 60:
                    self.retries = 0
            else:
                self.healthy_since = None
            return
        self.healthy_since = None
        if worker.get("phase") == "stopped":
            self.enabled = False
            self.phase = "stopped"
            return
        fault = worker.get("fault")
        if fault:
            from .runtime import recovery
            if not recovery(BridgeError(fault)).retry:
                self.enabled = False
                self.phase, self.code = "attention required", fault
                return
        if self.retries >= 3:
            self.enabled = False
            self.phase, self.code = "attention required", "supervisor_retry_budget_exhausted"
            return
        self.phase = "recovery backoff"
        if not self.next_retry:
            self.next_retry = self.clock() + 2 ** (self.retries + 1)
            return
        if self.clock() < self.next_retry:
            return
        self.adapter.native()
        self.retries += 1
        self.next_retry = 0
        self.last_worker = self.adapter.start()
        self.managed_run = self.last_worker.get("run_id")
        if not self.managed_run or not self.last_worker.get("running"):
            self.enabled = False
            raise BridgeError("supervisor_start_unconfirmed")
        self.phase = "running"

    def action(self, name, candidate_id=None):
        self.last_action_code = None
        previous = self.enabled, self.phase, self.code, self.managed_run
        try:
            {"Start": self.start, "Restore connection": self.start, "Stop": self.stop, "Exit": self.stop,
             "Scan instances": self.scan_instances,
             "Connect selected": lambda: self.connect_instance(candidate_id)}[name]()
        except Exception as error:
            self.last_action_code = fixed_code(error)
            if name in {"Scan instances", "Connect selected"} and previous[0] and self.managed_run == previous[3]:
                self.enabled, self.phase, self.code = previous[:3]
            else:
                self.enabled = False
                self.phase, self.code = "attention required", self.last_action_code

    def status_text(self):
        worker = self.last_worker
        return (f"State: {self.phase}\nDesktop/backend identity: {'verified' if self.native_verified else 'unverified'}\n"
                f"Bridge version: {worker.get('version', 'unknown')}\n"
                f"Bridge process: {'running' if worker.get('running') else 'stopped'}\n"
                f"Backend: {'connected' if worker.get('backend_connected') else 'disconnected'}\n"
                f"Slack: {'ready' if worker.get('ready') and worker.get('fresh') else 'not ready'}\n"
                f"Recovery attempts: {self.retries}/3\nRecent code: {self.code or worker.get('fault') or 'none'}\n"
                f"Last action error: {self.last_action_code or 'none'}\n\n"
                "Choose and confirm an existing compatible instance to connect without restarting it.\n"
                "Private or unsupported transports remain running; detection does not grant access.\n"
                "Stop / Exit affects only this supervisor's bridge. Native tasks continue.\n"
                "No login registration. No task, approval or uncertain upload is replayed.")


def show_window(supervisor):
    """Win32 status window and tray icon, independent of Tcl/Tk installation."""
    from ctypes import wintypes as wt
    user = ctypes.WinDLL("user32", use_last_error=True)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    class WindowClass(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("proc", callback_type), ("class_extra", ctypes.c_int),
                    ("window_extra", ctypes.c_int), ("instance", wt.HINSTANCE), ("icon", wt.HICON),
                    ("cursor", wt.HANDLE), ("background", wt.HBRUSH), ("menu", wt.LPCWSTR), ("name", wt.LPCWSTR)]
    class Icon(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                    ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                    ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                    ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                    ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]
    kernel.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    kernel.GetModuleHandleW.restype = wt.HMODULE
    user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
    user.RegisterClassW.restype = wt.WORD
    user.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
    user.CreateWindowExW.restype = wt.HWND
    user.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user.DefWindowProcW.restype = ctypes.c_ssize_t
    user.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user.SendMessageW.restype = ctypes.c_ssize_t
    user.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]
    user.EnableWindow.argtypes = [wt.HWND, wt.BOOL]
    user.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wt.HWND]
    user.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
    user.MessageBoxW.restype = ctypes.c_int
    user.DestroyWindow.argtypes = [wt.HWND]
    user.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
    user.SetTimer.restype = ctypes.c_size_t
    user.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
    user.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user.IsDialogMessageW.argtypes = [wt.HWND, ctypes.POINTER(wt.MSG)]
    user.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user.DispatchMessageW.restype = ctypes.c_ssize_t
    user.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
    user.LoadIconW.restype = wt.HICON
    user.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
    user.LoadCursorW.restype = wt.HANDLE
    shell.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(Icon)]
    instance = kernel.GetModuleHandleW(None)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bridge-supervisor")
    actions = {100: "Start", 101: "Stop", 102: "Restore connection", 103: "Exit",
               104: "Scan instances", 105: "Connect selected"}
    controls, pending, queued = [], None, "Scan instances"
    displayed = []
    rendered_ids = None
    confirming = False
    last_poll, exiting, tray_added = 0, False, False
    label, selection, details, hwnd, icon_data = None, None, None, None, Icon()

    def render_instances():
        nonlocal displayed, rendered_ids
        ids = [c["id"] for c in supervisor.candidates]
        if rendered_ids == ids:
            return
        rendered_ids = ids
        displayed = list(supervisor.candidates)
        user.SendMessageW(selection, 0x014B, 0, 0)  # CB_RESETCONTENT
        for candidate in displayed:
            text = ctypes.c_wchar_p(instance_description(candidate))
            user.SendMessageW(selection, 0x0143, 0, ctypes.cast(text, ctypes.c_void_p).value)  # CB_ADDSTRING
        user.SendMessageW(selection, 0x014E, -1, 0)  # No implicit selection.
        user.SetWindowTextW(details, "Select an instance, then Connect selected. Refresh after two minutes." if displayed
                            else "Refresh instances to choose a connection, or use Start if no desktop is open.")

    def message_box(text, title, flags):
        nonlocal confirming
        confirming = True
        try:
            return user.MessageBoxW(hwnd, text, title, flags)
        finally:
            confirming = False

    def begin(name):
        nonlocal pending, exiting
        candidate_id = None
        if name == "Connect selected":
            index = user.SendMessageW(selection, 0x0147, 0, 0)  # CB_GETCURSEL
            if not 0 <= index < len(displayed):
                message_box("Select a running instance first.", "Choose an instance", 0x40)
                return
            candidate = displayed[index]
            if not candidate["connectable"]:
                message_box(instance_description(candidate) +
                            "\nNo process will be restarted or terminated.", "Cannot attach this instance", 0x40)
                return
            prompt = (instance_description(candidate) + f"\n{candidate['desktop'].get('path', '')}"
                      f"\nVerified desktop version: {candidate['desktop_version']}"
                      "\n\nConnect the bridge to this existing instance? Native processes stay running.")
            if candidate.get("transport") == "desktop_ipc":
                prompt += ("\n\nIPC mode supports loaded chats, tasks, exact-turn Stop and files."
                           "\nUse the desktop for Steer, approvals, model/Skills settings, Compact and Fork.")
            if message_box(prompt, "Confirm existing Codex instance", 0x121) != 1:
                return  # MB_OKCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2
            candidate_id = candidate["id"]
        exiting = name == "Exit"
        pending = executor.submit(supervisor.action, name, candidate_id)
        for button in controls:
            user.EnableWindow(button, False)

    def tick():
        nonlocal pending, queued, last_poll, exiting
        if confirming:
            return  # MessageBox pumps WM_TIMER; do not replace a pending UI future.
        if pending is not None and pending.done():
            try:
                pending.result()
            except Exception as error:
                supervisor.enabled = False
                supervisor.phase, supervisor.code = "attention required", fixed_code(error)
            pending = None
            render_instances()
            user.SetWindowTextW(label, supervisor.status_text())
            if exiting:
                if not supervisor.managed_run:
                    user.DestroyWindow(hwnd)
                    return
                user.SetWindowTextW(label, supervisor.status_text() + "\nExit blocked: worker stop is unconfirmed.")
                exiting = False
            for button in controls:
                user.EnableWindow(button, True)
        if pending is None and queued:
            name, queued = queued, None
            begin(name)
        elif pending is None and time.monotonic() - last_poll >= 2:
            last_poll = time.monotonic()
            pending = executor.submit(supervisor.poll)

    def callback(window, msg, wp, lp):
        nonlocal queued, tray_added
        if msg == 0x0111 and (wp & 0xffff) == 106 and (wp >> 16) == 1:
            index = user.SendMessageW(selection, 0x0147, 0, 0)
            if 0 <= index < len(displayed):
                candidate = displayed[index]
                user.SetWindowTextW(details, candidate["desktop"].get("path", "Identity unavailable") +
                                    "\n" + instance_description(candidate))
            return 0
        if msg == 0x0111 and (wp & 0xffff) in actions and (wp >> 16) == 0:
            queued = actions[wp & 0xffff]
            tick()
            return 0
        if msg == 0x0113 and label:
            tick()
            return 0
        if msg == 0x0010:
            user.ShowWindow(window, 0)
            return 0
        if msg == 0x8001 and lp in {0x0202, 0x0205}:
            user.ShowWindow(window, 9)
            user.SetForegroundWindow(window)
            return 0
        if msg == 0x0002:
            user.KillTimer(window, 1)
            if tray_added:
                shell.Shell_NotifyIconW(2, ctypes.byref(icon_data))
                tray_added = False
            user.PostQuitMessage(0)
            return 0
        return user.DefWindowProcW(window, msg, wp, lp)
    proc = callback_type(callback)
    cls = WindowClass(0, proc, 0, 0, instance, user.LoadIconW(None, ctypes.c_void_p(32512)),
                      user.LoadCursorW(None, ctypes.c_void_p(32512)), 6, None, "SlackCodexSupervisorStatus")
    try:
        if not user.RegisterClassW(ctypes.byref(cls)):
            raise BridgeError("supervisor_window_class_unavailable")
        hwnd = user.CreateWindowExW(0, cls.name, "Slack Codex Bridge Supervisor", 0x00CA0000,
                                   0x80000000 - 0x100000000, 0x80000000 - 0x100000000,
                                   800, 690, None, None, instance, None)
        if not hwnd:
            raise BridgeError("supervisor_window_unavailable")
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wt.DWORD] * 8 + [wt.LPCWSTR]
        gdi.CreateFontW.restype = wt.HANDLE
        font = gdi.CreateFontW(-16, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        def child(kind, title, x, y, width, height, identifier=0, style=0):
            control = user.CreateWindowExW(0, kind, title, 0x50000000 | style |
                                          (0x10000 if kind in {"BUTTON", "COMBOBOX"} else 0),
                                          x, y, width, height, hwnd, identifier, instance, None)
            if not control:
                raise BridgeError("supervisor_control_unavailable")
            user.SendMessageW(control, 0x0030, font, 1)
            return control
        child("STATIC", "Slack Codex Bridge — on-demand supervisor", 24, 18, 730, 24)
        label = child("STATIC", supervisor.status_text(), 24, 52, 740, 316)
        child("STATIC", "Running Codex instances — select and explicitly confirm", 24, 380, 730, 24)
        selection = child("COMBOBOX", "", 24, 408, 740, 200, 106, 0x00200003)
        controls.append(selection)
        details = child("STATIC", "Scanning running instances…", 24, 440, 740, 44)
        controls.append(child("BUTTON", "Refresh instances", 24, 494, 140, 32, 104))
        controls.append(child("BUTTON", "Connect selected", 176, 494, 160, 32, 105))
        for identifier, x, width in [(100, 24, 100), (101, 136, 100), (102, 248, 170), (103, 430, 100)]:
            controls.append(child("BUTTON", actions[identifier], x, 548, width, 32, identifier))
        child("STATIC", "Close hides this window. Use Exit to stop the managed bridge and monitoring.", 24, 602, 730, 24)
        icon_data.cbSize, icon_data.hWnd, icon_data.uID = ctypes.sizeof(Icon), hwnd, 1
        icon_data.uFlags, icon_data.uCallbackMessage, icon_data.hIcon = 7, 0x8001, cls.icon
        icon_data.szTip = "Slack Codex Bridge — click for status"
        if not shell.Shell_NotifyIconW(0, ctypes.byref(icon_data)):
            raise BridgeError("supervisor_tray_unavailable")
        tray_added = True
        user.ShowWindow(hwnd, 5)
        if not user.SetTimer(hwnd, 1, 200, None):
            raise BridgeError("supervisor_timer_unavailable")
        message = wt.MSG()
        while True:
            result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result <= 0:
                if result < 0:
                    raise BridgeError("supervisor_message_loop_failed")
                break
            if not user.IsDialogMessageW(hwnd, ctypes.byref(message)):
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        if tray_added:
            shell.Shell_NotifyIconW(2, ctypes.byref(icon_data))
        if hwnd:
            user.DestroyWindow(hwnd)
        if 'font' in locals() and font:
            gdi.DeleteObject.argtypes = [wt.HANDLE]
            gdi.DeleteObject(font)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--operator", type=Path, required=True)
    parser.add_argument("--profile", default="p5")
    args = parser.parse_args()
    windows.require_windows()
    adapter = OperatorAdapter(args.deployment, args.profile, args.operator)
    with windows.single_instance(adapter.cfg.host_id + "-supervisor"):
        show_window(Supervisor(adapter))


if __name__ == "__main__":
    main()
