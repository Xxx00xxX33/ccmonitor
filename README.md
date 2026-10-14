# ccmonitor

Phone-side intervention for Claude Code Desktop local sessions. Design: [docs/DESIGN.md](docs/DESIGN.md); terms: [GLOSSARY.md](GLOSSARY.md); pre-implementation checks: [docs/VERIFY.md](docs/VERIFY.md).

Python 3.12, standard library only. Windows only. Licensed under [MIT](LICENSE).

Write-back finds Claude Desktop as any `claude.exe` with an Electron window, so it is no longer limited to the Microsoft Store build; it has only been live-tested with the Store build. It looks for Desktop's English control names (`Prompt`, `Send`, `Allow once`, …), so Desktop's interface must be in English.

## Module 1: event intake and classification

```
Claude Code hook ──stdin──▶ hooks/relay.py ──127.0.0.1:47821──▶ ccmonitor.daemon ──▶ Classifier ──▶ sink
```

- `hooks/relay.py`: the hook command. Forwards the payload and always exits 0; never prints. When the daemon isn't listening, `Stop`, `StopFailure` and `PermissionRequest` retry for up to 3.5 s, so a turn that ends while the daemon restarts still gets its notice. Other events give up after 0.5 s. A mark file (`%LOCALAPPDATA%\ccmonitor\relay-down`, empty) records when the daemon was first found down; after 30 s down nothing waits, so a stopped daemon never slows sessions. The daemon starts listening before its Telegram startup calls, to keep a restart's gap short.
- `ccmonitor/classify.py`: `PermissionRequest` → urgent (`approval`, or `question` for `AskUserQuestion`); `Stop` → deferred `completed` with the body clipped to 600 chars and the options section parsed; `SessionStart` / `UserPromptSubmit` / `SessionEnd` only maintain the session label.
- `ccmonitor/options.py`: parses the options section defined in DESIGN.md 3.2; anything off-contract falls back to plain text.
- `ccmonitor/daemon.py`: listens on 127.0.0.1 only; content stays in memory, the log has event types and times only. Without Telegram config the sink prints to the console.

## Module 2: Telegram side channel

```
notice ──▶ SideChannel.notify ──sendMessage──▶ phone
phone ──getUpdates──▶ SideChannel.handle_update ──▶ DesktopWriteBack (module 3)
```

- Outbound: 🔴 approvals with `允许一次` / `拒绝` buttons, 🔴 questions with buttons (module 9), ✅ completions with one button per option. Each button carries the option's full text in memory, is single-use, and disappears after it is pressed.
- Stale buttons come off: the side channel remembers which notices of each session still show buttons and clears them (`editMessageReplyMarkup`) when a later hook event of that session settles them: `UserPromptSubmit` for a completion's options and 💡, `PermissionRequest` for earlier approvals and questions, `Stop` / `StopFailure` / `SessionEnd` for all, and `PostToolUse` / `PostToolUseFailure` for the approval or question of that very tool call (matched on tool name + input, since `PermissionRequest` has no `tool_use_id`), so approving on the computer clears the Telegram buttons once the tool has run. A Deny pressed in Desktop fires no hook at all (the turn is interrupted: no `Stop`, and `PostToolUseFailure` / `PermissionDenied` skip manual denials), but Desktop writes a rejected tool result to the transcript right away; so on the session's next `UserPromptSubmit`, and in a once-a-minute sweep, the side channel reads the tail (1 MB) of the session's transcript (path taken from its latest hook payload) and clears an approval or question whose latest matching call already has a result. `UserPromptSubmit` alone still never clears approvals, because a mid-turn message fires it while a request may still wait; unreadable transcripts or calls not found count as still waiting. Those two hooks fire on every tool call, so they are installed with `"async": true` (Claude Code doesn't wait), the relay drops the tool result before sending, and the daemon doesn't log them. The daemon queues the event before the notice it produces. Tapping an expired button also clears that message's buttons. A suggestion read that finishes after the session moved on is dropped.
- Inbound: only the configured chat id is listened to; everything else is dropped silently. Replying to a notice sends the text to that session; a plain message goes to the session notified most recently.
- Writes are refused unless the user is away (see module 3).
- Notices go out only while away; at the desk nothing is sent to Telegram (if presence can't be read, notices still go out). The daemon's own messages (startup, failure reminders, queue results) always go.
- Commands: `/away`, `/back`, `/pause`, `/resume`, `/status`, `/mode`. `/away` is also a button under the input box (a reply keyboard), shown only while not away; there is no `/back` button. Every answer carries the keyboard for the current state, and when the state changes (3 minutes idle, back at the computer, a reply or button from the phone) the daemon sends a silent message within 15 s to show or hide it. Every command is in the bot's "/" menu (`setMyCommands` at startup).
- `ccmonitor/sidechannel.py` keeps its state on one worker thread in `daemon.TelegramBridge`; nothing is written to disk.

Setup:

1. In Telegram, create a bot with @BotFather (`/newbot`) and copy its token.
2. Open `bin\ccmonitor.exe` (see Running) and go to the Telegram tab. Paste the token into Bot token.
3. Send your bot any message from the phone, then press 「获取 Chat ID」 (Get Chat ID); it fills in Chat ID.
4. Press 「保存」 (Save), then 「发送测试消息」 (Send test message) to check that it reaches the phone.

The tab writes `config.json` in the storage folder (by default `%USERPROFILE%\.ccmonitor`), `{"telegram_token": "<token>", "chat_id": <chat id>}`; writing that file by hand works too. An existing `%APPDATA%\ccmonitor\config.json` is still read. `CCMONITOR_TELEGRAM_TOKEN` and `CCMONITOR_CHAT_ID` override the file.

## Module 3: write-back into Desktop

```
DesktopWriteBack ──find_title(cli session id)──▶ Desktop's session metadata (read-only)
                 ──JSON on stdin──▶ ccmonitor/desktop.ps1 (UI Automation) ──▶ {"code": ...}
```

- `ccmonitor/away.py`: away = 3 minutes without keyboard/mouse input (10 minutes until 2026-10-12, briefly 1), or a declaration from the phone. Sending `/away` declares it; so does a free-text reply, whatever the computer is doing (until 2026-10-14 a reply needed a minute without input). A declaration ends at the next keyboard/mouse input. Every button (options, 💡, 允许一次, 拒绝) always acts, whether or not the computer is in use, and also declares away, so the answer it starts still ends with options.
- `ccmonitor/sessions.py`: maps the hook's `session_id` to the Desktop title via `cliSessionId` in Desktop's `claude-code-sessions\*\*\local_*.json`. The same lookup names sessions in notices. `ccmonitor/paths.py` finds that folder: `%APPDATA%\Claude\...` when it exists, else the Store package's copy under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\...`. A daemon started at login runs outside the package and only sees the second (2026-10-13).
- `ccmonitor/desktop.ps1`, one write:
  1. Restore the window if minimized (fills and clicks are deferred while minimized).
  2. Remember the open session, then switch to the target by its sidebar row. The row is the button right before `More options for <title>`.
  3. Prompt: a draft in the box is set aside and put back afterwards. Fill, wait for `Send`, click it, and confirm the turn started. A prompt from the phone goes in even while the session runs (`"interject": true`): Desktop shows `Send` once the box holds text and delivers the message mid-turn, confirmed by the box clearing (code `interjected`). Other writers (helper rotation's `/clear`) still get `busy`.
  4. Approval: refuse unless exactly one approval card shows the command from the notice. Then click `Allow once` / `Deny`, retrying while the card stays (the first click is sometimes ignored).
  5. Switch back to the original session and re-minimize if needed.
- Results come back to Telegram as short status lines (`desktop.py MESSAGES`).

Live-tested on a throwaway Manual-mode session, starting from another open session:
- Sending works with the window normal and minimized.
- A deliberately wrong command was refused.
- Allow once and Deny work through a session switch and while minimized.
- The draft and busy guards refused.
- Every run ended back on the original session with the original window state.

## Module 4: options while away

```
UserPromptSubmit ──▶ relay ──▶ daemon: away? ──yes──▶ {"hookSpecificOutput": {"additionalContext": AWAY_INSTRUCTION}}
                      ◀──────── reply (≤1.5 s) ◀──no───▶ nothing
                                                  ◀──can't tell──▶ {"hookSpecificOutput": {"additionalContext": OPTIONS_INSTRUCTION}}
relay prints the reply only if it is exactly that shape
```

- While away (module 3's `Presence`), every prompt carries a one-line instruction, `options.AWAY_INSTRUCTION`, so the model ends answers with an `选项：` section, which `Stop` notices parse into buttons. At the desk nothing is injected, so Desktop's own input-box suggestion isn't crowded out (user's call, 2026-10-12).
  - The wording is VERIFY.md 4's minus its fixed first item "按刚才说的继续", which Desktop's suggestion kept copying; each item is now a concrete instruction.
  - If presence can't be read, notices still go out, so the prompt gets `options.OPTIONS_INSTRUCTION`, the same line without the "[用户不在电脑前]" prefix.
  - Both are built from the same constants the parser checks.
- The relay waits at most 1.5 s, prints nothing on a malformed, slow or missing reply, and never prints for other events.
- Live check with `claude -p` through the installed hooks (2026-10-11, with the old fixed first item): away → `['按刚才说的继续', …]`; at desk → no section.

## Module 5: resident and health

- `bin\ccmonitor.exe` is a launcher, not a frozen build ([ADR 0001](docs/adr/0001-exe-is-a-launcher.md)): a few KB of C# compiled by Windows' own `csc.exe` (`python -m ccmonitor.build_exe`) that runs this machine's `pythonw.exe -m ccmonitor.gui` in this checkout, passing its arguments on. The window, the tray and the daemon are one process; merged code is used on the next start without rebuilding. Rebuild only when Python or the repository moves. It carries the tray's three dots (green, grey, red; the green one is its icon), drawn by `ccmonitor/icons.py`. The exe is not in git.
- One ccmonitor at a time (`ccmonitor/instance.py`): a named mutex per hook port. Opening the exe again only brings up the running window; `--tray` starts hidden in the tray; `--replace` makes the running one quit, waits until the mutex and the ports are free, and takes over.
- 开机自启: the 设置 tab's checkbox, or `python -m ccmonitor.autostart --enable | --disable | --status`, writes the same per-user Run value (`HKCU\...\Run`, value `ccmonitor`): `"<repo>\bin\ccmonitor.exe" --tray`. A value that runs anything else (the old `pythonw bin\ccmonitor-daemon.pyw`, or another checkout's exe) shows as 指向旧位置; ticking the box once rewrites it. Started at login, ccmonitor stays in the tray; without Telegram config its icon is grey and a balloon says so.
- The old background daemon (`pythonw bin\ccmonitor-daemon.pyw`, before #71): when it holds the port, the 状态 page says so with a 「接管」 button, which ends only `pythonw.exe` processes whose command line contains `ccmonitor-daemon.pyw` and then runs ccmonitor here.
- Storage folder (存储目录): `CCMONITOR_HOME` if set, else `DataDir` in `bin\ccmonitor.ini` next to the exe (written by the 设置 tab's 存储目录, not in git), else `%USERPROFILE%\.ccmonitor`. It holds `config.json`, `settings.json`, the log, `topics.json`, `relay-mode.json` and the driver's DLL. A worktree's exe doesn't see the main checkout's ini, so it falls back to the default folder. Windows redirects a packaged app's AppData writes, so a daemon started from a Claude session and one started at login used to keep separate copies under `%LOCALAPPDATA%\ccmonitor`; this folder isn't redirected. On start the daemon brings older copies over once: the newest `relay-mode.json`, and every `topics.json` merged.
- Log file: `daemon.log` in the storage folder, rotated at midnight to `daemon.log.YYYY-MM-DD`; files older than 3 days are deleted. It holds event types, times and error kinds only. Without a console the Telegram-less sink prints nothing.
- `ccmonitor/health.py`: a failure sends one Telegram reminder per kind per day; repeats only increase the count. Kinds:
  - `write`: a write-back broke. Refusals such as busy or draft don't count.
  - `telegram`: an outage, reported once Telegram is reachable again.
  - `presence`: idle time can't be read.
- `/status`: away state, notifications on or paused, uptime and failure counts.
- If the port is busy anyway (another program), the window shows 出错 with the port; 启用 retries.

## Module 6 (v2): stuck and error detection

- `ccmonitor/stuck.py`: a turn counts as in progress from `UserPromptSubmit` until `Stop`, `StopFailure` or `SessionEnd`. Once a minute the daemon reads the tail of each such session's transcript, using only the timestamp and structure of the last user/assistant record. Records in the session's subagent transcripts (`<session>/subagents/*.jsonl`) count too, since a subagent's work never touches the main transcript. If there has been no new record for 10 minutes, it sends one deferred `⏳ 可能卡住` notice; new output re-arms it.
- A turn that ended without `Stop` (Deny or Esc in Desktop) is recognised from `end_turn` or the interrupt marker and dropped. A turn waiting for approval isn't counted as stuck, since the approval was already sent as an urgent notice.
- `StopFailure` (API errors such as `rate_limit`) becomes a deferred `❌ 出错` notice with the error type and details.
- Long silent commands (a 15-minute build) also trigger the stuck notice, by definition (DESIGN.md 5). The notice says so.

## Module 7 (v2): held prompts, and the optional helper-session relay

```
SideChannel ─▶ DesktopWriteBack ──busy / UI broken──▶ PendingPrompts (in memory)
                     ▲                                       │ retry: Stop hook of that session (+3 s), or every 60 s
                     └───────────── try_prompt ◀─────────────┘
```

- The default backup path for prompts, with no model in the loop (`ccmonitor/pending.py`). Phone prompts no longer hit `busy` (they interject); a prompt held for a busy session is sent once that session's `Stop`/`StopFailure` arrives; a minute-long timer covers turns that end without `Stop`. A prompt that hit a UI breakage is retried every minute, up to 5 times.
- Never retried after `send_failed`. Given up after 3 hours. If the user is back at the computer when a retry comes due, the session's held prompts are dropped and their text is sent back on Telegram, so nothing is typed while someone is using the computer.
- A draft in the target's box no longer blocks a prompt: `desktop.ps1` reads it into memory, sends the prompt, and puts the draft back. If it can't put the draft back, the reply carries the draft's text (`sent_draft_lost`).
- `/status` shows how many prompts are held, the relay mode and the helper's state.

### Relay modes A and B

- Mode A (default, no agent) is the held-prompt path above. Mode B forwards through a helper session (below), falling back to A while no helper is connected or it is rotating. `ccmonitor/relaymode.py` keeps the mode in `relay-mode.json` in the storage directory (by default `%USERPROFILE%\.ccmonitor`). It can be set on the window's 状态 page, with Telegram `/mode a|b`, or `python -m ccmonitor.relaymode a|b|status`, which talks to the running daemon and, for B without a helper, prints how to create one by hand: a new Desktop session in the repo folder, the cheapest model, effort low, Bypass permissions, and `SETUP_PROMPT` as its first message (the window shows the same steps and a 「复制启动指令」 button). Not created by another session: `start_session` is missing from some Claude Desktop builds. `SETUP_PROMPT` runs the feed with the Python that runs ccmonitor (`python.exe` beside `pythonw.exe`), not whatever `python` is on PATH, and tells the helper to stop and say so if `Monitor` or `send_message` is missing.
- `ccmonitor/rotation.py` rotates the mode-B helper every 6 hours, only while the user is away. It pauses the helper, types `/clear` and then `SETUP_PROMPT` into it through UI write-back, and resumes when the helper's feed reconnects. If the feed isn't back within 3 minutes, it reports on Telegram. It runs on the side-channel worker, so a forward in flight always finishes first. While rotating, the helper's events are recognised by its Desktop title, because `/clear` gives it a new CLI session id.

### Helper-session relay (mode B)

```
SideChannel ─▶ DesktopWriteBack ──busy / draft / UI broken──▶ HelperRelay ─127.0.0.1:47822─▶ hooks/relay_feed.py
                                                                    ▲                          (helper's Monitor)
                                   hooks/relay_ack.py ─127.0.0.1:47821─┘          helper ─send_message─▶ target session
```

- Used in mode B only. A backup path for prompts only, used when UI write-back finds the session busy (queued instead) or is broken. It is never used after `sent` or `send_failed`, because that could send twice, and never for approvals.
- The helper is an ordinary Desktop session set up once with the prompt in [docs/helper-session.md](docs/helper-session.md). It identifies itself through `CLAUDE_CODE_SESSION_ID`, so its own turns produce no notices.
- The target sees the message as coming from another session and acts on it as a teammate's request, within its own permissions. It won't change settings or approve pending prompts on that basis.
- Live-tested with a real helper and target, both in bypass mode: `已经辅助会话送达` in 7 s, and the target answered. The helper also re-armed its Monitor, backed off while the daemon was down, and stopped on request.

## Module 8: official suggestion button

- After a `✅ 完成` notice is sent, the daemon reads that session's official Desktop suggestion. Only while away (the notice itself only goes out while away); a miss is read once more a minute later, unless the user is back by then. It switches to the session, posts a Tab to Chromium's render window (`PostMessage`, so the idle timer is untouched), reads the box through UI Automation, clears it (the grey suggestion comes back), and switches back. It then edits the notice to add `💡 官方建议：<text>` and a `💡 官方建议` button.
- The driver keeps itself to 50 s (`BudgetMs`): the switch and the wait for a suggestion only use what is left after 12 s kept back for switching back, and it returns `out_of_time` if nothing is left. `desktop.py`'s 90 s `TIMEOUT` is only a safety net, since a killed driver skips the restore. Every driver run logs its step times, e.g. `write-back suggestion (suggest 5.0 s; init=287,window=304,wake=74,ready=165,switch=1405,suggest=66,back=1353)`: `init` loads UI Automation and the native helper, `window` finds Desktop, `restore` (only if it was minimized), `wake` the display nudge.
- The native helper (C#) is compiled once into `%USERPROFILE%\.ccmonitor\nativetab-<hash>.dll`, keyed by its source and the PowerShell edition, and only loaded afterwards: compiling took 9.5 s on a cold first run after a restart, against ~0.3 s to load. If the cached DLL can't be built or loaded, it compiles in memory as before.
- Every run nudges the mouse once (a zero move) before touching the UI, since the render window stays while the display is off and Chromium then ignores the UI; after a quiet minute it waits 1.5 s for the display. The nudge is reported as `own_input`, so the away check ignores it.
- Tapping the button accepts the suggestion again and sends it, echoing the exact text sent. It is refused if the session is busy, the box holds text, or there is no suggestion. Like option taps, it ignores the away check and declares away.
- No suggestion is normal (Desktop doesn't make one every turn), and the options still work. Breakage goes to Health as `suggest`.
- `desktop.ps1` actions `suggest` and `accept`; all screen work stays on the side channel's single worker, so it never overlaps write-back.

## Module 9: question buttons

- A `🔴 Claude 提问` notice (`AskUserQuestion`) lists every question, with buttons for the one Desktop's card is showing. Desktop asks one question at a time, so after each answer the same message's buttons switch to the next question, and they disappear when all are answered. With several questions, buttons read `问2·<option>`.
- **Single-select:** one button per option. A tap clicks that option in the card, which moves on (or submits, on the last question).
- **Multi-select:** `☐`/`☑` ticks change only the Telegram buttons. `提交所选` then sets the card's toggles to match ("Other" off) and clicks `Next` or `Submit`. Submitting nothing is refused.
- **Refusals** (the buttons stay, so you can tap again):
  - the session has no question card;
  - the card is on a different question (text and "i / n" are compared);
  - no option button is named like the notice's `<label> <description>`.
- **Confirmation:** the driver waits for the card to move on and retries the click up to 3 times. After that it reports "可能未回答", which goes to Health.
- Like the other buttons, a tap ignores the away state and declares away.
- `desktop.ps1` action `answer`; card structure in [VERIFY.md](docs/VERIFY.md) (提问卡片探针).

## Telegram layout

`ccmonitor/tgformat.py` turns the Markdown in answers into Telegram HTML: bold, italics, code, code blocks, headings, lists, links and quotes. Tables become one line per row (`• a · b · c`), which reads better on a phone. Each notice has this layout:

```
✅ 完成｜<session title>
↳ <your last prompt, first 30 characters>

<the full answer>

选项
1. …
💡 官方建议：…

#1a2b3c4d
```

Answers over Telegram's limit are split at paragraph boundaries (code blocks stay whole when they fit) and marked `（续 2/3）`, with the buttons on the last part. Replying to any part reaches the session. If Telegram rejects the formatting, that message is resent as plain text. Prompts the app injects (CI events, task notices, messages from other sessions) never replace "your last prompt".

## Telegram threaded mode

If the bot has threaded mode (topics in private chats) turned on in @BotFather, the daemon sees it in `getMe` at startup:
- **One topic per session:** each session gets a topic named `<title> #tag`, renamed when the title changes, and all its notices go there.
- **Typing in a topic goes to that topic's session;** replying to a specific notice still takes priority.
- **Answers** to taps and commands come back in the same topic.
- **Persistence:** the session ↔ topic ids live in `%USERPROFILE%\.ccmonitor\topics.json` (ids and each topic's last activity time only). If that pairing is lost, it's recovered from the tag in the topic name.
- **Idle topics** (no notice and no reply or button in it for 3 days) are deleted with `deleteForumTopic`, checked a minute after start and then hourly; a session that comes back gets a new topic. Topics from a file without times count from the first start after the upgrade.
- **Commands from "All Messages"**: Telegram opens a new topic, named after the message, for anything sent there. A command that lands in a topic holding no session is carried out, answered in the main chat, and that topic is deleted. A topic whose name has a session tag is kept. The command doesn't always land in that topic (2026-10-13: two empty "/away" topics), so a topic Telegram reports as created under a command's name (starting with `/`, without a session tag, holding no session) is deleted at once too.
- **Keyboards are per topic**, so the silent away/back message goes to the topic the user last acted in (the main chat if that topic is gone).
- **Deleted topics** are recreated on the next notice. With threaded mode off, nothing changes.

## Running

Build the launcher once per machine (only Windows' own tools; rebuild if Python or the repository moves):

```bash
python -m ccmonitor.build_exe
```

Then double-click `bin\ccmonitor.exe`. The first time, fill in the Telegram tab; tick 开机自启 on the 设置 tab to start it in the tray at login. Closing or minimizing the window only hides it in the tray; 「退出 ccmonitor」 in the tray menu stops it. `--port N` (or `CCMONITOR_PORT`, read by the hooks too) changes the hook port.

Moving from the old background daemon: build the exe, open it, press 「接管」 on the 状态 page, and tick 开机自启 once to replace the old Run value.

Install the hooks for all sessions (writes `~/.claude/settings.json`; `--dry-run` previews, `--uninstall` removes only ccmonitor's entries):

```bash
python -m ccmonitor.install
```

Re-run it after updating ccmonitor: new versions may listen to more events (module 6 added `StopFailure`).

Deploying a merged PR: in the main checkout, pull, run the tests, then restart ccmonitor in place and check that `daemon.log` shows `listening on 127.0.0.1:47821`:

```bash
python -m unittest discover -s tests -t .
bin\ccmonitor.exe --replace
```

Testing a fix branch with real events: build the exe in the worktree (`python -m ccmonitor.build_exe`) and run `bin\ccmonitor.exe --replace` there; it takes over from the main one. Run `bin\ccmonitor.exe --replace` in the main checkout to switch back.

Tests:

```bash
python -m unittest discover -s tests -t .
```
