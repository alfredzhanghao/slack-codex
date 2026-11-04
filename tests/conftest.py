import asyncio
from dataclasses import replace

import pytest
import pytest_asyncio

from slack_codex_bridge.config import Config
from slack_codex_bridge.controller import Controller
from slack_codex_bridge.model import Binding, Receipt, Snapshot, Thread, new_id, now_ms
from slack_codex_bridge.store import Store


class Backend:
    def __init__(self, cfg):
        self.current = Snapshot(Thread(new_id(), "Test chat", str(cfg.projects[0])), trusted=True,
                                idle=True, model="test-model", effort="high", generation=1)
        self.sent = []
        self.send_error = None
        self.entered = asyncio.Event()
        self.release = None
        self.receipt = Receipt(new_id())

    async def snapshot(self, thread_id):
        assert thread_id == self.current.thread.id
        return replace(self.current, observed_ms=now_ms())

    async def send(self, action):
        self.sent.append(action)
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.send_error:
            raise self.send_error
        return self.receipt

    async def list_threads(self):
        return [self.current.thread]

    async def final_messages(self, thread_id, turn_id):
        return "completed", ["Final test response"]


@pytest.fixture
def cfg(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return Config("A123", "T123", "U123", "D123", "test-host", (project.resolve(),),
                  tmp_path / "backend.exe", tmp_path / "desktop.exe", "test", "test", tmp_path / "data")


@pytest_asyncio.fixture
async def setup(cfg):
    store = await Store(cfg.data_dir / "test.sqlite3").open()
    backend = Backend(cfg)
    controller = Controller(cfg, backend, store)
    await controller.load()
    await controller.synchronized()
    binding = Binding(new_id(), cfg.team_id, cfg.dm_id, "100.000001", cfg.user_id, cfg.host_id,
                      backend.current.thread.id, str(cfg.projects[0]), "windows", effective_from_ms=0)
    state = await controller.bind(binding)
    yield controller, state, backend, store
    await store.close()
