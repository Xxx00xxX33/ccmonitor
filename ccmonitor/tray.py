"""The notification-area icon (#66), straight Win32 through ctypes: Shell_NotifyIcon on a
win32.HiddenWindow, created on the Tk thread, so Tk's own message loop dispatches its
messages and every callback runs on the Tk thread: no second thread. But they run inside
the window procedure, where Tk must not be touched: the window hands in callbacks that
only queue the real action (gui.Inbox). Untested by design (#63) apart from that
(tests/test_gui_winproc.py); the behaviour behind each item lives in the controller.
"""
import ctypes
from ctypes import wintypes
from typing import Callable

from .win32 import HiddenWindow, user32

shell32 = ctypes.WinDLL("shell32", use_last_error=True)

WM_NULL, WM_DESTROY, WM_LBUTTONUP, WM_RBUTTONUP = 0x0000, 0x0002, 0x0202, 0x0205
WM_TRAY = 0x8000 + 1  # WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_ERROR = 0x3
ICON_SIZE = 16
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IDI_APPLICATION = 32512


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]


gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
user32.LoadIconW.restype = wintypes.HICON
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, wintypes.LPVOID]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

CLASS_NAME = "ccmonitorTray"


def dot_icon(rgb: str):
    """A filled circle in `rgb` ("#rrggbb") on a transparent 16x16 icon (#67)."""
    r, g, b = (int(rgb[i:i + 2], 16) for i in (1, 3, 5))
    n, centre, radius = ICON_SIZE, (ICON_SIZE - 1) / 2, ICON_SIZE / 2 - 1
    pixels = (ctypes.c_uint32 * (n * n))()
    for y in range(n):
        for x in range(n):
            d = ((x - centre) ** 2 + (y - centre) ** 2) ** 0.5
            alpha = max(0.0, min(1.0, radius + 0.5 - d))  # soft edge
            if alpha:
                a = round(alpha * 255)  # premultiplied BGRA
                pixels[y * n + x] = (a << 24) | (r * a // 255 << 16) | (g * a // 255 << 8) | (b * a // 255)
    mask = (ctypes.c_ubyte * (n * n // 8))()  # all 0: the alpha channel decides
    color_bm = gdi32.CreateBitmap(n, n, 1, 32, pixels)
    mask_bm = gdi32.CreateBitmap(n, n, 1, 1, mask)
    info = ICONINFO(fIcon=True, hbmMask=mask_bm, hbmColor=color_bm)
    icon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(color_bm)
    gdi32.DeleteObject(mask_bm)
    return icon or user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))


class Tray:
    """on_click: left click. menu(): the right-click items as (label, action) pairs, read
    each time the menu opens, with None for a separator; an item whose action is None is
    greyed out. All three are called inside the window procedure: none may touch Tk."""

    def __init__(self, tip: str, on_click: Callable[[], None],
                 menu: Callable[[], list[tuple[str, Callable[[], None] | None] | None]]):
        self._on_click, self._menu = on_click, menu
        # Set before the window exists: it gets messages while being created.
        # Explorer broadcasts TaskbarCreated after it restarts; the icon has to be added again.
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._hwnd = None
        self._icons: dict[str, int] = {}  # set_color's dots, made once each
        self._window = HiddenWindow(CLASS_NAME, CLASS_NAME, self._on_message)
        self._hwnd = self._window.hwnd
        self._data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self._hwnd, uID=1,
                                     uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY,
                                     hIcon=user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION)),
                                     szTip=tip[:127])
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data))

    def set_tip(self, tip: str) -> None:
        self._data.szTip = tip[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data))

    def set_color(self, rgb: str) -> None:
        """The icon: a dot in this colour (#67: running / disabled / error)."""
        if rgb not in self._icons:
            self._icons[rgb] = dot_icon(rgb)
        if self._data.hIcon != self._icons[rgb]:
            self._data.hIcon = self._icons[rgb]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data))

    def balloon(self, title: str, text: str) -> None:
        """A notification balloon with the error icon (#67: only when ccmonitor stopped)."""
        self._data.uFlags |= NIF_INFO
        self._data.szInfoTitle, self._data.szInfo = title[:63], text[:255]
        self._data.dwInfoFlags = NIIF_ERROR
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data))
        self._data.uFlags &= ~NIF_INFO  # later updates don't show it again

    def close(self) -> None:
        if self._hwnd:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data))
            self._window.destroy()
            self._hwnd = None
            for icon in self._icons.values():
                user32.DestroyIcon(icon)
            self._icons.clear()

    def _on_message(self, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == WM_LBUTTONUP:
                self._on_click()
            elif event == WM_RBUTTONUP:
                self._popup()
            return 0
        if msg == self._taskbar_created and self._hwnd:
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data))
            return 0
        return None


    def _popup(self) -> None:
        items = self._menu()
        menu = user32.CreatePopupMenu()
        for i, item in enumerate(items, start=1):
            if item is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, MF_STRING if item[1] else MF_GRAYED, i, item[0])
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self._hwnd)  # else the menu doesn't close on a click elsewhere
        chosen = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                       point.x, point.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if chosen and items[chosen - 1] and items[chosen - 1][1]:
            items[chosen - 1][1]()

