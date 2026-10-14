"""Start ccmonitor at login via the per-user Run key: `"<repo>\\bin\\ccmonitor.exe" --tray`,
the same value the 设置 tab's 开机自启 writes (#71, ADR 0001).

    python -m ccmonitor.autostart --enable | --disable | --status
"""
import argparse
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ccmonitor"
EXE = Path(__file__).resolve().parent.parent / "bin" / "ccmonitor.exe"
OFF, ON, STALE = "off", "on", "stale"  # stale: our value exists but runs something else


def default_command() -> str:
    return f'"{EXE}" --tray'


class WinRegistry:
    """HKCU\\...\\Run values; only the one named value is ever touched."""

    def __init__(self):
        import winreg
        self._w = winreg

    def get(self, name: str) -> str | None:
        try:
            with self._w.OpenKey(self._w.HKEY_CURRENT_USER, RUN_KEY) as k:
                return self._w.QueryValueEx(k, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        with self._w.CreateKey(self._w.HKEY_CURRENT_USER, RUN_KEY) as k:
            self._w.SetValueEx(k, name, 0, self._w.REG_SZ, value)

    def delete(self, name: str) -> None:
        try:
            with self._w.OpenKey(self._w.HKEY_CURRENT_USER, RUN_KEY, 0, self._w.KEY_SET_VALUE) as k:
                self._w.DeleteValue(k, name)
        except FileNotFoundError:
            pass


class Autostart:
    def __init__(self, registry, command: str):
        self._reg = registry
        self._command = command

    def enable(self) -> None:
        self._reg.set(VALUE_NAME, self._command)

    def disable(self) -> None:
        self._reg.delete(VALUE_NAME)

    def enabled(self) -> bool:
        return self._reg.get(VALUE_NAME) == self._command

    def state(self) -> str:
        current = self._reg.get(VALUE_NAME)
        if current is None:
            return OFF
        return ON if current == self._command else STALE

    def status(self) -> str:
        current = self._reg.get(VALUE_NAME)
        if current is None:
            return "autostart: off"
        if current == self._command:
            return f"autostart: on\n  {current}"
        return f"autostart: on, but points elsewhere (re-run --enable to update)\n  {current}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Start ccmonitor (bin\\ccmonitor.exe --tray) at Windows login")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    g.add_argument("--status", action="store_true")
    args = ap.parse_args()
    a = Autostart(WinRegistry(), default_command())
    if args.enable:
        a.enable()
    elif args.disable:
        a.disable()
    print(a.status())


if __name__ == "__main__":
    main()
