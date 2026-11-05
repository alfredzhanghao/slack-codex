"""Non-secret user preferences, separate from pinned deployment configuration."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .model import BridgeError

MB = 1_000_000


@dataclass(frozen=True)
class Preferences:
    language: str = "en"
    auto_send: bool = True
    auto_send_max_bytes: int = 30 * MB
    max_file_bytes: int = 100 * MB

    def validate(self):
        if (self.language not in {"en", "zh-CN"} or type(self.auto_send) is not bool
                or type(self.auto_send_max_bytes) is not int or type(self.max_file_bytes) is not int
                or not 1 <= self.auto_send_max_bytes <= 100 * MB or not 1 <= self.max_file_bytes <= 100 * MB):
            raise BridgeError("invalid_bridge_preferences")
        return self


def preference_path(cfg):
    return cfg.data_dir / "bridge-preferences.json"


def read(cfg):
    path = preference_path(cfg)
    try:
        if path.is_symlink():
            raise BridgeError("invalid_bridge_preferences")
        if not path.exists():
            return Preferences(cfg.language, cfg.artifact_auto_send, cfg.artifact_auto_send_max_bytes,
                               cfg.artifact_max_bytes).validate()
        if not path.is_file() or path.stat().st_size > 4096:
            raise BridgeError("invalid_bridge_preferences")
        with path.open("rb") as stream:
            from .artifacts import handle_path
            if handle_path(stream) != path.resolve():
                raise BridgeError("invalid_bridge_preferences")
            value = json.loads(stream.read(4097))
        if not isinstance(value, dict) or set(value) != set(asdict(Preferences())):
            raise BridgeError("invalid_bridge_preferences")
        return Preferences(**value).validate()
    except (OSError, ValueError, TypeError):
        raise BridgeError("invalid_bridge_preferences") from None


def save(cfg, value):
    value.validate()
    path = preference_path(cfg)
    if path.is_symlink():
        raise BridgeError("invalid_bridge_preferences")
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".bridge-preferences-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(value), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise BridgeError("bridge_preferences_save_failed") from None
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def language(cfg):
    # A malformed preference must not crash the local error display or mask its code.
    try:
        return read(cfg).language
    except BridgeError:
        return cfg.language
