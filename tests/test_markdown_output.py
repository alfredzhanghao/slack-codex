import json

import pytest

from slack_codex_bridge.model import BridgeError, now_ms
from slack_codex_bridge.slack import markdown_chunks
from test_slack import adapter


def test_markdown_source_is_preserved_without_plain_text_escaping():
    source = '# Result\n\n**Bold** & [issue](https://example.com/?a=1&b=2)\n\n| A | B |\n|---|---|\n| 1 | 2 |'
    assert list(markdown_chunks(source)) == [source]


def test_long_plain_reply_preserves_all_content_and_payload_budget():
    source = ('中文 paragraph\n\n' * 3000) + 'end'
    chunks = list(markdown_chunks(source))
    assert len(chunks) > 1 and ''.join(chunks) == source
    assert all(0 < len(chunk) <= 12000 for chunk in chunks)


def test_long_code_block_is_balanced_in_each_message():
    chunks = list(markdown_chunks('```python\n' + 'print(1)\n' * 3000 + '```'))
    assert len(chunks) > 1
    assert all(chunk.startswith('```python\n') and chunk.endswith('```') for chunk in chunks)
    assert sum(chunk.count('print(1)') for chunk in chunks) == 3000
    assert all(len(chunk) <= 12000 for chunk in chunks)


@pytest.mark.parametrize('partial_failure', [False, True])
async def test_final_reply_uses_markdown_blocks_and_never_replays_partial_delivery(setup, partial_failure):
    c, s, backend, store = setup
    await c.transfer(s, 'slack', s.epoch)
    prepared = await c.prepare(s, 'markdown-reply', 'turn_start', 'Hello', s.epoch, now_ms())
    op = await c.execute(s, prepared)
    tid = backend.receipt.turn_id
    backend.current.confirmed = {tid}
    backend.current.terminals[tid] = 'completed'
    source = '**Answer**\n\n' + '中文 text\n\n' * 3000

    async def final_messages(*_):
        return 'completed', [source]

    backend.final_messages = final_messages
    slack = adapter(c, backend)
    post = slack.web.post
    calls = []

    async def record(text, **kwargs):
        if any(b['type'] == 'markdown' for b in kwargs.get('blocks', [])):
            calls.append((text, kwargs))
            if partial_failure and len(calls) == 2:
                raise BridgeError('slack_write_unconfirmed', unknown=True)
        return await post(text, **kwargs)

    slack.web.post = record
    await slack.tick(s)
    count = len(calls)
    await slack.tick(s)
    assert len(calls) == count and len(backend.sent) == 1
    chunks = [next(b['text'] for b in kw['blocks'] if b['type'] == 'markdown') for _, kw in calls]
    assert all(kw['root'] == s.binding.slack_root_ts for _, kw in calls)
    assert all(len(chunk) <= 12000 for chunk in chunks)
    assert all([b['type'] for b in kw['blocks']] == ['markdown'] for _, kw in calls)
    assert all('Operation ' not in text and 'Account limits' not in text for text, _ in calls)
    saved = json.loads((await store.operation(op.op_id)).result_json)
    if partial_failure:
        assert count == 2 and saved['delivery'] == 'unknown_no_automatic_retry'
    else:
        assert ''.join(chunks) == source and saved['delivery'] == 'confirmed'
