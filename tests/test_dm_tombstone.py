"""History owner proof must distinguish Slack's deletion marker from a human."""
import pytest

from slack_codex_bridge.model import BridgeError
from slack_codex_bridge.slack import verify_dm


@pytest.mark.parametrize("marker,with_owner,expected", [
    ({"user": "USLACKBOT", "subtype": "tombstone"}, True, "fixed_dm_history_owner"),
    ({"user": "USLACKBOT", "subtype": "tombstone"}, False, "slack_dm_owner_evidence_unavailable"),
    ({"user": "UOTHER", "subtype": "tombstone"}, True, "slack_dm_owner_mismatch"),
    ({"user": "USLACKBOT"}, True, "slack_dm_owner_mismatch"),
    ({"user": "USLACKBOT", "subtype": "message_deleted"}, True, "slack_dm_owner_mismatch"),
    ({"user": "UOTHER", "text": "USLACKBOT tombstone"}, True, "slack_dm_owner_mismatch"),
])
async def test_only_reserved_slack_tombstone_is_not_owner_evidence(cfg, marker, with_owner, expected):
    calls = []

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "conversations.info":
            raise BridgeError("slack_missing_scope")
        rows = [marker] + ([{"user": cfg.user_id}] if with_owner else [])
        return {"messages": rows}

    if expected == "fixed_dm_history_owner":
        assert await verify_dm(cfg, "UBOT", call) == expected
    else:
        with pytest.raises(BridgeError, match=expected):
            await verify_dm(cfg, "UBOT", call)
    assert [method for method, _ in calls] == ["conversations.info", "conversations.history"]
    assert all(kwargs["channel"] == cfg.dm_id for _, kwargs in calls)
