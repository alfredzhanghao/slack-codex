"""Local operator for immutable bridge packages; credentials come from environment only.

No native desktop/backend lifecycle actions, test fault hooks or automatic restarts.
"""
import argparse
import asyncio
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from ctypes import wintypes as wt
from pathlib import Path


class OperatorError(Exception):
    pass


def now_ms():
    return int(time.time() * 1000)


def read(path):
    # A Windows atomic status replacement can briefly deny a concurrent reader.
    # Retry only opening this local file, never an operator/native/Slack effect.
    for attempt in range(9):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 8:
                raise OperatorError("operator_state_read_unavailable") from None
            time.sleep(0.025)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    # Windows readers can briefly hold a handle without FILE_SHARE_DELETE.
    # Retry only this local atomic rename; never retry any Slack/native effect.
    for attempt in range(9):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 8:
                raise
            time.sleep(0.025)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def profile(deployment, name):
    value = deployment["profiles"][name]
    if Path(value["expected_worker_exe"]).name.casefold() != "pythonw.exe":
        raise OperatorError("operator_worker_path_invalid")
    if sha(value["config"]) != value["config_sha256"]:
        raise OperatorError("operator_config_changed")
    runtime = Path(value["runtime"])
    actual = {p.name: sha(p) for p in (runtime / "slack_codex_bridge").glob("*.py")}
    if actual != value["module_sha256"]:
        raise OperatorError("operator_package_changed")
    return value


def runtime_for(value):
    sys.path.insert(0, value["runtime"])
    package = importlib.import_module("slack_codex_bridge")
    if package.__version__ != value["version"]:
        raise OperatorError("operator_version_mismatch")
    return importlib.import_module("slack_codex_bridge.windows")


def state_paths(value, run_id=None):
    root = Path(value["state_dir"])
    if value.get("legacy"):
        return root / "status.json", root / "command.json", None
    if run_id is None:
        pointer = root / "current.json"
        if not pointer.exists():
            return None, None, None
        run_id = read(pointer)["run_id"]
    if not re.fullmatch(r"[a-f0-9-]{36}", run_id):
        raise OperatorError("operator_run_id_invalid")
    directory = root / "runs" / run_id
    return directory / "status.json", directory / "command.json", directory


def status(value, windows):
    path, _, directory = state_paths(value)
    if path is None or not path.exists():
        if directory and (directory / "launch-intent.json").exists():
            launch = directory / "launch.json"
            identity = read(launch)["process"] if launch.exists() else None
            if identity and windows.process_identity(identity["pid"]) != identity:
                return {"running": False, "version": value["version"]}
            return {"running": True, "fresh": False, "version": value["version"],
                    "phase": "launch_unconfirmed"}
        return {"running": False, "version": value["version"]}
    result = read(path)
    identity = result.get("process")
    running = bool(identity and windows.process_identity(identity["pid"]) == identity)
    return {**result, "running": running,
            "fresh": 0 <= now_ms() - result.get("observed_ms", 0) < 10000}


def shutdown_command(run_id, clock=None):
    return {"run_id": run_id, "id": str(uuid.uuid4()), "action": "shutdown",
            "expires_ms": (now_ms() if clock is None else clock) + 60000}


def claim_shutdown(command, run_id, claimed, clock):
    if (command.get("run_id") != run_id or command.get("action") != "shutdown"
            or not isinstance(command.get("id"), str)
            or not re.fullmatch(r"[a-f0-9-]{36}", command["id"])
            or command["id"] in claimed
            or not clock < command.get("expires_ms", 0) <= clock + 120000):
        return False
    claimed.add(command["id"])
    return True


@contextmanager
def process_handle(windows, expected):
    kernel = windows._kernel()
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.WaitForSingleObject.restype = wt.DWORD
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, expected["pid"])
    if not handle:
        raise OperatorError("operator_process_handle_unavailable")
    try:
        if windows._handle_identity(kernel, handle, expected["pid"]) != expected:
            raise OperatorError("operator_process_identity_changed")
        yield lambda: kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)


async def stop_instance(value, windows, timeout=30, handle_factory=process_handle):
    s = status(value, windows)
    if not s["running"]:
        return {"stopped": True, "already_stopped": True}
    if (not s["fresh"] or s.get("version") != value["version"]
            or Path(s["process"]["path"]).resolve() != Path(value["expected_worker_exe"]).resolve()):
        raise OperatorError("operator_stop_target_unverified")
    _, command_path, directory = state_paths(value)
    with handle_factory(windows, s["process"]) as exited:
        command = shutdown_command(s["run_id"])
        if directory:
            write(directory / "stop-intent.json", {"process": s["process"], "command": command})
        write(command_path, command)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if exited():
                proof = {"stopped": True, "same_handle_exit": True, "process": s["process"]}
                if directory:
                    write(directory / "stop-proof.json", proof)
                return proof
            await asyncio.sleep(0.1)
        raise OperatorError("operator_stop_timeout_no_force")


class OperatorProbe:
    """Status and one-use shutdown only; works while bridge startup is pending."""
    def __init__(self, directory, run_id, version, windows):
        self.directory, self.run_id, self.version, self.windows = Path(directory), run_id, version, windows
        self.controller = self.backend = self.slack = None
        self.claimed = set()
        self.phase, self.code = "starting", None

    def start(self, controller, backend, slack, root_task):
        self.controller, self.backend, self.slack = controller, backend, slack

    async def checkpoint(self, *args):
        pass

    def event(self, kind, **fields):
        # Runtime emits fixed codes; never record payloads, prompts or credentials.
        if kind == "runtime_paused":
            self.code = fields.get("code")

    async def close(self):
        pass

    def snapshot(self):
        if self.phase == "starting" and self.slack and self.slack.ready and self.controller.healthy:
            self.phase = "running"
        bindings = []
        if self.controller:
            for s in self.controller.states.values():
                snap = s.snapshot
                bindings.append({"thread_id": s.binding.codex_thread_id, "binding_id": s.binding.binding_id,
                    "root_ts": s.binding.slack_root_ts, "mode": s.mode, "trusted": bool(snap and snap.trusted),
                    "active_turn": snap.active_turn if snap else None, "protected_turn": s.active_turn,
                    "approval": bool(snap and snap.approval), "compacting": s.compaction_pending,
                    "blocked": s.blocked})
        return {"run_id": self.run_id, "version": self.version, "observed_ms": now_ms(),
            "process": self.windows.process_identity(os.getpid()), "phase": self.phase,
            "ready": bool(self.slack and self.slack.ready),
            "healthy": bool(self.controller and self.controller.healthy),
            "backend_connected": bool(self.backend and self.backend.connected),
            "native_chain": getattr(self.backend, "native_record", None),
            "fault": self.slack.last_fault if self.slack else self.code, "bindings": bindings,
            "pending_requests": len(self.backend.pending_requests)
                if self.backend else 0}

    async def monitor(self, root_task):
        while not root_task.done():
            write(self.directory / "status.json", self.snapshot())
            path = self.directory / "command.json"
            if path.exists() and claim_shutdown(read(path), self.run_id, self.claimed, now_ms()):
                write(self.directory / "claims.json", sorted(self.claimed))
                root_task.cancel()
            await asyncio.sleep(0.25)

    async def manage(self, coroutine):
        root = asyncio.create_task(coroutine)
        monitor = asyncio.create_task(self.monitor(root))
        try:
            done, _ = await asyncio.wait({root, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
                if not root.done():
                    raise OperatorError("operator_monitor_stopped")
            await root
            self.phase = "exited"
        except asyncio.CancelledError:
            self.phase = "stopped"
        except Exception as error:
            self.phase = "failed"
            code = str(error) if isinstance(error, OperatorError) else getattr(error, "code", "operator_worker_failed")
            self.code = code if re.fullmatch(r"[a-z0-9_]{1,100}", code) else "operator_worker_failed"
            write(self.directory / "failure.json", {"exception_type": type(error).__name__,
                "frames": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                           for f in traceback.extract_tb(error.__traceback__)]})
        finally:
            root.cancel()
            monitor.cancel()
            await asyncio.gather(root, monitor, return_exceptions=True)
            result = self.snapshot()
            result.update(ready=False, fault=self.code or result["fault"])
            write(self.directory / "status.json", result)


def bridge_environment(cfg):
    app, bot = cfg.credentials()
    env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP") if k in os.environ}
    env.update(PYTHONUTF8="1", **{cfg.app_token_env: app, cfg.bot_token_env: bot})
    return env


async def start_instance(deployment_path, deployment, name, value, windows, timeout=90):
    if value.get("legacy"):
        raise OperatorError("operator_legacy_start_forbidden")
    with windows.single_instance(deployment["host_id"] + "-operator"):
        for other in deployment["profiles"].values():
            if status(other, windows)["running"]:
                raise OperatorError("operator_bridge_already_running")
        config = importlib.import_module("slack_codex_bridge.config").load(Path(value["config"]))
        windows.verify_record(config)
        env = bridge_environment(config)
        run_id = str(uuid.uuid4())
        _, _, directory = state_paths(value, run_id)
        write(directory / "launch-intent.json", {"run_id": run_id, "version": value["version"]})
        write(Path(value["state_dir"]) / "current.json", {"run_id": run_id})
        child = windows.detached([value["python"], str(Path(__file__).resolve()), "--deployment",
            str(deployment_path), "--profile", name, "worker", "--run-id", run_id], env)
        env.clear()
        write(directory / "launch.json", {"process": child})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = status(value, windows)
            if s.get("run_id") == run_id:
                if s.get("running") and s.get("fresh") and s.get("ready") and s.get("healthy"):
                    return s
                if s.get("phase") in {"failed", "exited", "stopped"}:
                    raise OperatorError("operator_worker_start_failed")
            await asyncio.sleep(0.25)
        raise OperatorError("operator_start_unconfirmed_inspect_before_retry")


def run_tool(deployment_path, profile_name, command, python):
    result = subprocess.run([python, str(Path(__file__).resolve()), "--deployment", str(deployment_path),
        "--profile", profile_name, command], capture_output=True, text=True, timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise OperatorError("operator_rollback_" + command + "_failed")
    return json.loads(result.stdout)


def rollback(deployment_path, deployment):
    stopped = run_tool(deployment_path, "p4", "stop", deployment["profiles"]["p4"]["python"])
    restored = run_tool(deployment_path, "p2", "start", deployment["profiles"]["p2"]["python"])
    return {"candidate_stop": stopped, "p2_restored": restored}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--profile", default="p4")
    parser.add_argument("command", choices=["status", "start", "stop", "rollback", "worker"])
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    deployment = read(args.deployment)
    if sha(__file__) != deployment["operator_sha256"]:
        raise OperatorError("operator_source_changed")
    if args.command == "rollback":
        return rollback(args.deployment, deployment)
    value = profile(deployment, args.profile)
    windows = runtime_for(value)
    if args.command == "status":
        return status(value, windows)
    if args.command == "stop":
        return asyncio.run(stop_instance(value, windows))
    if args.command == "start":
        return asyncio.run(start_instance(args.deployment, deployment, args.profile, value, windows))
    if value.get("legacy") or not args.run_id:
        raise OperatorError("operator_worker_intent_invalid")
    pointer = read(Path(value["state_dir"]) / "current.json")
    if pointer["run_id"] != args.run_id:
        raise OperatorError("operator_worker_intent_changed")
    _, _, directory = state_paths(value, args.run_id)
    cfg = importlib.import_module("slack_codex_bridge.config").load(Path(value["config"]))
    run = importlib.import_module("slack_codex_bridge.cli").run
    probe = OperatorProbe(directory, args.run_id, value["version"], windows)
    asyncio.run(probe.manage(run(cfg, probe)))
    return {"phase": probe.phase}


if __name__ == "__main__":
    try:
        print(json.dumps(main()))
    except Exception as error:
        code = str(error) if isinstance(error, OperatorError) else getattr(error, "code", "operator_failed")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", code):
            code = "operator_failed"
        print(json.dumps({"code": code}))
        raise SystemExit(1)
