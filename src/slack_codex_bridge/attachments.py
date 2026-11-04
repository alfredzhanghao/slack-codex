"""Owned, bounded Slack attachment staging. No authenticated redirects or project writes."""

import asyncio
import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .artifacts import handle_path
from .inputs import SUPPORTED, sensitive_path
from .model import BridgeError, new_id

MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024**2
MAX_TOTAL_BYTES = 20 * 1024**2
RETENTION_SECONDS = 86400
UUID = re.compile(r"[0-9a-fA-F-]{36}")
IMAGES = {".png", ".jpg", ".jpeg", ".webp"}


def download_url(value):
    try:
        p = urlsplit(value)
        if (p.scheme == "https" and p.hostname == "files.slack.com" and p.port in {None, 443}
                and not p.username and not p.password and not p.fragment and not p.query
                and p.path.startswith("/files-pri/") and "\\" not in value):
            return value
    except (ValueError, TypeError):
        pass
    raise BridgeError("attachment_download_url_forbidden")


def safe_name(value):
    if (not isinstance(value, str) or not value or len(value) > 180
            or any(c in value for c in '/\\:\x00') or value in {".", ".."}
            or value.endswith((".", " ")) or sensitive_path(Path(value))):
        raise BridgeError("attachment_filename_forbidden")
    if Path(value).suffix.casefold() not in SUPPORTED:
        raise BridgeError("unsupported_file_type")
    # Prefixing the ID also prevents Windows device names from becoming devices.
    return re.sub(r'[<>"|?*\x00-\x1f]', "_", value)


def staging_root(cfg):
    original = cfg.data_dir / "attachments"
    root = original.resolve()
    if root != original.absolute() or any(root.is_relative_to(p) for p in cfg.projects):
        raise BridgeError("attachment_staging_path_unverified")
    return root


def cleanup(cfg, clock=None):
    """Delete only expired UUID batches under our dedicated physical staging root."""
    root = staging_root(cfg)
    if not root.exists():
        return
    deadline = (time.time() if clock is None else clock) - RETENTION_SECONDS
    for thread in root.iterdir():
        if not UUID.fullmatch(thread.name) or thread.is_symlink() or thread.resolve() != thread:
            continue
        if not thread.is_dir():
            continue
        for batch in thread.iterdir():
            if (UUID.fullmatch(batch.name) and batch.is_dir() and not batch.is_symlink()
                    and batch.resolve() == batch and batch.stat().st_mtime < deadline):
                shutil.rmtree(batch)


async def fetch(web, url, maximum):
    url = download_url(url)
    try:
        async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                                        timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.get(url, headers={"Authorization": "Bearer " + web.client.token},
                                   allow_redirects=False) as response:
                if response.status in {401, 403}:
                    raise BridgeError("attachment_access_denied_check_files_read")
                if response.status != 200:
                    raise BridgeError("attachment_download_failed")
                if response.content_length is not None and response.content_length > maximum:
                    raise BridgeError("file_too_large")
                content = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    content.extend(chunk)
                    if len(content) > maximum:
                        raise BridgeError("file_too_large")
                if response.content_type == "text/html":
                    raise BridgeError("attachment_download_not_file")
                return bytes(content)
    except (BridgeError, asyncio.CancelledError):
        raise
    except Exception:
        raise BridgeError("attachment_download_failed") from None


async def stage(cfg, web, thread_id, files, *, downloader=None):
    downloader = downloader or fetch
    if not UUID.fullmatch(thread_id) or not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise BridgeError("attachment_count_invalid")
    await asyncio.to_thread(cleanup, cfg)
    rows, ids, total = [], set(), 0
    # Validate ALL metadata before downloading any bytes.
    for item in files:
        fid = item.get("id", "") if isinstance(item, dict) else ""
        if not re.fullmatch(r"F[A-Z0-9]+", fid) or fid in ids:
            raise BridgeError("attachment_identity_invalid")
        ids.add(fid)
        info = (await web.call("files.info", file=fid)).get("file", {})
        if (info.get("id") != fid or info.get("user") != cfg.user_id
                or info.get("mode") not in {"hosted", "snippet"} or info.get("is_external") is not False):
            raise BridgeError("attachment_metadata_unverified")
        name = safe_name(info.get("name"))
        size = info.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_FILE_BYTES:
            raise BridgeError("file_too_large")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise BridgeError("files_total_too_large")
        rows.append((fid, name, size, download_url(info.get("url_private_download") or info.get("url_private"))))
    root = staging_root(cfg)
    parent = root / thread_id
    if parent.resolve() != parent:
        raise BridgeError("attachment_staging_path_unverified")
    directory = parent / new_id()
    directory.mkdir(parents=True, exist_ok=False)
    if directory.resolve() != directory:
        raise BridgeError("attachment_staging_path_unverified")
    result = []
    try:
        for fid, name, size, url in rows:
            content = await downloader(web, url, min(MAX_FILE_BYTES, size))
            if len(content) != size:
                raise BridgeError("attachment_size_changed")
            path = directory / (fid + "_" + name)
            with path.open("xb") as output:
                output.write(content)
            result.append({"path": str(path), "name": name, "size": size,
                           "sha256": hashlib.sha256(content).hexdigest(), "thread_id": thread_id})
        return result
    except BaseException:
        if directory.resolve() == directory and directory.is_relative_to(root):
            shutil.rmtree(directory)
        raise


def attachment_inputs(cfg, snapshot, rows, task):
    if snapshot.thread.environment != "windows" or not cfg.allows(snapshot.thread.cwd):
        raise BridgeError("file_environment_unverified")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
        raise BridgeError("attachment_count_invalid")
    tid = snapshot.thread.id
    if not UUID.fullmatch(tid):
        raise BridgeError("attachment_session_mismatch")
    root = staging_root(cfg) / tid
    paths, total = [], 0
    for row in rows:
        path = Path(row["path"])
        if (row.get("thread_id") != tid or not path.is_absolute() or sensitive_path(path)
                or not path.is_relative_to(root) or len(path.relative_to(root).parts) != 2
                or not UUID.fullmatch(path.parent.name) or path.resolve(strict=True) != path
                or path.suffix.casefold() not in SUPPORTED):
            raise BridgeError("attachment_session_mismatch")
        if time.time() - path.stat().st_mtime > RETENTION_SECONDS:
            raise BridgeError("attachment_expired_resend_files")
        with path.open("rb") as stream:
            if handle_path(stream) != path:
                raise BridgeError("attachment_path_changed")
            content = stream.read(MAX_FILE_BYTES + 1)
        if (not 0 < len(content) <= MAX_FILE_BYTES or len(content) != row.get("size")
                or hashlib.sha256(content).hexdigest() != row.get("sha256")):
            raise BridgeError("attachment_changed_resend_files")
        total += len(content)
        paths.append(path)
    if total > MAX_TOTAL_BYTES:
        raise BridgeError("files_total_too_large")
    return [{"type": "text", "text": task + "\n\nSlack attachments (read these actual local files in this task's environment):\n"
             + json.dumps([str(p) for p in paths], ensure_ascii=False)}] + [
        {"type": "localImage", "path": str(p)} for p in paths if p.suffix.casefold() in IMAGES]
