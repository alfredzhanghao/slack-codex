import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from slack_codex_bridge.controller import Controller
from slack_codex_bridge.model import BridgeError, Operation, new_id, now_ms
from slack_codex_bridge.recovery import fingerprint
from test_p2_approval import interaction
from test_p3_artifacts import enable_upload
from test_p2_operations import add_p2_backend
from test_slack import adapter, drain


async def unknown(c, s, store, *, kind="turn_start", target=None, text="Original task", data=None):
    op = Operation(new_id(), new_id(), s.binding.binding_id, s.binding.binding_version, kind,
                   s.binding.codex_thread_id, target, json.dumps({"text": text, "data": data or {}, "sent_ms": now_ms()}),
                   status="unknown", error_reason="fixture_lost_receipt", unknown_review_state="pending")
    await store.record_once(op)
    await c.refresh(s)
    return op


async def acknowledge(c, s, store, op):
    prepared = await c.prepare(s, new_id(), "unknown_review", "", s.epoch, now_ms(),
        data={"review_op_id": op.op_id, "fingerprint": fingerprint(op), "reason": "Checked original password-policy task"})
    assert prepared.execute
    result = await c.execute(s, prepared)
    assert result.status == "accepted"
    return await store.operation(op.op_id), result


async def test_risk_acknowledgement_keeps_original_unknown_and_does_not_take_control(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    current, review = await acknowledge(c, s, store, old)
    assert current.status == "unknown" and current.unknown_review_state == "acknowledged"
    assert (current.payload_json, current.error_reason, current.backend_ref_json) == (
        old.payload_json, old.error_reason, old.backend_ref_json)
    audit = json.loads(current.review_json)
    assert audit["actor"] == c.cfg.user_id and audit["review_op_id"] == review.op_id
    assert "password-policy" in audit["reason"]
    assert not backend.sent and s.mode == "desktop" and not s.blocked
    assert not await c.completed(s)


async def test_review_releases_only_one_block_and_survives_restart(setup):
    c, s, backend, store = setup
    one = await unknown(c, s, store)
    two = await unknown(c, s, store)
    await acknowledge(c, s, store, one)
    assert s.blocked and s.blocker_ids == (two.op_id,)
    restarted = Controller(c.cfg, backend, store)
    await restarted.load()
    await restarted.synchronized()
    state = restarted.states[s.binding.binding_id]
    assert state.mode == "desktop" and state.blocker_ids == (two.op_id,)
    assert (await store.operation(one.op_id)).unknown_review_state == "acknowledged"
    assert not backend.sent


async def test_atomic_review_rolls_back_if_audit_update_fails(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    await store._call(lambda: store.db.execute("CREATE TEMP TRIGGER fixture_failure BEFORE UPDATE OF status ON operations "
        "WHEN OLD.kind='unknown_review' AND NEW.status='accepted' BEGIN SELECT RAISE(ABORT,'fixture'); END"))
    prepared = await c.prepare(s, new_id(), "unknown_review", "", s.epoch, now_ms(),
        data={"review_op_id": old.op_id, "fingerprint": fingerprint(old), "reason": "checked"})
    result = await c.execute(s, prepared)
    assert result.status == "unknown" and not c.healthy
    saved = await store.operation(old.op_id)
    assert saved.unknown_review_state == "pending" and saved.review_json is None
    assert not backend.sent


@pytest.mark.parametrize("change", ["outcome", "evidence", "version", "epoch", "generation"])
async def test_review_cannot_apply_to_changed_operation_or_connection(setup, change):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    prepared = await c.prepare(s, new_id(), "unknown_review", "", s.epoch, now_ms(),
        data={"review_op_id": old.op_id, "fingerprint": fingerprint(old), "reason": "checked"})
    if change == "outcome":
        await store.transition(old.op_id, "unknown", "accepted")
    elif change == "evidence":
        await store._call(lambda: store.db.execute("UPDATE operations SET backend_ref_json='{}' WHERE op_id=?", (old.op_id,)))
    elif change == "version":
        s.binding.binding_version += 1
    elif change == "epoch":
        c.invalidate()
        await c.synchronized()
    else:
        backend.current.generation += 1
    result = await c.execute(s, prepared)
    assert result.status == "rejected"
    assert (await store.operation(old.op_id)).review_json is None and not backend.sent


async def test_review_rechecks_binding_row_in_atomic_transaction(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    prepared = await c.prepare(s, new_id(), "unknown_review", "", s.epoch, now_ms(),
        data={"review_op_id": old.op_id, "fingerprint": fingerprint(old), "reason": "checked"})
    await store._call(lambda: store.db.execute("UPDATE bindings SET binding_version=binding_version+1 WHERE binding_id=?",
                                              (s.binding.binding_id,)))
    result = await c.execute(s, prepared)
    assert result.status == "rejected" and (await store.operation(old.op_id)).review_json is None
    assert not backend.sent


async def test_new_submission_has_new_identity_and_keeps_original_record(setup):
    c, s, backend, store = setup
    old, _ = await acknowledge(c, s, store, await unknown(c, s, store))
    retained = asdict(old)
    assert await c.transfer(s, "slack", s.epoch)
    key = new_id()
    prepared = await c.prepare_replacement(s, key, old.op_id, fingerprint(old), s.epoch, now_ms())
    assert prepared.execute and prepared.operation.op_id != old.op_id
    result = await c.execute(s, prepared)
    assert result.status == "accepted" and result.supersedes_op_id == old.op_id
    assert len(backend.sent) == 1 and backend.sent[0].text == "Original task"
    duplicate = await c.prepare_replacement(s, key, old.op_id, fingerprint(old), s.epoch, now_ms())
    assert not duplicate.execute
    await c.execute(s, duplicate)
    assert len(backend.sent) == 1 and asdict(await store.operation(old.op_id)) == retained


async def test_replacement_requires_review_then_normal_input_handoff(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    with pytest.raises(BridgeError, match="replacement_requires_acknowledged_unknown"):
        await c.prepare_replacement(s, new_id(), old.op_id, fingerprint(old), s.epoch, now_ms())
    old, _ = await acknowledge(c, s, store, old)
    prepared = await c.prepare_replacement(s, new_id(), old.op_id, fingerprint(old), s.epoch, now_ms())
    assert not prepared.execute and prepared.operation.error_reason in {"desktop_input_mode", "input_precedes_opening"}
    assert not backend.sent


async def test_parent_acceptance_before_send_prevents_replacement(setup):
    c, s, backend, store = setup
    old, _ = await acknowledge(c, s, store, await unknown(c, s, store))
    assert await c.transfer(s, "slack", s.epoch)
    prepared = await c.prepare_replacement(s, new_id(), old.op_id, fingerprint(old), s.epoch, now_ms())
    await store.transition(old.op_id, "unknown", "accepted")
    result = await c.execute(s, prepared)
    assert result.status == "rejected" and not backend.sent


async def test_store_rechecks_parent_atomically_at_sending_boundary(setup):
    c, s, _, store = setup
    old, _ = await acknowledge(c, s, store, await unknown(c, s, store))
    assert await c.transfer(s, "slack", s.epoch)
    prepared = await c.prepare_replacement(s, new_id(), old.op_id, fingerprint(old), s.epoch, now_ms())
    await store.transition(old.op_id, "unknown", "accepted")
    with pytest.raises(BridgeError, match="operation_changed_refresh_review"):
        await store.transition(prepared.operation.op_id, "received", "sending")
    assert (await store.operation(prepared.operation.op_id)).status == "received"


@pytest.mark.parametrize("kind", ["approval", "compact", "effort", "unknown_review"])
async def test_old_native_controls_cannot_be_replayed(setup, kind):
    c, s, backend, store = setup
    old, _ = await acknowledge(c, s, store, await unknown(c, s, store, kind=kind))
    with pytest.raises(BridgeError, match="use_fresh_native_control"):
        await c.prepare_replacement(s, new_id(), old.op_id, fingerprint(old), s.epoch, now_ms())
    assert not backend.sent


@pytest.mark.parametrize("kind", ["steer", "interrupt"])
async def test_replacement_never_retargets_a_new_active_turn(setup, kind):
    c, s, backend, store = setup
    old, _ = await acknowledge(c, s, store, await unknown(c, s, store, kind=kind, target=new_id()))
    backend.current.active_turn, backend.current.idle = new_id(), False
    await c.refresh(s)
    prepared = await c.prepare_replacement(s, new_id(), old.op_id, fingerprint(old), s.epoch, now_ms())
    assert prepared.operation.error_reason == "target_turn_changed" and not prepared.execute
    assert not backend.sent


async def test_acknowledgement_does_not_clear_native_approval_or_compaction(setup):
    c, s, backend, store = setup
    backend.current.approval, backend.current.compacting = True, True
    old = await unknown(c, s, store)
    await acknowledge(c, s, store, old)
    assert not s.blocked and backend.current.approval and backend.current.compacting
    assert not await c.transfer(s, "slack", s.epoch)


async def open_review(slack, c, s, op):
    await slack.command(s, "!review " + op.op_id, s.epoch)
    token = slack.web.posts[-1][1]["blocks"][-1]["elements"][0]["value"]
    views = []

    async def call(method, **kwargs):
        views.append(kwargs["view"])
        return {}
    slack.web.call = call
    _, _, after = await slack.prepare_interaction(interaction(c.cfg, token, "200.000001"))
    await after
    return views[0]


def review_submission(c, view):
    return SimpleNamespace(type="interactive", envelope_id=new_id(), payload={
        "type": "view_submission", "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
        "api_app_id": c.cfg.app_id, "view": {"callback_id": "bridge_review", "private_metadata": view["private_metadata"],
        "state": {"values": {"risk": {"choice": {"selected_option": {"value": "acknowledge"}}},
                             "reason": {"text": {"value": "Checked original task"}}}}}})


async def test_slack_review_is_durable_before_ack_and_one_use_after_ack(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    slack = adapter(c, backend)
    view = await open_review(slack, c, s, old)
    key = "review:" + view["private_metadata"]

    async def check():
        assert (await store.by_source(key)).status == "received"
        assert (await store.operation(old.op_id)).unknown_review_state == "pending"
        assert not backend.sent
    slack.socket.check = check
    req = review_submission(c, view)
    await slack.receive(slack.socket, req)
    await drain(slack)
    reviewed = await store.operation(old.op_id)
    assert reviewed.unknown_review_state == "acknowledged" and reviewed.status == "unknown"
    slack.socket.check = None
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert asdict(await store.operation(old.op_id)) == asdict(reviewed)
    assert (await store.by_source(key)).status == "accepted" and not backend.sent


async def test_failed_slack_ack_never_releases_original_block(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    slack = adapter(c, backend)
    view = await open_review(slack, c, s, old)
    slack.socket.fail = True
    await slack.receive(slack.socket, review_submission(c, view))
    await drain(slack)
    assert (await store.operation(old.op_id)).unknown_review_state == "pending"
    assert (await store.by_source("review:" + view["private_metadata"])).status == "rejected"
    assert not backend.sent


@pytest.mark.parametrize("change", ["user", "team", "app", "epoch", "version", "root", "callback", "risk"])
async def test_review_modal_cannot_cross_scope_or_skip_confirmation(setup, change):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    slack = adapter(c, backend)
    view = await open_review(slack, c, s, old)
    req = review_submission(c, view)
    if change in {"user", "team"}:
        req.payload[change]["id"] = "OTHER"
    elif change == "app":
        req.payload["api_app_id"] = "OTHER"
    elif change == "epoch":
        s.epoch = new_id()
    elif change == "version":
        s.binding.binding_version += 1
    elif change == "root":
        s.binding.slack_root_ts = "300.000001"
    elif change == "callback":
        req.payload["view"]["callback_id"] = "bridge_compose"
    else:
        req.payload["view"]["state"]["values"]["risk"] = {}
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert (await store.operation(old.op_id)).unknown_review_state == "pending" and not backend.sent


async def test_large_review_preview_is_read_only(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store, text="x" * 25000)
    slack = adapter(c, backend)
    await slack.command(s, "!review " + old.op_id, s.epoch)
    assert "no action" in slack.web.posts[-1][0] and not slack.buttons.tokens


async def test_explicit_artifact_replacement_reads_current_bytes_and_links_original(setup):
    c, s, backend, store = setup
    path = c.cfg.projects[0] / "report.txt"
    path.write_text("current contents")
    old = await unknown(c, s, store, kind="artifact_upload", text="", data={"path": str(path)})
    old, _ = await acknowledge(c, s, store, old)
    retained = asdict(old)
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    await slack.command(s, "!replace " + old.op_id, s.epoch)
    token = slack.web.posts[-1][1]["blocks"][-1]["elements"][0]["value"]
    req = SimpleNamespace(type="interactive", envelope_id=new_id(), payload=interaction(c.cfg, token, "200.000001"))
    await slack.receive(slack.socket, req)
    await drain(slack)
    await slack.receive(slack.socket, req)
    await drain(slack)
    child = await store.by_source("replacement:" + token)
    assert child.status == "accepted" and child.supersedes_op_id == old.op_id
    assert len(sent) == 1 and sent[0][0].content == path.read_bytes() and not backend.sent
    assert asdict(await store.operation(old.op_id)) == retained


async def test_unknown_replacement_is_not_replayed_on_restart(setup):
    c, s, backend, store = setup
    old, _ = await acknowledge(c, s, store, await unknown(c, s, store))
    assert await c.transfer(s, "slack", s.epoch)
    backend.send_error = BridgeError("fixture_timeout", unknown=True)
    prepared = await c.prepare_replacement(s, new_id(), old.op_id, fingerprint(old), s.epoch, now_ms())
    result = await c.execute(s, prepared)
    assert result.status == "unknown" and result.supersedes_op_id == old.op_id
    restarted = Controller(c.cfg, backend, store)
    await restarted.load()
    await restarted.synchronized()
    rs = restarted.states[s.binding.binding_id]
    assert rs.mode == "desktop" and result.op_id in rs.blocker_ids and old.op_id not in rs.blocker_ids
    assert len(backend.sent) == 1


async def test_replacement_revalidates_exact_skill_and_files_without_replaying_old_effort(setup):
    c, s, backend, store = setup
    add_p2_backend(backend)
    path = c.cfg.projects[0] / "input.txt"
    path.write_text("input")
    old = await unknown(c, s, store, data={"skill": "C:/skills/test/SKILL.md", "skill_selector": "old name",
                                         "files": [str(path)], "effort": "high"})
    old, _ = await acknowledge(c, s, store, old)
    assert await c.transfer(s, "slack", s.epoch)
    prepared = await c.prepare_replacement(s, new_id(), old.op_id, fingerprint(old), s.epoch, now_ms())
    result = await c.execute(s, prepared)
    assert result.status == "accepted" and len(backend.sent) == 1
    assert backend.sent[0].data["skill"] == "C:/skills/test/SKILL.md"
    assert backend.sent[0].data["files"] == [str(path)] and "effort" not in backend.sent[0].data


async def test_missing_replacement_input_file_is_rejected_without_backend_write(setup):
    c, s, backend, store = setup
    path = c.cfg.projects[0] / "input.txt"
    path.write_text("input")
    old, _ = await acknowledge(c, s, store, await unknown(c, s, store, data={"files": [str(path)]}))
    assert await c.transfer(s, "slack", s.epoch)
    prepared = await c.prepare_replacement(s, new_id(), old.op_id, fingerprint(old), s.epoch, now_ms())
    path.unlink()
    assert (await c.execute(s, prepared)).status == "rejected"
    assert not backend.sent and (await store.operation(old.op_id)).status == "unknown"


async def test_other_binding_operation_cannot_be_reviewed_or_replaced(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)
    other = replace(s.binding, binding_id=new_id(), codex_thread_id=new_id(), slack_root_ts="400.000001")
    await store.bind(other)
    old = replace(old, op_id=new_id(), source_key=new_id(), binding_id=other.binding_id,
                  target_thread_id=other.codex_thread_id)
    await store.record_once(old)
    prepared = await c.prepare(s, new_id(), "unknown_review", "", s.epoch, now_ms(),
        data={"review_op_id": old.op_id, "fingerprint": fingerprint(old), "reason": "checked"})
    assert (await c.execute(s, prepared)).status == "rejected"
    assert (await store.operation(old.op_id)).unknown_review_state == "pending" and not backend.sent


async def test_failed_ack_of_replacement_keeps_original_and_never_sends(setup):
    c, s, backend, store = setup
    old, _ = await acknowledge(c, s, store, await unknown(c, s, store))
    assert await c.transfer(s, "slack", s.epoch)
    slack = adapter(c, backend)
    await slack.command(s, "!replace " + old.op_id, s.epoch)
    token = slack.web.posts[-1][1]["blocks"][-1]["elements"][0]["value"]
    slack.socket.fail = True
    req = SimpleNamespace(type="interactive", envelope_id=new_id(), payload=interaction(c.cfg, token, "200.000001"))
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert (await store.by_source("replacement:" + token)).error_reason == "ack_failed_before_send"
    assert (await store.operation(old.op_id)).unknown_review_state == "acknowledged" and not backend.sent


async def test_disconnect_after_review_send_intent_prevents_acknowledgement(setup):
    c, s, backend, store = setup
    old = await unknown(c, s, store)

    async def checkpoint(stage, *args):
        if stage == "sending":
            c.invalidate()
    c.checkpoint = checkpoint
    prepared = await c.prepare(s, new_id(), "unknown_review", "", s.epoch, now_ms(),
        data={"review_op_id": old.op_id, "fingerprint": fingerprint(old), "reason": "checked"})
    result = await c.execute(s, prepared)
    assert result.status == "rejected" and (await store.operation(old.op_id)).unknown_review_state == "pending"
    assert not backend.sent
