"""Read desktop project display metadata; labels never grant directory access."""

from functools import lru_cache
import json
import os
from pathlib import Path

from .artifacts import handle_path
from .model import BridgeError

FIELDS = {"local-projects", "electron-workspace-root-labels"}
LIMIT = 8 * 1024**2


def _end(text, start):
    """Skip an unrelated JSON value without decoding its strings or object fields."""
    depth, quoted, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
        elif char in "}]":
            if depth == 0:
                return index
            depth -= 1
        elif char == "," and depth == 0:
            return index
    raise ValueError("incomplete_project_metadata")


def _selected(text):
    decoder, result, pos = json.JSONDecoder(), {}, 0
    def whitespace(index):
        while index < len(text) and text[index] in " \r\n\t":
            index += 1
        return index
    pos = whitespace(pos)
    if text[pos:pos + 1] != "{":
        raise ValueError("invalid_project_metadata")
    pos = whitespace(pos + 1)
    while text[pos:pos + 1] != "}":
        key, pos = decoder.raw_decode(text, pos)
        pos = whitespace(pos)
        if not isinstance(key, str) or text[pos:pos + 1] != ":":
            raise ValueError("invalid_project_metadata")
        start = whitespace(pos + 1)
        pos = _end(text, start)
        if key in FIELDS:
            if key in result or pos - start > 512 * 1024:
                raise ValueError("project_metadata_too_large_or_duplicate")
            result[key] = json.loads(text[start:pos])
        pos = whitespace(pos)
        if text[pos:pos + 1] == "}":
            break
        if text[pos:pos + 1] != ",":
            raise ValueError("invalid_project_metadata")
        pos = whitespace(pos + 1)
        if text[pos:pos + 1] == "}":
            raise ValueError("invalid_project_metadata")
    if text[pos:pos + 1] != "}" or whitespace(pos + 1) != len(text):
        raise ValueError("invalid_project_metadata")
    return result


@lru_cache(maxsize=4)
def _read(path, signature):
    expected = path.parent.resolve() / ".codex-global-state.json"
    with path.open("rb") as stream:
        if handle_path(stream) != expected:
            raise ValueError("project_metadata_path_changed")
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("project_metadata_too_large")
    return _selected(raw.decode("utf-8-sig"))


def labels(cfg):
    """Only project names for exact allowlisted roots leave this module."""
    paths = {str(path): path for path in cfg.projects}
    result = {key: path.name or "Project" for key, path in paths.items()}
    if cfg.native_state_db is None:
        return _disambiguate(result)
    source = cfg.native_state_db.with_name(".codex-global-state.json")
    try:
        if source.is_symlink():
            return _disambiguate(result)
        stat = source.stat()
        if stat.st_size > LIMIT:
            return _disambiguate(result)
        data = _read(source, (stat.st_mtime_ns, stat.st_size, stat.st_ino))
        names = {}
        legacy = data.get("electron-workspace-root-labels", {})
        if isinstance(legacy, dict):
            names.update(legacy)
        projects = data.get("local-projects", {})
        if isinstance(projects, dict):
            for row in projects.values():
                if (isinstance(row, dict) and _valid_name(row.get("name"))
                        and isinstance(row.get("rootPaths"), list)):
                    for root in row["rootPaths"]:
                        if isinstance(root, str):
                            names[root] = row.get("name")
        canonical = {os.path.normcase(os.path.normpath(str(path))): key for key, path in paths.items()}
        for root, name in names.items():
            if not isinstance(root, str) or not _valid_name(name):
                continue
            path = Path(root)
            # Metadata is only a lexical lookup; never resolve unconfigured or network paths.
            if path.is_absolute() and (key := canonical.get(os.path.normcase(os.path.normpath(root)))):
                result[key] = name.strip()
    except (OSError, ValueError, TypeError, RuntimeError, BridgeError):
        pass  # Presentation failure falls back to directory names, never paths or new access.
    return _disambiguate(result)


def _disambiguate(names):
    groups = {}
    for path, name in names.items():
        groups.setdefault(name.casefold(), []).append(path)
    result = dict(names)
    used = {name.casefold() for name in names.values()}
    for group in groups.values():
        if len(group) > 1:
            index = 1
            for path in sorted(group, key=str.casefold):
                while (candidate := f"{names[path]} ({index})").casefold() in used:
                    index += 1
                result[path] = candidate
                used.add(candidate.casefold())
    return result


def _valid_name(name):
    return (isinstance(name, str) and bool(name.strip()) and len(name) <= 200
            and not any(ord(char) < 32 for char in name))
