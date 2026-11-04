"""Explicit task inputs. Never opens local files or translates execution environments."""

import json
import re
from pathlib import Path

from .config import safe_text
from .model import BridgeError

SENSITIVE = {".aws", ".ssh", ".azure", ".gnupg", ".codex", "credentials", "credential",
             "credentials.json", "auth.json", "token.env", "tokens.json", "secrets.json",
             "id_rsa", "id_ed25519", "login data", "cookies", "local state"}
SUPPORTED = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".log",
             ".py", ".js", ".ts", ".html", ".css", ".sql", ".pdf", ".docx", ".xlsx",
             ".pptx", ".png", ".jpg", ".jpeg", ".webp"}


def sensitive_path(path: Path) -> bool:
    return any(p.casefold() in SENSITIVE or p.casefold().startswith(".env")
               or p.casefold().endswith((".env", ".pem", ".key", ".pfx", ".p12")) for p in path.parts)


def local_file(cfg, cwd: str, value: str, *, max_bytes: int | None = None) -> Path:
    # Reject alternate data streams, UNC/device paths and drive-relative paths before resolve.
    if (not isinstance(value, str) or not value or not safe_text(value) or "\x00" in value
            or value.startswith(("\\\\", "//", "/")) or re.search(r":", value[2:])
            or (":" in value and not re.match(r"^[A-Za-z]:[\\/]", value))):
        raise BridgeError("invalid_file_path")
    original = Path(value)
    if sensitive_path(original):
        raise BridgeError("credential_file_forbidden")
    try:
        path = (original if original.is_absolute() else Path(cwd) / original).resolve(strict=True)
        if sensitive_path(path):
            raise BridgeError("credential_file_forbidden")
        if not path.is_file() or not any(path.is_relative_to(root) for root in cfg.projects):
            raise BridgeError("file_outside_allowlist")
        if path.suffix.casefold() not in SUPPORTED:
            raise BridgeError("unsupported_file_type")
        limit = max_bytes if max_bytes is not None else (
            10 if path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
            else 2 if path.suffix.casefold() in {".txt", ".md"} else 20) * 1024**2
        if path.stat().st_size > limit:
            raise BridgeError("file_too_large")
        return path
    except (OSError, ValueError, RuntimeError):
        raise BridgeError("file_unavailable") from None


def parse_files(text: str) -> tuple[list[str], str]:
    # Quoted Windows paths retain backslashes; shlex's POSIX mode would destroy them.
    paths, sep, task = text.partition(" -- ")
    values = re.findall(r'"([^"\r\n]+)"', paths)
    residue = re.sub(r'"[^"\r\n]+"', "", paths).strip()
    if not sep or residue or not 1 <= len(values) <= 5 or not task.strip():
        raise BridgeError("use_file_quoted_paths_then_task")
    return values, task.strip()


def file_inputs(cfg, snapshot, paths: list[str], task: str) -> list[dict]:
    if snapshot.thread.environment != "windows" or not cfg.allows(snapshot.thread.cwd):
        raise BridgeError("file_environment_unverified")
    files = [local_file(cfg, snapshot.thread.cwd, p) for p in paths]
    if sum(p.stat().st_size for p in files) > 20 * 1024**2:
        raise BridgeError("files_total_too_large")
    inputs = [{"type": "text", "text": task + "\n\nExplicit local files (read in this task's environment):\n"
               + json.dumps([str(p) for p in files], ensure_ascii=False)}]
    inputs += [{"type": "localImage", "path": str(p)} for p in files
               if p.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}]
    return inputs
