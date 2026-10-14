"""Look up a session's Desktop title from its CLI session id.

Reads Claude Desktop's own session metadata read-only; only cliSessionId, title,
isArchived and lastActivityAt are looked at.
"""
import json
import os
from pathlib import Path

from . import paths

SESSIONS_ROOT = paths.desktop_sessions_root()


def find_title(cli_session_id: str, root: Path = SESSIONS_ROOT) -> str | None:
    found = _find(cli_session_id, root)
    return found["title"] if found else None


def find_local_id(cli_session_id: str, root: Path = SESSIONS_ROOT) -> str | None:
    found = _find(cli_session_id, root)
    return found.get("sessionId") if found else None


def find_cli_by_tag(tag: str, root: Path = SESSIONS_ROOT) -> str | None:
    """The CLI session id starting with `tag` (a notice's #tag), if exactly one live session has it."""
    found = set()
    for f in root.glob("*/*/local_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cli = d.get("cliSessionId") or ""
        if cli.startswith(tag) and not d.get("isArchived"):
            found.add(cli)
    return found.pop() if len(found) == 1 else None


def _find(cli_session_id: str, root: Path) -> dict | None:
    best, best_at = None, -1
    for f in root.glob("*/*/local_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("cliSessionId") != cli_session_id or d.get("isArchived") or not d.get("title"):
            continue
        at = d.get("lastActivityAt") or 0
        if at > best_at:
            best, best_at = {"title": d["title"], "sessionId": d.get("sessionId")}, at
    return best
