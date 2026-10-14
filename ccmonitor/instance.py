"""One ccmonitor at a time (#71): a named mutex, plus a hidden window the next launch talks to.

- A second launch tells the running one to show its window, then exits (with --tray it
  just exits: a login start finding ccmonitor already running has nothing to do).
- --replace tells the running one to quit, waits until it has released the mutex and the
  ports, and takes over: how a merge is deployed, and how a worktree tests a fix.

Both names carry the hook port, so a throwaway instance on another port (a smoke test)
never meets the real one. Untested by design (#63), like the tray.
"""
import ctypes
import logging
import socket
import time
from ctypes import wintypes
from typing import Callable

from .win32 import HiddenWindow, kernel32, user32

log = logging.getLogger("ccmonitor")

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0, WAIT_ABANDONED = 0x0, 0x80
ASFW_ANY = 0xFFFFFFFF
CLASS_NAME = "ccmonitorInstance"
SHOW_MESSAGE, QUIT_MESSAGE = "ccmonitor-show", "ccmonitor-quit"

user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]


def mutex_name(port: int) -> str:
    return f"Local\\ccmonitor-{port}"


def window_title(port: int) -> str:
    return f"ccmonitor-instance-{port}"


class AlreadyRunning(Exception):
    """Another ccmonitor holds the mutex (and --replace was not asked)."""


def claim(port: int, replace: bool = False, wait: float = 30) -> int:
    """Returns the mutex handle, held until the process ends. Raises AlreadyRunning, or
    TimeoutError when --replace could not make the running one let go in `wait` seconds."""
    handle = kernel32.CreateMutexW(None, True, mutex_name(port))
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return handle
    if not replace:
        kernel32.CloseHandle(handle)
        raise AlreadyRunning()
    log.info("--replace: asking the running ccmonitor to quit")
    deadline = time.monotonic() + wait
    _post(port, QUIT_MESSAGE, deadline)
    remaining = max(0, int((deadline - time.monotonic()) * 1000))
    if kernel32.WaitForSingleObject(handle, remaining) not in (WAIT_OBJECT_0, WAIT_ABANDONED):
        kernel32.CloseHandle(handle)
        raise TimeoutError("the running ccmonitor did not quit")
    _wait_ports_free(port, deadline)
    log.info("--replace: took over")
    return handle


def show_running(port: int) -> bool:
    """Asks the running instance to bring up its window. False if it could not be reached."""
    user32.AllowSetForegroundWindow(ASFW_ANY)  # this launch may raise a window; let it
    return _post(port, SHOW_MESSAGE, time.monotonic() + 10)


def _post(port: int, message: str, deadline: float) -> bool:
    """The running one may still be starting (mutex held, window not yet made): retry."""
    msg = user32.RegisterWindowMessageW(message)
    while True:
        hwnd = user32.FindWindowW(CLASS_NAME, window_title(port))
        if hwnd and user32.PostMessageW(hwnd, msg, 0, 0):
            return True
        if time.monotonic() >= deadline:
            log.warning("running ccmonitor not reachable")
            return False
        time.sleep(0.2)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _wait_ports_free(port: int, deadline: float) -> None:
    """The hook port and the helper relay's (port + 1). Returns at the deadline regardless:
    a port still busy then shows up as the usual 出错 state."""
    while not (_port_free(port) and _port_free(port + 1)):
        if time.monotonic() >= deadline:
            log.warning("port %d still busy after --replace", port)
            return
        time.sleep(0.2)


def tell_user(text: str) -> None:
    """A plain message box, for failures before there is a window (pythonw has no console)."""
    user32.MessageBoxW(None, text, "ccmonitor", 0x30)  # MB_ICONWARNING


class Listener:
    """The hidden window the next launch finds. Made on the Tk thread, so Tk's message loop
    runs its callbacks there, as for the tray, and like the tray's they must not touch Tk
    (gui.Inbox)."""

    def __init__(self, port: int, on_show: Callable[[], None], on_quit: Callable[[], None]):
        self._show_msg = user32.RegisterWindowMessageW(SHOW_MESSAGE)
        self._quit_msg = user32.RegisterWindowMessageW(QUIT_MESSAGE)
        self._on_show, self._on_quit = on_show, on_quit
        self._window = HiddenWindow(CLASS_NAME, window_title(port), self._on_message)

    def close(self) -> None:
        self._window.destroy()

    def _on_message(self, msg, wparam, lparam):
        if msg == self._show_msg:
            self._on_show()
            return 0
        if msg == self._quit_msg:
            log.info("asked to quit by another launch (--replace)")
            self._on_quit()
            return 0
        return None

