import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .config import load
from .controller import Controller
from .desktop import Desktop
from .model import BridgeError
from .slack import Slack, Web
from .store import Store
from .runtime import recovery, reconnect_delay
from . import windows


async def watch_wake(slack, probe=None):
    # This clock runs independently of network requests. A slow Slack read must
    # not be mistaken for system sleep and revoke a freshly synchronized session.
    previous_time, previous_wall = time.monotonic(), time.time()
    while True:
        await asyncio.sleep(1)
        clock, wall = time.monotonic(), time.time()
        if clock - previous_time > 10 or abs((wall - previous_wall) - (clock - previous_time)) > 5:
            if hasattr(slack, "backend"):
                slack.backend.invalidate_requests()
            slack.lost()
            slack.last_fault = "runtime_wake_or_stall"
            if probe:
                probe.event("runtime_paused", code=slack.last_fault)
        previous_time, previous_wall = clock, wall


async def run(cfg, probe=None):
    with windows.single_instance(cfg.host_id + "-run"):
        record = await asyncio.to_thread(windows.verify_record, cfg)
        backend = Desktop(cfg, record["endpoint"])
        backend.native_record = {key: record[key] for key in ("endpoint", "desktop", "backend", "peer")}
        store = await Store(cfg.data_dir / "bridge.sqlite3").open()
        controller = Controller(cfg, backend, store, checkpoint=probe.checkpoint if probe else None)
        slack = None
        events = None
        wake_watch = None
        try:
            await controller.load()
            app, bot = cfg.credentials()
            slack = Slack(cfg, controller, backend, Web(cfg, bot), app)
            if probe:
                probe.start(controller, backend, slack, asyncio.current_task())
            wake_watch = asyncio.create_task(watch_wake(slack, probe))

            async def consume():
                while True:
                    event = await backend.events.get()
                    if event.generation != backend.rpc.generation:
                        continue
                    if event.kind == "disconnected":
                        slack.lost()
                    elif event.kind == "progress":
                        for state in controller.states.values():
                            if state.binding.codex_thread_id == event.thread_id:
                                key = state.binding.binding_id
                                slack.progress[key] = (slack.progress.get(key, "") + event.text)[-1000:]
                    elif event.kind == "started":
                        for state in controller.states.values():
                            if state.binding.codex_thread_id == event.thread_id:
                                slack.progress.pop(state.binding.binding_id, None)

            events = asyncio.create_task(consume())
            failures = 0
            last_identity = 0
            print(f"Bridge PID {os.getpid()}. Ctrl+C disconnects the bridge; desktop/backend tasks remain running.", flush=True)
            while True:
                clock = time.monotonic()
                try:
                    if slack.stop_error:
                        raise slack.stop_error
                    if clock - last_identity >= 5:
                        current = await asyncio.to_thread(windows.verify_record, cfg, check_binaries=False)
                        if current["endpoint"] != record["endpoint"] or current["backend"] != record["backend"]:
                            raise BridgeError("controlled_session_changed_restart_bridge")
                        last_identity = clock
                    if not backend.connected:
                        slack.lost()
                        await backend.connect()
                    if not slack.ready or not await slack.socket.is_connected():
                        await slack.connect()
                    for state in list(controller.states.values()):
                        slack.schedule_tick(state)
                    if not controller.healthy:
                        raise BridgeError("database_unavailable")
                    failures = 0
                    await asyncio.sleep(1)
                except BridgeError as error:
                    slack.last_fault = error.code
                    if probe:
                        probe.event("runtime_paused", code=error.code)
                    slack.lost()
                    # Fixed codes only: never emit SDK payloads, URLs or raw exceptions.
                    policy = recovery(error)
                    print(f"Bridge paused: {error.code} [{policy.category}]. {policy.action}", flush=True)
                    if not controller.healthy or not policy.retry:
                        raise
                    failures += 1
                    await asyncio.sleep(reconnect_delay(error, failures))
        finally:
            controller.invalidate("shutdown")
            if probe:
                await probe.close()
            if wake_watch:
                wake_watch.cancel()
                await asyncio.gather(wake_watch, return_exceptions=True)
            if slack:
                await slack.close()
            if events:
                events.cancel()
                await asyncio.gather(events, return_exceptions=True)
            await backend.close()
            await store.close()


def doctor(cfg):
    value = {"bridge_version": __version__, "python": sys.version.split()[0],
             "platform": sys.platform, "entry_route": "verified_controlled_start",
             "credentials_in_environment": False, "versions_match": False, "session_verified": False}
    try:
        cfg.credentials()
        value["credentials_in_environment"] = True
    except BridgeError:
        pass
    try:
        windows.verify_versions(cfg)
        value["versions_match"] = True
    except BridgeError as error:
        value["version_status"] = error.code
    try:
        record = windows.verify_record(cfg)
        value["session_verified"] = True
        value["backend_pid"] = record["backend"]["pid"]
        value["desktop_pid"] = record["desktop"]["pid"]
    except BridgeError as error:
        value["session_status"] = error.code
    print(json.dumps(value, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Personal single-entry Slack control of a verified Codex desktop")
    parser.add_argument("--config", type=Path, default=Path("bridge.local.json"), help="Non-secret JSON configuration")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("run")
    desktop = sub.add_parser("desktop").add_subparsers(dest="desktop_command", required=True)
    for name in ("start", "status", "stop"):
        desktop.add_parser(name)
    args = parser.parse_args(argv)
    try:
        cfg = load(args.config)
        if args.command == "doctor":
            doctor(cfg)
        elif args.command == "run":
            asyncio.run(run(cfg))
        elif args.desktop_command == "start":
            result = windows.start(cfg)
            print(json.dumps({"status": result["status"], "endpoint": result["endpoint"]}))
        elif args.desktop_command == "status":
            result = windows.verify_record(cfg)
            print(json.dumps({"status": result["status"], "backend_pid": result["backend"]["pid"],
                              "desktop_pid": result["desktop"]["pid"]}))
        elif args.desktop_command == "stop":
            asyncio.run(windows.stop(cfg))
            print("Owned inactive backend stopped.")
    except KeyboardInterrupt:
        print("Bridge disconnected. Desktop and backend were not stopped.")
    except BridgeError as error:
        print(f"Error: {error.code}", file=sys.stderr)
        return 1
    except Exception:
        print("Error: operation_failed; inspect the non-secret configuration and local runtime.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
