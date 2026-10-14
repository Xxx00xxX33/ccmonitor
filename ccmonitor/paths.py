"""Where ccmonitor keeps its files, and where Claude Desktop keeps its sessions.

Claude Desktop from the Microsoft Store is a packaged app: Windows redirects what its
processes write under AppData into the package's own folder
(%LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\...). A daemon started from a Claude
session runs inside that package and sees the redirected AppData; the same daemon
started at login runs outside it and sees the real one, where Desktop's data does not
exist. Found 2026-10-13 after a reboot: the login-started daemon found no session
titles, so it never read a suggestion, and it kept its topics in a second, empty file.

So ccmonitor's own files live in %USERPROFILE%\\.ccmonitor, which Windows does not
redirect, and Desktop's sessions are looked up in both places.

The storage directory can be moved (#70): the choice is DataDir in bin\\ccmonitor.ini,
beside the exe (ADR 0001), never the registry. CCMONITOR_HOME still overrides it. A
worktree's bin\\ has no ini, so running from one uses the default.
"""
import configparser
import io

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Mapping

log = logging.getLogger("ccmonitor")

INI_PATH = Path(__file__).resolve().parent.parent / "bin" / "ccmonitor.ini"
INI_SECTION, INI_KEY = "ccmonitor", "DataDir"


def default_data_dir(env: Mapping[str, str] = os.environ) -> Path:
    """%USERPROFILE%\\.ccmonitor, the profile taken from `env` (tests pass a fake one)."""
    return Path(env.get("USERPROFILE") or Path.home()) / ".ccmonitor"


def ini_data_dir(ini: Path = INI_PATH) -> Path | None:
    """DataDir from the ini; None when the file, the key or a value is missing or unreadable."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(ini, encoding="utf-8-sig") as f:
            parser.read_file(f)
    except FileNotFoundError:
        return None
    except (OSError, configparser.Error, UnicodeDecodeError) as e:
        log.warning("%s unreadable, ignored: %s", ini.name, type(e).__name__)
        return None
    value = parser.get(INI_SECTION, INI_KEY, fallback="").strip()
    if not value:
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else ini.parent / path


def write_ini_data_dir(data_dir: Path, ini: Path = INI_PATH) -> None:
    """Records the chosen storage directory, keeping anything else in the ini."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep "DataDir" as written
    try:
        with open(ini, encoding="utf-8-sig") as f:
            parser.read_file(f)
    except FileNotFoundError:
        pass
    except (OSError, configparser.Error, UnicodeDecodeError):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
    if not parser.has_section(INI_SECTION):
        parser.add_section(INI_SECTION)
    parser.set(INI_SECTION, INI_KEY, str(data_dir))
    text = io.StringIO()
    parser.write(text)
    write_text_atomic(ini, text.getvalue())


def write_text_atomic(path: Path, text: str) -> None:
    """Writes the whole file through a temporary one beside it, so a crash or a full disk
    never leaves it half written (config.json, settings.json, ccmonitor.ini)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def resolve_data_dir(env: Mapping[str, str] = os.environ, ini: Path = INI_PATH) -> Path:
    """CCMONITOR_HOME, else the ini's DataDir, else %USERPROFILE%\\.ccmonitor."""
    if env.get("CCMONITOR_HOME"):
        return Path(env["CCMONITOR_HOME"])
    return ini_data_dir(ini) or default_data_dir(env)


# The process's storage directory. Read it at use time (paths.DATA_DIR), not copied at
# import: the GUI changes it in place when the user moves the folder (use()).
DATA_DIR = resolve_data_dir()


def relay_down_mark(env: Mapping[str, str] = os.environ) -> Path:
    """The hooks' "daemon is down" mark: hooks/relay.py down_mark(), which can't import
    this package. Never part of the storage directory's files, so it never moves."""
    return Path(env.get("LOCALAPPDATA") or Path.home()) / "ccmonitor" / "relay-down"


def use(data_dir: Path) -> None:
    """Points this process, and the PowerShell it starts (desktop.py), at a new storage directory."""
    global DATA_DIR
    DATA_DIR = Path(data_dir)


def _packages(env: Mapping[str, str] = os.environ) -> list[Path]:
    local = env.get("LOCALAPPDATA")
    if not local:
        return []
    return sorted((Path(local) / "Packages").glob("Claude_*"))


def desktop_sessions_root() -> Path:
    """Desktop's claude-code-sessions folder: the one seen through %APPDATA% when it exists
    (an unpackaged install, or a process inside the package), else the package's own copy.
    When both exist, the one written to most recently."""
    candidates = []
    roaming = os.environ.get("APPDATA")
    if roaming:
        candidates.append(Path(roaming) / "Claude" / "claude-code-sessions")
    candidates += [p / "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions" for p in _packages()]
    found = [c for c in candidates if c.is_dir()]
    if not found:
        return candidates[0] if candidates else Path.home() / "Claude" / "claude-code-sessions"
    return max(found, key=lambda c: c.stat().st_mtime)


def _legacy_dirs(kind: str, env: Mapping[str, str] = os.environ) -> list[Path]:
    """Where older versions kept a file: %APPDATA% (kind "Roaming") or %LOCALAPPDATA%
    ("Local"), as seen from here and as redirected into a Claude package."""
    var = "APPDATA" if kind == "Roaming" else "LOCALAPPDATA"
    dirs = [Path(env[var]) / "ccmonitor"] if env.get(var) else []
    return dirs + [p / "LocalCache" / kind / "ccmonitor" for p in _packages(env)]


def migrate(data_dir: Path | None = None) -> list[str]:
    """Bring files older versions kept in AppData into DATA_DIR, once: run by the daemon
    at start, never on import. Returns the names it wrote.

    relay-mode.json: the newest copy. topics.json: every copy merged, keeping for each
    session the pairing active most recently, since a daemon started inside the package
    and one started at login each kept their own (2026-10-13: 20 topics in one, 1 in the
    other) and the newest file is not the fullest.
    """
    data_dir = data_dir or DATA_DIR
    wrote = []
    mode = data_dir / "relay-mode.json"
    if not mode.exists():
        old = [d / mode.name for d in _legacy_dirs("Local") if (d / mode.name).is_file()]
        if old:
            data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(max(old, key=lambda p: p.stat().st_mtime), mode)
            wrote.append(mode.name)
    topics = data_dir / "topics.json"
    if not topics.exists():
        merged: dict = {}
        for d in _legacy_dirs("Local"):
            try:
                data = json.loads((d / topics.name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for sid, v in data.items():
                active = v[1] if isinstance(v, list) and len(v) == 2 else 0
                had = merged.get(sid)
                if had is None or active > (had[1] if isinstance(had, list) else 0):
                    merged[sid] = v
        if merged:
            data_dir.mkdir(parents=True, exist_ok=True)
            topics.write_text(json.dumps(merged), encoding="utf-8")
            wrote.append(topics.name)
    return wrote


def config_file(data_dir: Path | None = None, env: Mapping[str, str] = os.environ) -> Path:
    """The Telegram config: DATA_DIR/config.json, else where older versions read it
    (%APPDATA% taken from `env`). Not copied, so the token is not duplicated. Saving always
    writes DATA_DIR/config.json, which then wins.

    A CCMONITOR_HOME in `env` gets no fallback: it is a throwaway instance's own storage.
    Found 2026-10-14: one on a temp folder read the real token from %APPDATA% and polled
    the real bot, so the running daemon got 409 Conflict."""
    target = (data_dir or DATA_DIR) / "config.json"
    if target.exists() or env.get("CCMONITOR_HOME"):
        return target
    for d in _legacy_dirs("Roaming", env):
        if (d / "config.json").is_file():
            return d / "config.json"
    return target
