"""The ccmonitor window and tray (#63, #66). bin\\ccmonitor.exe runs it (ADR 0001):

    pythonw -m ccmonitor.gui [--port N] [--tray] [--replace]

--tray starts hidden in the tray (开机自启); --replace makes a running ccmonitor quit and
takes over (after a merge, or to test a worktree). Without it a second launch only brings
up the running one's window (instance.py).

A thin view: it draws the controller's snapshot and calls its methods, nothing else.
Minimize and close both hide the window into the tray; while hidden the window is not
refreshed, only the tray icon's colour is kept up (and its balloon raised when ccmonitor
stops by itself). 「退出 ccmonitor」 in the tray menu stops the daemon and ends the process.
"""
import argparse
import base64
import collections
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import autostart, icons, instance, package, paths
from .controller import DISABLED, ERROR, RUNNING, UNCONFIGURED, Controller, Snapshot, TelegramResult
from .daemon import DEFAULT_PORT, Service, setup_logging
from .settings import AWAY_MAX, AWAY_MIN
from .tray import Tray

WIDTH = 360
REFRESH_MS = 2000  # while the window is visible
FETCH_ROUNDS, FETCH_WAIT = 4, 15  # 「获取 Chat ID」 waits up to a minute for a message
GREEN, RED, AMBER, GREY, MUTED = "#1a7f37", "#cf222e", "#9a6700", "#8c959f", "#57606a"
COLORS = {RUNNING: GREEN, ERROR: RED, UNCONFIGURED: AMBER}  # the state's text; anything else: MUTED
# The tray's three dots (#67), also the exe's icons (build_exe); 未配置 isn't running
# either, so it is grey like 已停用.
TRAY_COLORS = {RUNNING: GREEN, DISABLED: GREY, ERROR: RED}
TRAY_MS = 2000  # the tray icon's check, also while the window is hidden: state only
INBOX_MS = 50  # how soon a tray click or a later launch's request is acted on

log = logging.getLogger("ccmonitor")


class Inbox:
    """Actions that arrive in a Win32 window procedure (the tray icon, a later launch's
    request: tray.py, instance.py), run a moment later from a Tk timer instead.

    Tk must not be touched inside such a procedure. Tk's own loop dispatches it, and a Tk
    call made there (even root.after) clears _tkinter's saved thread state; the next
    Python callback Tcl runs before that event ends, typically a timer that fell due while
    the tray menu was open, then aborts the process ("Fatal Python error:
    PyEval_RestoreThread: ... thread state is NULL"; under pythonw 0xc0000409, no log
    line). So the procedure only queues the action, which touches no Tk."""

    def __init__(self):
        self._actions: collections.deque = collections.deque()

    def later(self, action: Callable[[], None]) -> Callable[[], None]:
        """A stand-in for `action` to hand to a window procedure."""
        return lambda: self._actions.append(action)

    def run_pending(self) -> None:
        """On the Tk thread, from a timer: the queued actions, oldest first."""
        while self._actions:
            self._actions.popleft()()


class Window:
    def __init__(self, root: tk.Tk, controller: Controller):
        self.root, self.c = root, controller
        self._timer: str | None = None
        # One controller call that may block (starting or stopping the daemon, 接管, a
        # storage move, a Telegram request) runs at a time, off the Tk thread; meanwhile
        # the buttons that would start another are greyed out.
        self._busy = False
        self._doing = ""  # shown under the state while it runs, e.g. 正在启用…
        root.title("ccmonitor")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.bind("<Unmap>", self._on_unmap)

        tabs = ttk.Notebook(root, width=WIDTH - 16)
        tabs.pack(fill="both", expand=True, padx=8, pady=8)
        status = ttk.Frame(tabs, padding=12)
        tabs.add(status, text="状态")

        row = ttk.Frame(status)
        row.pack(fill="x")
        ttk.Label(row, text="ccmonitor：").pack(side="left")
        self.state = tk.Label(row, font=("Microsoft YaHei UI", 10, "bold"))
        self.state.pack(side="left")
        self.toggle = ttk.Button(row, width=6, command=self.on_toggle)
        self.toggle.pack(side="right")
        self.detail = ttk.Label(status, wraplength=WIDTH - 48, foreground=MUTED)
        self.detail.pack(fill="x", pady=(8, 0))
        self._build_status_lines(status)
        self._build_takeover(status)

        self.tabs = tabs
        self._build_telegram_tab(tabs)
        self._build_settings_tab(tabs)
        self.inbox = Inbox()  # what the tray and a later launch ask for (see Inbox)
        self._run_inbox()
        self.tray = Tray("ccmonitor", on_click=self.inbox.later(self.show), menu=self.tray_menu)
        self._watch_tray()

    def _run_inbox(self) -> None:
        self.root.after(INBOX_MS, self._run_inbox)  # first: an action may destroy the root
        self.inbox.run_pending()

    # -- 状态 tab: what the phone will get (#67) ----------------------------------------
    def _build_status_lines(self, page: ttk.Frame) -> None:
        grid = ttk.Frame(page)
        grid.pack(fill="x", pady=(10, 0))
        grid.columnconfigure(1, weight=1)
        self.lines: dict[str, ttk.Label] = {}
        for row, (key, name) in enumerate([("telegram", "Telegram"), ("away", "离开状态"),
                                           ("notifications", "通知"), ("relay_mode", "转发模式"),
                                           ("helper", "转发会话")]):
            ttk.Label(grid, text=f"{name}：").grid(row=row, column=0, sticky="nw", pady=2)
            if key == "relay_mode":
                cell = ttk.Frame(grid)
                self.mode = tk.StringVar()
                for mode in ("a", "b"):
                    ttk.Radiobutton(cell, text=mode.upper(), value=mode, variable=self.mode,
                                    command=self.on_mode).pack(side="left", padx=(0, 12))
            else:
                cell = self.lines[key] = ttk.Label(grid, wraplength=WIDTH - 140)
            cell.grid(row=row, column=1, sticky="w", pady=2)

        self.helper_hint = ttk.Frame(page)  # shown only in mode B without a helper session
        ttk.Label(self.helper_hint, wraplength=WIDTH - 48, foreground=AMBER,
                  text="转发模式 B 还没有转发会话，消息先按转发模式 A 排队。点「新建转发会话」，Claude Desktop "
                       "会打开一个填好启动指令的新会话（文件夹、模型、effort 都已设好）；第一次请信任这个文件夹、"
                       "把权限选成 Bypass permissions，再按发送。").pack(fill="x")
        hint_row = ttk.Frame(self.helper_hint)
        hint_row.pack(fill="x", pady=(4, 0))
        ttk.Button(hint_row, text="新建转发会话", command=self.on_new_helper).pack(side="left")
        ttk.Button(hint_row, text="复制启动指令", command=self.on_copy_prompt).pack(side="left", padx=(8, 0))
        self.copied = ttk.Label(hint_row, foreground=MUTED)
        self.copied.pack(side="left", padx=(8, 0))

        self.log_row = ttk.Frame(page)
        self.log_row.pack(fill="x", pady=(12, 0))
        ttk.Button(self.log_row, text="打开日志", command=self.c.open_log).pack(side="left")

    def _draw_status_lines(self, snap: Snapshot) -> None:
        for key, label in self.lines.items():
            label.config(text=getattr(snap, key))
        self.mode.set(snap.relay_mode)
        shown = bool(self.helper_hint.winfo_manager())
        if snap.needs_helper and not shown:
            self.helper_hint.pack(fill="x", pady=(8, 0), before=self.log_row)
        elif not snap.needs_helper and shown:
            self.helper_hint.pack_forget()
            self.copied.config(text="")

    def on_mode(self) -> None:
        self.draw(self.c.set_relay_mode(self.mode.get()))

    def on_new_helper(self) -> None:
        self.copied.config(text=self.c.new_helper_session())

    def on_copy_prompt(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.c.setup_prompt())
        self.copied.config(text="已复制")

    # -- the tray icon: watched whether or not the window is shown (#67) ------------------
    def _draw_tray(self, snap: Snapshot) -> None:
        """The tray icon's colour and tip: the only place either is set."""
        self.tray.set_color(TRAY_COLORS.get(snap.state, TRAY_COLORS[DISABLED]))
        self.tray.set_tip(f"ccmonitor：{snap.state}")

    def _watch_tray(self) -> None:
        self._draw_tray(self.c.snapshot())
        reason = self.c.alert()
        if reason:
            self.tray.balloon("ccmonitor 出错", reason)
        self.root.after(TRAY_MS, self._watch_tray)

    # -- 设置 tab (#69; #70 storage directory and #71 autostart go below) ---------------
    def _build_settings_tab(self, tabs: ttk.Notebook) -> None:
        page = ttk.Frame(tabs, padding=12)
        tabs.add(page, text="设置")
        row = ttk.Frame(page)
        row.pack(fill="x")
        ttk.Label(row, text="离开时间：").pack(side="left")
        self.away = tk.StringVar(value=str(self.c.away_minutes))
        box = ttk.Spinbox(row, from_=AWAY_MIN, to=AWAY_MAX, increment=1, width=5,
                          textvariable=self.away, command=self.on_away)
        box.pack(side="left")
        box.bind("<Return>", lambda e: self.on_away())
        box.bind("<FocusOut>", lambda e: self.on_away())
        ttk.Label(row, text="分钟无键鼠输入即算离开").pack(side="left", padx=(4, 0))
        self.away_error = ttk.Label(page, foreground=RED, wraplength=WIDTH - 48)
        self.away_error.pack(fill="x", pady=(6, 0))
        self._build_storage_row(page)

        # 开机自启 (#71): checked only when the Run value is this exe; 指向旧位置 unchecked
        # with a note, and ticking it once rewrites the value.
        self.autostart = tk.BooleanVar()
        ttk.Checkbutton(page, text="开机自启（登录后在托盘运行，不弹窗口）", variable=self.autostart,
                        command=self.on_autostart).pack(anchor="w", pady=(14, 0))
        self.autostart_note = ttk.Label(page, foreground=AMBER, wraplength=WIDTH - 48)
        self.autostart_note.pack(fill="x", pady=(2, 0))
        self._draw_autostart(self.c.snapshot())

    # -- 存储目录 (#70) ------------------------------------------------------------------
    def _build_storage_row(self, page: ttk.Frame) -> None:
        box = ttk.Frame(page)
        box.pack(fill="x", pady=(12, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="存储目录：").pack(side="left")
        self.storage_button = ttk.Button(row, text="更改…", width=6, command=self.on_change_storage)
        self.storage_button.pack(side="right")
        self.storage_label = ttk.Label(box, wraplength=WIDTH - 48)
        self.storage_label.pack(fill="x", pady=(2, 0))
        self.storage_note = ttk.Label(box, foreground=MUTED, wraplength=WIDTH - 48)
        self.storage_note.pack(fill="x", pady=(2, 0))
        self._draw_storage()

    def _draw_storage(self) -> None:
        self.storage_label.config(text=str(self.c.storage_dir))
        locked = self.c.storage_locked
        self.storage_button.config(state="disabled" if locked or self._busy else "normal")
        self.storage_note.config(text=locked or "配置、话题、日志都在这里；选择记在 exe 旁的 ccmonitor.ini。")

    def on_change_storage(self) -> None:
        from tkinter import filedialog, messagebox
        chosen = filedialog.askdirectory(parent=self.root, title="选择新的存储目录",
                                         initialdir=str(self.c.storage_dir.parent))
        if not chosen:
            return
        try:
            plan = self.c.plan_storage_move(chosen)
        except ValueError as e:
            messagebox.showerror("存储目录", str(e), parent=self.root)
            return
        keep = None
        if plan.config_conflict:
            answer = messagebox.askyesnocancel(
                "存储目录", f"{plan.target} 里已有一份 config.json（Telegram 设置）。\n\n"
                "是：用新目录里的那份，删除当前这份\n否：用当前这份，覆盖新目录里的\n取消：不更改",
                parent=self.root)
            if answer is None:
                return
            keep = "new" if answer else "old"
        if not messagebox.askokcancel(
                "存储目录", f"将把 {plan.files} 个文件从\n{self.c.storage_dir}\n移动到\n{plan.target}\n\n"
                "移动期间 ccmonitor 停用，完成后恢复原来的启用/停用。", parent=self.root):

            return

        def done(result) -> None:
            if result.left:
                messagebox.showwarning("存储目录", "已改用新目录，但旧目录里这些文件未能删除，请手动删除：\n"
                                       + "\n".join(result.left), parent=self.root)

        def failed(e: Exception) -> None:
            messagebox.showerror("存储目录", str(e) if isinstance(e, ValueError) else type(e).__name__,
                                 parent=self.root)

        def after() -> None:
            self._draw_storage()
            self._load_telegram()

        self._in_background(lambda: self.c.move_storage(plan.target, keep_config=keep), done,
                            doing="正在移动存储目录…", failed=failed, after=after)

    def on_autostart(self) -> None:
        try:
            self.c.set_autostart(self.autostart.get())
        except OSError as e:
            log.error("autostart not changed: %s", type(e).__name__)
        self._draw_autostart(self.c.snapshot())

    def _draw_autostart(self, snap: Snapshot) -> None:
        self.autostart.set(snap.autostart == autostart.ON)
        self.autostart_note.config(text="开机自启还指向旧的启动方式，勾选一次即更新为本程序。"
                                   if snap.autostart == autostart.STALE else "")

    # -- 接管 the old pythonw daemon (#71) ------------------------------------------------
    def _build_takeover(self, status: ttk.Frame) -> None:
        self.takeover = ttk.Frame(status)
        self.takeover_button = ttk.Button(self.takeover, text="接管", command=self.on_take_over)
        self.takeover_button.pack(side="left")
        ttk.Label(self.takeover, foreground=MUTED, text="只结束旧的 ccmonitor 后台进程"
                  ).pack(side="left", padx=(6, 0))

    def on_take_over(self) -> None:
        if not self._busy:  # it can wait up to ~20 s for the process list
            self._in_background(self.c.take_over, doing="正在接管…")

    def on_away(self) -> None:
        try:
            minutes = self.c.set_away_minutes(self.away.get())
        except ValueError as e:
            self.away_error.config(text=str(e))
            return
        self.away.set(str(minutes))
        self.away_error.config(text="")

    # -- drawing ------------------------------------------------------------------
    def draw(self, snap: Snapshot) -> None:
        self.state.config(text=snap.state, fg=COLORS.get(snap.state, MUTED))
        self.toggle.config(text=snap.toggle_label,
                           state="normal" if snap.can_toggle and not self._busy else "disabled")
        self.detail.config(text=self._doing or snap.detail)
        self.takeover_button.config(state="disabled" if self._busy else "normal")
        self._draw_storage()
        self._draw_status_lines(snap)
        self._draw_autostart(snap)
        if self.c.legacy_daemon():
            self.takeover.pack(fill="x", pady=(8, 0), before=self.log_row)
        else:
            self.takeover.pack_forget()
        self._draw_tray(snap)
        self._draw_telegram()

    def _tick(self) -> None:
        self._timer = None
        self.draw(self.c.snapshot())
        self._timer = self.root.after(REFRESH_MS, self._tick)

    # -- visibility: refresh only while shown ----------------------------------------
    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self._timer is None:
            self._tick()

    def hide(self) -> None:
        if self._timer is not None:
            self.root.after_cancel(self._timer)
            self._timer = None
        self.root.withdraw()

    def _on_unmap(self, event) -> None:
        if event.widget is self.root and self.root.state() == "iconic":
            self.hide()  # minimize goes to the tray, not the taskbar

    # -- actions ------------------------------------------------------------------
    def on_toggle(self) -> None:
        if self._busy:
            return
        doing = "正在停用…" if self.c.snapshot().enabled else "正在启用…"
        self._in_background(self.c.toggle, doing=doing)

    def restore(self, tray: bool) -> None:
        """Opening ccmonitor: runs it (off the Tk thread) unless the user last chose 停用.
        At login (tray) a 未配置 ccmonitor says so in a balloon."""
        def done(snap: Snapshot) -> None:
            if tray and snap.state == UNCONFIGURED:
                self.tray.balloon("未配置 Telegram", "点托盘图标打开窗口，在「Telegram」页填好 bot token 和 chat id。")
        self._in_background(self.c.restore, done, doing="正在启用…")

    def _in_background(self, work, done=None, *, doing: str = "", waiting: str = "",
                       failed=None, after=None) -> None:
        """Runs work() off the Tk thread (it may block for seconds), then, back on it,
        done(result), or failed(exception), then after(), and redraws. doing: shown under
        the state meanwhile; waiting: shown under the Telegram tab's buttons."""
        self._busy, self._doing = True, doing
        if waiting:
            self.tg_result.config(text=waiting, foreground=MUTED)
        self.draw(self.c.snapshot())
        box: queue.Queue = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                box.put((True, work()))
            except Exception as e:  # handed to the Tk thread
                box.put((False, e))
        threading.Thread(target=run, name="gui-work", daemon=True).start()
        self.root.after(100, self._await_result, box, done, failed, after)

    def _await_result(self, box: queue.Queue, done, failed, after) -> None:
        try:
            ok, value = box.get_nowait()
        except queue.Empty:
            self.root.after(100, self._await_result, box, done, failed, after)
            return
        self._busy, self._doing = False, ""
        if ok:
            if done:
                done(value)
        elif failed:
            failed(value)
        else:
            log.error("%s", type(value).__name__)
        if after:
            after()
        self.draw(self.c.snapshot())

    def tray_menu(self):
        """Read inside the tray's window procedure: touches no Tk, and its actions are
        queued in the inbox rather than run there."""
        snap = self.c.snapshot()
        later = self.inbox.later
        if snap.state == UNCONFIGURED:
            toggle = ("配置 Telegram…", later(self.show_telegram))
        else:  # greyed out while it can't be pressed (a 「获取 Chat ID」 or another action running)
            toggle = (snap.toggle_label, later(self.on_toggle) if snap.can_toggle and not self._busy else None)
        return [("显示窗口", later(self.show)),
                toggle,
                None,
                ("退出 ccmonitor", later(self.quit))]

    # -- the Telegram tab (#68) ------------------------------------------------------
    def _build_telegram_tab(self, tabs: ttk.Notebook) -> None:
        page = self.telegram = ttk.Frame(tabs, padding=12)
        tabs.add(page, text="Telegram")
        page.columnconfigure(1, weight=1)

        ttk.Label(page, text="Bot token").grid(row=0, column=0, sticky="w")
        self.token = ttk.Entry(page, show="•")
        self.token.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.reveal = tk.BooleanVar(value=False)
        ttk.Checkbutton(page, text="显示", variable=self.reveal,
                        command=lambda: self.token.config(show="" if self.reveal.get() else "•")
                        ).grid(row=0, column=2, padx=(4, 0))
        self.token_note = ttk.Label(page, foreground=MUTED)
        self.token_note.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0))

        ttk.Label(page, text="Chat ID").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.chat = ttk.Entry(page)
        self.chat.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))
        self.fetch = ttk.Button(page, text="获取 Chat ID", command=self.on_fetch_chat_id)
        self.fetch.grid(row=2, column=2, padx=(4, 0), pady=(6, 0))
        self.chat_note = ttk.Label(page, foreground=MUTED)
        self.chat_note.grid(row=3, column=1, columnspan=2, sticky="w", padx=(8, 0))

        buttons = ttk.Frame(page)
        buttons.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.save = ttk.Button(buttons, text="保存", command=self.on_save)
        self.save.pack(side="left")
        self.probe = ttk.Button(buttons, text="测试连接", command=self.on_test_connection)
        self.probe.pack(side="left", padx=(6, 0))
        self.ping = ttk.Button(buttons, text="发送测试消息", command=self.on_send_test)
        self.ping.pack(side="left", padx=(6, 0))
        self.tg_result = ttk.Label(page, wraplength=WIDTH - 48)
        self.tg_result.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._load_telegram()

    def _load_telegram(self) -> None:
        """Fills the fields from the config; a field set by an environment variable is
        shown read-only with its source."""
        form = self.c.telegram_settings()
        for entry, value, var, note in ((self.token, form.token, form.token_env, self.token_note),
                                        (self.chat, form.chat_id, form.chat_id_env, self.chat_note)):
            entry.config(state="normal")
            entry.delete(0, "end")
            entry.insert(0, value)
            entry.config(state="readonly" if var else "normal")
            note.config(text=f"来自环境变量 {var}，在此修改无效" if var else "")
        if not form.configured:
            self.tg_result.config(text="未配置：填好 bot token 和 chat id 后保存", foreground=AMBER)

    def _draw_telegram(self) -> None:
        form = self.c.telegram_settings()
        idle = "disabled" if self._busy else "normal"
        for button in (self.probe, self.ping):
            button.config(state=idle)
        both_env = form.token_env and form.chat_id_env
        self.save.config(state="disabled" if self._busy or both_env else "normal")
        can_fetch = not self._busy and not form.chat_id_env and self.c.can_fetch_chat_id()
        self.fetch.config(state="normal" if can_fetch else "disabled")

    def show_telegram(self) -> None:
        self.show()
        self.tabs.select(self.telegram)

    def _say(self, result: TelegramResult) -> None:
        self.tg_result.config(text=result.message, foreground=GREEN if result.ok else RED)

    def on_save(self) -> None:
        token, chat = self.token.get(), self.chat.get()

        def done(result: TelegramResult) -> None:
            self._say(result)
            if result.ok:
                self._load_telegram()
        # It restarts a running daemon.
        self._in_background(lambda: self.c.save_telegram(token, chat), done, waiting="正在保存…")

    def on_test_connection(self) -> None:
        token = self.token.get()
        self._in_background(lambda: self.c.test_connection(token), self._say, waiting="请稍候…")

    def on_send_test(self) -> None:
        token, chat = self.token.get(), self.chat.get()
        self._in_background(lambda: self.c.send_test_message(token, chat), self._say, waiting="请稍候…")

    def on_fetch_chat_id(self) -> None:
        token = self.token.get()

        def work() -> TelegramResult:
            return self.c.fetch_chat_id(token, wait=FETCH_WAIT, rounds=FETCH_ROUNDS)

        def done(result: TelegramResult) -> None:
            if result.chat_id:
                self.chat.delete(0, "end")
                self.chat.insert(0, result.chat_id)
            self._say(result)

        self._in_background(work, done, waiting="等待中：在 Telegram 里给 bot 发一条消息（最多等一分钟）")

    def listen(self, port: int) -> None:
        """The hidden window a later launch finds (instance.py): show the window, or quit."""
        self.listener = instance.Listener(port, on_show=self.inbox.later(self.show),
                                          on_quit=self.inbox.later(self.quit))

    def quit(self) -> None:
        self.hide()
        self.tray.close()
        self.c.quit()
        self.root.destroy()


def main() -> None:
    ap = argparse.ArgumentParser(description="ccmonitor")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CCMONITOR_PORT", DEFAULT_PORT)))
    ap.add_argument("--tray", action="store_true", help="start hidden in the tray (开机自启)")
    ap.add_argument("--replace", action="store_true",
                    help="make a running ccmonitor quit and take over from it")
    args = ap.parse_args()
    setup_logging()
    # Started from a Claude session: move outside Claude's package, or 开机自启 is written
    # where Explorer never reads it (package.py).
    flags = [flag for flag, on in (("--tray", args.tray), ("--replace", args.replace)) if on]
    if package.relaunch_outside(flags, port=args.port, default_port=DEFAULT_PORT):
        log.info("started inside an app package; started again outside it")
        logging.shutdown()
        sys.exit(0)
    try:
        instance.claim(args.port, replace=args.replace)  # held until the process ends
    except instance.AlreadyRunning:
        log.info("already running; %s", "nothing to do" if args.tray else "showing its window")
        if not args.tray:
            instance.show_running(args.port)
        logging.shutdown()
        sys.exit(0)
    except TimeoutError:
        log.error("--replace: the running ccmonitor did not quit")
        instance.tell_user("正在运行的 ccmonitor 没有退出，无法接管。请从托盘退出它后再试。")
        logging.shutdown()
        sys.exit(1)
    log.info("started%s", " in the tray" if args.tray else "")
    try:
        moved = paths.migrate()
        if moved:
            log.info("brought %s into %s", ", ".join(moved), paths.DATA_DIR)
    except OSError as e:
        log.warning("migration failed: %s", type(e).__name__)

    controller = Controller(port=args.port, service_factory=Service, data_dir=paths.DATA_DIR)
    root = tk.Tk()
    root.withdraw()  # shown below unless --tray
    root.iconphoto(True, tk.PhotoImage(data=base64.b64encode(icons.png(TRAY_COLORS[RUNNING], 32))))
    window = Window(root, controller)
    window.listen(args.port)
    if not args.tray:  # 开机自启 (--tray) stays in the tray
        if controller.snapshot().state == UNCONFIGURED:
            window.show_telegram()  # first run: the first step is the bot config
        else:
            window.show()
    window.restore(tray=args.tray)  # runs it, unless the user last chose 停用

    root.mainloop()
    log.info("quit")
    logging.shutdown()
    # Not a normal exit: that would join the worker thread of a Telegram long poll still
    # in flight (up to ~40 s); the daemon is already stopped, so nothing is lost.
    os._exit(0)


if __name__ == "__main__":
    main()
