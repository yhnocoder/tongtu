"""跨论文的全局配置目录，与其中 credentials 文件的读写。

配置目录放与论文无关的全局内容：`$XDG_CONFIG_HOME/tongtu/`，环境变量未设时退化为
`~/.config/tongtu/`。目录内两样内容：全局 input glossary `glossary.json`（survey 三层合并
里最低的一层，解析与合并在 `tongtu/glossary.py`，本模块只给出它的路径）与本模块管理的
`credentials.json`。

`credentials.json` 由通途写入（0600 权限），也可手改，字段两个：

- `opencode_api_key`：用户在通途里录入的 OpenCode 密钥；
- `opencode_login_notice_shown`：借用本机 opencode 登录态时「已提醒过一次」的标记位。

读取对文件缺失与不可解析都宽容，一律按默认值处理：credentials 缺失是常态（用环境变量
或 opencode 登录态的用户不需要它），坏文件的影响也只是回到「没有录入过密钥」的状态。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .glossary import GLOSSARY_FILENAME

#: 配置目录根的环境变量名与未设时的默认根。
CONFIG_ROOT_ENV = "XDG_CONFIG_HOME"
DEFAULT_CONFIG_ROOT = Path("~/.config")

#: 配置目录在根下的目录名。
CONFIG_DIRNAME = "tongtu"

#: credentials 文件名与写出权限（只限本用户读写：内容是密钥）。
CREDENTIALS_FILENAME = "credentials.json"
CREDENTIALS_FILE_MODE = 0o600

#: credentials.json 的两个字段名。
API_KEY_FIELD = "opencode_api_key"
LOGIN_NOTICE_FIELD = "opencode_login_notice_shown"


@dataclass(frozen=True)
class Credentials:
    """credentials.json 的内容；文件缺失或不可解析时两个字段都取默认值。"""

    opencode_api_key: str = ""
    opencode_login_notice_shown: bool = False


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """配置目录：`$XDG_CONFIG_HOME/tongtu`，环境变量未设时 `~/.config/tongtu`。"""
    environ = os.environ if env is None else env
    root = (environ.get(CONFIG_ROOT_ENV) or "").strip()
    if root:
        return Path(root).expanduser() / CONFIG_DIRNAME
    return DEFAULT_CONFIG_ROOT.expanduser() / CONFIG_DIRNAME


def credentials_path(env: Mapping[str, str] | None = None) -> Path:
    """credentials.json 的路径（不保证文件存在）。"""
    return config_dir(env) / CREDENTIALS_FILENAME


def glossary_path(env: Mapping[str, str] | None = None) -> Path:
    """全局 input glossary 的路径（不保证文件存在）；它是 survey 三层合并里最低的一层。"""
    return config_dir(env) / GLOSSARY_FILENAME


def load_credentials(env: Mapping[str, str] | None = None) -> Credentials:
    """读 credentials.json；文件缺失、不可解析或字段类型不符都按默认值处理。"""
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
    """把录入的密钥写进 credentials.json（另一字段保留原值），返回写入路径。"""
    return _write(replace(load_credentials(env), opencode_api_key=key), env)


def mark_login_notice_shown(env: Mapping[str, str] | None = None) -> Path:
    """记下「借用 opencode 登录态已提醒过一次」的标记位，返回写入路径。"""
    return _write(replace(load_credentials(env), opencode_login_notice_shown=True), env)


def _write(credentials: Credentials, env: Mapping[str, str] | None) -> Path:
    """写出 credentials.json 并收紧权限；目录不存在则建出。"""
    path = credentials_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {
        API_KEY_FIELD: credentials.opencode_api_key,
        LOGIN_NOTICE_FIELD: credentials.opencode_login_notice_shown,
    }
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, CREDENTIALS_FILE_MODE)
    return path
