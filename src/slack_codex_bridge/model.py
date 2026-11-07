"""Small transport-independent values shared by the four modules."""

from dataclasses import dataclass, field
import math
from time import time
from uuid import uuid4


def now_ms() -> int:
    return int(time() * 1000)


def new_id() -> str:
    return str(uuid4())


class BridgeError(Exception):
    """Only fixed, safe error codes may cross the CLI/Slack/log boundary."""

    def __init__(self, code: str, *, unknown: bool = False, retry_after: float | None = None):
        self.code = code
        self.unknown = unknown
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(frozen=True)
class Thread:
    id: str
    title: str
    cwd: str
    environment: str = "windows"
    allowed: bool = True
    updated_at_ms: int | None = None


def activity_ms(value):
    """Native updatedAt / SQLite updated_at are Unix seconds, not UI timestamps."""
    if (type(value) not in {int, float} or not 0 < value <= 253402300799 or not math.isfinite(value)):
        return None
    return int(value * 1000)


def recent_threads(rows):
    unique = {}
    for thread in rows:
        previous = unique.get(thread.id)
        if previous is None or (thread.updated_at_ms or 0) > (previous.updated_at_ms or 0):
            unique[thread.id] = thread
    return sorted(unique.values(), key=lambda t: (-(t.updated_at_ms or 0), t.id))


@dataclass
class Snapshot:
    thread: Thread
    trusted: bool = False
    idle: bool = False
    active_turn: str | None = None
    approval: bool = False
    compacting: bool = False
    model: str | None = None
    effort: str | None = None
    generation: int = 0
    observed_ms: int = field(default_factory=now_ms)
    terminals: dict[str, str] = field(default_factory=dict)
    confirmed: set[str] = field(default_factory=set)
    known_turns: set[str] = field(default_factory=set)
    context: dict = field(default_factory=dict)
    progress: str = ""


@dataclass(frozen=True)
class Action:
    kind: str
    thread_id: str
    turn_id: str | None = None
    text: str = ""
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    turn_id: str | None = None
    semantic_mismatch: bool = False
    thread_id: str | None = None
    result: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Event:
    kind: str
    thread_id: str = ""
    turn_id: str = ""
    item_id: str = ""
    text: str = ""
    status: str = ""
    generation: int = 0


@dataclass
class Binding:
    binding_id: str
    slack_team_id: str
    slack_channel_id: str
    slack_root_ts: str
    owner_user_id: str
    host_id: str
    codex_thread_id: str
    project_path: str
    environment_key: str
    binding_version: int = 1
    disabled: int = 0
    effective_from_ms: int = field(default_factory=now_ms)
    next_effort_json: str | None = None
    created_at_ms: int = field(default_factory=now_ms)
    updated_at_ms: int = field(default_factory=now_ms)


@dataclass
class Operation:
    op_id: str
    source_key: str
    binding_id: str
    binding_version: int
    kind: str
    target_thread_id: str
    target_turn_id: str | None = None
    payload_json: str = "{}"
    status: str = "received"
    backend_ref_json: str | None = None
    result_json: str | None = None
    error_reason: str | None = None
    unknown_review_state: str = "not_required"
    review_json: str | None = None
    supersedes_op_id: str | None = None
    created_at_ms: int = field(default_factory=now_ms)
    updated_at_ms: int = field(default_factory=now_ms)
