import asyncio
from types import SimpleNamespace

import pytest

from slack_codex_bridge import cli


@pytest.mark.parametrize("jump", [(20, 20), (1, 8)])
async def test_network_await_does_not_revoke_input_but_clock_gap_does(monkeypatch, jump):
    samples = asyncio.Queue()
    clock = [0, 0]
    lost = []
    observed = asyncio.Queue()

    async def sleep(_):
        mono, wall = await samples.get()
        clock[0] += mono
        clock[1] += wall

    monkeypatch.setattr(cli, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[1]))
    slack = SimpleNamespace(lost=lambda: lost.append(tuple(clock)), last_fault=None)
    probe = SimpleNamespace(event=lambda *args, **kwargs: observed.put_nowait(kwargs))
    watchdog = asyncio.create_task(cli.watch_wake(slack, probe))
    network = asyncio.create_task(asyncio.Event().wait())
    try:
        await asyncio.sleep(0)
        # Twenty seconds of normal clock ticks while an I/O coroutine is pending.
        for _ in range(20):
            samples.put_nowait((1, 1))
            await asyncio.sleep(0)
        assert not network.done() and not lost
        samples.put_nowait(jump)
        event = await asyncio.wait_for(observed.get(), 1)
        assert len(lost) == 1 and event["code"] == "runtime_wake_or_stall"
    finally:
        watchdog.cancel()
        network.cancel()
        await asyncio.gather(watchdog, network, return_exceptions=True)
