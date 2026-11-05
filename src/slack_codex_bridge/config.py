import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .model import BridgeError

SECRET_PATTERN = re.compile(r"(?:xox[baprs]-|xapp-)[A-Za-z0-9-]+|-----BEGIN .*PRIVATE KEY-----")


def safe_text(text: str) -> bool:
    """Transport bounds only; relay content is not classified or rewritten."""
    return isinstance(text, str) and len(text) <= 16000


@dataclass(frozen=True)
class Config:
    app_id: str
    team_id: str
    user_id: str
    dm_id: str
    host_id: str
    projects: tuple[Path, ...]
    backend_exe: Path
    desktop_exe: Path
    backend_version: str
    desktop_version: str
    data_dir: Path
    message_age_ms: int = 600000
    request_timeout: float = 12
    root_interval: float = 3
    app_token_env: str = "SLACK_APP_TOKEN"
    bot_token_env: str = "SLACK_BOT_TOKEN"
    quota_refresh: float = 300
    quota_stale: float = 600
    environment_id: str | None = None
    message_apps: tuple[str, ...] = ()
    update_budget: int = 30
    artifact_max_bytes: int = 100_000_000
    native_state_db: Path | None = None
    language: str = "en"
    artifact_auto_send: bool = True
    artifact_auto_send_max_bytes: int = 30_000_000

    def allows(self, cwd: str) -> bool:
        try:
            candidate = Path(cwd).resolve(strict=True)
            return candidate.is_dir() and any(candidate.is_relative_to(p) for p in self.projects)
        except (OSError, ValueError, RuntimeError):
            return False

    def credentials(self) -> tuple[str, str]:
        # Never read credential files or include environment values in exceptions.
        app = os.environ.get(self.app_token_env, "")
        bot = os.environ.get(self.bot_token_env, "")
        if not app.startswith("xapp-") or not bot.startswith("xoxb-"):
            raise BridgeError("credentials_not_in_environment")
        return app, bot


def load(path: Path) -> Config:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        if SECRET_PATTERN.search(raw):
            raise BridgeError("credential_values_forbidden_in_config")
        obj = json.loads(raw)
        slack, desktop = obj["slack"], obj["desktop"]
        if desktop["entry_route"] != "verified_controlled_start":
            raise BridgeError("unsupported_entry_route")
        projects = tuple(Path(x).resolve(strict=True) for x in desktop["project_allowlist"])
        if not projects or not all(p.is_dir() for p in projects):
            raise BridgeError("invalid_project_allowlist")
        data = Path(obj.get("data_dir") or Path(os.environ.get("LOCALAPPDATA", Path.home())) /
                    "SlackCodexBridge")
        data = data.resolve()
        if any(data.is_relative_to(p) for p in projects):
            raise BridgeError("data_dir_must_be_outside_projects")
        cfg = Config(
            app_id=slack["app_id"], team_id=slack["team_id"], user_id=slack["owner_user_id"],
            dm_id=slack["dm_channel_id"], host_id=obj["host_id"], projects=projects,
            backend_exe=Path(desktop["backend_exe"]), desktop_exe=Path(desktop["desktop_exe"]),
            backend_version=desktop["backend_version"], desktop_version=desktop["desktop_version"],
            data_dir=data, message_age_ms=int(obj.get("input", {}).get("max_message_age_minutes", 10) * 60000),
            request_timeout=float(obj.get("request_timeout_seconds", 12)),
            root_interval=float(obj.get("status", {}).get("root_min_interval_seconds", 3)),
            app_token_env=slack.get("app_token_env", "SLACK_APP_TOKEN"),
            bot_token_env=slack.get("bot_token_env", "SLACK_BOT_TOKEN"),
            quota_refresh=float(obj.get("status", {}).get("quota_fallback_refresh_seconds", 300)),
            quota_stale=float(obj.get("status", {}).get("quota_stale_after_seconds", 600)),
            environment_id=desktop.get("environment_id"),
            message_apps=tuple(slack.get("message_app_allowlist", [])),
            update_budget=int(obj.get("status", {}).get("update_budget_per_minute", 30)),
            artifact_max_bytes=int(obj.get("artifacts", {}).get("max_file_bytes", 100_000_000)),
            native_state_db=Path(desktop["state_db"]) if desktop.get("state_db") else None,
            language=obj.get("ui", {}).get("language", "en"),
            artifact_auto_send=obj.get("artifacts", {}).get("auto_send", True),
            artifact_auto_send_max_bytes=obj.get("artifacts", {}).get("auto_send_max_bytes", 30_000_000),
        )
        for val, prefix in [(cfg.app_id, "A"), (cfg.team_id, "T"), (cfg.user_id, "U"), (cfg.dm_id, "D")]:
            if not re.fullmatch(prefix + r"[A-Z0-9]+", val):
                raise BridgeError("invalid_slack_identity")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cfg.host_id):
            raise BridgeError("invalid_host_id")
        if not all(re.fullmatch(r"[A-Z_][A-Z0-9_]*", x) for x in (cfg.app_token_env, cfg.bot_token_env)):
            raise BridgeError("invalid_environment_variable_name")
        if not (1000 <= cfg.message_age_ms <= 3600000 and 1 <= cfg.request_timeout <= 60
                and 1 <= cfg.root_interval <= 60):
            raise BridgeError("invalid_time_configuration")
        if not 30 <= cfg.quota_refresh <= cfg.quota_stale <= 86400:
            raise BridgeError("invalid_quota_configuration")
        if cfg.environment_id is not None and (not isinstance(cfg.environment_id, str) or not safe_text(cfg.environment_id)):
            raise BridgeError("invalid_environment_id")
        if any(not isinstance(v, str) or not re.fullmatch(r"A[A-Z0-9]+", v) for v in cfg.message_apps):
            raise BridgeError("invalid_message_app_allowlist")
        if not 1 <= cfg.update_budget <= 120:
            raise BridgeError("invalid_status_update_budget")
        if not 1 <= cfg.artifact_max_bytes <= 100_000_000:
            raise BridgeError("invalid_artifact_size_limit")
        if (cfg.language not in {"en", "zh-CN"} or type(cfg.artifact_auto_send) is not bool
                or type(cfg.artifact_auto_send_max_bytes) is not int
                or not 1 <= cfg.artifact_auto_send_max_bytes <= 100_000_000):
            raise BridgeError("invalid_bridge_preferences")
        if not all(p.is_absolute() and p.is_file() for p in (cfg.desktop_exe, cfg.backend_exe)):
            raise BridgeError("configured_executable_not_found")
        if cfg.native_state_db is not None and (not cfg.native_state_db.is_absolute() or
                                               not cfg.native_state_db.is_file() or
                                               cfg.native_state_db.name != "state_5.sqlite"):
            raise BridgeError("desktop_ipc_catalog_unavailable")
        return cfg
    except BridgeError:
        raise
    except (KeyError, ValueError, TypeError, OSError, RuntimeError):
        raise BridgeError("invalid_configuration") from None


def child_environment(cfg: Config) -> dict[str, str]:
    env = dict(os.environ)
    for key in (cfg.app_token_env, cfg.bot_token_env, "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN",
                "P0D_APP_TOKEN", "P0D_BOT_TOKEN", "CODEX_APP_SERVER_WS_URL"):
        env.pop(key, None)
    return env
