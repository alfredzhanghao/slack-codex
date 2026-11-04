"""Bounded capture of an explicitly selected ordinary file, without project scans."""

import hashlib
import html
import os
import stat
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from .inputs import local_file
from .model import BridgeError


def linked_files(text: str) -> list[str]:
    """Index explicit local Markdown/Slack links; never rewrite the relayed text."""
    displayed = html.unescape(text)
    targets = [m[1] for m in re.finditer(
        r"\[[^\]\n]{0,400}\]\((<[^>\n]{1,2000}>|(?:[^()\n]|\([^()\n]*\)){1,2000})\)", displayed)]
    targets += [m[1] for m in re.finditer(r"<([^|<>\n]{1,2000})\|[^<>\n]{0,400}>", displayed)]
    paths = []
    for target in targets:
        path = unquote(target.strip().strip("<>"))
        if (not path or path.startswith("#") or
                (re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", path) and not re.match(r"[A-Za-z]:[/\\]", path))):
            continue
        paths.append(path)
    return paths


@dataclass(frozen=True)
class Artifact:
    path: Path
    content: bytes
    sha256: str


def handle_path(file) -> Path:
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        method = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
        method.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        method.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        count = method(msvcrt.get_osfhandle(file.fileno()), buffer, len(buffer), 0)
        if (not 0 < count < len(buffer) or not buffer.value.startswith("\\\\?\\")
                or buffer.value.startswith("\\\\?\\UNC\\")):
            raise BridgeError("artifact_handle_unverified")
        return Path(buffer.value[4:])
    return Path(os.readlink(f"/proc/self/fd/{file.fileno()}"))


def capture(cfg, cwd: str, value: str) -> Artifact:
    try:
        path = local_file(cfg, cwd, value, max_bytes=cfg.artifact_max_bytes)
        with path.open("rb") as file:
            actual = local_file(cfg, cwd, str(handle_path(file)), max_bytes=cfg.artifact_max_bytes)
            if actual != path:
                raise BridgeError("artifact_path_changed")
            before = os.fstat(file.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise BridgeError("artifact_not_regular_file")
            content = file.read(cfg.artifact_max_bytes + 1)
            after = os.fstat(file.fileno())
            if len(content) > cfg.artifact_max_bytes:
                raise BridgeError("file_too_large")
            if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
                    (after.st_size, after.st_mtime_ns, after.st_ino) or len(content) != after.st_size
                    or handle_path(file) != actual):
                raise BridgeError("artifact_changed_during_read")
        return Artifact(actual, content, hashlib.sha256(content).hexdigest())
    except (OSError, ValueError, RuntimeError):
        raise BridgeError("file_unavailable") from None
