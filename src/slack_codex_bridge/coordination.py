"""Version-pinned Windows desktop coordination transport; never starts a daemon.

This is a desktop follower, not a second app-server. Only explicitly mapped
operations are available. Unknown RPC methods never reach the desktop pipe.
"""

import asyncio
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import struct
import time
from urllib.parse import urlparse

from .model import BridgeError, new_id


PIPE = r"\\.\pipe\codex-ipc"
MAX_FRAME = 32 * 1024 * 1024
# This private desktop protocol was inspected and read-probed on this build.
DESKTOP_VERSION = "156.0.8078.48"
DESKTOP_HASH = "d8b9c500c4dc0fa2fd5a8a3a5592cd637c2a9e625a191d8733a0b7aa8143b11b"
DESKTOP_PACKAGE = "OpenAI.Codex_26.1027.3384.0_x64__2p2nqsd0c76g0"


def endpoint(identity):
    return f"codex-ipc://{identity['pid']}/{identity['created']}"


def parse_endpoint(value):
    try:
        p = urlparse(value)
        pid, created = int(p.netloc), int(p.path[1:])
        if pid <= 0 or created <= 0 or value != f"codex-ipc://{pid}/{created}":
            raise ValueError
        return pid, created
    except (ValueError, TypeError, AttributeError):
        raise BridgeError("unverified_backend_endpoint") from None


def supported(proof, cfg):
    return (cfg.desktop_version == DESKTOP_VERSION and proof.get("desktop_hash") == DESKTOP_HASH and
            cfg.desktop_exe.parent.parent.name == DESKTOP_PACKAGE)


def native_path(value):
    if not isinstance(value, str):
        raise BridgeError("thread_identity_mismatch")
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\") and len(value) > 6 and value[5:7] == ":\\":
        return value[4:]
    return value


async def open_pipe():
    if not hasattr(asyncio.get_running_loop(), "create_pipe_connection"):
        raise BridgeError("desktop_ipc_requires_windows_proactor")
    reader = asyncio.StreamReader(limit=MAX_FRAME)
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        transport, _ = await asyncio.get_running_loop().create_pipe_connection(lambda: protocol, PIPE)
        writer = asyncio.StreamWriter(transport, protocol, reader, asyncio.get_running_loop())
        return reader, writer, transport.get_extra_info("pipe").fileno()
    except OSError:
        raise BridgeError("desktop_ipc_unavailable") from None


def catalog(cfg, thread_id=None, project=None, sources=None):
    """Read metadata only. Native history/authentication/configuration is not read."""
    path = cfg.native_state_db or Path.home() / ".codex" / "state_5.sqlite"
    if not path.is_absolute() or path.name != "state_5.sqlite" or not path.is_file():
        raise BridgeError("desktop_ipc_catalog_unavailable")
    clauses, args = [], []
    if project is not None and not cfg.allows(native_path(project)):
        raise BridgeError("thread_outside_allowlist")
    projects = (Path(native_path(project)),) if project is not None else cfg.projects
    for folder in projects:
        root = str(folder.resolve()).rstrip("\\/")
        # Match directory boundaries, not a similarly named sibling project.
        variants = [root]
        if root.startswith("\\\\"):
            variants.append("\\\\?\\UNC\\" + root[2:])
        elif len(root) > 2 and root[1] == ":":
            variants.append("\\\\?\\" + root)
        for variant in variants:
            if project is not None:
                clauses.append("cwd = ? COLLATE NOCASE")
                args.append(variant)
            else:
                clauses.append("(cwd = ? COLLATE NOCASE OR substr(cwd,1,?) = ? COLLATE NOCASE "
                               "OR substr(cwd,1,?) = ? COLLATE NOCASE)")
                args.extend([variant, len(variant) + 1, variant + "\\", len(variant) + 1, variant + "/"])
    if not clauses:
        return []
    source_values = {"cli": ["cli"], "vscode": ["vscode"], "exec": ["exec"],
                     "appServer": ["app-server", "appServer"], "unknown": ["unknown"]}
    try:
        values = [v for key in (source_values if sources is None else sources) for v in source_values[key]]
    except (KeyError, TypeError):
        raise BridgeError("desktop_ipc_unsupported_operation") from None
    if not values:
        return []
    where = ("archived = 0 AND source IN (" + ",".join("?" for _ in values) + ") AND (" +
             " OR ".join(clauses) + ")")
    args = values + args
    if thread_id is not None:
        where += " AND id = ?"
        args.append(thread_id)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only = ON")
            rows = db.execute("SELECT id,title,cwd FROM threads WHERE " + where +
                              " ORDER BY updated_at DESC,id", args).fetchall()
        return [{"id": tid, "name": title, "cwd": native_path(cwd)}
                for tid, title, cwd in rows if cfg.allows(native_path(cwd))]
    except (sqlite3.Error, OSError, ValueError):
        raise BridgeError("desktop_ipc_catalog_unavailable") from None


class CoordinationRPC:
    desktop_only_controls = frozenset({"steer", "approval", "model_picker", "effort_picker",
                                       "skills", "compact", "fork"})

    def __init__(self, cfg, address, notify):
        self.cfg, self.endpoint, self.notify = cfg, address, notify
        self.pid, self.created = parse_endpoint(address)
        self.reader = self.writer = self.task = self.handle = None
        self.client_id = None
        self.generation = 0
        self.after_write = None
        self.pending = {}
        self.owners = {}
        self.states = {}
        self.state_events = {}
        self.stamps = {}
        self.read_locks = {}
        self.write_lock = asyncio.Lock()
        # Desktop approval tokens require an original RPC socket. This transport
        # leaves approvals in the desktop rather than inventing equivalent tokens.
        self.ws = None

    @property
    def connected(self):
        return self.writer is not None and not self.writer.is_closing() and self.client_id is not None

    def guard(self):
        from . import windows
        identity = windows.process_identity(self.pid)
        if (not identity or identity["created"] != self.created or
                identity["path"].casefold() != str(self.cfg.desktop_exe.resolve()).casefold() or
                not windows.same_user_session(self.pid) or
                windows.pipe_server_identity(self.handle) != identity):
            raise BridgeError("desktop_ipc_identity_changed")

    async def connect(self):
        from . import windows
        await self.close()
        self.generation += 1
        try:
            proof = await asyncio.to_thread(windows._instance_binary_proof, self.cfg)
            if not supported(proof, self.cfg):
                raise BridgeError("desktop_ipc_protocol_unverified")
            async with asyncio.timeout(self.cfg.request_timeout):
                self.reader, self.writer, self.handle = await open_pipe()
                self.guard()
                self.task = asyncio.create_task(self._read())
                reply = await self.request("initialize", {"clientType": "slack-codex-bridge"}, version=0)
                client = reply.get("result", {}).get("clientId")
                if not isinstance(client, str) or not client:
                    raise BridgeError("desktop_ipc_initialize_failed")
                self.client_id = client
                self.guard()
        except BaseException as error:
            await self.close()
            if isinstance(error, (TimeoutError, OSError)):
                raise BridgeError("backend_connect_failed") from None
            raise

    async def frame(self, message, *, business_write=False):
        data = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if not 0 < len(data) <= MAX_FRAME:
            raise BridgeError("desktop_ipc_frame_too_large")
        self.guard()
        if self.writer is None or self.writer.is_closing():
            raise BridgeError("backend_unavailable")
        async with self.write_lock:
            try:
                self.writer.write(struct.pack("<I", len(data)) + data)
                if business_write and self.after_write:
                    self.after_write(message["method"], message["requestId"])
                await self.writer.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise BridgeError("backend_outcome_unknown" if business_write else "backend_read_failed",
                                  unknown=business_write) from None

    async def request(self, method, params, *, version=1, target=None, write=False):
        key = new_id()
        message = {"type": "request", "requestId": key, "method": method, "version": version,
                   "params": params, "timeoutMs": int(self.cfg.request_timeout * 1000)}
        if self.client_id:
            message["sourceClientId"] = self.client_id
        if target:
            message["targetClientId"] = target
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = (future, write, method, target)
        try:
            await self.frame(message, business_write=write)
            reply = await asyncio.wait_for(future, self.cfg.request_timeout)
            if reply.get("resultType") != "success":
                if method == "thread-owner-discovery" and reply.get("error") == "no-client-found":
                    raise BridgeError("desktop_ipc_open_chat_in_desktop")
                raise BridgeError("backend_outcome_unknown" if write else "desktop_ipc_request_unavailable",
                                  unknown=write)
            if target and reply.get("handledByClientId") != target:
                raise BridgeError("backend_outcome_unknown" if write else "desktop_ipc_owner_changed",
                                  unknown=write)
            return reply
        except (TimeoutError, ConnectionError, OSError):
            raise BridgeError("backend_outcome_unknown" if write else "backend_read_failed", unknown=write) from None
        finally:
            self.pending.pop(key, None)
            if not future.done():
                future.cancel()

    async def _read(self):
        try:
            while True:
                size, = struct.unpack("<I", await self.reader.readexactly(4))
                if not 0 < size <= MAX_FRAME:
                    raise ValueError
                message = json.loads(await self.reader.readexactly(size))
                if not isinstance(message, dict):
                    raise ValueError
                if message.get("type") == "response":
                    pending = self.pending.get(message.get("requestId"))
                    if pending and not pending[0].done():
                        # A response with a different method is never a receipt.
                        if message.get("method") not in {None, pending[2]}:
                            raise ValueError
                        pending[0].set_result(message)
                elif message.get("type") == "broadcast":
                    self.broadcast(message)
                elif message.get("type") == "client-discovery-request":
                    # Never become a thread owner or a request handler.
                    await self.frame({"type": "client-discovery-response", "requestId": message["requestId"],
                                      "response": {"canHandle": False}})
        except (OSError, ValueError, TypeError, KeyError, asyncio.IncompleteReadError, BridgeError):
            pass
        finally:
            if self.writer:
                self.writer.close()
            self.client_id = None
            self.states.clear()
            self.stamps.clear()
            for future, write, _, _ in list(self.pending.values()):
                if not future.done():
                    future.set_exception(BridgeError("backend_outcome_unknown" if write else "backend_disconnected",
                                                     unknown=write))
            for event in self.state_events.values():
                event.set()
            self.notify({"method": "bridge/disconnected", "params": {}}, self.generation)

    def broadcast(self, message):
        if message.get("method") == "client-status-changed":
            p = message.get("params", {})
            if p.get("status") == "disconnected":
                for tid, owner in list(self.owners.items()):
                    if owner == p.get("clientId"):
                        self.states.pop(tid, None)
                        self.stamps.pop(tid, None)
                        self.owners.pop(tid, None)
            return
        if message.get("method") != "thread-stream-state-changed" or message.get("version") != 11:
            return
        p = message.get("params", {})
        tid = p.get("conversationId")
        if (tid not in self.owners or p.get("hostId") != "local" or
                message.get("sourceClientId") != self.owners[tid]):
            return
        change = p.get("change", {})
        if change.get("type") != "snapshot":
            # Unsupported patches invalidate the cache; the next read requests
            # an exact full snapshot. Never apply guessed patch semantics.
            self.states.pop(tid, None)
            self.stamps.pop(tid, None)
        else:
            state = change.get("conversationState")
            if not isinstance(state, dict) or state.get("id") != tid or state.get("hostId") != "local":
                return
            self.states[tid] = state
            self.stamps[tid] = time.monotonic()
        if tid in self.state_events:
            self.state_events[tid].set()
        self.notify({"method": "thread/status/changed", "params": {"threadId": tid}}, self.generation)

    async def owner(self, tid):
        reply = await self.request("thread-owner-discovery", {"hostId": "local", "conversationId": tid})
        owner = reply.get("handledByClientId")
        if not isinstance(owner, str) or not owner or not reply.get("result", {}).get("supportsUntrustedAppInput"):
            raise BridgeError("desktop_ipc_owner_unavailable")
        if owner != self.owners.get(tid):
            self.states.pop(tid, None)
            self.stamps.pop(tid, None)
        self.owners[tid] = owner
        return owner

    async def following(self, tid, owner, value):
        await self.frame({"type": "broadcast", "method": "thread-stream-following-changed", "version": 1,
                          "sourceClientId": self.client_id, "targetClientIds": [owner],
                          "params": {"hostId": "local", "conversationId": tid, "following": value}})

    async def state(self, tid):
        async with self.read_locks.setdefault(tid, asyncio.Lock()):
            metadata = await asyncio.to_thread(catalog, self.cfg, tid)
            if len(metadata) != 1:
                raise BridgeError("thread_outside_allowlist_or_unavailable")
            owner = await self.owner(tid)
            if time.monotonic() - self.stamps.get(tid, -1e9) > .5:
                event = self.state_events.setdefault(tid, asyncio.Event())
                event.clear()
                self.states.pop(tid, None)
                await self.following(tid, owner, False)
                await self.following(tid, owner, True)
                try:
                    await asyncio.wait_for(event.wait(), self.cfg.request_timeout)
                except TimeoutError:
                    raise BridgeError("desktop_ipc_snapshot_unavailable") from None
            self.guard()
            state = self.states.get(tid)
            if not state or self.owners.get(tid) != owner:
                raise BridgeError("desktop_ipc_snapshot_unavailable")
            cwd = native_path(state.get("cwd"))
            if (not self.cfg.allows(cwd) or Path(cwd).resolve() != Path(metadata[0]["cwd"]).resolve()):
                raise BridgeError("thread_identity_mismatch")
            # All consumers in a native RPC sequence see a stable snapshot.
            result = copy.deepcopy(state)
            result["cwd"] = cwd
            for environment in result.get("environments", []):
                if isinstance(environment.get("cwd"), str):
                    environment["cwd"] = native_path(environment["cwd"])
            return result

    @staticmethod
    def turns(state):
        history = state.get("turnHistory", {})
        if history.get("kind") == "canonical":
            rows = list(history.get("history", {}).get("entitiesByKey", {}).values())
        else:
            rows = state.get("turns", [])
        rows = sorted(rows, key=lambda r: r.get("turnStartedAtMs") or 0, reverse=True)
        result = []
        for row in rows:
            if row.get("turnId") and row.get("status") in {"inProgress", "completed", "failed", "interrupted"}:
                result.append({"id": row["turnId"], "status": row["status"],
                               "items": row.get("items", []), "error": row.get("error")})
        return result

    async def call(self, method, params, *, write=False):
        if not self.connected:
            raise BridgeError("backend_unavailable")
        self.guard()
        allowed = {
            "thread/list": {"limit", "cursor", "sortKey", "sortDirection", "archived", "sourceKinds",
                            "useStateDbOnly", "cwd"},
            "thread/read": {"threadId", "includeTurns"},
            "thread/resume": {"threadId", "excludeTurns"},
            "thread/turns/list": {"threadId", "limit", "sortDirection", "itemsView"},
            "turn/start": {"threadId", "input", "model", "effort"},
            "turn/interrupt": {"threadId", "turnId"}}
        if (method not in allowed or not isinstance(params, dict) or set(params) - allowed[method] or
                write != (method in {"turn/start", "turn/interrupt"})):
            raise BridgeError("desktop_ipc_unsupported_operation")
        if method == "thread/list":
            sources = params.get("sourceKinds")
            if sources is not None and (not isinstance(sources, list) or
                    any(not isinstance(s, str) or s not in {"cli", "vscode", "exec", "appServer", "unknown"}
                        for s in sources)):
                raise BridgeError("desktop_ipc_unsupported_operation")
            if (params.get("archived") or params.get("sortKey", "updated_at") != "updated_at" or
                    params.get("useStateDbOnly", True) is not True or
                    params.get("sortDirection", "desc") != "desc"):
                raise BridgeError("desktop_ipc_unsupported_operation")
            limit, cursor = params.get("limit", 100), params.get("cursor")
            if type(limit) is not int or not 1 <= limit <= 100:
                raise BridgeError("desktop_ipc_unsupported_operation")
            try:
                offset = 0 if cursor is None else int(cursor.removeprefix("ipc:"))
                if cursor is not None and (offset <= 0 or offset > 1000000 or cursor != f"ipc:{offset}"):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise BridgeError("desktop_ipc_unsupported_operation") from None
            rows = await asyncio.to_thread(catalog, self.cfg, None, params.get("cwd"), sources)
            return {"data": rows[offset:offset+limit],
                    "nextCursor": f"ipc:{offset+limit}" if offset+limit < len(rows) else None}
        if (method == "thread/read" and params.get("includeTurns") or
                method == "thread/resume" and params.get("excludeTurns") is not True or
                method == "thread/turns/list" and (params.get("itemsView") not in {"notLoaded", "full"} or
                    params.get("sortDirection", "desc") != "desc" or
                    type(params.get("limit")) is not int or not 1 <= params["limit"] <= 100)):
            raise BridgeError("desktop_ipc_unsupported_operation")
        tid = params.get("threadId")
        if not isinstance(tid, str) or not tid:
            raise BridgeError("thread_identity_mismatch")
        state = await self.state(tid)
        runtime = copy.deepcopy(state.get("threadRuntimeStatus") or {})
        if state.get("requests"):
            runtime["activeFlags"] = list(set(runtime.get("activeFlags", [])) | {"waitingOnApproval"})
        thread = {"id": tid, "name": state.get("title"), "cwd": state["cwd"],
                  "status": runtime, "environments": state.get("environments", [])}
        if method == "thread/read":
            return {"thread": thread}
        if method == "thread/resume":
            if state.get("resumeState") != "resumed":
                raise BridgeError("desktop_ipc_open_chat_in_desktop")
            permissions = state.get("currentPermissions") or {}
            return {"thread": thread, "model": state.get("latestModel"),
                    "reasoningEffort": state.get("latestReasoningEffort"), "cwd": state["cwd"],
                    "approvalPolicy": permissions.get("approvalPolicy"),
                    "approvalsReviewer": permissions.get("approvalsReviewer"),
                    "sandbox": permissions.get("sandboxPolicy"), "modelProvider": state.get("modelProvider")}
        if method == "thread/turns/list":
            history = state.get("turnHistory", {})
            if (params.get("itemsView") == "full" and history.get("kind") == "canonical" and
                    not history.get("history", {}).get("isComplete")):
                await self.request("thread-follower-load-complete-history", {"conversationId": tid},
                                   target=self.owners[tid])
                self.stamps.pop(tid, None)
                state = await self.state(tid)
                if not (state.get("turnHistory", {}).get("history", {}).get("isComplete")):
                    raise BridgeError("desktop_ipc_history_incomplete")
            return {"data": self.turns(state)[:params.get("limit", 20)], "nextCursor": None}
        if not write:
            raise BridgeError("desktop_ipc_write_flag_required")
        if method == "turn/interrupt" and (not isinstance(params.get("turnId"), str) or not params["turnId"]):
            raise BridgeError("missing_target_turn")
        owner = self.owners[tid]
        if method == "turn/start":
            if (state.get("resumeState") != "resumed" or
                    (state.get("threadRuntimeStatus") or {}).get("type") != "idle" or state.get("requests")):
                raise BridgeError("desktop_ipc_chat_not_idle")
            result = await self.request("thread-follower-start-turn",
                {"conversationId": tid, "turnStart": {"request": params,
                 "context": {"inheritThreadSettings": True}}}, version=2, target=owner, write=True)
            value = result.get("result", {}).get("result")
            if not isinstance(value, dict) or not value.get("turn", {}).get("id"):
                raise BridgeError("backend_missing_turn_id", unknown=True)
            return value
        result = await self.request("thread-follower-interrupt-turn",
            {"conversationId": tid, "mode": "user-stop", "expectedTurnId": params.get("turnId")},
            version=4, target=owner, write=True)
        if not params.get("turnId") or result.get("result", {}).get("interruptedTurnId") != params["turnId"]:
            raise BridgeError("backend_target_unconfirmed", unknown=True)
        return {}

    async def close(self):
        writer, task = self.writer, self.task
        self.client_id = None
        if writer:
            writer.close()
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if writer:
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, TimeoutError):
                pass
        self.writer = self.reader = self.task = self.handle = None
        self.owners.clear()
        self.states.clear()
        self.stamps.clear()
        self.state_events.clear()
        self.read_locks.clear()
