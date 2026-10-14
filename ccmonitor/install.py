"""Add or remove ccmonitor's hook entries in a Claude Code settings file.

Our entries are recognised by the relay script path, so other hooks are left alone.
"""
import argparse
import json
import sys
from pathlib import Path

EVENTS = ["SessionStart", "UserPromptSubmit", "PermissionRequest", "Stop", "StopFailure", "SessionEnd",
          "PostToolUse", "PostToolUseFailure"]
# Fired on every tool call, only to take settled approval buttons off (sidechannel), so
# they run in the background: Claude Code doesn't wait for them (~0.4 s per relay start).
ASYNC_EVENTS = {"PostToolUse", "PostToolUseFailure"}
MARKER = "hooks/relay.py"
RELAY = Path(__file__).resolve().parent.parent / "hooks" / "relay.py"
USER_SETTINGS = Path.home() / ".claude" / "settings.json"


def default_command() -> str:
    return f'"{Path(sys.executable).as_posix()}" -I "{RELAY.as_posix()}"'


def _is_ours(group: dict) -> bool:
    return any(MARKER in h.get("command", "") for h in group.get("hooks", []))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _strip(settings: dict) -> dict:
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not _is_ours(g)]
        if not hooks[event]:
            del hooks[event]
    if "hooks" in settings and not hooks:
        del settings["hooks"]
    return settings


def _save(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def install(path: Path, command: str, dry_run: bool = False) -> dict:
    settings = _strip(_load(path))
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        hook = {"type": "command", "command": command, "timeout": 5}
        if event in ASYNC_EVENTS:
            hook["async"] = True
        hooks.setdefault(event, []).append({"hooks": [hook]})
    if not dry_run:
        _save(path, settings)
    return settings


def uninstall(path: Path, dry_run: bool = False) -> dict:
    settings = _strip(_load(path))
    if not dry_run and path.exists():
        _save(path, settings)
    return settings


def main() -> None:
    ap = argparse.ArgumentParser(description="Install ccmonitor hooks into a Claude Code settings file")
    ap.add_argument("--settings", type=Path, default=USER_SETTINGS, help=f"default: {USER_SETTINGS}")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the resulting hooks without writing")
    args = ap.parse_args()
    if args.uninstall:
        result = uninstall(args.settings, args.dry_run)
    else:
        result = install(args.settings, default_command(), args.dry_run)
    print(json.dumps(result.get("hooks", {}), indent=2, ensure_ascii=False))
    print(("would write " if args.dry_run else "wrote ") + str(args.settings))


if __name__ == "__main__":
    main()
