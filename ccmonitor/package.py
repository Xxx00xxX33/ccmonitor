"""Run ccmonitor outside Claude Desktop's app package.

Claude Desktop from the Microsoft Store is a packaged app, and everything started from a
Claude session runs inside its package. Windows virtualizes what such a process writes to
HKCU: found 2026-10-13 after a reboot, 开机自启 ticked in a ccmonitor started from a
session wrote the Run value only into the package's copy, Explorer at login read the real
one, still pointing at the deleted .pyw launcher, and ccmonitor did not start.

So a ccmonitor that finds itself inside a package starts itself again outside it, the way
a login start runs, and exits. WMI's Win32_Process.Create is what breaks out: the process
it creates belongs to the WMI service, not to us.
"""
import ctypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

APPMODEL_ERROR_NO_PACKAGE = 15700
ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000


def has_package_identity() -> bool:
    length = ctypes.c_uint32(0)
    try:
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
    except (AttributeError, OSError):
        return False
    return rc != APPMODEL_ERROR_NO_PACKAGE


def inside(env: Mapping[str, str] = os.environ,
           identity: Callable[[], bool] = has_package_identity) -> bool:
    """Whether this process may be inside Claude's package. Processes Claude Desktop starts
    have no package identity of their own (GetCurrentPackageFullName says so, 2026-10-13),
    yet their HKCU writes are still redirected; what marks them is the environment Claude
    Code gives its sessions. A Claude Code started from a plain terminal has it too: moving
    that one outside does no harm."""
    return identity() or bool(env.get("CLAUDECODE"))


def wmi_create(command_line: str, cwd: str) -> bool:
    """Starts command_line through WMI, so outside our package. True when it started."""
    script = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              "-Arguments @{CommandLine=$env:CCMONITOR_RELAUNCH; CurrentDirectory=$env:CCMONITOR_RELAUNCH_CWD}; "
              "exit $r.ReturnValue")
    env = {**os.environ, "CCMONITOR_RELAUNCH": command_line, "CCMONITOR_RELAUNCH_CWD": cwd}
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                              env=env, creationflags=CREATE_NO_WINDOW, timeout=30,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def relaunch_outside(args: list[str], *, port: int, default_port: int,
                     env: Mapping[str, str] = os.environ,
                     is_inside: Callable[[Mapping[str, str]], bool] = inside, python: str = sys.executable,
                     cwd: str = str(ROOT),
                     create: Callable[[str, str], bool] = wmi_create) -> bool:
    """True when ccmonitor was started again outside the package and this one should exit.

    Only the real instance moves: the new process gets the user's own environment, so a
    throwaway instance's port or CCMONITOR_HOME would be lost. It stays where it is."""
    if not is_inside(env) or port != default_port or env.get("CCMONITOR_HOME"):
        return False
    command = subprocess.list2cmdline([python, "-m", "ccmonitor.gui", *args, "--port", str(port)])
    return create(command, cwd)
