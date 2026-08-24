from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

CONFIG_ROOT_ENV = "XDG_CONFIG_HOME"
DEFAULT_CONFIG_ROOT = Path("~/.config")

CONFIG_DIRNAME = "tongtu"


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    root = (environ.get(CONFIG_ROOT_ENV) or "").strip()
    if root:
        return Path(root).expanduser() / CONFIG_DIRNAME
    return DEFAULT_CONFIG_ROOT.expanduser() / CONFIG_DIRNAME
