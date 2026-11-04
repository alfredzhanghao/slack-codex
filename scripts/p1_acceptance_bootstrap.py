"""Local-only credential boundary for the explicitly authorized P1 acceptance run.

Never print credential material, raw exceptions, HTTP responses, or socket URLs.
The application itself continues to accept credentials only from its environment.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

from slack_codex_bridge import windows
from slack_codex_bridge.cli import main as bridge_main
from slack_codex_bridge.config import load
from slack_codex_bridge.model import BridgeError
from slack_codex_bridge.slack import Web, private_logger, verify_dm

ROOT = Path(__file__).resolve().parents[1]


def inject_credentials(cfg):
    # This exact local source is an acceptance-only exception authorized by Alfred.
    # It is not a dotenv loader, executable script, CLI argument, or model input.
    source = ROOT / "Token.env"
    if source.is_symlink() or source.stat().st_size > 65536:
        raise BridgeError("acceptance_credential_source_invalid")
    material = source.read_text(encoding="utf-8-sig")
    selected = {}
    for prefix, name in (("xapp", cfg.app_token_env), ("xoxb", cfg.bot_token_env)):
        values = set(re.findall(r"(?<![A-Za-z0-9-])" + prefix + r"-[A-Za-z0-9-]+", material))
        if len(values) != 1:
            raise BridgeError("acceptance_credential_format_invalid")
        selected[name] = values.pop()
    del material
    os.environ.update(selected)
    selected.clear()
    cfg.credentials()


async def preflight(cfg):
    app, bot = cfg.credentials()
    client = AsyncWebClient(token=bot, retry_handlers=[], logger=private_logger(), timeout=15,
                            trust_env_in_session=False)
    stage = "auth"
    try:
        web = Web(cfg, bot, client)
        auth = await web.call("auth.test")
        if auth.get("team_id") != cfg.team_id or not auth.get("bot_id"):
            raise BridgeError("acceptance_bot_identity_mismatch")
        stage = "dm"
        dm_verification = await verify_dm(cfg, auth.get("user_id"), web.call)
        stage = "socket_open"
        response = await client.apps_connections_open(app_token=app)
        address = response.get("url", "")
        parsed = urlparse(address)
        if parsed.scheme != "wss" or not (parsed.hostname or "").endswith(".slack.com"):
            raise BridgeError("acceptance_socket_host_invalid")
        stage = "socket_hello"
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with asyncio.timeout(20):
                async with session.ws_connect(address) as ws:
                    frame = await ws.receive()
                    if frame.type != aiohttp.WSMsgType.TEXT:
                        raise BridgeError("acceptance_socket_hello_invalid")
                    hello = json.loads(frame.data)
                    if (hello.get("type") != "hello"
                            or hello.get("connection_info", {}).get("app_id") != cfg.app_id):
                        raise BridgeError("acceptance_app_identity_mismatch")
        return {"credentials_valid": True, "team_verified": True, "dm_owner_verified": True,
                "socket_app_verified": True, "dm_verification": dm_verification}
    except BridgeError:
        raise
    except SlackApiError as error:
        safe_codes = {"invalid_auth", "token_revoked", "missing_scope", "not_authed", "account_inactive",
                      "not_allowed_token_type", "channel_not_found", "not_in_channel", "ratelimited"}
        code = error.response.get("error")
        raise BridgeError("acceptance_" + stage + "_" + (code if code in safe_codes else "rejected")) from None
    except Exception:
        raise BridgeError("acceptance_" + stage + "_connection_failed") from None
    finally:
        if client.session:
            await client.session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded local P1 credential bootstrap")
    parser.add_argument("mode", choices=["preflight", "bridge", "r14-worker", "r14-resume"])
    parser.add_argument("--run-id")
    parser.add_argument("--intent-id")
    args = parser.parse_args(argv)
    cfg = None
    try:
        cfg = load(ROOT / "bridge.local.json")
        inject_credentials(cfg)
        if args.mode in {"r14-worker", "r14-resume"}:
            if not args.run_id or not args.intent_id:
                raise BridgeError("r14_worker_intent_unverified")
            from p1_acceptance_supervisor import prepare
            if prepare()["run_id"] != args.run_id:
                raise BridgeError("r14_worker_intent_unverified")
            if args.mode == "r14-resume":
                from p1_r14_resume import main as lifecycle_main
            else:
                from p1_r14_lifecycle import main as lifecycle_main
            lifecycle_main(["worker", "--intent-id", args.intent_id])
            return 0
        if args.mode == "bridge":
            if not args.run_id:
                return bridge_main(["--config", str(ROOT / "bridge.local.json"), "run"])
            from p1_acceptance_probe import Probe
            from slack_codex_bridge.cli import run
            try:
                asyncio.run(run(cfg, Probe(args.run_id)))
            except asyncio.CancelledError:
                pass
            return 0
        windows.verify_versions(cfg)
        report = asyncio.run(preflight(cfg))
        report.update(status="passed", versions_match=True)
        print(json.dumps(report))
        return 0
    except BridgeError as error:
        print(json.dumps({"status": "blocked", "code": error.code}))
        return 1
    except Exception:
        print(json.dumps({"status": "blocked", "code": "acceptance_bootstrap_failed"}))
        return 1
    finally:
        if cfg:
            for name in (cfg.app_token_env, cfg.bot_token_env):
                os.environ.pop(name, None)


if __name__ == "__main__":
    sys.exit(main())
