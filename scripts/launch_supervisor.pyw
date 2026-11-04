"""Reusable local entry: open a stopped supervisor or restore its hidden window."""
import argparse
import ctypes
import importlib.util
import json
import os
import sys
from pathlib import Path


def reopen_window():
    from ctypes import wintypes as wt
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user.FindWindowW.restype = wt.HWND
    user.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wt.HWND]
    window = user.FindWindowW("SlackCodexSupervisorStatus", "Slack Codex Bridge Supervisor")
    if window:
        user.ShowWindow(window, 9)
        user.SetForegroundWindow(window)
    return bool(window)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, help="Explicit trusted local credential adapter; otherwise use the environment")
    args = parser.parse_args(argv)
    run = args.run_dir.resolve(strict=True)
    state = json.loads((run / "current-run.json").read_text())
    sys.path.insert(0, state["candidate_runtime"])
    from slack_codex_bridge import windows
    from slack_codex_bridge.config import load
    from slack_codex_bridge.model import BridgeError
    from slack_codex_bridge.supervisor import OperatorAdapter, Supervisor, show_window
    cfg = load(run / "candidate-config.json")
    try:
        with windows.single_instance(cfg.host_id + "-supervisor-launch"):
            try:
                with windows.single_instance(cfg.host_id + "-supervisor"):
                    adapter = OperatorAdapter(Path(state["active_deployment"]), "p5", Path(state["active_operator"]))
                    if args.bootstrap:
                        spec = importlib.util.spec_from_file_location("local_supervisor_boundary", args.bootstrap.resolve(strict=True))
                        boundary = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(boundary)
                        boundary.inject_credentials(cfg)
                    try:
                        # No running-worker prerequisite: Exit may have stopped it.
                        show_window(Supervisor(adapter))
                    finally:
                        if args.bootstrap:
                            os.environ.pop(cfg.app_token_env, None)
                            os.environ.pop(cfg.bot_token_env, None)
            except BridgeError as error:
                if error.code != "another_instance_is_running":
                    raise
                if not reopen_window():
                    raise BridgeError("supervisor_window_not_ready") from None
    except BridgeError as error:
        if error.code != "another_instance_is_running":
            raise
        if not reopen_window():
            raise BridgeError("supervisor_window_not_ready") from None


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Fixed diagnostics only; never persist exception messages or credentials.
        code = getattr(error, "code", type(error).__name__)
        import tempfile
        (Path(tempfile.gettempdir()) / "slack-codex-supervisor-launch-failure.json").write_text(json.dumps({"code": code}))
