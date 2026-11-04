"""Root recovery uses synthetic messages and temporary stores only."""

import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from slack_codex_bridge.controller import Controller, State
from slack_codex_bridge.model import BridgeError, Operation, new_id, now_ms
from slack_codex_bridge.recovery import fingerprint
from test_p2_approval import backend_request, interaction
from test_p3_recovery import acknowledge, unknown
from test_slack import FakeWeb, adapter, drain, message


class RootWeb(FakeWeb):
    def __init__(self, cfg, state):
        super().__init__()
        self.cfg, self.state = cfg, state
        self.roots = {}
        self.issued = []
        self.root_attempts = 0
        self.failure = None
        self.after_post = None

    async def call(self, method, **kwargs):
        assert method == "conversations.replies"
        return {"messages": self.roots.get(kwargs["ts"], [])}

    async def post(self, text, **kwargs):
        self.posts.append((text, kwargs))
        ts = f"200.{len(self.posts):06d}"
        self.issued.append(ts)
        if text.startswith("Replacement Codex root."):
            self.root_attempts += 1
            if self.failure:
                raise self.failure
            self.roots[ts] = [{"ts": ts, "bot_id": "BBOT123", "app_id": self.cfg.app_id}]
            if self.after_post:
                self.after_post()
        return ts

    async def update(self, root, *args):
        self.updates.append((root, *args))
        if root not in self.roots:
            raise BridgeError("slack_write_message_not_found")


def roots(c, s, backend):
    slack = adapter(c, backend)
    slack.web = RootWeb(c.cfg, s)
    return slack


async def preview(slack, state):
    await slack.command(None, "!root-replace " + state.binding.binding_id, "")
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "root_confirm")
    intent = slack.buttons.tokens[token]
    return token, interaction(slack.cfg, token, intent.root)


async def confirm(slack, state):
    token, payload = await preview(slack, state)
    req = SimpleNamespace(type="interactive", envelope_id=new_id(), payload=payload)
    await slack.receive(slack.socket, req)
    await drain(slack)
    return await slack.controller.store.by_source("root:" + token), req


async def test_lost_root_blocks_only_its_binding_and_keeps_authenticated_dm_usable(setup):
    c, s, backend, store = setup
    other = State(replace(s.binding, binding_id=new_id(), codex_thread_id=new_id(), slack_root_ts="101.000001"),
                  snapshot=replace(backend.current), mode="slack", syncing=False)
    c.states[other.binding.binding_id] = other
    await store.bind(other.binding)
    slack = roots(c, s, backend)
    slack.card(s)
    slack.card(other)
    old = s.epoch
    assert not await slack.check_root(s)
    await c.refresh(s)
    assert s.root_fault and s.mode == "suspended" and s.epoch != old
    assert other.mode == "slack" and not other.root_fault and slack.ready and c.available
    assert all(i.binding_id != s.binding.binding_id for i in slack.buttons.tokens.values())
    assert any(i.binding_id == other.binding.binding_id for i in slack.buttons.tokens.values())
    await slack.receive(slack.socket, message(c.cfg, s, text="Do not execute"))
    await drain(slack)
    await slack.command(None, "!roots", "")
    assert s.binding.binding_id in slack.web.posts[-1][0] and not backend.sent
    assert not await c.transfer(s, "slack", s.epoch)


@pytest.mark.parametrize("fault", ["missing", "other_app", "other_bot", "wrong_ts", "pending"])
async def test_unverified_roots_are_never_automatically_reposted(setup, fault):
    c, s, backend, _ = setup
    slack = roots(c, s, backend)
    row = {"ts": s.binding.slack_root_ts, "bot_id": slack.bot_id, "app_id": c.cfg.app_id}
    if fault == "pending":
        s.binding.slack_root_ts = "pending:fixture"
    elif fault == "other_app":
        row["app_id"] = "AOTHER"
    elif fault == "other_bot":
        row["bot_id"] = "BOTHER"
    elif fault == "wrong_ts":
        row["ts"] = "999.000001"
    if fault != "missing":
        slack.web.roots[s.binding.slack_root_ts] = [row]
    assert not await slack.check_root(s)
    await slack.tick(s)
    assert not slack.web.posts and not backend.sent and not slack.stop_error


async def test_accessible_root_cannot_be_replaced_just_by_requesting_preview(setup):
    c, s, backend, _ = setup
    slack = roots(c, s, backend)
    slack.web.roots[s.binding.slack_root_ts] = [{"ts": s.binding.slack_root_ts, "bot_id": slack.bot_id,
                                               "app_id": c.cfg.app_id}]
    await slack.command(None, "!root-replace " + s.binding.binding_id, "")
    assert not any(i.kind == "root_confirm" for i in slack.buttons.tokens.values())
    assert slack.web.root_attempts == 0 and s.binding.binding_version == 1


async def test_receive_journals_before_ack_then_rotates_once_and_defaults_to_desktop(setup):
    c, s, backend, store = setup
    assert await c.transfer(s, "slack", s.epoch)
    slack = roots(c, s, backend)
    original = replace(s.binding)
    token, payload = await preview(slack, s)

    async def at_ack():
        op = await store.by_source("root:" + token)
        assert op.status == "received" and s.binding.binding_version == 1
        assert not slack.web.root_attempts
    slack.socket.check = at_ack
    req = SimpleNamespace(type="interactive", envelope_id="fixture", payload=payload)
    await slack.receive(slack.socket, req)
    await drain(slack)
    saved = await store.by_source("root:" + token)
    assert saved.status == "accepted" and saved.binding_version == 1
    assert s.binding.binding_version == 2 and s.binding.slack_root_ts != original.slack_root_ts
    assert s.mode == "desktop" and not s.root_fault and c.by_root(original.slack_root_ts) is None
    assert json.loads(saved.payload_json)["data"]["binding"] == asdict(original)
    assert json.loads(saved.result_json)["root_ts"] == s.binding.slack_root_ts
    assert not backend.sent and slack.web.root_attempts == 1
    slack.socket.check = None
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert slack.web.root_attempts == 1


@pytest.mark.parametrize("fault", ["empty", "pending", "changed_read_error"])
async def test_periodic_root_checks_preserve_fresh_recovery_confirmation(setup, fault):
    c, s, backend, store = setup
    slack = roots(c, s, backend)
    if fault == "pending":
        s.binding.slack_root_ts = "pending:fixture"

        def pending_root():
            with store.db:
                store.db.execute("UPDATE bindings SET slack_root_ts=? WHERE binding_id=?",
                                 (s.binding.slack_root_ts, s.binding.binding_id))

        await store._call(pending_root)
    slack.card(s)
    old_controls = set(slack.buttons.tokens)
    assert not await slack.check_root(s)
    assert old_controls.isdisjoint(slack.buttons.tokens)
    token, payload = await preview(slack, s)
    epoch = s.epoch
    if fault == "changed_read_error":
        original_call = slack.web.call
        old_root = s.binding.slack_root_ts

        async def changed_call(method, **kwargs):
            if kwargs["ts"] == old_root:
                raise BridgeError("slack_read_message_not_found")
            return await original_call(method, **kwargs)

        slack.web.call = changed_call
    for _ in range(3):
        assert not await slack.check_root(s)
        await slack.tick(s)
        assert s.epoch == epoch and token in slack.buttons.tokens
    req = SimpleNamespace(type="interactive", envelope_id=new_id(), payload=payload)
    await slack.receive(slack.socket, req)
    await drain(slack)
    op = await store.by_source("root:" + token)
    assert op.status == "accepted" and s.binding.binding_version == 2
    assert s.mode == "desktop" and not s.root_fault
    assert slack.web.root_attempts == 1 and not backend.sent


async def test_disconnect_still_invalidates_fresh_root_recovery_confirmation(setup):
    c, s, backend, _ = setup
    slack = roots(c, s, backend)
    token, payload = await preview(slack, s)
    assert not await slack.check_root(s)
    assert token in slack.buttons.tokens
    await slack.tick(s)
    slack.lost()
    assert token not in slack.buttons.tokens
    assert await slack.prepare_interaction(payload) == (None, None, None)
    assert not slack.web.root_attempts and not backend.sent


@pytest.mark.parametrize("change", ["user", "team", "channel", "app", "card", "version", "epoch", "generation", "binding", "expiry"])
async def test_replacement_requires_exact_unexpired_preview_scope(setup, change):
    c, s, backend, _ = setup
    slack = roots(c, s, backend)
    token, payload = await preview(slack, s)
    if change in {"user", "team", "channel"}:
        payload[change]["id"] = "OTHER"
    elif change == "app":
        payload["api_app_id"] = "AOTHER"
    elif change == "card":
        payload["container"]["message_ts"] = "999.000001"
    elif change == "version":
        s.binding.binding_version += 1
    elif change == "epoch":
        s.epoch = "other"
    elif change == "generation":
        s.snapshot.generation += 1
    elif change == "expiry":
        slack.buttons.tokens[token].expires = 0
    else:
        s.binding.next_effort_json = "{}"
    assert await slack.prepare_interaction(payload) == (None, None, None)
    assert not slack.web.root_attempts and not backend.sent


async def test_failed_ack_never_rotates_or_posts_and_does_not_resume_after_restart(setup):
    c, s, backend, store = setup
    slack = roots(c, s, backend)
    token, payload = await preview(slack, s)
    slack.socket.fail = True
    await slack.receive(slack.socket, SimpleNamespace(type="interactive", envelope_id="lost", payload=payload))
    assert (await store.by_source("root:" + token)).status == "rejected"
    assert s.binding.binding_version == 1 and not slack.web.root_attempts
    restarted = Controller(c.cfg, backend, store)
    await restarted.load()
    await restarted.synchronized()
    recovered = restarted.states[s.binding.binding_id]
    other = roots(restarted, recovered, backend)
    await other.check_root(recovered)
    await other.tick(recovered)
    assert not other.web.root_attempts and not backend.sent


@pytest.mark.parametrize("stage", ["journal", "post_known", "post_unknown", "attach", "disconnect", "cancel"])
async def test_failures_preserve_rotation_evidence_without_an_automatic_second_post(setup, stage):
    c, s, backend, store = setup
    slack = roots(c, s, backend)
    old_binding = asdict(s.binding)
    if stage in {"journal", "attach"}:
        status = "sending" if stage == "journal" else "accepted"
        await store._call(lambda: store.db.execute(
            "CREATE TEMP TRIGGER fixture_failure BEFORE UPDATE OF status ON operations "
            f"WHEN OLD.kind='root_replace' AND NEW.status='{status}' BEGIN SELECT RAISE(ABORT,'fixture'); END"))
    elif stage.startswith("post_"):
        slack.web.failure = BridgeError("slack_write_unconfirmed" if stage == "post_unknown" else "slack_rate_limited",
                                       unknown=stage == "post_unknown")
    elif stage == "disconnect":
        slack.web.after_post = slack.lost
    else:
        slack.web.failure = asyncio.CancelledError()
    op, _ = await confirm(slack, s)
    assert op.status == ("rejected" if stage in {"journal", "post_known"} else "unknown")
    assert slack.web.root_attempts == (0 if stage == "journal" else 1)
    if stage == "journal":
        assert asdict(s.binding) == old_binding
    else:
        assert s.binding.slack_root_ts.startswith("pending:") and s.binding.binding_version == 2
        assert json.loads(op.result_json)["binding_rotated"]
    if stage in {"attach", "disconnect"}:
        assert json.loads(op.backend_ref_json)["root_ts"] in slack.web.roots
    # A restart retains the pending placeholder and no send queue.
    restarted = Controller(c.cfg, backend, store)
    await restarted.load()
    await restarted.synchronized()
    recovered = restarted.states[s.binding.binding_id]
    other = roots(restarted, recovered, backend)
    await other.check_root(recovered)
    await other.tick(recovered)
    assert not other.web.root_attempts and not backend.sent


async def test_second_explicit_replacement_can_recover_an_unconfirmed_post_and_old_unknown(setup):
    c, s, backend, store = setup
    original = await unknown(c, s, store)
    unchanged = asdict(original)
    slack = roots(c, s, backend)
    slack.web.failure = BridgeError("slack_write_unconfirmed", unknown=True)
    first, _ = await confirm(slack, s)
    slack.web.failure = None
    second, _ = await confirm(slack, s)
    assert first.status == "unknown" and second.status == "accepted" and s.binding.binding_version == 3
    assert asdict(await store.operation(original.op_id)) == unchanged
    assert s.blocked and not await c.transfer(s, "slack", s.epoch)
    await slack.recovery_card(s, original.op_id, replace=False, epoch=s.epoch)
    assert "Original binding version: 1; current: 3" in slack.web.posts[-1][0]
    source, review = await acknowledge(c, s, store, original)
    assert json.loads(source.review_json)["original_binding_version"] == 1
    assert source.status == "unknown" and not s.blocked and not backend.sent
    assert await c.transfer(s, "slack", s.epoch)
    prepared = await c.prepare_replacement(s, "explicit-new-task", source.op_id, fingerprint(source), s.epoch, now_ms())
    new = await c.execute(s, prepared)
    assert new.status == "accepted" and new.supersedes_op_id == source.op_id and new.binding_version == 3
    assert len(backend.sent) == 1 and (await store.operation(source.op_id)).status == "unknown"
    assert review.binding_version == 3 and (await store.operation(first.op_id)).status == "unknown"


@pytest.mark.parametrize("change", ["gap", "owner", "target", "root"])
async def test_unproven_version_or_identity_changes_cannot_transfer_unknown_review_access(setup, change):
    c, s, backend, store = setup
    original = await unknown(c, s, store)
    slack = roots(c, s, backend)
    root_op, _ = await confirm(slack, s)
    if change == "gap":
        await store._call(lambda: store.db.execute("UPDATE operations SET result_json=NULL WHERE op_id=?", (root_op.op_id,)))
    elif change == "root":
        s.binding.slack_root_ts = "999.000001"
    else:
        payload = json.loads(root_op.payload_json)
        payload["data"]["binding"]["owner_user_id" if change == "owner" else "codex_thread_id"] = "OTHER"
        await store._call(lambda: store.db.execute("UPDATE operations SET payload_json=? WHERE op_id=?",
                                                  (json.dumps(payload), root_op.op_id)))
    with pytest.raises(BridgeError, match="root_lineage_unverified"):
        await store.check_unknown(original, s.binding, fingerprint(original))
    assert (await store.operation(original.op_id)).unknown_review_state == "pending" and not backend.sent


async def test_root_rotation_does_not_cancel_active_native_turn_and_clears_pending_effort(setup):
    c, s, backend, store = setup
    turn = new_id()
    backend.current = replace(backend.current, active_turn=turn, idle=False)
    s.binding = await store.effort(s.binding, {"value": "high", "state": "pending", "sent_ms": now_ms(), "op_id": new_id()})
    slack = roots(c, s, backend)
    op, _ = await confirm(slack, s)
    assert op.status == "accepted" and s.active_turn == turn and not s.binding.next_effort_json
    assert backend.current.active_turn == turn and not backend.sent and s.mode == "desktop"


async def test_binding_rotation_invalidates_native_nonces_only_for_affected_thread(cfg):
    backend, original, wire = backend_request(cfg)
    backend._notify({"id": 8, "method": original.message["method"],
                     "params": {**original.params, "threadId": "other-thread"}}, 1)
    other = next(r for r in backend.pending_requests.values() if r.thread_id == "other-thread")
    backend.invalidate_requests(original.thread_id)
    with pytest.raises(BridgeError):
        backend.request(original.token)
    assert backend.request(other.token) is other and not wire.sent


async def test_root_update_not_found_is_isolated_without_stopping_other_chats(setup):
    c, s, backend, _ = setup
    slack = roots(c, s, backend)
    assert await c.transfer(s, "slack", s.epoch)
    await slack.render(s, force=True)
    assert s.root_fault == "root_missing" and slack.ready and c.available and not slack.stop_error
    assert not c._fresh(s) and not backend.sent


@pytest.mark.parametrize("stage", ["upload", "notice"])
@pytest.mark.parametrize("code", ["slack_write_thread_not_found", "slack_write_message_not_found"])
async def test_artifact_root_loss_is_local_and_keeps_recorded_outcome(setup, stage, code):
    c, s, backend, store = setup
    other = State(replace(s.binding, binding_id=new_id(), codex_thread_id=new_id(), slack_root_ts="101.000001"),
                  snapshot=replace(backend.current), mode="slack", syncing=False)
    c.states[other.binding.binding_id] = other
    await store.bind(other.binding)
    (c.cfg.projects[0] / "report.txt").write_text("fixture")
    slack = roots(c, s, backend)
    attempts = []

    async def upload(artifact, *, root, guard, checkpoint):
        attempts.append(root)
        if stage == "upload":
            raise BridgeError(code)
        return "F123ABC"

    async def post(text, **kwargs):
        raise BridgeError(code)

    slack.web.upload, slack.web.post = upload, post
    await slack.receive(slack.socket, message(c.cfg, s, '!artifact "report.txt"'))
    await drain(slack)
    await asyncio.sleep(0)  # Observe the completed task's fault callback too.
    assert slack.ready and not slack.stop_error and c.available and c.healthy
    if stage == "notice":
        assert not s.root_fault  # Successful uploads no longer post a diagnostic receipt.
    else:
        assert s.root_fault and s.mode == "suspended"
    assert not s.reservations
    assert other.mode == "slack" and not other.root_fault
    op = (await store.uploads(s.binding.binding_id))[0]
    assert op.status == ("accepted" if stage == "notice" else "rejected")
    assert attempts == [s.binding.slack_root_ts] and not backend.sent


@pytest.mark.parametrize("stage", ["upload", "notice", "command"])
async def test_artifact_auth_failure_still_stops_bridge_when_root_is_missing(setup, stage):
    c, s, backend, _ = setup
    (c.cfg.projects[0] / "report.txt").write_text("fixture")
    slack = roots(c, s, backend)

    async def upload(artifact, **kwargs):
        if stage == "upload":
            raise BridgeError("slack_write_token_revoked")
        return "F123ABC"

    async def post(text, **kwargs):
        raise BridgeError("slack_write_token_revoked")

    slack.web.upload, slack.web.post = upload, post
    text = '!artifacts' if stage == "command" else '!artifact "report.txt"'
    await slack.receive(slack.socket, message(c.cfg, s, text))
    await drain(slack)
    await asyncio.sleep(0)
    if stage == "notice":
        assert slack.ready and not slack.stop_error and c.available
    else:
        assert not slack.ready and slack.stop_error.code == "slack_write_token_revoked"
        assert not c.available
    assert not backend.sent


@pytest.mark.parametrize("code", ["slack_write_thread_not_found", "slack_write_message_not_found"])
async def test_artifact_list_command_root_loss_is_local(setup, code):
    c, s, backend, _ = setup
    slack = roots(c, s, backend)

    async def post(text, **kwargs):
        raise BridgeError(code)

    slack.web.post = post
    await slack.receive(slack.socket, message(c.cfg, s, '!artifacts'))
    await drain(slack)
    await asyncio.sleep(0)
    assert slack.ready and not slack.stop_error and c.available and c.healthy
    assert s.root_fault and s.mode == "suspended" and not backend.sent


@pytest.mark.parametrize("stage", ["before_send", "after_post"])
async def test_native_cwd_change_refuses_attachment_without_changing_target(setup, stage):
    c, s, backend, store = setup
    slack = roots(c, s, backend)
    moved = c.cfg.projects[0] / "moved"
    moved.mkdir()

    def move():
        backend.current.thread = replace(backend.current.thread, cwd=str(moved))

    token, payload = await preview(slack, s)
    if stage == "before_send":
        move()
    else:
        slack.web.after_post = move
    await slack.receive(slack.socket, SimpleNamespace(type="interactive", envelope_id=new_id(), payload=payload))
    await drain(slack)
    op = await store.by_source("root:" + token)
    assert op.status == ("rejected" if stage == "before_send" else "unknown")
    assert op.error_reason == "root_replacement_target_changed"
    assert s.binding.project_path == str(c.cfg.projects[0]) and not backend.sent
    assert slack.web.root_attempts == (0 if stage == "before_send" else 1)


async def test_database_active_submission_blocks_rotation_even_without_memory_reservation(setup):
    c, s, backend, store = setup
    slack = roots(c, s, backend)
    token, payload = await preview(slack, s)
    busy = Operation(new_id(), new_id(), s.binding.binding_id, 1, "turn_start", s.binding.codex_thread_id)
    await store.record_once(busy)
    await slack.receive(slack.socket, SimpleNamespace(type="interactive", envelope_id=new_id(), payload=payload))
    await drain(slack)
    op = await store.by_source("root:" + token)
    assert op.status == "rejected" and op.error_reason == "root_replacement_submission_in_progress"
    assert s.binding.binding_version == 1 and not slack.web.root_attempts and not backend.sent


async def test_backend_generation_change_between_preview_and_send_never_posts(setup):
    c, s, backend, store = setup
    slack = roots(c, s, backend)
    token, payload = await preview(slack, s)
    backend.current.generation += 1
    await slack.receive(slack.socket, SimpleNamespace(type="interactive", envelope_id=new_id(), payload=payload))
    await drain(slack)
    op = await store.by_source("root:" + token)
    assert op.status == "rejected" and s.binding.binding_version == 1
    assert not slack.web.root_attempts and not backend.sent


async def test_roots_command_reports_pending_replacement_identity_and_receipt(setup):
    c, s, backend, _ = setup
    slack = roots(c, s, backend)
    slack.web.after_post = slack.lost
    op, _ = await confirm(slack, s)
    await slack.command(None, "!roots", "")
    text = slack.web.posts[-1][0]
    assert op.op_id in text and "unknown" in text and "post_ack" in text


async def test_old_unknown_review_requires_a_bounded_rotation_history(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    s.binding.binding_version = 34
    with pytest.raises(BridgeError, match="root_lineage_unverified"):
        await store.check_unknown(old, s.binding, fingerprint(old))
    assert not backend.sent


async def test_untrusted_readback_after_post_keeps_new_root_unattached(setup):
    c, s, backend, _ = setup
    slack = roots(c, s, backend)
    slack.web.after_post = lambda: setattr(backend.current, "trusted", False)
    op, _ = await confirm(slack, s)
    assert op.status == "unknown" and s.binding.slack_root_ts.startswith("pending:")
    assert s.root_fault and not c._fresh(s) and not backend.sent


async def test_lost_root_does_not_remove_shared_workspace_busy_protection(setup):
    c, s, backend, _ = setup
    backend.current.idle = False
    await c.refresh(s)
    slack = roots(c, s, backend)
    await slack.check_root(s)
    other = State(replace(s.binding, binding_id=new_id(), codex_thread_id=new_id(), slack_root_ts="101.000001"),
                  snapshot=replace(backend.current, idle=True), mode="slack", syncing=False)
    c.states[other.binding.binding_id] = other
    assert c._reason(other, "turn_start", None, other.epoch) == "shared_directory_busy_or_untrusted"
    assert not backend.sent


async def test_rejected_unrotated_attempt_does_not_break_valid_unknown_review_lineage(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    slack = roots(c, s, backend)
    await slack.check_root(s)
    s.reservations["fixture"] = (s.epoch, 1, "turn_start")
    rejected = await c.prepare(s, "refused-root", "root_replace", "", s.epoch, now_ms())
    s.reservations.pop("fixture")
    assert rejected.operation.status == "rejected" and rejected.operation.payload_json == "{}"
    accepted, _ = await confirm(slack, s)
    assert accepted.status == "accepted"
    assert (await store.check_unknown(old, s.binding, fingerprint(old))).op_id == old.op_id
    assert not backend.sent
