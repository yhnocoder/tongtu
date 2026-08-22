from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

CONFIG_ROOT_ENV = "XDG_CONFIG_HOME"
DEFAULT_CONFIG_ROOT = Path("~/.config")

CONFIG_DIRNAME = "tongtu"

CREDENTIALS_FILENAME = "credentials.json"
CREDENTIALS_FILE_MODE = 0o600

API_KEY_FIELD = "opencode_api_key"
LOGIN_NOTICE_FIELD = "opencode_login_notice_shown"


@dataclass(frozen=True)
class Credentials:
    opencode_api_key: str = ""
    opencode_login_notice_shown: bool = False


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    root = (environ.get(CONFIG_ROOT_ENV) or "").strip()
    if root:
        return Path(root).expanduser() / CONFIG_DIRNAME
    return DEFAULT_CONFIG_ROOT.expanduser() / CONFIG_DIRNAME


def credentials_path(env: Mapping[str, str] | None = None) -> Path:
    return config_dir(env) / CREDENTIALS_FILENAME


def load_credentials(env: Mapping[str, str] | None = None) -> Credentials:
    try:
        data = json.loads(credentials_path(env).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Credentials()
    if not isinstance(data, dict):
        return Credentials()
    api_key = data.get(API_KEY_FIELD)
    notice_shown = data.get(LOGIN_NOTICE_FIELD)
    return Credentials(
        opencode_api_key=api_key if isinstance(api_key, str) else "",
        opencode_login_notice_shown=notice_shown if isinstance(notice_shown, bool) else False,
    )


def store_api_key(key: str, env: Mapping[str, str] | None = None) -> Path:
    return _write(replace(load_credentials(env), opencode_api_key=key), env)


def mark_login_notice_shown(env: Mapping[str, str] | None = None) -> Path:
    return _write(replace(load_credentials(env), opencode_login_notice_shown=True), env)


def _write(credentials: Credentials, env: Mapping[str, str] | None) -> Path:
    path = credentials_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {
        API_KEY_FIELD: credentials.opencode_api_key,
        LOGIN_NOTICE_FIELD: credentials.opencode_login_notice_shown,
    }
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, CREDENTIALS_FILE_MODE)
    return path
