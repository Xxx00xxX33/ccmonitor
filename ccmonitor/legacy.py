"""The old background daemon (before #71): `pythonw bin\\ccmonitor-daemon.pyw`, started by
the old Run value. The window offers 「接管」 while one holds the port; it ends only those
processes, never another Python program.
"""
import ctypes
import json
import logging
import subprocess
from dataclasses import dataclass

from .win32 import kernel32

log = logging.getLogger("ccmonitor")

OLD_LAUNCHER = "ccmonitor-daemon.pyw"
PYTHONW = "pythonw.exe"


@dataclass(frozen=True)
class Proc:
    pid: int
    name: str
    command_line: str | None  # None when Windows won't tell


def is_old_daemon(p: Proc) -> bool:
    return p.name.lower() == PYTHONW and OLD_LAUNCHER in (p.command_line or "").lower()


def pythonw_processes() -> list[Proc]:
    """Every pythonw.exe with its command line, through CIM (the stdlib can't read another
    process's command line). Takes about a second; only asked when the port is busy."""
    script = ("Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
              "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, timeout=20, encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("process list unavailable: %s", type(e).__name__)
        return []
    if not out:
        return []
    try:
        rows = json.loads(out)
    except ValueError:
        log.warning("process list unreadable")
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return [Proc(int(r["ProcessId"]), r.get("Name") or "", r.get("CommandLine")) for r in rows]


PROCESS_TERMINATE, SYNCHRONIZE = 0x0001, 0x00100000


def kill(pid: int, wait_ms: int = 5000) -> None:
    """Ends the process and waits until it is gone, so its port is free on return."""

    handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not handle:
        log.warning("cannot open old daemon %d: error %d", pid, ctypes.get_last_error())
        return
    try:
        kernel32.TerminateProcess(handle, 1)
        kernel32.WaitForSingleObject(handle, wait_ms)
    finally:
        kernel32.CloseHandle(handle)
    log.info("ended old daemon %d", pid)
