"""The GUI's own settings (#69): settings.json in the storage directory.

Kept apart from config.json (the Telegram connection) so the two change independently.
Keys: away_minutes (离开时间), rotate_hours (换班间隔, mode B), enabled (启用/停用). Unknown keys are kept on save, so
later settings can sit beside these.
"""
import json
import logging
import re
from pathlib import Path

from . import paths

log = logging.getLogger("ccmonitor")

FILE_NAME = "settings.json"

AWAY_MIN, AWAY_MAX, AWAY_DEFAULT = 1, 60, 3  # 离开时间, whole minutes
AWAY_RANGE = f"离开时间须为 {AWAY_MIN}–{AWAY_MAX} 之间的整数分钟"
ROTATE_MIN, ROTATE_MAX, ROTATE_DEFAULT = 1, 24, 2  # 换班间隔 of the mode-B helper, whole hours
ROTATE_RANGE = f"换班间隔须为 {ROTATE_MIN}–{ROTATE_MAX} 之间的整数小时"


def _whole(value, low: int, high: int, message: str) -> int:
    if isinstance(value, bool):
        raise ValueError(message)
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        n = int(value.strip())
    else:
        raise ValueError(message)
    if not low <= n <= high:
        raise ValueError(message)
    return n


def parse_away_minutes(value) -> int:
    """Whole minutes from the settings field; ValueError with a message to show otherwise."""
    return _whole(value, AWAY_MIN, AWAY_MAX, AWAY_RANGE)


def parse_rotate_hours(value) -> int:
    """Whole hours from the 换班间隔 field; ValueError with a message to show otherwise."""
    return _whole(value, ROTATE_MIN, ROTATE_MAX, ROTATE_RANGE)


def load(data_dir: Path) -> dict:
    """The stored settings; {} when missing or unreadable (each reader falls back to its default)."""
    try:
        data = json.loads((Path(data_dir) / FILE_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("settings unreadable, using defaults: %s", type(e).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def save(data_dir: Path, **changes) -> None:
    """Merges `changes` into settings.json, written whole through a temporary file."""
    data = {**load(data_dir), **changes}
    paths.write_text_atomic(Path(data_dir) / FILE_NAME, json.dumps(data, ensure_ascii=False, indent=2))


def away_minutes(data: dict) -> int:
    try:
        return parse_away_minutes(data.get("away_minutes", AWAY_DEFAULT))
    except ValueError:
        log.warning("stored away_minutes out of range; using %d", AWAY_DEFAULT)
        return AWAY_DEFAULT


def rotate_hours(data: dict) -> int:
    try:
        return parse_rotate_hours(data.get("rotate_hours", ROTATE_DEFAULT))
    except ValueError:
        log.warning("stored rotate_hours out of range; using %d", ROTATE_DEFAULT)
        return ROTATE_DEFAULT
