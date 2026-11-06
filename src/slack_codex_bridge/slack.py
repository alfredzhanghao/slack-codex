"""Scoped Socket Mode adapter with one-use, connection-bound daily operations."""

import asyncio
import json
import logging
import hashlib
import re
import secrets
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp

from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from .config import Config
from .controller import Controller, Prepared, State
from .model import Binding, BridgeError, new_id, now_ms, recent_threads
from .quota import quota_text
from .inputs import parse_files, local_file
from .artifacts import capture, automatic, file_identity
from . import preferences
from .project_names import labels as project_labels
from .thread_names import names as thread_names
from .i18n import Literal, translate, blocks as translated_blocks
from . import attachments
from .skill_view import description as skill_description
from .retrieval import is_retrieval, matching_outputs
from .recovery import fingerprint, replacement
from .roots import ROOT_ERRORS
from .runtime import recovery, retry_after
from .approval import FORM, response

TS = re.compile(r"\d{1,13}\.\d{1,6}")
TURN = re.compile(r"[0-9a-fA-F-]{36}")
NAVIGATION_ACTIONS = {"bridge_bind": "bind", "bridge_project": "project", "bridge_projects": "projects",
                      "bridge_projects_next": "projects_page", "bridge_tasks_page": "tasks_page"}
NAVIGATION_KINDS = frozenset(NAVIGATION_ACTIONS.values())
CHAT_STATE_UNAVAILABLE = {"desktop_ipc_open_chat_in_desktop", "thread_outside_allowlist_or_unavailable",
    "desktop_ipc_snapshot_unavailable", "desktop_ipc_owner_unavailable", "backend_thread_not_loaded",
    "persisted_thread_metadata_unavailable", "thread_not_trusted"}
NAVIGATION_ERRORS = {
    "backend_thread_active_writer", "backend_rejected", "backend_read_failed", "backend_disconnected",
    "backend_unavailable", "backend_invalid_snapshot", "backend_thread_not_loaded", "thread_not_trusted",
    "thread_outside_allowlist", "thread_identity_mismatch", "thread_environment_unverified",
    "persisted_thread_metadata_unavailable", "thread_listing_incomplete", "database_unavailable",
    "slack_unavailable", "slack_write_unconfirmed", "slack_read_failed", "slack_rate_limited",
    "slack_read_message_not_found", "slack_read_thread_not_found", "slack_missing_scope",
    "chat_open_failed", "chat_selection_expired", "chat_connection_changed", "chat_root_unavailable",
    "chat_entry_link_unverified", "chat_open_cancelled",
    "snapshot_target_mismatch",
}


def timestamp(value: str) -> int:
    if not isinstance(value, str) or not TS.fullmatch(value):
        raise BridgeError("invalid_slack_timestamp")
    return int(Decimal(value) * 1000)


def clean(text: str, limit=35000):
    value = str(text)
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return value if len(value) <= limit else value[:limit] + "\n[Truncated; view the full output in Codex.]"


def text_sections(text):
    """Budget the actual escaped payload, without splitting an escape sequence."""
    blocks, parts, size = [], [], 0
    escapes = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
    for char in text:
        escaped = escapes.get(char, char)
        if size + len(escaped) > 2800:
            blocks.append({"type": "section", "text": {"type": "plain_text", "text": "".join(parts)}})
            parts, size = [], 0
        parts.append(escaped)
        size += len(escaped)
    if parts:
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": "".join(parts)}})
    return blocks


def markdown_chunks(text, limit=12000):
    """Keep normal paragraphs intact and balance fenced code across payloads."""
    prefix, fence = "", None
    while text:
        budget = limit - len(prefix) - 256
        end = min(len(text), budget)
        if end < len(text):
            paragraph = text.rfind("\n\n", 0, end)
            line = text.rfind("\n", 0, end)
            end = paragraph + 2 if paragraph >= 0 else line + 1 if line >= 0 else end
        part, text = text[:end], text[end:]
        for line in part.splitlines():
            match = re.fullmatch(r" {0,3}(`{3,32}|~{3,32})([^`~]{0,120})", line)
            if match:
                marker, info = match.groups()
                if fence is None:
                    fence = marker, info
                elif marker[0] == fence[0][0] and len(marker) >= len(fence[0]) and not info.strip():
                    fence = None
        suffix = "\n" + fence[0] if fence else ""
        yield prefix + part + suffix
        prefix = fence[0] + fence[1] + "\n" if fence else ""


def private_logger():
    # The SDK logs URLs, payloads and exception responses at some levels. Never propagate them.
    logger = logging.Logger("bridge.slack.transport", level=logging.CRITICAL + 1)
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


class Web:
    def __init__(self, cfg: Config, bot_token: str, client=None):
        self.cfg = cfg
        self.client = client or AsyncWebClient(token=bot_token, retry_handlers=[], logger=private_logger(),
                                               timeout=15, trust_env_in_session=False)
        self.cooldown: dict[str, float] = {}

    async def call(self, method, *, write=False, **kwargs):
        if method in {"views.open", "views.update"} and "view" in kwargs:
            kwargs["view"] = translated_blocks(kwargs["view"], preferences.language(self.cfg))
        delay = self.cooldown.get(method, 0) - time.monotonic()
        if delay > 0:
            raise BridgeError("slack_rate_limited", retry_after=delay)
        try:
            if method == "apps.connections.open":
                response = await self.client.apps_connections_open(**kwargs)
            elif method == "files.getUploadURLExternal":
                response = await self.client.files_getUploadURLExternal(**kwargs)
            elif method == "files.completeUploadExternal":
                response = await self.client.files_completeUploadExternal(**kwargs)
            elif method in {"conversations.info", "conversations.history", "conversations.replies", "chat.getPermalink", "files.info"}:
                response = await self.client.api_call(method, http_verb="GET", params=kwargs)
            else:
                response = await self.client.api_call(method, json=kwargs)
            return response.data
        except asyncio.CancelledError:
            raise
        except SlackApiError as error:
            if error.response.status_code == 429 or error.response.get("error") == "ratelimited":
                delay = retry_after(error.response.headers)
                self.cooldown[method] = max(self.cooldown.get(method, 0), time.monotonic() + delay)
                raise BridgeError("slack_rate_limited", retry_after=delay) from None
            if not write and error.response.get("error") == "missing_scope":
                raise BridgeError("slack_missing_scope") from None
            safe_codes = {"invalid_auth", "token_revoked", "channel_not_found", "not_in_channel",
                          "invalid_arguments", "invalid_arg_name", "invalid_json", "invalid_blocks",
                          "message_not_found", "thread_not_found", "method_not_supported_for_channel_type",
                          "missing_scope", "not_allowed_token_type", "no_permission", "not_authed",
                          "token_expired", "account_inactive"}
            code = error.response.get("error")
            if not write and code in safe_codes:
                raise BridgeError("slack_read_" + code) from None
            if write and code in safe_codes:
                raise BridgeError("slack_write_" + code) from None
            raise BridgeError("slack_write_unconfirmed" if write else "slack_read_failed", unknown=write) from None
        except Exception:
            raise BridgeError("slack_write_unconfirmed" if write else "slack_unavailable", unknown=write) from None

    async def post(self, text, *, root=None, blocks=None, verbatim=False):
        if not verbatim:
            language = preferences.language(self.cfg)
            text, blocks = translate(text, language), translated_blocks(blocks, language)
        body = {"channel": self.cfg.dm_id, "text": clean(text), "unfurl_links": False,
                "unfurl_media": False, "parse": "none", "link_names": False}
        if root:
            body["thread_ts"] = root
        if blocks:
            body["blocks"] = blocks
        value = await self.call("chat.postMessage", write=True, **body)
        if value.get("channel") != self.cfg.dm_id or not TS.fullmatch(value.get("ts", "")):
            raise BridgeError("slack_write_target_unconfirmed", unknown=True)
        return value["ts"]

    async def update(self, root, text, blocks):
        language = preferences.language(self.cfg)
        text, blocks = translate(text, language), translated_blocks(blocks, language)
        value = await self.call("chat.update", write=True, channel=self.cfg.dm_id, ts=root,
                                text=clean(text), blocks=blocks, parse="none", link_names=False)
        if value.get("channel") != self.cfg.dm_id or value.get("ts") != root:
            raise BridgeError("slack_update_target_unconfirmed", unknown=True)

    async def upload(self, artifact, *, root, guard, checkpoint):
        """One attempt per documented stage; no authenticated redirects or helper retries."""
        await guard()
        value = await self.call("files.getUploadURLExternal", write=True,
                                filename=artifact.path.name, length=len(artifact.content))
        file_id, url = value.get("file_id", ""), value.get("upload_url", "")
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname == "files.slack.com"
                     and parsed.port in {None, 443} and not parsed.username and not parsed.password
                     and not parsed.fragment and parsed.path.startswith("/upload/"))
        except (TypeError, ValueError):
            valid = False
        if value.get("ok") is not True or not valid or not re.fullmatch(r"F[A-Z0-9]+", file_id):
            raise BridgeError("slack_upload_allocation_unconfirmed", unknown=True)
        await checkpoint("allocated", file_id)
        await guard()
        try:
            # A fresh session has no Slack API Authorization header or cookies.
            async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                                            timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.post(url, data=artifact.content, allow_redirects=False,
                                        headers={"Content-Type": "application/octet-stream"}) as response:
                    if response.status != 200:
                        raise BridgeError("slack_upload_bytes_unconfirmed", unknown=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise BridgeError("slack_upload_bytes_unconfirmed", unknown=True) from None
        await checkpoint("uploaded", file_id)
        await guard()
        completed = await self.call("files.completeUploadExternal", write=True,
                                    files=[{"id": file_id, "title": artifact.path.name}],
                                    channel_id=self.cfg.dm_id, thread_ts=root)
        if (completed.get("ok") is not True
                or [f.get("id") for f in completed.get("files", [])] != [file_id]):
            raise BridgeError("slack_upload_completion_unconfirmed", unknown=True)
        return file_id


async def verify_dm(cfg, bot_user_id, call):
    """Verify a fixed single-person DM without expanding the reduced App scopes.

    History fallback inspects author IDs only; it never imports old messages as inputs.
    Fresh events must still match owner, DM, App and authenticated-bot authorization.
    """
    try:
        info = (await call("conversations.info", channel=cfg.dm_id)).get("channel", {})
    except BridgeError as error:
        if error.code != "slack_missing_scope":
            raise
        cursor = None
        for _ in range(5):
            page = await call("conversations.history", channel=cfg.dm_id, limit=50, cursor=cursor,
                              include_all_metadata=False)
            humans = {msg["user"] for msg in page.get("messages", [])
                      if msg.get("user") and msg.get("user") != bot_user_id and not msg.get("bot_id")
                      and not (msg["user"] == "USLACKBOT" and msg.get("subtype") == "tombstone")}
            if humans - {cfg.user_id}:
                raise BridgeError("slack_dm_owner_mismatch")
            if cfg.user_id in humans:
                return "fixed_dm_history_owner"
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        raise BridgeError("slack_dm_owner_evidence_unavailable")
    if info.get("id") != cfg.dm_id or info.get("is_im") is not True or info.get("user") != cfg.user_id:
        raise BridgeError("slack_dm_owner_mismatch")
    return "conversation_info"


@dataclass
class Intent:
    kind: str
    binding_id: str = ""
    epoch: str = ""
    turn_id: str = ""
    thread_id: str = ""
    root: str = ""
    expires: float = 0
    request_token: str = ""
    choice: str = ""
    binding_version: int = 0
    operation_id: str = ""
    operation_fingerprint: str = ""
    binding_root: str = ""


class Buttons:
    def __init__(self):
        self.tokens: dict[str, Intent] = {}

    def issue(self, intent: Intent) -> str:
        current = time.monotonic()
        for token, old in list(self.tokens.items()):
            if old.expires < current:
                self.tokens.pop(token)
            elif intent.kind not in NAVIGATION_KINDS and (old.kind, old.binding_id, old.epoch, old.turn_id, old.thread_id, old.root,
                  old.request_token, old.choice, old.binding_version, old.operation_id,
                  old.operation_fingerprint, old.binding_root) == (
                    intent.kind, intent.binding_id, intent.epoch, intent.turn_id, intent.thread_id, intent.root,
                    intent.request_token, intent.choice, intent.binding_version, intent.operation_id,
                    intent.operation_fingerprint, intent.binding_root):
                return token
        token = secrets.token_urlsafe(24)
        intent.expires = current + 600
        self.tokens[token] = intent
        return token

    def take(self, token) -> Intent | None:
        intent = self.tokens.pop(token, None)
        return intent if intent and intent.expires >= time.monotonic() else None

    def navigation(self, token, kind, root) -> Intent | None:
        if not isinstance(token, str) or kind not in NAVIGATION_KINDS or not isinstance(root, str) or not TS.fullmatch(root):
            return None
        intent = self.tokens.get(token)
        if intent and intent.expires < time.monotonic():
            self.tokens.pop(token)
            return None
        return intent if intent and intent.kind == kind and intent.root == root else None

    def clear(self):
        self.tokens.clear()

    def invalidate(self, binding_id):
        self.tokens = {token: intent for token, intent in self.tokens.items() if intent.binding_id != binding_id}


class ManualSocket(SocketModeClient):
    """Even Slack 'disconnect' instructions go through our authority invalidation."""

    def __init__(self, *args, lost, open_url=None, **kwargs):
        self.lost = lost
        self.open_url = open_url
        super().__init__(*args, **kwargs)

    async def issue_new_wss_url(self):
        # The SDK implementation recursively retries 429s outside Web's cooldown.
        # Production always supplies our bounded, single-attempt opener.
        if not self.open_url:
            raise BridgeError("slack_socket_opener_missing")
        value = await self.open_url("apps.connections.open", app_token=self.app_token)
        url = value.get("url")
        if not isinstance(url, str) or not url.startswith("wss://"):
            raise BridgeError("slack_socket_url_unconfirmed")
        return url

    async def connect_to_new_endpoint(self, force=False):
        self.lost()
        await self.disconnect()


class Slack:
    def __init__(self, cfg: Config, controller: Controller, backend, web: Web, app_token: str):
        self.cfg, self.controller, self.backend, self.web, self.app_token = cfg, controller, backend, web, app_token
        self.socket = None
        self.ready = False
        self.opened_ms = 2**63 - 1
        self.buttons = Buttons()
        self.work: set[asyncio.Task] = set()
        self.binding_lock = asyncio.Lock()
        self.last_render: dict[str, tuple[float, str]] = {}
        self.progress: dict[str, str] = {}
        self.project_labels = {str(path): path.name or "Project" for path in cfg.projects}
        self.seen_commands: dict[str, int] = {}
        self.navigation_receipts: dict[tuple[str, str], float] = {}
        self.last_fault: str | None = None
        self.stop_error: BridgeError | None = None
        self.bot_user_id = ""
        self.bot_id = ""
        self.request_cards: dict[str, dict] = {}
        self.ticks: dict[str, asyncio.Task] = {}
        self.render_times: list[float] = []
        self.upload_lock = asyncio.Lock()
        self.selected_project = ""
        self.navigation = deque(maxlen=50)
        self.expired_navigation_roots = deque(maxlen=20)
        self.input_locks = {}
        self.pending_inputs = {s.binding.binding_id for s in controller.states.values() if s.input_pending}
        self.staged_inputs = {}

    def fault(self, error):
        self.last_fault = error.code
        if not recovery(error).retry:
            self.stop_error = error
            self.lost()

    def lost(self):
        self.ready = False
        self.opened_ms = 2**63 - 1
        self.buttons.clear()
        self.navigation_receipts.clear()
        self.selected_project = ""
        self.staged_inputs.clear()
        # Keep pending markers until queued handlers observe their revoked epoch.
        for card in self.request_cards.values():
            card["needs_refresh"] = True
        self.controller.invalidate("Slack connection lost")

    def root_lost(self, state, reason):
        if not state.root_fault:
            self.controller._suspend(state)
            self.buttons.invalidate(state.binding.binding_id)
        state.root_fault = reason
        self.last_render.pop(state.binding.binding_id, None)

    async def check_root(self, state):
        root = state.binding.slack_root_ts
        if not TS.fullmatch(root):
            self.root_lost(state, "root_creation_unconfirmed")
            return False
        try:
            rows = (await self.web.call("conversations.replies", channel=self.cfg.dm_id, ts=root, limit=1)).get("messages", [])
        except BridgeError as error:
            if error.code not in ROOT_ERRORS:
                raise
            self.root_lost(state, "root_missing")
            return False
        msg = rows[0] if rows else {}
        app = msg.get("app_id") or msg.get("bot_profile", {}).get("app_id")
        if msg.get("ts") != root or msg.get("bot_id") != self.bot_id or app != self.cfg.app_id:
            self.root_lost(state, "root_identity_unverified")
            return False
        return not state.root_fault

    async def connect(self):
        self.lost()
        await self.refresh_project_labels()
        if self.socket:
            await self.socket.close()
        auth = await self.web.call("auth.test")
        if auth.get("team_id") != self.cfg.team_id or not auth.get("bot_id") or not auth.get("user_id"):
            raise BridgeError("slack_account_mismatch")
        self.bot_user_id, self.bot_id = auth["user_id"], auth["bot_id"]
        await verify_dm(self.cfg, self.bot_user_id, self.web.call)
        for state in self.controller.states.values():
            await self.check_root(state)
        hello = asyncio.Event()
        hello_valid = False

        async def observed(frame):
            nonlocal hello_valid
            if frame.type.name != "TEXT":
                return
            try:
                message = json.loads(frame.data)
            except (ValueError, TypeError):
                self.lost()
                return
            if message.get("type") == "hello":
                hello_valid = message.get("connection_info", {}).get("app_id") == self.cfg.app_id
                hello.set()
            if message.get("type") == "disconnect":
                self.lost()

        async def failed(_):
            self.lost()

        self.socket = ManualSocket(self.app_token, web_client=self.web.client, lost=self.lost,
                                   open_url=self.web.call,
                                   auto_reconnect_enabled=False, logger=private_logger(),
                                   on_message_listeners=[observed], on_close_listeners=[failed],
                                   on_error_listeners=[failed])
        self.socket.socket_mode_request_listeners.append(self.receive)
        try:
            async with asyncio.timeout(30):
                await self.socket.connect()
                await hello.wait()
                if not hello_valid:
                    raise BridgeError("slack_app_mismatch")
                await self.controller.synchronized()
                if not await self.socket.is_connected():
                    raise BridgeError("slack_disconnected_during_sync")
            self.opened_ms = now_ms()
            self.ready = True
        except BaseException as error:
            self.lost()
            await self.socket.close()
            if isinstance(error, (asyncio.CancelledError, BridgeError)):
                raise
            raise BridgeError("slack_connect_failed") from None

    async def close(self):
        self.lost()
        for task in list(self.work):
            task.cancel()
        await asyncio.gather(*self.work, return_exceptions=True)
        if self.socket:
            await self.socket.close()
        if self.web.client.session:
            await self.web.client.session.close()

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.work.add(task)

        def done(value):
            self.work.discard(value)
            if not value.cancelled() and value.exception():
                error = value.exception()
                self.fault(error if isinstance(error, BridgeError) else BridgeError("background_operation_failed"))
        task.add_done_callback(done)
        return task

    def schedule_tick(self, state):
        key = state.binding.binding_id
        previous = self.ticks.get(key)
        if previous and not previous.done():
            return

        async def observed():
            try:
                await self.tick(state)
            except BridgeError as error:
                if error.code in CHAT_STATE_UNAVAILABLE:
                    self.last_fault = error.code
                    await self.render(state)
                    return
                if error.code in ROOT_ERRORS and not await self.check_root(state):
                    self.last_fault = "binding_root_unavailable"
                    return
                if error.code != "slack_rate_limited":
                    self.controller._suspend(state)
                self.fault(error)
                if error.code == "database_unavailable":
                    self.controller.healthy = False
                    self.controller.invalidate("database_write_failed")
        self.ticks[key] = self.spawn(observed())

    def scoped_message(self, payload) -> dict | None:
        if payload.get("team_id") != self.cfg.team_id or payload.get("api_app_id") != self.cfg.app_id:
            return None
        if not self.bot_user_id or not any(
                x.get("team_id") == self.cfg.team_id and x.get("user_id") == self.bot_user_id and x.get("is_bot") is True
                for x in payload.get("authorizations", [])):
            return None
        event = payload.get("event", {})
        if (event.get("type") != "message" or event.get("subtype") not in {None, "file_share"} or event.get("bot_id")
                or event.get("user") != self.cfg.user_id or event.get("channel") != self.cfg.dm_id
                or not isinstance(event.get("text", ""), str)):
            return None
        if "text" not in event and not event.get("files"):
            return None
        text = event.get("text", "")
        output_attribution = bool(event.get("files") and event.get("app_id") in self.cfg.message_apps
                and re.fullmatch(r"\s*\*发送工具\*\s+<@U[A-Z0-9]+>\s*", text))
        if output_attribution:
            # A file-only send by an allowlisted integration has no task instruction.
            # Preserve ordinary task bodies, including their visible attribution.
            text = ""
        return {**event, "text": text, "bridge_output_attribution": output_attribution}

    async def receive(self, client, req):
        prepared = None
        state = None
        after = None
        try:
            if client is self.socket and self.ready:
                if req.type == "events_api":
                    event = self.scoped_message(req.payload)
                    if event:
                        root = event.get("thread_ts")
                        state = self.controller.by_root(root) if root else None
                        sent = timestamp(event.get("ts"))
                        key = f"message:{self.cfg.team_id}:{self.cfg.dm_id}:{event['ts']}"
                        text = event["text"].strip()
                        if event.get("app_id") in self.cfg.message_apps and text.startswith("!"):
                            # Configured user-message integrations may append this visible
                            # attribution. Remove only its exact suffix, never ordinary text.
                            text = re.sub(r"\s+\*发送工具\*\s+<@U[A-Z0-9]+>\s*$", "", text)
                        epoch = state.epoch if state else ""
                        parsed = self.parse_task(state, text)
                        if state and event["bridge_output_attribution"] and state.mode == "desktop":
                            if self.opened_ms <= sent <= now_ms() + 30000 and now_ms() - sent <= self.cfg.message_age_ms:
                                if await self.controller.store.claim_input(key, state.binding, epoch):
                                    await self.controller.store.finish_input(key, "output", "integration_output_share")
                        elif state and state.root_fault and not (text == "!roots" or text.startswith("!root-replace ")):
                            after = self.web.post("This root is unavailable. Use !roots in this DM for explicit recovery.")
                        elif state and is_retrieval(text) and not event.get("files"):
                            if self.opened_ms <= sent <= now_ms() + 30000 and now_ms() - sent <= self.cfg.message_age_ms:
                                if await self.controller.store.claim_input(key, state.binding, epoch):
                                    after = self.retrieve_outputs(state, text, key, epoch, sent)
                        elif state and (event.get("files") or event.get("subtype") == "file_share" or state.binding.binding_id in self.pending_inputs
                                        and (not text.startswith("!") or text.startswith("!skill "))):
                            if self.opened_ms <= sent <= now_ms() + 30000 and now_ms() - sent <= self.cfg.message_age_ms:
                                if await self.controller.store.claim_input(key, state.binding, epoch, attachment=True):
                                    self.pending_inputs.add(state.binding.binding_id)
                                    after = self.attachment_message(state, event, key, epoch)
                        elif state and text.startswith('!artifact ') and sent >= self.opened_ms:
                            match = re.fullmatch(r'!artifact "([^"\r\n]+)"', text)
                            if match:
                                prepared = await self.controller.prepare(
                                    state, key, "artifact_upload", "", epoch, sent, data={"path": match[1]})
                            else:
                                after = self.web.post('Use !artifact "PATH" to retrieve one file.',
                                                      root=state.binding.slack_root_ts)
                        elif state and state.binding.binding_id in self.pending_inputs and text.startswith("!file "):
                            after = self.web.post("This session has pending Slack attachments. Send a plain task instruction "
                                "in this thread to use them; local-path input cannot silently replace that context.",
                                root=state.binding.slack_root_ts)
                        elif state and parsed:
                            kind, target, content = parsed
                            prepared = await self.controller.prepare(state, key, kind, content, epoch, sent, target)
                        elif state and text.startswith(("!decision ", "!answer ")):
                            prepared = await self.prepare_response_text(state, text, sent)
                        elif state and (text in {"!compact", "!fork"}
                                        or text.startswith(("!effort ", "!model ", "!skill ", "!file "))):
                            prepared = await self.controller.prepare_command(state, key, text, epoch, sent)
                        elif self.opened_ms <= sent <= now_ms() + 30000 and now_ms() - sent <= self.cfg.message_age_ms:
                            # Local commands are ephemeral and invalidated at every reconnect.
                            if key not in self.seen_commands:
                                self.seen_commands[key] = sent
                                self.seen_commands = {k: t for k, t in self.seen_commands.items()
                                                      if now_ms() - t <= self.cfg.message_age_ms}
                                after = self.command(state, text, epoch)
                elif req.type == "interactive":
                    state, prepared, after = await self.prepare_interaction(
                        req.payload, navigation_delivery=req.envelope_id)
        except BridgeError as error:
            self.fault(error)
        except (AttributeError, TypeError, KeyError, ValueError):
            self.last_fault = "malformed_slack_input"
        try:
            # Receipt has completed, but no model/backend write is awaited on this ACK path.
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            if prepared and state:
                await self.controller.abandon(state, prepared)
            if after:
                after.close()
            self.lost()
            return
        if prepared and state:
            self.spawn(self.submit(state, prepared))
        if after:
            self.spawn(after)

    async def attachment_message(self, state, event, key, epoch):
        bid, version = state.binding.binding_id, state.binding.binding_version
        lock = self.input_locks.setdefault(bid, asyncio.Lock())
        async with lock:
            try:
                def guard():
                    if (not self.ready or epoch != state.epoch or version != state.binding.binding_version
                            or state.binding.disabled or state.root_fault or not self.controller.available):
                        raise BridgeError("attachment_binding_changed_resend_files")
                    reason = self.controller._reason(state, "turn_start", None, epoch)
                    if reason:
                        raise BridgeError(reason)

                await self.controller.refresh(state)
                guard()
                staged = self.staged_inputs.get(bid)
                rows = []
                if staged:
                    if staged["epoch"] != epoch or staged["version"] != version or staged["expires"] < time.monotonic():
                        raise BridgeError("attachment_expired_resend_files")
                    rows = list(staged["files"])
                files = event.get("files") or []
                text = event["text"].strip()
                data = {}
                if text.startswith("!skill "):
                    selector, sep, text = text[7:].partition(" -- ")
                    if not sep or not selector.strip() or not text.strip():
                        raise BridgeError("use_skill_name_then_task")
                    data["skill_selector"] = selector.strip().strip('"')
                if event.get("subtype") == "file_share" and not files:
                    raise BridgeError("attachment_files_unavailable_check_files_read")
                if text.startswith("!"):
                    raise BridgeError("attachment_requires_plain_task_instruction")
                if files:
                    rows += await attachments.stage(self.cfg, self.web, state.binding.codex_thread_id, files)
                if not rows:
                    raise BridgeError("attachment_context_unavailable_resend_files")
                await asyncio.to_thread(attachments.attachment_inputs, self.cfg, state.snapshot, rows, text)
                await self.controller.refresh(state)
                guard()
                if not text:
                    self.staged_inputs[bid] = {"files": rows, "epoch": epoch, "version": version,
                                              "expires": time.monotonic() + 600}
                    await self.controller.store.finish_input(key, "staged")
                    await self.web.post(f"Received {len(rows)} attachment(s) for this session. No task started. "
                        "Send your instruction in this thread within 10 minutes. Context is used once; "
                        "reconnect/restart requires sending the files again.", root=state.binding.slack_root_ts)
                else:
                    prepared = await self.controller.prepare(state, key, "turn_start", text, epoch,
                        timestamp(event["ts"]), data={**data, "attachments": rows})
                    self.staged_inputs.pop(bid, None)
                    self.pending_inputs.discard(bid)
                    await self.controller.store.finish_input(key, "consumed")
                    await self.submit(state, prepared)
            except asyncio.CancelledError:
                await asyncio.shield(self.controller.store.finish_input(key, "revoked", "cancelled_no_replay"))
                self.staged_inputs.pop(bid, None)
                self.pending_inputs.discard(bid)
                raise
            except Exception as error:
                code = error.code if isinstance(error, BridgeError) else "attachment_processing_failed"
                self.staged_inputs.pop(bid, None)
                self.pending_inputs.discard(bid)
                await self.controller.store.finish_input(key, "rejected", code)
                if code == "desktop_input_mode":
                    await self.web.post("Click Take control on the session card before sending attachments and instructions. "
                        "The shared files remain in Slack; no task was started.", root=state.binding.slack_root_ts)
                    return
                await self.web.post(f"Attachment input did not complete: {code}. No partial text-only task was submitted. "
                    "Inspect !status / operation receipts before retrying; resend all required files with your instruction.",
                    root=state.binding.slack_root_ts)

    async def retrieve_outputs(self, state, text, key, epoch, sent):
        try:
            if epoch != state.epoch or not self.ready:
                raise BridgeError("artifact_binding_changed")
            rows = await self.artifact_rows(state, include_retained=False, latest_completed=True)
            matches = matching_outputs(text, rows)
            if len(matches) != 1:
                if matches:
                    await self.output_files_card(state, "", rows=matches)
                else:
                    await self.web.post('No matching output file from the latest task. Use !artifacts or !artifact "PATH". '
                                        'No task was rerun.', root=state.binding.slack_root_ts)
                await self.controller.store.finish_input(key, "selection")
                return
            row = matches[0]
            if row.get("unavailable"):
                raise BridgeError(row["unavailable"])
            prepared = await self.controller.prepare(state, key, "artifact_upload", "", epoch, sent,
                                                      data={"path": row["path"]})
            await self.controller.store.finish_input(key, "consumed")
            await self.submit_artifact(state, prepared)
        except BridgeError as error:
            await self.controller.store.finish_input(key, "rejected", error.code)
            await self.web.post(f"File retrieval did not complete: {error.code}. No task was rerun. "
                "An unconfirmed upload is never automatically repeated.", root=state.binding.slack_root_ts)

    @staticmethod
    def parse_task(state, text):
        if state is None:
            return None
        if not text.startswith("!"):
            return "turn_start", None, text
        if text.startswith("!steer "):
            left, sep, content = text.partition(" -- ")
            parts = left.split()
            if sep and len(parts) == 2 and TURN.fullmatch(parts[1]) and content.strip():
                return "steer", parts[1], content.strip()
        if text.startswith("!interrupt "):
            parts = text.split()
            if len(parts) == 2 and TURN.fullmatch(parts[1]):
                return "interrupt", parts[1], ""
        return None

    async def submit(self, state: State, prepared: Prepared):
        if prepared.operation.kind == "root_replace":
            await self.submit_root(state, prepared)
            return
        if prepared.operation.kind == "artifact_upload":
            await self.submit_artifact(state, prepared)
            return
        op = await self.controller.execute(state, prepared)
        if op.status == "accepted":
            if op.kind == "unknown_review":
                reviewed = json.loads(op.result_json)["reviewed_op_id"]
                await self.web.post(f"Risk acknowledged; the outcome remains unknown. Use !replace {reviewed} to preview a new request.",
                                    root=state.binding.slack_root_ts)
            if op.kind == "fork":
                await self.finish_fork(state, op)
            return
        reasons = {
            "desktop_input_mode": "Click Take control on the session card before sending a new task.",
            "input_precedes_opening": "This message arrived before control was acquired. Send a new message after taking control.",
            "busy_use_explicit_steer": "This chat is working. Use Steer to add instructions to its current task.",
            "old_control_epoch": "Use the latest session card; this control has expired.",
            "message_expired_or_future": "This message is too old or its timestamp is invalid. Send a new message.",
            "approval_or_compaction_pending": "Resolve the pending approval or wait for compaction before sending a new task.",
            "state_not_trusted": "The chat is reconnecting. Refresh its session card before sending a new task.",
            "unknown_or_semantic_mismatch": "An earlier outcome needs review. Check !status before sending another task.",
            "shared_directory_busy_or_untrusted": "Another task is using this project. Wait for it to finish.",
            "submission_in_progress": "A request is still being submitted. Wait before sending another task.",
        }
        if "steer" in getattr(self.backend, "desktop_only_controls", ()):
            reasons["busy_use_explicit_steer"] = "This chat is working. Use Codex desktop to guide the active task; Slack Steer cannot safely target this turn."
        message = ("Delivery could not be confirmed. Do not resend; check !status and the original client."
                   if op.status == "unknown" else reasons.get(op.error_reason,
                   "The request could not be submitted. Check !status for details."))
        try:
            await self.web.post(message, root=state.binding.slack_root_ts)
        except BridgeError as error:
            if error.code not in ROOT_ERRORS or await self.check_root(state):
                raise

    async def artifact_guard(self, state, epoch, version):
        if not self.ready or not self.controller.healthy or not self.controller.available:
            raise BridgeError("artifact_connection_unavailable")
        await self.controller.refresh(state)
        binding, snap = state.binding, state.snapshot
        if (not self.ready or not self.controller._fresh(state) or binding.disabled
                or state.epoch != epoch or binding.binding_version != version
                or (binding.slack_team_id, binding.slack_channel_id, binding.owner_user_id, binding.host_id) !=
                   (self.cfg.team_id, self.cfg.dm_id, self.cfg.user_id, self.cfg.host_id)
                or binding.environment_key != "windows" or snap.thread.environment != "windows"
                or Path(snap.thread.cwd).resolve() != Path(binding.project_path).resolve()):
            raise BridgeError("artifact_binding_changed")

    async def artifact_rows(self, state, turn_id="", *, include_retained=True, latest_completed=False):
        await self.artifact_guard(state, state.epoch, state.binding.binding_version)
        prefs = preferences.read(self.cfg)
        if latest_completed and hasattr(self.backend, "latest_artifacts"):
            native = await self.backend.latest_artifacts(state.binding.codex_thread_id)
        else:
            native = (await self.backend.artifacts(state.binding.codex_thread_id, turn_id) if turn_id
                      else await self.backend.artifacts(state.binding.codex_thread_id)) if hasattr(self.backend, "artifacts") else []
        retained = [{"path": json.loads(op.payload_json).get("data", {}).get("path", "")}
                    for op in await self.controller.store.uploads(state.binding.binding_id)] if not turn_id and include_retained else []
        rows, seen = [], set()
        for row in native + retained:
            path = row.get("path")
            if not isinstance(path, str) or not path or path.casefold() in seen:
                continue
            seen.add(path.casefold())
            try:
                actual = await asyncio.to_thread(local_file, self.cfg, state.binding.project_path, path,
                                                 max_bytes=prefs.max_file_bytes, output=True)
                if str(actual).casefold() != path.casefold() and str(actual).casefold() in seen:
                    continue
                seen.add(str(actual).casefold())
                identity = await asyncio.to_thread(file_identity, actual)
                rows.append({**row, "path": str(actual), "name": actual.name, "identity": identity})
            except BridgeError as error:
                rows.append({"path": path, "unavailable": error.code})
            if len(rows) == 20:
                break
        return rows

    async def deliver_outputs(self, state, operation, turn_id):
        """New task delivery only; the parent's durable attempting flag prevents replay."""
        rows = await self.artifact_rows(state, turn_id, include_retained=False)
        prefs = preferences.read(self.cfg)
        epoch, version, root = state.epoch, state.binding.binding_version, state.binding.slack_root_ts
        failures = []
        for row in rows:
            await self.artifact_guard(state, epoch, version)
            if row.get("unavailable"):
                await self.web.post(f"Output unavailable: {row['path']} ({row['unavailable']}).", root=root)
                continue
            data = {"path": row["path"], "expected_file": row["identity"]}
            key = "output:" + operation.op_id + ":" + hashlib.sha256(row["path"].casefold().encode()).hexdigest()
            if automatic(row["path"], row["identity"][0], prefs):
                prepared = await self.controller.prepare(state, key, "artifact_upload", "", epoch,
                                                         now_ms(), data={**data, "automatic": True})
                await self.submit_artifact(state, prepared)
                outcome = await self.controller.store.operation(prepared.operation.op_id)
                if outcome.status != "accepted":
                    failures.append(outcome)
            else:
                intent = Intent("output_confirm", state.binding.binding_id, epoch, turn_id=turn_id,
                    binding_version=version, binding_root=root, operation_id=key, choice=json.dumps(data))
                token = self.buttons.issue(intent)
                body = f"Send output file to this Slack thread?\n{row['path']} ({row['identity'][0] / preferences.MB:.2f} MB)"
                blocks = text_sections(body) + [{"type": "actions", "elements": [{"type": "button",
                    "text": {"type": "plain_text", "text": "Send file"}, "action_id": "bridge_output_confirm",
                    "value": token}]}]
                intent.root = await self.web.post(body, root=root, blocks=blocks)
        if failures:
            unknown = any(op.status in {"sending", "unknown"} for op in failures)
            raise BridgeError("artifact_output_delivery_incomplete", unknown=unknown)
        return bool(rows)

    async def output_files_card(self, state, turn_id, *, rows=None):
        rows = rows if rows is not None else await self.artifact_rows(state, turn_id)
        if not rows:
            return False
        language = preferences.language(self.cfg)
        body = translate("Output files referenced by this task (up to 20; current contents):", language) + "\n"
        for row in rows:
            body += row["path"] + (" [unavailable: " + row["unavailable"] + "]" if row.get("unavailable") else "") + "\n"
        body += translate("Choose one or more files, then Send to Slack. No task is rerun.", language)
        intent = Intent("artifacts_open", state.binding.binding_id, state.epoch, turn_id=turn_id,
                        binding_version=state.binding.binding_version, binding_root=state.binding.slack_root_ts)
        token = self.buttons.issue(intent)
        blocks = text_sections(body) + [{"type": "actions", "elements": [{"type": "button",
            "text": {"type": "plain_text", "text": "Get output files"}, "action_id": "bridge_output_files",
            "value": token}]}]
        intent.root = await self.web.post(body, blocks=blocks, root=state.binding.slack_root_ts)
        return True

    async def artifact_modal(self, state, intent, trigger_id):
        def current():
            return (self.ready and self.controller.available and self.controller.healthy
                    and not state.root_fault and not state.binding.disabled
                    and intent.epoch == state.epoch and intent.root == state.binding.slack_root_ts
                    and intent.binding_version == state.binding.binding_version)

        if not current():
            raise BridgeError("artifact_binding_changed")
        token = self.buttons.issue(Intent("artifact_submit", state.binding.binding_id, intent.epoch,
                                          root=intent.root, binding_version=intent.binding_version, choice=new_id()))

        def view(options, notice):
            blocks = [{"type": "section", "text": {"type": "plain_text", "text": notice}}]
            if options:
                blocks.append({"type": "input", "block_id": "recent", "optional": True,
                               "label": {"type": "plain_text", "text": "Output files (select one or more)"},
                               "element": {"type": "multi_static_select", "action_id": "choice",
                                           "max_selected_items": 20, "options": options}})
            # Keep block/action IDs stable so Slack preserves a typed path during the update.
            blocks.append({"type": "input", "block_id": "path", "optional": bool(options),
                           "label": {"type": "plain_text", "text": "Local file path"},
                           "element": {"type": "plain_text_input", "action_id": "text", "max_length": 2000}})
            return {"type": "modal", "callback_id": "bridge_artifact", "private_metadata": token,
                    "title": {"type": "plain_text", "text": "Get output files"},
                    "submit": {"type": "plain_text", "text": "Send to Slack"}, "blocks": blocks}

        # Consume the short-lived trigger before any backend/file discovery awaits.
        opened = await self.web.call("views.open", write=True, trigger_id=trigger_id,
            view=view([], "Recent files are loading. You can enter a project file path now. Retrieval never reruns the task."))
        target = opened.get("view", {})
        if not isinstance(target.get("id"), str) or not target["id"] or not target.get("hash"):
            raise BridgeError("slack_modal_open_unconfirmed", unknown=True)
        try:
            rows = await self.artifact_rows(state, intent.turn_id)
            notice = "Select up to 20 output files OR enter one project file path. Sends current files to this Slack thread; never reruns the task."
        except BridgeError as error:
            if not recovery(error).retry:
                raise
            rows = []
            notice = "Recent files are unavailable. Enter a project file path; it will be revalidated before upload."
        if token not in self.buttons.tokens or not current():
            self.buttons.tokens.pop(token, None)
            return
        # No new native references are exposed without fresh binding verification.
        try:
            await self.artifact_guard(state, intent.epoch, intent.binding_version)
        except BridgeError as error:
            self.buttons.tokens.pop(token, None)
            if not recovery(error).retry:
                raise
            return
        if token not in self.buttons.tokens or not current():
            return
        options = []
        for i, row in enumerate(r for r in rows if not r.get("unavailable")):
            choice = self.buttons.issue(Intent("artifact_choice", state.binding.binding_id, intent.epoch,
                                               root=intent.root, choice=row["path"],
                                               binding_version=intent.binding_version, request_token=token))
            options.append({"text": {"type": "plain_text", "text": f"{i + 1}. {row['name']}"[:75]}, "value": choice})
        await self.web.call("views.update", write=True, view_id=target["id"], hash=target["hash"], view=view(options, notice))

    async def submit_artifact_batch(self, state, prepared):
        """One durable batch receipt, separate one-attempt upload operations per file."""
        op, store = prepared.operation, self.controller.store
        if not prepared.execute:
            if op.status != "accepted":
                await self.web.post("Files were not resent. Check !status before trying again.", root=state.binding.slack_root_ts)
            return
        children, pending = [], None
        epoch = state.reservations[op.op_id][0]
        final_status, reason = "accepted", None
        try:
            await self.artifact_guard(state, epoch, op.binding_version)
            paths = json.loads(op.payload_json)["data"]["paths"]
            if not 1 <= len(paths) <= 20:
                raise BridgeError("artifact_batch_size_invalid")
            await store.transition(op.op_id, "received", "sending")
            # The aggregate reservation must not reject its own sequential children.
            async with state.lock:
                state.reservations.pop(op.op_id, None)
            for index, path in enumerate(paths):
                await self.artifact_guard(state, epoch, op.binding_version)
                pending = await self.controller.prepare(state, f"artifact-batch:{op.op_id}:{index}",
                    "artifact_upload", "", epoch, now_ms(), data={"path": path})
                children.append({"op_id": pending.operation.op_id, "path": path})
                await store.transition(op.op_id, "sending", "sending", result_json=json.dumps({"files": children}))
                child = pending
                pending = None
                await self.submit_artifact(state, child)
                outcome = await store.operation(child.operation.op_id)
                children[-1]["status"] = outcome.status
                if outcome.status == "unknown":
                    final_status = "unknown"
                elif outcome.status != "accepted" and final_status != "unknown":
                    final_status = "rejected"
            if final_status != "accepted":
                reason = "artifact_batch_partial_failure"
        except asyncio.CancelledError:
            final_status, reason = "unknown", "artifact_batch_cancelled"
            raise
        except BridgeError as error:
            reason = error.code
            final_status = "unknown" if error.unknown or any(c.get("status") == "unknown" for c in children) else "rejected"
            if error.code == "database_unavailable":
                self.controller.healthy = False
                self.controller.invalidate("database_write_failed")
        except Exception:
            final_status, reason = "unknown", "artifact_batch_unconfirmed"
        finally:
            if pending and pending.execute:
                await asyncio.shield(self.controller.abandon(state, pending))
            async with state.lock:
                state.reservations.pop(op.op_id, None)
            current = await store.operation(op.op_id)
            if current.status in {"received", "sending"}:
                await asyncio.shield(store.transition(op.op_id, current.status,
                    final_status if current.status == "sending" else "rejected", error_reason=reason,
                    result_json=json.dumps({"files": children, "requested": len(json.loads(op.payload_json)["data"]["paths"])})))
        if final_status != "accepted":
            await self.web.post("Some files could not be sent. Check !status before resending.", root=state.binding.slack_root_ts)

    async def submit_artifact(self, state, prepared):
        op, store = prepared.operation, self.controller.store
        if json.loads(op.payload_json or "{}").get("data", {}).get("paths"):
            await self.submit_artifact_batch(state, prepared)
            return
        root = state.binding.slack_root_ts

        async def notice(text):
            try:
                await self.web.post(text, root=root)
            except BridgeError as error:
                if error.code not in ROOT_ERRORS or await self.check_root(state):
                    raise

        if not prepared.execute:
            if op.status != "accepted":
                await notice("The file was not sent. Check !status before trying again.")
            return
        epoch = state.reservations[op.op_id][0]
        artifact = None
        data = json.loads(op.payload_json)

        async def guard():
            await self.artifact_guard(state, epoch, op.binding_version)
            if state.binding.codex_thread_id != op.target_thread_id or state.binding.slack_root_ts != root:
                raise BridgeError("artifact_binding_changed")
            prefs = preferences.read(self.cfg)
            if artifact is not None:
                if len(artifact.content) > prefs.max_file_bytes:
                    raise BridgeError("file_too_large")
                if data["data"].get("automatic") and not automatic(artifact.path, len(artifact.content), prefs):
                    raise BridgeError("artifact_automatic_policy_changed")
            if op.supersedes_op_id:
                await self.controller.replacement_source(state, op.supersedes_op_id,
                    json.loads(op.payload_json)["supersedes_fingerprint"])

        async def checkpoint(stage, file_id):
            await store.transition(op.op_id, "sending", "sending",
                                   backend_ref_json=json.dumps({"file_id": file_id, "stage": stage}))

        async def failed(code, unknown=False):
            current = await store.operation(op.op_id)
            if current.status in {"received", "sending"}:
                await store.transition(op.op_id, current.status,
                                       "unknown" if unknown and current.status == "sending" else "rejected",
                                       error_reason=code)

        try:
            if self.upload_lock.locked():
                raise BridgeError("artifact_transfer_busy")
            async with self.upload_lock:
                await guard()
                prefs = preferences.read(self.cfg)
                path = data["data"]["path"]
                expected = data["data"].get("expected_file")
                if expected is not None:
                    actual = await asyncio.to_thread(local_file, self.cfg, state.binding.project_path, path,
                                                     max_bytes=prefs.max_file_bytes, output=True)
                    if await asyncio.to_thread(file_identity, actual) != expected:
                        raise BridgeError("artifact_changed_since_offer")
                artifact = await asyncio.to_thread(capture, replace(self.cfg, artifact_max_bytes=prefs.max_file_bytes),
                                                  state.binding.project_path, path)
                if expected is not None and await asyncio.to_thread(file_identity, artifact.path) != expected:
                    raise BridgeError("artifact_changed_since_offer")
                if data["data"].get("automatic") and not automatic(artifact.path, len(artifact.content), preferences.read(self.cfg)):
                    raise BridgeError("artifact_automatic_policy_changed")
                pending = await store.uploads(state.binding.binding_id, pending=True)
                if any(other.op_id != op.op_id and json.loads(other.payload_json).get("data", {}).get("path", "").casefold()
                       == str(artifact.path).casefold() for other in pending):
                    raise BridgeError("artifact_previous_delivery_requires_review")
                await guard()
                await store.transition(op.op_id, "received", "sending",
                    payload_json=json.dumps({**data, "data": {"path": str(artifact.path)}}),
                    result_json=json.dumps({"bytes": len(artifact.content), "sha256": artifact.sha256}))
                file_id = await self.web.upload(artifact, root=root,
                                                guard=guard, checkpoint=checkpoint)
                await store.transition(op.op_id, "sending", "accepted",
                                       backend_ref_json=json.dumps({"file_id": file_id, "stage": "completed"}))
        except asyncio.CancelledError:
            await asyncio.shield(failed("artifact_cancelled", unknown=True))
            raise
        except BridgeError as error:
            if error.code not in ROOT_ERRORS or await self.check_root(state):
                self.fault(error)
            if error.code == "database_unavailable":
                self.controller.healthy = False
                self.controller.invalidate("database_write_failed")
            await failed(error.code, unknown=error.unknown or error.code == "database_unavailable")
        except Exception:
            await failed("artifact_upload_unconfirmed", unknown=True)
        finally:
            async with state.lock:
                state.reservations.pop(op.op_id, None)
        current = await store.operation(op.op_id)
        if current.status != "accepted":
            await notice("The file could not be sent. Check !status before resending.")

    async def prepare_response_text(self, state, text, sent):
        """A real Slack message may make the same one-use decision as its card."""
        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            raise BridgeError("invalid_request_reply_command")
        request = self.backend.request(parts[1])
        card = self.request_cards.get(request.token)
        if (request.thread_id != state.binding.codex_thread_id or not card or not card.get("ts")
                or card.get("binding_id") != state.binding.binding_id or card.get("closed")
                or card.get("binding_version", state.binding.binding_version) != state.binding.binding_version):
            raise BridgeError("request_reply_binding_mismatch")
        data = {"request_token": request.token}
        if parts[0] == "!answer":
            data.update(choice="answer", answers=json.loads(parts[2]))
        else:
            data["choice"] = {"grant": "grant for this turn"}.get(parts[2], parts[2])
        response(request, self.backend.items, data["choice"], data.get("answers"))
        return await self.controller.prepare(state, "approval:" + request.token, "approval", "", state.epoch,
                                             sent, request.turn_id, data=data)

    async def command(self, state, text, epoch):
        try:
            await self._command(state, text, epoch)
        except BridgeError as error:
            if not state or error.code not in ROOT_ERRORS or await self.check_root(state):
                raise

    async def _command(self, state, text, epoch):
        root = state.binding.slack_root_ts if state else None
        if text == "!task" or text.startswith("!task "):
            text = "!tasks" + text[5:]
        if text == "!roots":
            rows = []
            for s in list(self.controller.states.values())[:20]:
                operations = await self.controller.store.root_operations(s.binding.binding_id)
                rows.append(f"Binding: {s.binding.binding_id}\nChat: {s.binding.codex_thread_id}\n"
                            f"Version: {s.binding.binding_version}; root: {s.binding.slack_root_ts}\n"
                            f"Root status: {s.root_fault or 'verified at connection'}\n" +
                            "\n".join(f"Replacement {op.op_id}: {op.status}; reason: {op.error_reason or 'none'}; "
                                      f"receipt: {op.backend_ref_json or 'none'}" for op in operations))
            await self.web.post("Bound roots (up to 20). Use !root-replace BINDING_ID to preview.\n" + "\n\n".join(rows))
        elif text.startswith("!root-replace "):
            target = self.controller.states.get(text.split(maxsplit=1)[1].strip())
            if not target:
                await self.web.post("Binding not found in this configured DM.")
            else:
                await self.root_preview(target)
        elif text == "!tasks" or text.startswith("!tasks "):
            if self.selected_project:
                await self.tasks(text[6:].strip(), project=self.selected_project)
            else:
                await self.projects()
        elif text in {"!project", "!projects"}:
            await self.projects()
        elif text.startswith("!open ") and TURN.fullmatch(text[6:].strip()):
            await self.open_chat(text[6:].strip())
        elif text == "!status":
            if state:
                await self.refresh_limits(force=True)
                await self.controller.refresh(state)
                await self.render(state, force=True)
            else:
                history = "\n".join(json.dumps(row, sort_keys=True) for row in list(self.navigation)[-5:])
                await self.web.post(f"Slack ready: {self.ready}. Backend connected: {getattr(self.backend, 'connected', True)}. "
                    f"Bound chats: {len(self.controller.states)}. Use !project or !tasks.\n"
                    "Recent chat navigation (UTC milliseconds; retained until bridge restart):\n" + (history or "None."))
        elif text == "!quota":
            if not getattr(self.backend, "rate_limits_supported", True):
                await self.web.post("This connection does not provide account limits. View them in Codex desktop.", root=root)
            else:
                await self.refresh_limits(force=True)
                await self.web.post("Account limits are temporarily unavailable." if getattr(self, "quota_fault", None)
                                    else quota_text(self.backend, self.cfg), root=root)
        elif state and text.startswith("!control "):
            mode = text.split(maxsplit=1)[1]
            if mode == "status":
                await self.render(state, force=True)
            else:
                changed = await self.controller.transfer(state, mode, epoch)
                explanations = {
                    "session_has_active_task": "This chat is still running a task. Wait for it to finish, then click Take control again.",
                    "old_control_epoch": "This control is outdated. Use the latest session card and click Take control again.",
                    "approval_pending": "Resolve the pending approval before changing input mode.",
                    "compaction_pending": "Wait for context compaction to finish before changing input mode.",
                    "unknown_outcome_requires_review": "Review the protected unknown operation before changing input mode.",
                    "submission_in_progress": "A submission is still in progress. Wait for its receipt before changing input mode.",
                    "state_not_trusted": "The chat is not synchronized. Refresh its session card before trying again.",
                }
                if not changed:
                    notice = explanations.get(state.transfer_refusal, "Refresh the session card and check its status before trying again.")
                    previous = getattr(self, "last_transfer_notice", {}).get(state.binding.binding_id)
                    if not previous or previous[0] != notice or now_ms() - previous[1] >= 30000:
                        await self.web.post(notice, root=root)
                        if not hasattr(self, "last_transfer_notice"):
                            self.last_transfer_notice = {}
                        self.last_transfer_notice[state.binding.binding_id] = notice, now_ms()
                await self.render(state, force=True)
        elif state and text.startswith("!op "):
            try:
                op = await self.controller.operation(state, text.split(maxsplit=1)[1].strip())
                await self.web.post(f"Operation {op.op_id}\nSubmission: {op.status}\n"
                                    f"Input: {op.payload_json}\nCreated: {op.created_at_ms} UTC milliseconds\n"
                                    f"Binding version: {op.binding_version}\n"
                                    f"Review: {op.unknown_review_state}\nAudit: {op.review_json or 'none'}\n"
                                    f"Replaces: {op.supersedes_op_id or 'none'}\n"
                                    f"Target: {op.target_turn_id or 'new turn'}\n"
                                    f"Evidence: {op.backend_ref_json or 'unavailable'}\n"
                                    f"Result: {op.result_json or 'not confirmed'}\n"
                                    f"Reason: {op.error_reason or 'none'}"
                                    + (f"\nUse !{'replace' if op.unknown_review_state == 'acknowledged' else 'review'} {op.op_id}."
                                       if op.status == "unknown" else ""), root=root)
            except BridgeError as error:
                await self.web.post(error.code, root=root)
        elif state and text.startswith(("!review ", "!replace ")):
            parts = text.split()
            if len(parts) == 2 and TURN.fullmatch(parts[1]):
                try:
                    await self.recovery_card(state, parts[1], replace=parts[0] == "!replace", epoch=epoch)
                except BridgeError as error:
                    await self.web.post(error.code, root=root)
        elif state and (text == "!skills" or text.startswith("!skills ")):
            try:
                await self.skills_card(state, text[7:].strip())
            except BridgeError as error:
                await self.web.post(error.code, root=root)
        elif state and text in {"!model", "!models"}:
            await self.controller.refresh(state)
            rows = await self.backend.models()
            rows = [m for m in rows if not m.get("hidden") or m["model"] == state.snapshot.model]
            body = f"Current model: {state.snapshot.model}. Choose with !model MODEL [EFFORT]; applies to the next task.\n"
            for row in rows:
                body += row['model'] + ': ' + ', '.join(r['reasoningEffort'] for r in row.get('supportedReasoningEfforts', [])) + '\n'
            await self.web.post(body, root=root, blocks=text_sections(body))
        elif state and text == "!effort":
            await self.controller.refresh(state)
            setting = json.loads(state.binding.next_effort_json or "{}")
            model = setting.get("model") if setting.get("state") == "pending" else state.snapshot.model
            options = await self.backend.efforts(model)
            state.effort_options = tuple(options)
            await self.web.post("Next-turn effort options: " + ", ".join(options) + ". Use !effort VALUE.", root=root)
            await self.render(state, force=True)
        elif state and text.startswith("!fork-bind "):
            op = await self.controller.operation(state, text.split(maxsplit=1)[1].strip())
            await self.finish_fork(state, op)
        elif state and text == "!artifacts":
            if not await self.output_files_card(state, ""):
                await self.web.post('No explicit file references yet. Use !artifact "PATH". '
                                    'No task is rerun.', root=root)
        elif not text.startswith("!"):
            await self.web.post("Select a project with !project, then Open chat and use the session card's replies. "
                                "Send tasks and attachments only inside that session's reply thread. No task was submitted.")
        elif text.startswith("!"):
            await self.web.post("Commands: !project, !task [filter] / !tasks [filter], !projects, !status, !control slack|desktop|status, "
                                "!steer FULL_TURN_ID -- text, !interrupt FULL_TURN_ID, !op OP_ID, !quota. "
                                "!skills, !skill NAME_OR_PATH -- task, !file \"PATH\" -- task, "
                                "!compact, !fork, !fork-bind OP_ID, !model [MODEL [EFFORT]], !models, !effort [VALUE]. "
                                '!artifacts, !artifact "PATH", !review OP_ID, !replace OP_ID. '
                                '!roots, !root-replace BINDING_ID.', root=root)

    async def root_preview(self, state):
        await self.check_root(state)
        await self.controller.refresh(state)
        reason = self.controller._reason(state, "root_replace", None, state.epoch)
        if reason:
            await self.web.post("Root replacement refused: " + reason)
            return
        binding = state.binding
        intent = Intent("root_confirm", binding.binding_id, state.epoch,
                        binding_version=binding.binding_version, binding_root=binding.slack_root_ts,
                        operation_fingerprint=fingerprint(binding), choice=str(state.snapshot.generation))
        text = (f"Replace unavailable root\nBinding: {binding.binding_id}\nChat: {binding.codex_thread_id}\n"
                f"Project: {binding.project_path}\nOld root: {binding.slack_root_ts}\n"
                f"Version: {binding.binding_version} -> {binding.binding_version + 1}\n"
                f"Reason: {state.root_fault}\n"
                "Create ONE new root in this DM. Old controls and native request nonces will be invalidated. "
                "Next-turn effort will be cleared. Unknown operations remain unknown and queryable. "
                "Active tasks continue; pending approvals may require the original client. "
                "Input defaults to desktop; no task is repeated. "
                "A previous unconfirmed post may already exist; it will not gain input authority.")
        if len(text) > 2800:
            await self.web.post("Root preview is too large to display completely. Inspect locally; no replacement action is available.")
            return
        token = self.buttons.issue(intent)
        button = {"type": "button", "action_id": "bridge_root_replace", "value": token,
                  "text": {"type": "plain_text", "text": "Create replacement root"},
                  "confirm": {"title": {"type": "plain_text", "text": "Replace this root?"},
                    "text": {"type": "plain_text", "text": "Invalidate old controls and attempt one new root? An unconfirmed root may already exist."},
                    "confirm": {"type": "plain_text", "text": "Replace root"},
                    "deny": {"type": "plain_text", "text": "Cancel"}}}
        intent.root = await self.web.post(text, blocks=text_sections(text) + [{"type": "actions", "elements": [button]}])

    async def submit_root(self, state, prepared):
        op, store = prepared.operation, self.controller.store
        if not prepared.execute:
            return
        posted = None
        try:
            async with self.binding_lock:
                await self.controller.refresh(state)
                async with state.lock:
                    reservation = state.reservations.get(op.op_id)
                    reason = "reservation_lost" if not reservation else self.controller._reason(
                        state, "root_replace", None, reservation[0], own_op=op.op_id)
                    if (not self.ready or reason or state.snapshot.generation != reservation[1]):
                        raise BridgeError(reason or "root_replacement_connection_changed")
                    # The durable binding rotation and send intent commit together.
                    state.binding = await store.begin_root_replacement(op)
                    self.controller._suspend(state)
                    self.root_lost(state, "root_creation_unconfirmed")
                    epoch, version, generation = state.epoch, state.binding.binding_version, state.snapshot.generation
                    self.request_cards = {token: card for token, card in self.request_cards.items()
                                          if card.get("binding_id") != state.binding.binding_id}
                    if hasattr(self.backend, "invalidate_requests"):
                        self.backend.invalidate_requests(state.binding.codex_thread_id)

                def guard():
                    if (not self.ready or not self.controller.available or not self.controller.healthy
                            or state.epoch != epoch or state.binding.binding_version != version
                            or not state.snapshot.trusted or now_ms() - state.snapshot.observed_ms > 5000
                            or state.snapshot.generation != generation
                            or not getattr(self.backend, "connected", True)
                            or getattr(getattr(self.backend, "rpc", None), "generation", generation) != generation):
                        raise BridgeError("root_replacement_connection_changed")
                    if (Path(state.snapshot.thread.cwd).resolve() != Path(state.binding.project_path).resolve()
                            or state.snapshot.thread.environment != state.binding.environment_key):
                        raise BridgeError("root_replacement_target_changed")

                guard()
                posted = await self.web.post(f"Replacement Codex root. Operation {op.op_id}. "
                                             "Attaching; controls remain unavailable until confirmed.")
                await self.controller.refresh(state)
                async with state.lock:
                    guard()
                    state.binding = await store.finish_root_replacement(op, state.binding, posted)
                    state.root_fault = None
                await self.controller.refresh(state)
        except asyncio.CancelledError:
            await asyncio.shield(self.root_failed(op, "root_replacement_cancelled", posted))
            raise
        except BridgeError as error:
            self.fault(error)
            if error.code == "database_unavailable":
                self.controller.healthy = False
            await self.root_failed(op, error.code, posted, unknown=error.unknown or error.code == "database_unavailable")
        except Exception:
            await self.root_failed(op, "root_replacement_unconfirmed", posted, unknown=True)
        finally:
            async with state.lock:
                state.reservations.pop(op.op_id, None)
        if not state.root_fault and self.ready:
            await self.render(state, force=True)

    async def root_failed(self, op, code, posted, *, unknown=True):
        current = await self.controller.store.operation(op.op_id)
        if current.status in {"received", "sending"}:
            fields = {"error_reason": code}
            if posted:
                fields["backend_ref_json"] = json.dumps({"root_ts": posted, "post_ack": True})
            await self.controller.store.transition(op.op_id, current.status,
                "unknown" if current.status == "sending" and (unknown or posted) else "rejected", **fields)

    async def recovery_card(self, state, op_id, *, replace, epoch):
        op = await self.controller.operation(state, op_id)
        await self.controller.refresh(state)
        reason = self.controller._reason(state, "unknown_review", None, epoch)
        if reason:
            raise BridgeError(reason)
        expected = fingerprint(op)
        await self.controller.store.check_unknown(op, state.binding, expected, acknowledged=replace)
        text = (f"{'NEW submission preview' if replace else 'Unknown operation review'}\nOperation: {op.op_id}\n"
                f"Original outcome: {op.status}; review: {op.unknown_review_state}\n"
                f"Thread: {op.target_thread_id}\nTurn: {op.target_turn_id or 'new turn'}\n"
                f"Original binding version: {op.binding_version}; current: {state.binding.binding_version}; "
                f"submitted: {op.created_at_ms} UTC milliseconds\n"
                f"Original input: {op.payload_json}\nEvidence: {op.backend_ref_json or 'unavailable'}\n"
                f"Result: {op.result_json or 'unavailable'}\nReason: {op.error_reason or 'unavailable'}\n"
                f"Prior review: {op.review_json or 'none'}\n")
        if replace:
            kind, content, data, target = replacement(op)
            text += (f"NEW request: {kind}\nThread: {state.binding.codex_thread_id}\n"
                     f"Turn: {target or 'new turn'}\nInput: {content}\nOptions: {json.dumps(data, ensure_ascii=False)}\n"
                     f"Current input mode: {state.mode}; model: {state.snapshot.model}\n"
                     "Normal admission and current next-turn settings still apply. Files are read again at submission.\n"
                     "The original may already have executed. A NEW request can duplicate effects. No automatic retry.")
        else:
            text += ("Check the original client and actual effects. Acknowledgement releases only this record's block; "
                     "it leaves the outcome unknown and does not resend or change input ownership.")
        if len(text) > 24000:
            await self.web.post(text[:23000] + "\n[Preview too large; no action available. Review locally.]",
                                root=state.binding.slack_root_ts)
            return
        intent = Intent("replacement_confirm" if replace else "review_open", state.binding.binding_id, epoch,
                        binding_version=state.binding.binding_version, operation_id=op.op_id,
                        operation_fingerprint=expected, binding_root=state.binding.slack_root_ts)
        token = self.buttons.issue(intent)
        blocks = text_sections(text)
        button = {"type": "button", "action_id": "bridge_recovery", "value": token,
                  "text": {"type": "plain_text", "text": "Submit NEW request" if replace else "Review this block"}}
        if replace:
            button["confirm"] = {"title": {"type": "plain_text", "text": "New request, duplicate risk"},
                "text": {"type": "plain_text", "text": "The original may already have executed. Create one NEW request?"},
                "confirm": {"type": "plain_text", "text": "Submit new"}, "deny": {"type": "plain_text", "text": "Cancel"}}
        blocks.append({"type": "actions", "elements": [button]})
        intent.root = await self.web.post(text, root=state.binding.slack_root_ts, blocks=blocks)

    async def review_modal(self, state, intent, trigger_id):
        op = await self.controller.store.operation(intent.operation_id)
        await self.controller.store.check_unknown(op, state.binding, intent.operation_fingerprint)
        token = self.buttons.issue(Intent("review_submit", intent.binding_id, intent.epoch, root=intent.root,
            binding_version=intent.binding_version, operation_id=intent.operation_id,
            operation_fingerprint=intent.operation_fingerprint, binding_root=intent.binding_root))
        await self.web.call("views.open", write=True, trigger_id=trigger_id, view={
            "type": "modal", "callback_id": "bridge_review", "private_metadata": token,
            "title": {"type": "plain_text", "text": "Acknowledge unknown"},
            "submit": {"type": "plain_text", "text": "Release this block"},
            "blocks": [{"type": "section", "text": {"type": "plain_text", "text":
                f"Operation {op.op_id}\nOutcome stays unknown. This does not repeat the task or revive an approval."}},
                {"type": "input", "block_id": "risk", "label": {"type": "plain_text", "text": "Risk confirmation"},
                 "element": {"type": "static_select", "action_id": "choice", "options": [{
                     "text": {"type": "plain_text", "text": "I checked the original and accept the remaining uncertainty"},
                     "value": "acknowledge"}]}},
                {"type": "input", "block_id": "reason", "label": {"type": "plain_text", "text": "What did you check?"},
                 "element": {"type": "plain_text_input", "action_id": "text", "multiline": True, "max_length": 1000}}]})

    async def refresh_project_labels(self):
        self.project_labels = await asyncio.to_thread(project_labels, self.cfg)

    def project_label(self, path):
        candidate = Path(path)
        return next((name for root, name in self.project_labels.items() if Path(root) == candidate),
                    candidate.name or "Project")

    def chat_title(self, state):
        return (thread_names(self.cfg).get(state.binding.codex_thread_id)
                or (state.snapshot.thread.title if state.snapshot else state.known_title)
                or state.binding.codex_thread_id[:8])

    async def projects(self, page=0):
        await self.refresh_project_labels()
        activity = {}
        unavailable = False
        try:
            rows = await self.backend.list_threads()
            for thread in rows:
                if thread.allowed and thread.updated_at_ms:
                    path = Path(thread.cwd)
                    activity[path] = max(activity.get(path, 0), thread.updated_at_ms)
        except BridgeError:
            unavailable = True
        projects = sorted({str(p) for p in self.cfg.projects},
                          key=lambda p: (-activity.get(Path(p), 0), p.casefold()))
        blocks, issued = [], []
        for cwd in projects[page * 20:(page + 1) * 20]:
            intent = Intent("project", choice=cwd)
            issued.append(intent)
            token = self.buttons.issue(intent)
            blocks.append({"type": "section", "text": {"type": "plain_text", "text":
                Literal(self.project_label(cwd))},
                "accessory": {"type": "button", "text": {"type": "plain_text", "text": "View chats"},
                              "action_id": "bridge_project", "value": token}})
        if (page + 1) * 20 < len(projects):
            intent = Intent("projects_page", choice=str(page + 1))
            issued.append(intent)
            blocks.append({"type": "actions", "elements": [{"type": "button",
                "text": {"type": "plain_text", "text": "More projects"}, "action_id": "bridge_projects_next",
                "value": self.buttons.issue(intent)}]})
        heading = f"Choose a project. {len(projects)} projects; page {page + 1}."
        if unavailable:
            notice = "Activity unavailable; projects are listed by path order. Refresh to retry."
            language = preferences.language(self.cfg)
            notice = translate(notice, language)
            heading = Literal(translate(heading, language) + "\n" + notice)
            blocks = text_sections(notice) + blocks
        ts = await self.web.post(heading, blocks=blocks)
        for intent in issued:
            intent.root = ts

    async def tasks(self, query="", project="", page=0):
        await self.refresh_project_labels()
        if project:
            self.selected_project = project
        try:
            rows = ([await self.backend.get_thread(query)] if TURN.fullmatch(query)
                    else await self.backend.list_threads(project) if project else await self.backend.list_threads())
        except BridgeError as error:
            self.last_fault = error.code
            await self.web.post(f"Task query failed: {error.code}. No task submitted.")
            return
        rows = recent_threads(rows)
        rows = [t for t in rows if query.casefold() in (t.title + " " + t.id + " " + t.cwd).casefold()
                and (not project or Path(t.cwd) == Path(project))]
        total = len(rows)
        rows = rows[page * 20:(page + 1) * 20]
        blocks, issued = [], []
        for thread in rows:
            intent = Intent("bind", thread_id=thread.id)
            token = self.buttons.issue(intent)
            issued.append(intent)
            block = {"type": "section", "text": {"type": "plain_text", "text":
                     Literal(clean(f"{thread.title}\n{self.project_label(thread.cwd)}\n{thread.id}", 500))}}
            if thread.allowed:
                block["accessory"] = {"type": "button", "text": {"type": "plain_text", "text": "Open chat"},
                                      "action_id": "bridge_bind", "value": token}
            else:
                block["text"]["text"] = Literal(block["text"]["text"] + "\n" + translate(
                    "Project access not enabled. Enable this exact project before binding.", preferences.language(self.cfg)))
            blocks.append(block)
        navigation = []
        for kind, label, choice in [("projects", "Choose project", "")] + (
                [("tasks_page", "Next page", json.dumps({"query": query, "project": project, "page": page + 1}))]
                if (page + 1) * 20 < total else []):
            intent = Intent(kind, choice=choice)
            issued.append(intent)
            navigation.append({"type": "button", "text": {"type": "plain_text", "text": label},
                               "action_id": "bridge_" + kind, "value": self.buttons.issue(intent)})
        blocks.append({"type": "actions", "elements": navigation})
        language = preferences.language(self.cfg)
        heading = translate(f"Fresh non-archived chats: {total}; page {page + 1}.", language)
        heading += "\n" + translate("Project:", language) + " " + (
            self.project_label(project) if project else translate("all projects", language))
        heading += "\n" + translate("Use !tasks to refresh or !project to change project.", language)
        ts = await self.web.post(Literal(heading), blocks=blocks)
        for intent in issued:
            intent.root = ts

    async def skills_card(self, state, query="", page=0):
        rows = await self.backend.skills(state.binding.codex_thread_id)
        rows = [s for s in rows if query.casefold() in
                (s["name"] + " " + s["path"] + " " + skill_description(s)).casefold()]
        page = max(0, min(page, max(0, (len(rows) - 1) // 8)))
        body = f"Skills — {len(rows)} matches. Page {page + 1}. Filter: !skills TEXT."
        blocks = text_sections(body)
        for index, skill in enumerate(rows[page * 8:(page + 1) * 8], page * 8):
            peers = [r for r in rows if r["name"] == skill["name"]]
            label = skill["name"] + (f" [{peers.index(skill) + 1}/{len(peers)}]" if len(peers) > 1 else "")
            item = label + (" (disabled)" if not skill.get("enabled") else "") + "\n" + skill_description(skill)
            actions = []
            for kind, title in [("skill_use", "Use skill"), ("skill_details", "Details")]:
                if kind == "skill_use" and not skill.get("enabled"):
                    continue
                token = self.buttons.issue(Intent(kind, state.binding.binding_id, state.epoch,
                    choice=skill["path"], binding_version=state.binding.binding_version,
                    binding_root=state.binding.slack_root_ts, thread_id=str(index)))
                actions.append({"type": "button", "text": {"type": "plain_text", "text": title},
                                "action_id": "bridge_" + kind, "value": token})
            blocks += [{"type": "divider"}] + text_sections(item) + [{"type": "actions", "elements": actions}]
        pagination = []
        for target, title in [(page - 1, "Previous"), (page + 1, "Next")]:
            if 0 <= target < (len(rows) + 7) // 8:
                token = self.buttons.issue(Intent("skills_page", state.binding.binding_id, state.epoch,
                    choice=json.dumps({"query": query, "page": target}), binding_version=state.binding.binding_version,
                    binding_root=state.binding.slack_root_ts))
                pagination.append({"type": "button", "text": {"type": "plain_text", "text": title},
                                   "action_id": "bridge_skills_page_" + title.casefold(), "value": token})
        if pagination:
            blocks.append({"type": "actions", "elements": pagination})
        root = await self.web.post(body, root=state.binding.slack_root_ts, blocks=blocks)
        for token in [e["value"] for b in blocks if b["type"] == "actions" for e in b["elements"]]:
            self.buttons.tokens[token].root = root

    async def skill_details(self, state, path):
        rows = [s for s in await self.backend.skills(state.binding.codex_thread_id) if s["path"] == path]
        if len(rows) != 1:
            raise BridgeError("skill_changed_or_unavailable")
        skill = rows[0]
        body = f"{skill['name']}\nExact path: {path}\n{skill.get('description') or skill_description(skill)}"
        await self.web.post(body[:8000], root=state.binding.slack_root_ts, blocks=text_sections(body[:8000]))

    async def settings_card(self, state):
        text = ('Session settings. Write ordinary tasks directly in this reply thread. '
                'File retrieval: !artifacts or ask to send a known output file. '
                'Compact and Fork are explicit operations; input ownership and approvals still apply.')
        if getattr(self.backend, "desktop_only_controls", ()):
            text = ('Send tasks and retrieve files in this thread. For this connection, '
                    'use the Codex desktop for Steer, approvals, model/Skills settings, Compact and Fork.')
        elements = self.settings_controls(state)
        root = await self.web.post(text, root=state.binding.slack_root_ts,
            blocks=text_sections(text) + [{"type": "actions", "elements": elements}])
        for element in elements:
            self.buttons.tokens[element["value"]].binding_root = state.binding.slack_root_ts
            self.buttons.tokens[element["value"]].root = root

    def settings_controls(self, state):
        elements = []
        for kind, label in [("model_picker", "Model"), ("effort_picker", "Reasoning effort"),
                            ("skills", "Skills"), ("compact", "Compact"), ("fork", "Fork"),
                            ("artifacts", "Get output files")]:
            if kind in getattr(self.backend, "desktop_only_controls", ()):
                continue
            token = self.buttons.issue(Intent(kind, state.binding.binding_id, state.epoch,
                root=state.binding.slack_root_ts, binding_version=state.binding.binding_version))
            elements.append({"type": "button", "text": {"type": "plain_text", "text": label},
                             "action_id": "bridge_" + kind, "value": token})
        return elements

    async def prepare_interaction(self, payload, *, navigation_delivery=None):
        if (payload.get("team", {}).get("id") != self.cfg.team_id
                or payload.get("user", {}).get("id") != self.cfg.user_id
                or payload.get("api_app_id") != self.cfg.app_id):
            return None, None, None
        is_modal = payload.get("type") == "view_submission"
        if is_modal:
            if payload.get("view", {}).get("callback_id") not in {
                    "bridge_steer", "bridge_answer", "bridge_compose", "bridge_artifact", "bridge_review", "bridge_settings"}:
                return None, None, None
            token = payload.get("view", {}).get("private_metadata")
        else:
            if payload.get("type") != "block_actions" or len(payload.get("actions", [])) != 1:
                return None, None, None
            if payload.get("channel", {}).get("id") != self.cfg.dm_id:
                return None, None, None
            token = payload["actions"][0].get("value")
        if not isinstance(token, str):
            return None, None, None
        navigation_kind = None if is_modal else NAVIGATION_ACTIONS.get(payload["actions"][0].get("action_id"))
        stored = self.buttons.tokens.get(token)
        if stored and stored.kind == "output_confirm" and (is_modal or
                payload["actions"][0].get("action_id") != "bridge_output_confirm"):
            return None, None, None
        if stored and stored.kind in NAVIGATION_KINDS and not navigation_kind:
            return None, None, None
        intent = (self.buttons.navigation(token, navigation_kind, payload.get("container", {}).get("message_ts"))
                  if navigation_kind else self.buttons.take(token))
        if intent and navigation_kind and navigation_delivery is not None:
            action_ts = payload["actions"][0].get("action_ts")
            delivery = ("action:" + action_ts if isinstance(action_ts, str) and TS.fullmatch(action_ts)
                        else "envelope:" + navigation_delivery if isinstance(navigation_delivery, str) else "")
            if not delivery or len(delivery) > 200:
                return None, None, None
            current = time.monotonic()
            self.navigation_receipts = {key: expires for key, expires in self.navigation_receipts.items()
                                        if expires >= current}
            key = (token, delivery)
            if key in self.navigation_receipts or len(self.navigation_receipts) >= 4096:
                return None, None, None
            self.navigation_receipts[key] = intent.expires
        if not intent:
            if not is_modal and payload["actions"][0].get("action_id") in NAVIGATION_ACTIONS:
                root = payload.get("container", {}).get("message_ts")
                if isinstance(root, str) and TS.fullmatch(root) and root not in self.expired_navigation_roots:
                    self.expired_navigation_roots.append(root)
                    return None, None, self.expired_navigation()
            return None, None, None
        if is_modal and payload["view"].get("callback_id") != {
                "steer_submit": "bridge_steer", "answer_submit": "bridge_answer",
                "compose_submit": "bridge_compose", "artifact_submit": "bridge_artifact",
                "review_submit": "bridge_review", "model_submit": "bridge_settings",
                "effort_submit": "bridge_settings"}.get(intent.kind):
            return None, None, None
        if not is_modal and payload.get("container", {}).get("message_ts") != intent.root:
            return None, None, None
        if intent.kind == "bind":
            return None, None, self.open_chat(intent.thread_id)
        if intent.kind in {"projects", "projects_page", "project", "tasks_page"}:
            if intent.kind == "projects":
                after = self.projects()
            elif intent.kind == "projects_page":
                after = self.projects(int(intent.choice))
            elif intent.kind == "project":
                after = self.tasks(project=intent.choice)
            else:
                after = self.tasks(**json.loads(intent.choice))
            return None, None, after
        state = self.controller.states.get(intent.binding_id)
        if intent.kind == "output_confirm":
            if (not state or state.root_fault or state.binding.disabled or intent.epoch != state.epoch
                    or intent.binding_version != state.binding.binding_version
                    or intent.binding_root != state.binding.slack_root_ts):
                return None, None, None
            prepared = await self.controller.prepare(state, intent.operation_id, "artifact_upload", "",
                intent.epoch, now_ms(), data=json.loads(intent.choice))
            return state, prepared, None
        if intent.kind in {"skills_page", "skill_use", "skill_details"}:
            if (is_modal or not state or intent.epoch != state.epoch
                    or intent.binding_version != state.binding.binding_version
                    or intent.binding_root != state.binding.slack_root_ts or state.binding.disabled or state.root_fault):
                return None, None, None
            if intent.kind == "skills_page":
                return state, None, self.skills_card(state, **json.loads(intent.choice))
            if intent.kind == "skill_details":
                return state, None, self.skill_details(state, intent.choice)
            child = Intent("compose", intent.binding_id, intent.epoch, root=intent.binding_root,
                           choice=intent.choice, binding_version=intent.binding_version)
            return state, None, self.compose_modal(state, child, payload.get("trigger_id"))
        if intent.binding_root and intent.kind in {"model_picker", "effort_picker", "skills", "compact", "fork", "artifacts"}:
            if not state or intent.binding_root != state.binding.slack_root_ts or intent.binding_version != state.binding.binding_version:
                return None, None, None
            intent.root = intent.binding_root
        if intent.kind == "artifacts_open":
            if (is_modal or not state or intent.epoch != state.epoch
                    or intent.binding_version != state.binding.binding_version
                    or intent.binding_root != state.binding.slack_root_ts):
                return None, None, None
            child = Intent("artifacts", intent.binding_id, intent.epoch, turn_id=intent.turn_id,
                           root=intent.binding_root, binding_version=intent.binding_version)
            return state, None, self.artifact_modal(state, child, payload.get("trigger_id"))
        if intent.kind == "root_confirm":
            if (is_modal or not state or intent.epoch != state.epoch
                    or intent.binding_version != state.binding.binding_version
                    or intent.binding_root != state.binding.slack_root_ts
                    or intent.operation_fingerprint != fingerprint(state.binding)
                    or not state.snapshot or intent.choice != str(state.snapshot.generation)):
                return None, None, None
            prepared = await self.controller.prepare(state, "root:" + str(token), "root_replace", "",
                intent.epoch, now_ms(), data={"binding": asdict(state.binding)})
            return state, prepared, None
        if intent.kind in {"review_open", "review_submit", "replacement_confirm"}:
            if (not state or state.binding.disabled or intent.epoch != state.epoch
                    or intent.binding_version != state.binding.binding_version
                    or intent.binding_root != state.binding.slack_root_ts):
                return None, None, None
            try:
                if intent.kind == "review_open" and not is_modal:
                    return state, None, self.review_modal(state, intent, payload.get("trigger_id"))
                if intent.kind == "review_submit" and is_modal:
                    values = payload.get("view", {}).get("state", {}).get("values", {})
                    choice = values.get("risk", {}).get("choice", {}).get("selected_option") or {}
                    if choice.get("value") != "acknowledge":
                        raise BridgeError("explicit_risk_confirmation_required")
                    reason = values.get("reason", {}).get("text", {}).get("value") or ""
                    prepared = await self.controller.prepare(state, "review:" + str(token), "unknown_review", "",
                        intent.epoch, now_ms(), data={"review_op_id": intent.operation_id,
                            "fingerprint": intent.operation_fingerprint, "reason": reason})
                    return state, prepared, None
                if intent.kind == "replacement_confirm" and not is_modal:
                    prepared = await self.controller.prepare_replacement(state, "replacement:" + str(token),
                        intent.operation_id, intent.operation_fingerprint, intent.epoch, now_ms())
                    return state, prepared, None
            except BridgeError as error:
                return state, None, self.web.post(error.code, root=state.binding.slack_root_ts)
            return None, None, None
        if intent.kind in {"approval", "answer", "answer_submit"}:
            if not state or state.binding.binding_version != intent.binding_version:
                return None, None, None
            card = self.request_cards.get(intent.request_token)
            if not card or card.get("ts") != intent.root or card.get("binding_id") != intent.binding_id:
                return None, None, None
            try:
                request = self.backend.request(intent.request_token)
                if request.thread_id != state.binding.codex_thread_id or request.turn_id != intent.turn_id:
                    return None, None, None
                if intent.kind == "answer" and not is_modal:
                    return state, None, self.answer_modal(state, intent, payload.get("trigger_id"))
                data = {"request_token": request.token, "choice": intent.choice}
                if intent.kind == "answer_submit" and is_modal:
                    values = payload.get("view", {}).get("state", {}).get("values", {})
                    _, _, questions = self.backend.request_view(request.token)
                    answers = {}
                    for i, question in enumerate(questions):
                        value = values.get(f"q{i}", {}).get("answer", {})
                        selected = value.get("selected_option")
                        value = selected.get("value") if selected else value.get("value", "")
                        if request.message["method"] == FORM:
                            spec = request.params["requestedSchema"]["properties"][question["id"]]
                            if spec["type"] != "string":
                                value = json.loads(value)
                        answers[question["id"]] = value
                    data["answers"] = answers
                    data["choice"] = "answer"
                elif intent.kind != "approval" or is_modal:
                    return None, None, None
                response(request, self.backend.items, data["choice"], data.get("answers"))
                prepared = await self.controller.prepare(state, "approval:" + request.token, "approval", "",
                                                         state.epoch, now_ms(), intent.turn_id, data=data)
                return state, prepared, None
            except (BridgeError, ValueError, TypeError):
                return None, None, None
        if not state or intent.root != state.binding.slack_root_ts or intent.epoch != state.epoch:
            return None, None, None
        if intent.kind in {"model_picker", "effort_picker"} and not is_modal:
            return state, None, self.settings_modal(state, intent, payload.get("trigger_id"))
        if intent.kind in {"model_submit", "effort_submit"} and is_modal:
            if intent.binding_version != state.binding.binding_version:
                return None, None, None
            value = payload.get("view", {}).get("state", {}).get("values", {}).get("setting", {}).get("choice", {})
            choice = (value.get("selected_option") or {}).get("value", "")
            if not choice:
                return None, None, None
            kind = "model" if intent.kind == "model_submit" else "effort"
            prepared = await self.controller.prepare(state, "settings:" + str(token), kind, choice,
                intent.epoch, now_ms(), data={"source_model": intent.thread_id, "selected_model": intent.choice})
            return state, prepared, None
        if intent.kind in {"artifacts", "artifact_submit"}:
            if intent.binding_version != state.binding.binding_version or state.binding.disabled:
                return None, None, None
            if intent.kind == "artifacts" and not is_modal:
                return state, None, self.artifact_modal(state, intent, payload.get("trigger_id"))
            if intent.kind != "artifact_submit" or not is_modal:
                return None, None, None
            values = payload.get("view", {}).get("state", {}).get("values", {})
            path = (values.get("path", {}).get("text", {}).get("value") or "").strip()
            value = values.get("recent", {}).get("choice", {})
            selected = value.get("selected_options") or ([value["selected_option"]] if value.get("selected_option") else [])
            if bool(path) == bool(selected):
                return state, None, self.web.post("Choose output files OR enter one file path.", root=intent.root)
            paths = []
            if selected:
                if len(selected) > 20:
                    return None, None, None
                for item in selected:
                    choice = self.buttons.take(item.get("value"))
                    if (not choice or choice.kind != "artifact_choice" or choice.binding_id != intent.binding_id
                            or choice.epoch != intent.epoch or choice.root != intent.root
                            or choice.binding_version != intent.binding_version or choice.request_token != token
                            or choice.choice.casefold() in {p.casefold() for p in paths}):
                        return None, None, None
                    paths.append(choice.choice)
                path = paths[0]
            prepared = await self.controller.prepare(state, "button:" + str(token), "artifact_upload", "",
                intent.epoch, now_ms(), data={"paths": paths} if len(paths) > 1 else {"path": path})
            return state, prepared, None
        if intent.kind in {"slack", "desktop"}:
            return state, None, self.command(state, "!control " + intent.kind, intent.epoch)
        if intent.kind == "status":
            return state, None, self.command(state, "!status", intent.epoch)
        if intent.kind == "settings":
            return state, None, self.settings_card(state)
        if intent.kind == "entry":
            return state, None, self.chat_entry(state)
        if intent.kind in {"compact", "fork", "effort"}:
            prepared = await self.controller.prepare(state, "button:" + str(token), intent.kind,
                                                     intent.choice, intent.epoch, now_ms(),
                                                     data={"model": intent.thread_id} if intent.kind == "effort" else None)
            return state, prepared, None
        if intent.kind == "skills":
            return state, None, self.command(state, "!skills", intent.epoch)
        if intent.kind == "compose" and not is_modal:
            return state, None, self.compose_modal(state, intent, payload.get("trigger_id"))
        if intent.kind == "compose_submit" and is_modal:
            if state.binding.binding_id in self.pending_inputs:
                return state, None, self.web.post("This session has pending Slack attachments. Send your instruction "
                    "in this reply thread; use !skill EXACT_PATH -- task to include a chosen skill. "
                    "No task was submitted from this form.", root=state.binding.slack_root_ts)
            values = payload.get("view", {}).get("state", {}).get("values", {})
            text = values.get("task", {}).get("text", {}).get("value", "") or ""
            skill = intent.choice or values.get("skill", {}).get("text", {}).get("value", "") or ""
            files = values.get("files", {}).get("text", {}).get("value", "") or ""
            data = {}
            if skill.strip():
                data["skill_selector"] = skill.strip()
            if files.strip():
                try:
                    data["files"] = parse_files(files + " -- task")[0]
                except BridgeError:
                    return None, None, None
            prepared = await self.controller.prepare(state, "button:" + str(token), "turn_start", text,
                                                     intent.epoch, now_ms(), data=data)
            return state, prepared, None
        if intent.kind == "steer":
            return state, None, self.modal(state, intent, payload.get("trigger_id"))
        if intent.kind == "steer_submit" and is_modal:
            text = payload.get("view", {}).get("state", {}).get("values", {}).get("instruction", {}).get("text", {}).get("value", "")
            kind = "steer"
        elif intent.kind == "interrupt" and not is_modal:
            kind, text = "interrupt", ""
        else:
            return None, None, None
        prepared = await self.controller.prepare(state, "button:" + str(token), kind, text,
                                                 intent.epoch, now_ms(), intent.turn_id)
        return state, prepared, None

    async def settings_modal(self, state, intent, trigger_id):
        version = state.binding.binding_version
        def current():
            return (self.ready and self.controller.available and self.controller.healthy
                    and not state.root_fault and not state.binding.disabled
                    and intent.epoch == state.epoch and intent.root == state.binding.slack_root_ts
                    and state.binding.binding_version == version)
        if not current():
            raise BridgeError("settings_selector_expired")
        is_model = intent.kind == "model_picker"
        submission = Intent("settings_loading", state.binding.binding_id, intent.epoch,
            root=intent.root, binding_version=version, request_token=new_id())
        token = self.buttons.issue(submission)
        title = {"type": "plain_text", "text": "Select model" if is_model else "Reasoning effort"}
        # Exchange Slack's short-lived trigger before native snapshot/catalog reads.
        opened = await self.web.call("views.open", write=True, trigger_id=trigger_id, view={
            "type": "modal", "callback_id": "bridge_settings", "private_metadata": token,
            "title": title, "close": {"type": "plain_text", "text": "Close"},
            "blocks": text_sections("Loading current model options. No setting has been changed.")})
        target = opened.get("view", {})
        if not target.get("id") or not target.get("hash"):
            raise BridgeError("slack_modal_open_unconfirmed", unknown=True)
        await self.controller.refresh(state)
        if not current() or token not in self.buttons.tokens or not self.controller._fresh(state):
            self.buttons.tokens.pop(token, None)
            return
        source = state.snapshot.model
        setting = json.loads(state.binding.next_effort_json or "{}")
        selected = setting.get("model") if setting.get("state") == "pending" else source
        if is_model:
            choices = [row["model"] for row in await self.backend.models()
                       if not row.get("hidden") or row["model"] == source]
        else:
            choices = await self.backend.efforts(selected)
        if not choices or len(choices) > 100 or any(len(choice) > 150 for choice in choices):
            raise BridgeError("settings_picker_options_unavailable")
        if not current() or token not in self.buttons.tokens:
            self.buttons.tokens.pop(token, None)
            return
        submission.kind = "model_submit" if is_model else "effort_submit"
        submission.thread_id, submission.choice = source, selected
        await self.web.call("views.update", write=True, view_id=target["id"], hash=target["hash"], view={
            "type": "modal", "callback_id": "bridge_settings", "private_metadata": token,
            "title": title,
            "submit": {"type": "plain_text", "text": "Save for next task"},
            "blocks": text_sections(f"Selected model: {selected}. Changes apply to the next task only. "
                + ("Model selection uses its default effort; use Reasoning effort to refine it." if is_model else
                   "Only efforts supported by this model are offered.")) + [
                {"type": "input", "block_id": "setting", "label": {"type": "plain_text",
                    "text": "Model" if is_model else "Reasoning effort"}, "element": {
                    "type": "static_select", "action_id": "choice", "options": [
                        {"text": {"type": "plain_text", "text": choice}, "value": choice}
                        for choice in choices]}}]})

    async def answer_modal(self, state, intent, trigger_id):
        evidence, _, questions = self.backend.request_view(intent.request_token)
        token = self.buttons.issue(Intent("answer_submit", state.binding.binding_id,
                                          turn_id=intent.turn_id, root=intent.root,
                                          request_token=intent.request_token, binding_version=intent.binding_version))
        fields = []
        for i, question in enumerate(questions):
            element = {"type": "plain_text_input", "action_id": "answer"}
            if self.backend.request(intent.request_token).message["method"] == FORM:
                spec = self.backend.request(intent.request_token).params["requestedSchema"]["properties"][question["id"]]
                if spec["type"] == "boolean":
                    element = {"type": "static_select", "action_id": "answer", "options": [
                        {"text": {"type": "plain_text", "text": label}, "value": value}
                        for label, value in [("Yes", "true"), ("No", "false")]]}
            fields.append({"type": "input", "block_id": f"q{i}", "label": {"type": "plain_text", "text": clean(question["question"], 150)},
                           "element": element})
        await self.web.call("views.open", write=True, trigger_id=trigger_id, view={
            "type": "modal", "callback_id": "bridge_answer", "private_metadata": token,
            "title": {"type": "plain_text", "text": "Answer request"},
            "submit": {"type": "plain_text", "text": "Send once"},
            "blocks": text_sections(evidence) +
                      fields})

    async def compose_modal(self, state, intent, trigger_id):
        token = self.buttons.issue(Intent("compose_submit", state.binding.binding_id, intent.epoch, root=intent.root,
                                         choice=intent.choice, binding_version=state.binding.binding_version))
        fields = []
        for key, label in [("task", "Task"), ("skill", "Skill name or exact path (optional; see Skills)"),
                           ("files", 'Local file paths in quotes (optional; up to 5)')]:
            if key == "skill" and intent.choice:
                continue
            fields.append({"type": "input", "block_id": key, "optional": key != "task",
                           "label": {"type": "plain_text", "text": label},
                           "element": {"type": "plain_text_input", "action_id": "text", "multiline": key == "task"}})
        await self.web.call("views.open", write=True, trigger_id=trigger_id, view={
            "type": "modal", "callback_id": "bridge_compose", "private_metadata": token,
            "title": {"type": "plain_text", "text": "Use selected skill" if intent.choice else "Compose task"},
            "submit": {"type": "plain_text", "text": "Send once"}, "blocks": fields})

    async def render_requests(self, state):
        if not self.ready or not TS.fullmatch(state.binding.slack_root_ts):
            return
        pending = list(getattr(self.backend, "pending_requests", {}).values())
        # New actionable notices precede card refreshes and retirement updates.
        for request in sorted(pending, key=lambda r: r.token in self.request_cards):
            if request.thread_id != state.binding.codex_thread_id:
                continue
            try:
                text, choices, questions = self.backend.request_view(request.token)
            except BridgeError as error:
                if error.code == "approval_expired_or_connection_changed":
                    continue
                params = {key: value for key, value in request.params.items() if key != "_meta"}
                text = request.message["method"] + "\n" + json.dumps(params, ensure_ascii=False, indent=2)
                if len(text) > 16000:
                    text = text[:16000] + "\n[Transport preview truncated.]"
                text += "\nThis request's response format is not supported here. Complete it in the original client."
                choices, questions = {}, []
            card = self.request_cards.get(request.token)
            if card and not card.get("needs_refresh"):
                continue
            if card and not card.get("ts"):
                continue  # An unconfirmed post is never retried automatically.
            intents, elements = [], []
            root = card["ts"] if card else ""
            for index, choice in enumerate(list(choices) + (["answer"] if questions else [])):
                intent = Intent("answer" if choice == "answer" else "approval", state.binding.binding_id,
                                turn_id=request.turn_id, root=root, request_token=request.token,
                                choice=choice, binding_version=state.binding.binding_version)
                intents.append(intent)
                token = self.buttons.issue(intent)
                elements.append({"type": "button", "text": {"type": "plain_text", "text": choice},
                                 "action_id": "bridge_approval_" + str(index), "value": token})
            if choices or questions:
                text += "\nOne response only, expires in 10 minutes. Send is not proof of backend acceptance."
            else:
                text += "\nRead-only request preview; no remote reply controls."
            commands = [choice for choice in choices if choice in {"accept", "decline", "cancel", "deny"}]
            if "grant for this turn" in choices:
                commands.append("grant")
            if commands:
                text += f"\nCommand alternative in the chat root: !decision {request.token} " + "|".join(commands)
            if questions:
                example = {q["id"]: "your answer" for q in questions}
                text += f"\nAnswer alternative in the chat root: !answer {request.token} " + json.dumps(example)
            blocks = text_sections(text)
            if elements:
                blocks.append({"type": "actions", "elements": elements})
            self.request_cards[request.token] = {"binding_id": state.binding.binding_id, "ts": root,
                                                  "binding_version": state.binding.binding_version, "needs_refresh": True}
            try:
                if root:
                    await self.web.update(root, text, blocks)
                else:
                    root = await self.web.post(text, root=state.binding.slack_root_ts, blocks=blocks)
            except BridgeError as error:
                if not root and error.code == "slack_rate_limited":
                    # A definitive 429 did not create a card. Re-observe the still
                    # pending native request after cooldown; never resend a decision.
                    self.request_cards.pop(request.token, None)
                raise
            self.request_cards[request.token]["ts"] = root
            self.request_cards[request.token]["needs_refresh"] = False
            for intent in intents:
                intent.root = root
        for token, card in list(self.request_cards.items()):
            if card.get("binding_id") != state.binding.binding_id or not card.get("ts") or card.get("closed"):
                continue
            try:
                self.backend.request(token)
            except BridgeError:
                text = "Request closed, expired, sent without a receipt, or invalidated. No remote decision is available."
                try:
                    await self.web.update(card["ts"], text, text_sections(text))
                except BridgeError as error:
                    if error.code not in ROOT_ERRORS:
                        raise
                    card["delivery"] = "message_unavailable"
                card["closed"] = True

    async def finish_fork(self, state, op):
        if op.kind != "fork" or op.status != "accepted":
            raise BridgeError("fork_receipt_required")
        tid = json.loads(op.backend_ref_json or "{}").get("thread_id")
        if not tid:
            raise BridgeError("fork_receipt_required")
        snap = await self.backend.snapshot(tid)
        if (not snap.trusted or snap.thread.environment != state.binding.environment_key
                or snap.thread.cwd.casefold() != state.binding.project_path.casefold()):
            raise BridgeError("fork_environment_unverified")
        if hasattr(self.backend, "verify_fork"):
            await self.backend.verify_fork(state.binding.codex_thread_id, tid, json.loads(op.result_json or "{}"))
        await self.bind(tid)
        await self.controller.store.result(op, {**json.loads(op.result_json or "{}"),
                                                "fork_binding": "created_or_existing", "thread_id": tid,
                                                "gui_visibility": "requires_live_acceptance"})

    async def refresh_limits(self, *, force=False):
        if getattr(self.backend, "rate_limits_supported", True) and hasattr(self.backend, "refresh_limits"):
            try:
                await self.backend.refresh_limits(force=force)
                self.quota_fault = None
            except BridgeError as error:
                self.quota_fault = error.code  # Quota errors do not replace connection/navigation diagnostics.

    async def modal(self, state, intent, trigger_id):
        token = self.buttons.issue(Intent("steer_submit", state.binding.binding_id, intent.epoch,
                                          intent.turn_id, root=intent.root))
        await self.web.call("views.open", write=True, trigger_id=trigger_id, view={
            "type": "modal", "callback_id": "bridge_steer", "private_metadata": token,
            "title": {"type": "plain_text", "text": "Steer active turn"},
            "submit": {"type": "plain_text", "text": "Send"},
            "blocks": [{"type": "input", "block_id": "instruction", "label": {"type": "plain_text", "text": "Instruction"},
                        "element": {"type": "plain_text_input", "action_id": "text", "multiline": True}}]})

    async def expired_navigation(self):
        self.navigation.append({"attempt": new_id(), "thread_id": None, "stage": "validation",
                                "code": "chat_selection_expired", "observed_ms": now_ms()})
        await self.web.post("This selection expired or was already used. Send !tasks for a fresh chat list "
                            "or !project to select a project. No task was submitted.")

    async def open_chat(self, thread_id):
        attempt, stage = new_id(), "validation"

        def trace(value, code=None):
            nonlocal stage
            stage = value
            self.navigation.append({"attempt": attempt, "thread_id": thread_id, "stage": value,
                                    "code": code, "observed_ms": now_ms()})

        trace("validation")
        try:
            if not TURN.fullmatch(thread_id):
                raise BridgeError("thread_identity_mismatch")
            state = await self.bind(thread_id, trace=trace)
            if state:
                trace("entry")
                await self.chat_entry(state)
                trace("opened")
        except asyncio.CancelledError:
            trace(stage, "chat_open_cancelled")
            raise
        except Exception as error:
            code = error.code if isinstance(error, BridgeError) and error.code in NAVIGATION_ERRORS else "chat_open_failed"
            trace(stage, code)
            if code == "database_unavailable":
                self.controller.healthy = False
                self.fault(BridgeError(code))
            detail = ("The native session already has an active writer. Inspect which desktop/backend owns it; "
                      "do not force-resume or terminate another writer. " if code == "backend_thread_active_writer" else "")
            text = (f"Open chat did not complete. Chat: {thread_id}. Stage: {stage}. Code: {code}. "
                    f"Attempt: {attempt}. {detail}No task was submitted. "
                    "A control root may already exist; use !roots to inspect before retrying. "
                    "Use !status for recent navigation diagnostics.")
            try:
                await self.web.post(text)
            except BridgeError as notice:
                # A failed notification is not evidence that a native task ran.
                notice_code = notice.code if notice.code in NAVIGATION_ERRORS else "chat_open_failed"
                trace("feedback", notice_code)
                self.fault(notice)

    async def chat_entry(self, state):
        binding = state.binding
        root, version, opened = binding.slack_root_ts, binding.binding_version, self.opened_ms

        def guard():
            if (not self.ready or not self.controller.available or not self.controller.healthy
                    or self.opened_ms != opened or state.root_fault or state.binding.disabled
                    or state.binding.slack_root_ts != root or state.binding.binding_version != version):
                raise BridgeError("chat_connection_changed")

        guard()
        try:
            await self.refresh_view(state)
        except BridgeError as error:
            if error.code not in CHAT_STATE_UNAVAILABLE:
                raise
        guard()
        # A language change or restart must refresh a reused root before navigating to it.
        await self.render(state, force=True)
        guard()
        value = await self.web.call("chat.getPermalink", channel=self.cfg.dm_id, message_ts=root)
        url = value.get("permalink", "")
        parts = urlsplit(url)
        seconds, fraction = root.split(".")
        query = parse_qs(parts.query, keep_blank_values=True)
        expected_query = {"thread_ts": [root], "cid": [self.cfg.dm_id]}
        if (value.get("channel") != self.cfg.dm_id or parts.scheme != "https" or not parts.hostname
                or not parts.hostname.endswith(".slack.com") or parts.port not in {None, 443}
                or parts.username or parts.password or parts.fragment
                or (parts.query and query != expected_query)
                or parts.path != f"/archives/{self.cfg.dm_id}/p{seconds}{fraction.ljust(6, '0')}"):
            raise BridgeError("chat_entry_link_unverified")
        guard()
        url = parts._replace(query="").geturl() + f"?thread_ts={root}&cid={self.cfg.dm_id}"
        navigation_key = f"navigation:{binding.binding_id}:{version}:{root}"
        if not await self.controller.store.claim_input(navigation_key, binding, state.epoch):
            return
        # A durable intent covers both notices, including an uncertain Slack write.
        # Native replies remain the normal entry; the validated URL is only a fallback.
        await self.web.post("Session thread ready. After taking control, write your task here. No task started.", root=root)
        guard()
        title = self.chat_title(state)
        language = preferences.language(self.cfg)
        notice = "Open the session card's replies to continue. Web fallback may open a browser."
        if not self.controller._fresh(state) or state.syncing:
            notice += "\nLive status unavailable; open this chat in Codex desktop, then Refresh."
        text = Literal(title + "\n" + "\n".join(translate(line, language) for line in notice.split("\n")))
        await self.web.post(text, blocks=text_sections(text) + [{"type": "actions", "elements": [{
            "type": "button", "action_id": "bridge_chat_thread", "text": {"type": "plain_text", "text": "Open thread (web fallback)"},
            "url": url}]}])
        await self.controller.store.finish_input(navigation_key, "notified")

    async def bind(self, thread_id, *, trace=lambda stage, code=None: None):
        async with self.binding_lock:
            opened = self.opened_ms

            def guard():
                if (not self.ready or self.opened_ms != opened or not self.controller.available
                        or not self.controller.healthy):
                    raise BridgeError("chat_connection_changed")

            guard()
            previous = next((s for s in self.controller.states.values() if s.binding.codex_thread_id == thread_id), None)
            if previous:
                trace("existing_root")
                if previous.root_fault:
                    await self.web.post(f"Root unavailable. Use !root-replace {previous.binding.binding_id} to preview recovery.")
                elif TS.fullmatch(previous.binding.slack_root_ts):
                    if await self.check_root(previous):
                        guard()
                        return previous
                    await self.web.post(f"Root unavailable. Use !root-replace {previous.binding.binding_id} to preview recovery.")
                else:
                    await self.web.post("Root creation is unconfirmed; a second root will not be posted. Check local state.")
                trace("existing_root", "chat_root_unavailable")
                return
            trace("snapshot")
            snap = await self.backend.snapshot(thread_id)
            guard()
            if not snap.trusted:
                raise BridgeError("thread_not_trusted")
            trace("binding")
            binding = Binding(new_id(), self.cfg.team_id, self.cfg.dm_id, "pending:" + new_id(), self.cfg.user_id,
                              self.cfg.host_id, thread_id, snap.thread.cwd, snap.thread.environment)
            state = await self.controller.bind(binding)
            # Persist the root intent BEFORE posting. An ambiguous post leaves this binding pending,
            # so a process restart cannot post another root for the same chat.
            guard()
            trace("root_post")
            ts = await self.web.post("Bound Codex chat; desktop input is the default. Use this thread.")
            trace("root_attach")
            state.binding = await self.controller.store.attach_root(binding.binding_id, ts)
            trace("render")
            guard()
            await self.render(state, force=True)
            return state

    def card(self, state):
        s = state.snapshot
        online = self.ready and self.controller.healthy and self.controller._fresh(state) and not state.syncing
        pending = ("yes" if s.approval else "no") if online else "unknown"
        activity = ("compacting" if s.compacting or state.compaction_pending else
                    "waiting for approval" if s.approval else "working" if not s.idle else
                    "confirming completion" if state.active_turn else "idle") if online else "unknown"
        context = s.context if s else {}
        if context and context.get("generation") == s.generation:
            headline = (f"Context remaining: {100 * context['remaining'] / context['window']:.1f}% "
                        f"(~{context['remaining']:,} / {context['window']:,} tokens; estimate)")
        else:
            headline = "Context remaining: unknown (waiting for native usage telemetry)."
        title = self.chat_title(state)
        project_line = f"{self.project_label(state.binding.project_path)} / {title[:140]}"
        text = (headline + "\n"
                f"Model: {s.model if s and s.model else 'unconfirmed'} | Effort: {s.effort if s and s.effort else 'unconfirmed'}\n"
                f"{project_line}\n"
                f"Status: {'online' if online else 'syncing/offline'} | {activity}\n"
                f"Input: {state.mode} (GUI is not locked) | Approval pending: {pending}\n"
                "Reply in this thread to continue this session. Settings contains skills and file retrieval.")
        if state.compaction_pending or s and s.compacting:
            text += "\nContext compaction pending; new tasks are protected."
        if state.blocked:
            text += "\nUnknown / semantic mismatch: protected; inspect operation receipts."
        effort = json.loads(state.binding.next_effort_json or "{}")
        if effort:
            text += f"\nNext settings: {effort.get('model')} / {effort.get('value')} ({effort.get('state')})"
        if state.blocker_ids:
            text += "\nQuery blocking operations: " + ", ".join("!op " + opid for opid in state.blocker_ids[:3])
        if getattr(self.backend, "desktop_only_controls", ()):
            text += "\nFor this connection, use Codex desktop for Steer, approvals and model/Skills settings."
        if online and s.active_turn and "steer" in getattr(self.backend, "desktop_only_controls", ()):
            text += "\nSteer in Slack is unavailable: this connection cannot guarantee the original active turn. Use Codex desktop."
        if not online:
            text += "\nNo fresh native status. Open this chat in Codex desktop, then Refresh."
        elements = []
        for kind, label in [("desktop", "Return to desktop") if state.mode == "slack" else ("slack", "Take control"),
                            ("settings", "Settings"), ("status", "Refresh")]:
            token = self.buttons.issue(Intent(kind, state.binding.binding_id, state.epoch, root=state.binding.slack_root_ts))
            elements.append({"type": "button", "text": {"type": "plain_text", "text": label},
                             "action_id": "bridge_" + kind, "value": token})
        if online and s.active_turn:
            for kind, label in [("steer", "Steer current task"), ("interrupt", "Stop")]:
                if kind in getattr(self.backend, "desktop_only_controls", ()):
                    continue
                token = self.buttons.issue(Intent(kind, state.binding.binding_id, state.epoch, s.active_turn,
                                                  root=state.binding.slack_root_ts))
                elements.append({"type": "button", "text": {"type": "plain_text", "text": label},
                                 "action_id": "bridge_" + kind, "value": token})
                if kind == "interrupt":
                    elements[-1]["style"] = "danger"
        language = preferences.language(self.cfg)
        # Localize bridge lines independently; project/chat names remain literal.
        header, _, rest = text.partition("\n" + project_line + "\n")
        text = "\n".join([translate(header, language), project_line]
                         + [translate(line, language) for line in rest.split("\n")])
        blocks = text_sections(text)
        for block in blocks:
            block["text"]["text"] = Literal(block["text"]["text"])
        blocks.append(translated_blocks({"type": "actions", "elements": elements}, language))
        progress = (s.progress or self.progress.get(state.binding.binding_id, "")) if online and not s.idle else ""
        if progress:
            progress_text = translate("Progress:", language) + " " + progress[-600:]
            text += "\n" + progress_text
            progress_blocks = text_sections(progress_text)
            for block in progress_blocks:
                block["text"]["text"] = Literal(block["text"]["text"])
            blocks[-1:-1] = progress_blocks
        return Literal(text), blocks

    async def refresh_view(self, state):
        try:
            await self.controller.refresh(state)
        except BridgeError as error:
            if error.code in CHAT_STATE_UNAVAILABLE:
                if state.snapshot:
                    state.known_title = state.snapshot.thread.title
                if not state.syncing:
                    self.controller._suspend(state)
                    self.buttons.invalidate(state.binding.binding_id)
                state.snapshot = None
                self.progress.pop(state.binding.binding_id, None)
            raise

    async def render(self, state, *, force=False):
        if not self.ready or state.root_fault or not TS.fullmatch(state.binding.slack_root_ts):
            return
        text, blocks = self.card(state)
        now = time.monotonic()
        last, previous = self.last_render.get(state.binding.binding_id, (0, ""))
        # Observed timestamps are intentionally refreshed at the configured throttle.
        if not force and (now - last < self.cfg.root_interval or text == previous):
            return
        self.render_times = [clock for clock in self.render_times if now - clock < 60]
        if not force and len(self.render_times) >= self.cfg.update_budget:
            return
        self.render_times.append(now)
        try:
            await self.web.update(state.binding.slack_root_ts, text, blocks)
        except BridgeError as error:
            if error.code not in ROOT_ERRORS:
                raise
            self.root_lost(state, "root_missing")
            return
        self.last_render[state.binding.binding_id] = now, text

    async def tick(self, state):
        await self.refresh_limits()
        await self.refresh_view(state)
        if state.root_fault:
            return
        await self.render_requests(state)
        for op in await self.controller.completed(state):
            result = json.loads(op.result_json)
            result["delivery"] = "attempting"
            await self.controller.store.result(op, result)
            try:
                if op.kind == "compact":
                    status = result["terminal"]
                    messages = [f"Native context compaction {status} (event confirmed)."]
                else:
                    tid = json.loads(op.backend_ref_json)["turn_id"]
                    status, messages = await self.backend.final_messages(op.target_thread_id, tid)
                if op.kind == "compact" or not any(messages):
                    await self.web.post(f"Context compaction {status}." if op.kind == "compact" else f"Task {status}; no text reply.",
                                        root=state.binding.slack_root_ts)
                else:
                    for chunk in markdown_chunks("\n\n".join(messages)):
                        blocks = (text_sections(f"Task {status}.") if status != "completed" else [])
                        blocks += [{"type": "markdown", "text": chunk}]
                        await self.web.post(chunk,
                                            root=state.binding.slack_root_ts, blocks=blocks, verbatim=True)
                result["delivery"] = "confirmed"
            except asyncio.CancelledError:
                result["delivery"] = "unknown_no_automatic_retry"
                await asyncio.shield(self.controller.store.result(op, result))
                raise
            except BridgeError as error:
                if error.code in ROOT_ERRORS and not await self.check_root(state):
                    result["delivery"] = "root_unavailable_not_delivered"
                else:
                    self.fault(error)
                    result["delivery"] = ("rate_limited_not_delivered" if error.code == "slack_rate_limited"
                                          else "unknown_no_automatic_retry")
                result["delivery_error"] = error.code
            await self.controller.store.result(op, result)
            if (op.kind == "turn_start" and result.get("delivery") == "confirmed" and status == "completed"
                    and hasattr(self.backend, "artifacts")):
                result["files_delivery"] = "attempting"
                await self.controller.store.result(op, result)
                try:
                    found = await self.deliver_outputs(state, op, tid)
                    result["files_delivery"] = "confirmed" if found else "no_references"
                except asyncio.CancelledError:
                    result["files_delivery"] = "unknown_no_automatic_retry"
                    await asyncio.shield(self.controller.store.result(op, result))
                    raise
                except BridgeError as error:
                    result["files_delivery"] = "unknown_no_automatic_retry" if error.unknown else "unavailable_no_automatic_retry"
                    result["files_error"] = error.code
                except Exception:
                    result["files_delivery"] = "unknown_no_automatic_retry"
                    result["files_error"] = "artifact_list_unavailable"
                await self.controller.store.result(op, result)
        await self.render(state)
