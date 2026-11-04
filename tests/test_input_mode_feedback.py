import pytest

from slack_codex_bridge.model import now_ms
from test_slack import adapter


@pytest.mark.parametrize('mode,expected', [('desktop','desktop_input_mode'),('slack','input_precedes_opening')])
async def test_ownership_reason_is_not_overwritten_by_opening_timestamp(setup, mode, expected):
    c, state, backend, _ = setup
    assert await c.transfer(state, mode, state.epoch)
    sent = min(now_ms(), state.opened_ms - 1)
    prepared = await c.prepare(state, 'mode-feedback', 'turn_start', 'hello', state.epoch, sent)
    assert not prepared.execute and prepared.operation.error_reason == expected
    assert not backend.sent


async def test_desktop_mode_rejection_explains_take_control_without_dispatch(setup):
    c, state, backend, store = setup
    assert await c.transfer(state, 'desktop', state.epoch)
    prepared = await c.prepare(state, 'disabled-feedback', 'turn_start', 'hello', state.epoch, now_ms())
    slack = adapter(c, backend)
    await slack.submit(state, prepared)
    text = slack.web.posts[0][0]
    assert 'Click Take control' in text and 'Operation ' not in text and 'desktop_input_mode' not in text
    assert not backend.sent
    assert (await store.operation(prepared.operation.op_id)).status == 'rejected'


async def test_busy_transfer_explains_waiting_and_does_not_acquire_input(setup):
    c, state, backend, _ = setup
    assert await c.transfer(state, 'desktop', state.epoch)
    backend.current.idle = False
    backend.current.active_turn = 'active-turn'
    slack = adapter(c, backend)
    await slack.command(state, '!control slack', state.epoch)
    assert state.mode == 'desktop' and state.transfer_refusal == 'session_has_active_task'
    assert any('Wait for it to finish' in text for text, _ in slack.web.posts)
    assert not backend.sent
