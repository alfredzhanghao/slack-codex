from slack_codex_bridge.model import now_ms
from test_slack import adapter


async def test_successful_submission_is_silent_but_keeps_durable_receipt(setup):
    c, state, backend, store = setup
    await c.transfer(state, 'slack', state.epoch)
    prepared = await c.prepare(state, 'quiet-start', 'turn_start', 'hello', state.epoch, now_ms())
    slack = adapter(c, backend)
    await slack.submit(state, prepared)
    assert not slack.web.posts and len(backend.sent) == 1
    assert (await store.operation(prepared.operation.op_id)).status == 'accepted'


async def test_take_control_success_uses_card_only_and_duplicate_failure_is_throttled(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    await slack.command(state, '!control slack', state.epoch)
    assert state.mode == 'slack' and not slack.web.posts and slack.web.updates
    backend.current.idle = False
    backend.current.active_turn = 'active-task'
    await slack.command(state, '!control desktop', state.epoch)
    await slack.command(state, '!control desktop', state.epoch)
    assert len(slack.web.posts) == 1 and 'Wait for it to finish' in slack.web.posts[0][0]
    assert 'Transfer refused' not in slack.web.posts[0][0]


async def test_limits_are_explicit_quota_only(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    assert 'Account limits' not in slack.card(state)[0]
    await slack.command(state, '!status', state.epoch)
    assert not slack.web.posts
    await slack.command(state, '!quota', state.epoch)
    assert any('Account limits' in text for text, _ in slack.web.posts)
