"""Bounded desktop chat-name metadata; names never authorize or route a chat."""

from functools import lru_cache
import json

from .artifacts import handle_path
from .model import BridgeError

LIMIT = 8 * 1024**2
LINE_LIMIT = 64 * 1024


@lru_cache(maxsize=4)
def _read(path, signature):
    expected = path.parent.resolve() / "session_index.jsonl"
    with path.open("rb") as stream:
        if handle_path(stream) != expected:
            raise ValueError("thread_name_path_changed")
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("thread_names_too_large")
    names = {}
    for line in raw.splitlines():
        if len(line) > LINE_LIMIT:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict):
            continue
        tid, name = row.get("id"), row.get("thread_name")
        if (isinstance(tid, str) and 0 < len(tid) <= 128 and isinstance(name, str)
                and 0 < len(name.strip()) <= 512 and not any(ord(c) < 32 for c in name)):
            names[tid] = name.strip()
    return names


def names(cfg):
    if cfg.native_state_db is None:
        return {}
    source = cfg.native_state_db.with_name("session_index.jsonl")
    try:
        if source.is_symlink():
            return {}
        stat = source.stat()
        if stat.st_size > LIMIT:
            return {}
        return dict(_read(source, (stat.st_mtime_ns, stat.st_size, stat.st_ino)))
    except (OSError, ValueError, RuntimeError, BridgeError):
        return {}
