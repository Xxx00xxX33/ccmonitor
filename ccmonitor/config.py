"""Local configuration: Telegram bot token and the one allowed chat id.

Lives outside the repo (default %USERPROFILE%/.ccmonitor/config.json, or where older
versions kept it under %APPDATA%; see paths.py); environment
variables override the file. The token never appears in repr or logs.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import paths


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    chat_id: int


def load_config(path: Path | None = None, env: Mapping[str, str] = os.environ) -> Config | None:
    """path None: paths.config_file() looked up now, so a config the Telegram tab just
    saved is what the restarted daemon reads."""
    path = path or paths.config_file()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    token = env.get("CCMONITOR_TELEGRAM_TOKEN") or data.get("telegram_token")
    chat_id = env.get("CCMONITOR_CHAT_ID") or data.get("chat_id")
    if not token or not chat_id:
        return None
    return Config(token, int(chat_id))
