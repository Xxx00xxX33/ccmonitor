# Write-back driver for Claude Code Desktop via UI Automation (DESIGN.md 3.5, 3.8, VERIFY.md 2-3).
# stdin:  {"action":"prompt","title":"...","text":"...","interject":false}
#          interject: send even while the session runs; Desktop delivers it mid-turn
#      or {"action":"approve","title":"...","allow":true,"expect":"<command or empty>"}
#      or {"action":"answer","title":"...","question":"...","index":1,"total":2,"multi":false,
#          "options":[{"label":"...","description":"..."}]}  pick options in the question card
#      or {"action":"suggest","title":"..."}   read the official suggestion, then clear the box
#      or {"action":"accept","title":"...","expect":"..."}  send the suggestion only if it still equals expect
# stdout: one JSON line {"code":"...","text":"...","own_input":[before,after],"timing":"..."}; codes
#         are listed in desktop.py MESSAGES. own_input is present when the driver nudged the mouse
#         to wake the display: the last real input tick and the tick of its own input. timing is
#         the milliseconds each step took, for the daemon log.
# Prints no session content except the suggestion text for suggest/accept, and a draft
# that prompt set aside but could not put back. Restores the
# window state and the originally open session.
$ErrorActionPreference = 'Stop'
$Clock = [Diagnostics.Stopwatch]::StartNew()
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]
$Walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$RenameSuffix = ', rename session'

# The whole run gets BudgetMs. Waits that can be cut short (the switch, waiting for the
# suggestion) only use what is left after RestoreMs, kept back for switching back to the
# original session; the daemon kills a driver that overruns (desktop.py TIMEOUT), which
# would skip that restore. Over 60 s was seen live 2026-10-12 20:03 while Desktop was busy.
$BudgetMs = 50000
$RestoreMs = 12000
$Steps = [System.Collections.Generic.List[string]]::new()
$LastMark = 0
function Mark($name) {
  $now = $Clock.ElapsedMilliseconds
  $script:Steps.Add("$name=$($now - $script:LastMark)")
  $script:LastMark = $now
}
# Milliseconds a cut-short wait may still use, at most $ms.
function Left-Ms([int]$ms) {
  [Math]::Max(0, [Math]::Min($ms, $BudgetMs - $RestoreMs - $Clock.ElapsedMilliseconds))
}

$OwnInput = $null
function Done($code, $text) {
  $out = @{ code = $code; timing = ($Steps -join ',') }
  if ($text) { $out.text = $text }
  if ($OwnInput) { $out.own_input = $OwnInput }
  $out | ConvertTo-Json -Compress; exit 0
}

# Tab is posted to Chromium's render window rather than typed: a posted message does
# not touch the system's last-input time, so it does not end the away state (DESIGN.md 3.8).
$NativeSource = @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class NativeTab {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h, EnumProc f, IntPtr p);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static IntPtr FindRender(IntPtr top) {
    IntPtr found = IntPtr.Zero;
    EnumChildWindows(top, (h, p) => {
      var sb = new StringBuilder(256); GetClassName(h, sb, 256);
      if (sb.ToString() == "Chrome_RenderWidgetHostHWND") { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
  public static bool Post(IntPtr h) {
    return PostMessage(h, 0x0100, (IntPtr)0x09, (IntPtr)0x000F0001)
        && PostMessage(h, 0x0101, (IntPtr)0x09, unchecked((IntPtr)(int)0xC00F0001));
  }
  [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx, dy; public uint data, flags, time; public IntPtr extra; }
  [StructLayout(LayoutKind.Sequential)] struct INPUT { public uint type; public MOUSEINPUT mi; }
  [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] i, int size);
  [StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO { public uint cb; public uint time; }
  [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO i);
  [DllImport("kernel32.dll")] static extern uint GetTickCount();
  public static uint Tick() { return GetTickCount(); }
  public static uint LastInput() { var i = new LASTINPUTINFO(); i.cb = 8; GetLastInputInfo(ref i); return i.time; }
  // A mouse move of zero: wakes the display without moving the pointer.
  public static bool Nudge() {
    var a = new INPUT[1]; a[0].type = 0; a[0].mi.flags = 0x0001;  // INPUT_MOUSE, MOUSEEVENTF_MOVE
    return SendInput(1, a, Marshal.SizeOf(typeof(INPUT))) == 1;
  }
}
"@

# Compiling that source costs seconds when the compiler is cold: 9.5 s on the first run
# after a daemon restart (2026-10-12 22:08), 0.3-0.8 s warm. So it is compiled once into
# a DLL named by a hash of the source and the PowerShell edition, then only loaded.
function Import-Native {
  $key = "$($PSVersionTable.PSEdition)`n$NativeSource"
  $sha = [Security.Cryptography.SHA256]::Create()
  $hash = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($key))[0..7] | ForEach-Object { $_.ToString('x2') })
  # Same folder as the daemon's files (paths.py): %USERPROFILE%\.ccmonitor, which Windows
  # does not redirect for the Claude package, so one cache serves both ways of starting.
  # desktop.py always passes CCMONITOR_HOME, so a storage directory moved in the GUI (#70) is followed.
  $dir = $(if ($env:CCMONITOR_HOME) { $env:CCMONITOR_HOME } else { Join-Path $env:USERPROFILE '.ccmonitor' })
  $dll = Join-Path $dir "nativetab-$hash.dll"
  try {
    if (-not (Test-Path $dll)) {
      New-Item -ItemType Directory -Force $dir | Out-Null
      $tmp = "$dll.$PID.tmp"
      Add-Type -TypeDefinition $NativeSource -OutputAssembly $tmp -OutputType Library
      Move-Item -Force $tmp $dll
    }
    Add-Type -Path $dll
  } catch {
    if (-not ('NativeTab' -as [type])) { Add-Type -TypeDefinition $NativeSource }  # in memory, as before
  }
}
Import-Native
Mark 'init'

function Wait-Until([scriptblock]$cond, [int]$ms = 5000) {
  $deadline = [DateTime]::UtcNow.AddMilliseconds($ms)
  do {
    $r = & $cond
    if ($r) { return $r }
    Start-Sleep -Milliseconds 150
  } while ([DateTime]::UtcNow -lt $deadline)
  return $null
}

function Find-All($root, $type, $name) {
  $c = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $type)
  if ($null -ne $name) {
    $n = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, $name)
    $c = New-Object System.Windows.Automation.AndCondition($c, $n)
  }
  @($root.FindAll($TS::Descendants, $c))
}

function Invoke-El($el) { $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }

function Get-DesktopWindow {
  # Any claude.exe, not only the Store build under WindowsApps, so a Desktop installed
  # another way is found too. Claude Code's own claude.exe processes own no window, and
  # only Electron's top-level windows (Chrome_WidgetWin_1) are taken.
  $pids = @(Get-Process claude -ErrorAction SilentlyContinue | ForEach-Object Id)
  if (-not $pids) { return $null }
  $wins = @($AE::RootElement.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition) |
    Where-Object { $pids -contains $_.Current.ProcessId -and $_.Current.ControlType -eq $CT::Window -and
                   $_.Current.ClassName -eq 'Chrome_WidgetWin_1' })
  # The main window is the one that hosts the composer; fall back to the first.
  foreach ($w in $wins) { if ((Find-All $w $CT::Edit 'Prompt').Count) { return $w } }
  if ($wins) { return $wins[0] }
  return $null
}

function Get-OpenTitle($w) {
  foreach ($b in Find-All $w $CT::Button $null) {
    $n = [string]$b.Current.Name  # some buttons have no name (null)
    if ($n.EndsWith($RenameSuffix)) { return $n.Substring(0, $n.Length - $RenameSuffix.Length) }
  }
  return $null
}

# A sidebar row is the button right before "More options for <title>"; the header's
# copy of that button follows "<title>, rename session" instead and is skipped.
function Find-SidebarRow($w, $title) {
  $rows = @()
  foreach ($m in Find-All $w $CT::Button "More options for $title") {
    $prev = $Walker.GetPreviousSibling($m)
    if ($prev -and -not ([string]$prev.Current.Name).EndsWith($RenameSuffix)) { $rows += $prev }
  }
  return ,$rows
}

function Switch-To($w, $title, [int]$ms = 15000) {
  if ((Get-OpenTitle $w) -eq $title) { return 'ok' }
  $rows = Find-SidebarRow $w $title
  if ($rows.Count -eq 0) { return 'session_not_found' }
  if ($rows.Count -gt 1) { return 'ambiguous_session' }
  Invoke-El $rows[0]
  # A session with a long conversation can take >10 s to open (seen live).
  if (-not (Wait-Until { (Get-OpenTitle $w) -eq $title } $ms)) { return 'switch_failed' }
  # The composer restores the session's saved draft asynchronously; let it land
  # before the draft check reads it.
  Start-Sleep -Milliseconds 400
  return 'ok'
}

# Small dismissable tips (e.g. "Try auto mode") have swallowed clicks before (VERIFY.md 2).
function Close-Tips($w) {
  foreach ($tip in Find-All $w $CT::Window $null) {
    foreach ($b in Find-All $tip $CT::Button 'Dismiss') { try { Invoke-El $b } catch {} }
  }
}

function Norm($s) { ($s -replace '\s+', ' ').Trim() }

# A draft in the box is set aside in memory, the prompt sent, and the draft put back
# (user's call, 2026-10-12). Returns a code, or @{code; text} when the draft could not
# be put back, so its text reaches the user instead of being lost.
# While the session runs, Desktop still shows Send once the box holds text and delivers
# the message mid-turn (seen live 2026-10-12 19:41); with -interject that is used,
# otherwise a running session is refused as busy.
function Send-Prompt($w, $text, [bool]$interject) {
  $running = [bool](Find-All $w $CT::Button 'Stop').Count
  if ($running -and -not $interject) { return 'busy' }
  $edit = Wait-Until { @(Find-All $w $CT::Edit 'Prompt')[0] }
  if (-not $edit) { return 'fill_failed' }
  $value = $edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
  $draft = $value.Current.Value
  if (-not $draft.Trim()) { $draft = $null }
  $value.SetValue($text)
  $expected = $text.Length
  $send = Wait-Until {
    $s = @(Find-All $w $CT::Button 'Send')[0]
    if ($s -and $s.Current.IsEnabled -and $value.Current.Value.Length -eq $expected) { $s }
  }
  if (-not $send) {
    try { $value.SetValue($(if ($draft) { $draft } else { '' })) } catch {}
    if ($draft -and (Norm $value.Current.Value) -ne (Norm $draft)) { return @{ code = 'fill_failed_draft_lost'; text = $draft } }
    return 'fill_failed'
  }
  Invoke-El $send
  # Running already, Stop proves nothing: only the box clearing shows the send landed.
  if (-not (Wait-Until { (-not $running -and (Find-All $w $CT::Button 'Stop').Count) -or -not $value.Current.Value.Trim() } 8000)) {
    if ($draft) { return @{ code = 'send_failed_draft_lost'; text = $draft } }
    return 'send_failed'
  }
  $ok = if ($running) { 'interjected' } else { 'sent' }
  if (-not $draft) { return $ok }
  # The composer clears itself after sending; put the draft back once it has.
  Wait-Until { -not $value.Current.Value.Trim() } 5000 | Out-Null
  try { $value.SetValue($draft) } catch {}
  if (Wait-Until { (Norm $value.Current.Value) -eq (Norm $draft) } 3000) { return $ok }
  return @{ code = 'sent_draft_lost'; text = $draft }
}

# (Callers must not name a variable $r: Wait-Until's scriptblocks would see its own $r.)
# Accepts the grey suggestion into the empty composer and returns its text, or $null if
# none appears within $ms. Refuses (returns a code) when the session is busy or the box
# already holds text, so nothing the user typed is ever touched.
function Accept-Suggestion($w, [int]$ms) {
  if ((Find-All $w $CT::Button 'Stop').Count) { return @{ code = 'busy' } }
  $edit = Wait-Until { @(Find-All $w $CT::Edit 'Prompt')[0] }
  if (-not $edit) { return @{ code = 'fill_failed' } }
  $value = $edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
  if ($value.Current.Value.Trim()) { return @{ code = 'draft_present' } }
  # Chromium has no render window while minimized and recreates it after the restore,
  # later after a long minimize (seen live 2026-10-11 20:16); wait for it.
  $hwnd = [IntPtr]$w.Current.NativeWindowHandle
  $render = Wait-Until { $h = [NativeTab]::FindRender($hwnd); if ($h -ne [IntPtr]::Zero) { $h } } 5000
  if (-not $render) { return @{ code = 'no_render_window' } }
  $ms = Left-Ms $ms
  if ($ms -le 0) { return @{ code = 'out_of_time' } }
  $deadline = [DateTime]::UtcNow.AddMilliseconds($ms)
  do {
    $edit.SetFocus()  # Tab only accepts the suggestion while the composer has focus
    [NativeTab]::Post($render) | Out-Null
    Start-Sleep -Milliseconds 500
    $text = $value.Current.Value.Trim()
    if ($text) { return @{ code = 'ok'; text = $text; value = $value; edit = $edit } }
    Start-Sleep -Milliseconds 500
  } while ([DateTime]::UtcNow -lt $deadline)
  return @{ code = 'no_suggestion' }
}

function Read-Suggestion($w) {
  # The suggestion is generated a few seconds after the turn ends, longer for a long
  # session (seen live >35 s); wait up to ~30 s.
  $acc = Accept-Suggestion $w 30000
  if ($acc.code -ne 'ok') { return $acc }
  $acc.value.SetValue('')  # the grey suggestion comes back once the box is empty again
  if (-not (Wait-Until { -not $acc.value.Current.Value.Trim() } 3000)) { return @{ code = 'clear_failed' } }
  return @{ code = 'suggestion'; text = $acc.text }
}

function Send-Suggestion($w, $expect) {
  $acc = Accept-Suggestion $w 3000
  if ($acc.code -ne 'ok') { return $acc }
  $value = $acc.value
  # After a read clears the box, Desktop may show nothing or a different suggestion
  # (seen live). Only send the text the user was shown; otherwise hand back the new one.
  if ($expect -and (Norm $acc.text) -ne (Norm $expect)) {
    $value.SetValue('')
    return @{ code = 'suggestion_changed'; text = $acc.text }
  }
  $send = Wait-Until { $s = @(Find-All $w $CT::Button 'Send')[0]; if ($s -and $s.Current.IsEnabled) { $s } }
  if (-not $send) { try { $value.SetValue('') } catch {}; return @{ code = 'fill_failed' } }
  Invoke-El $send
  if (Wait-Until { (Find-All $w $CT::Button 'Stop').Count -or -not $value.Current.Value.Trim() } 8000) {
    return @{ code = 'sent'; text = $acc.text }
  }
  return @{ code = 'send_failed'; text = $acc.text }
}

function Find-ApprovalCards($w) {
  @(Find-All $w $CT::Group $null | Where-Object { $_.Current.ClassName -like '*approval-card*' })
}

function Approve($w, [bool]$allow, $expect) {
  $cards = Wait-Until { $c = Find-ApprovalCards $w; if ($c.Count) { ,$c } } 3000
  if (-not $cards) { return 'no_approval' }
  if ($cards.Count -ne 1) { return 'approval_mismatch' }
  $card = $cards[0]
  if ($expect) {
    $want = Norm $expect
    $texts = @(Find-All $card $CT::Text $null | ForEach-Object { Norm $_.Current.Name })
    if (-not ($texts -contains $want)) { return 'approval_mismatch' }
  }
  $prefix = if ($allow) { 'Allow once' } else { 'Deny' }
  $btn = @(Find-All $card $CT::Button $null | Where-Object { ([string]$_.Current.Name).StartsWith($prefix) })
  if ($btn.Count -ne 1) { return 'approval_mismatch' }
  # A click sometimes does not register on the first try (seen right after the card
  # was located); retry while the card is still there.
  for ($try = 0; $try -lt 3; $try++) {
    Start-Sleep -Milliseconds 400
    try { Invoke-El $btn[0] } catch { return 'approval_failed' }
    if (Wait-Until { (Find-ApprovalCards $w).Count -eq 0 } 2000) { if ($allow) { return 'approved' } else { return 'denied' } }
    [Console]::Error.WriteLine("retry $($try + 1)")
  }
  return 'approval_failed'
}

# An AskUserQuestion card is an approval card with a "Dismiss question" button (VERIFY.md 提问卡片探针).
function Find-QuestionCards($w) {
  @(Find-ApprovalCards $w | Where-Object { (Find-All $_ $CT::Button 'Dismiss question').Count })
}

# The card shows one question at a time. Its own Text children are "i", "/", "n", then the
# question; a card with a single question has only the question.
function Get-QuestionState($card) {
  $c = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::Text)
  $t = @($card.FindAll($TS::Children, $c) | ForEach-Object { Norm ([string]$_.Current.Name) })
  if ($t.Count -ge 4 -and $t[1] -eq '/' -and $t[0] -match '^\d+$' -and $t[2] -match '^\d+$') {
    return @{ index = [int]$t[0]; total = [int]$t[2]; question = $t[3] }
  }
  if ($t.Count -ge 1) { return @{ index = 1; total = 1; question = $t[0] } }
  return $null
}

function Test-SameQuestion($s, $req) {
  $s -and $s.index -eq [int]$req.index -and $s.total -eq [int]$req.total -and $s.question -eq (Norm $req.question)
}

# Option buttons are named "<label> <description>"; single-select ones add a shortcut number.
function Get-OptionKey($b, [bool]$multi) {
  $n = Norm ([string]$b.Current.Name)
  if (-not $multi) { $n = $n -replace ' \d+$', '' }
  return $n
}

function Answer($w, $req) {
  $cards = Wait-Until { $c = Find-QuestionCards $w; if ($c.Count) { ,$c } } 3000
  if (-not $cards) { return 'no_question' }
  if ($cards.Count -ne 1) { return 'question_mismatch' }
  $card = $cards[0]
  if (-not (Test-SameQuestion (Get-QuestionState $card) $req)) { return 'question_mismatch' }
  $multi = [bool]$req.multi
  # Single-select options take Invoke and the click itself moves on; multi-select ones are toggles.
  $pattern = if ($multi) { [System.Windows.Automation.TogglePattern]::Pattern } else { [System.Windows.Automation.InvokePattern]::Pattern }
  $choices = @(Find-All $card $CT::Button $null | Where-Object { $_.GetSupportedPatterns() -contains $pattern })
  $picked = @()
  foreach ($o in @($req.options)) {
    $want = if ($o.description) { Norm "$($o.label) $($o.description)" } else { Norm $o.label }
    $hit = @($choices | Where-Object { (Get-OptionKey $_ $multi) -eq $want })
    if ($hit.Count -ne 1) { return 'option_mismatch' }
    $picked += $hit[0]
  }
  if (-not $picked) { return 'option_mismatch' }
  $target = $picked[0]
  if ($multi) {
    # Leave exactly the chosen toggles on (including "Other" off), one at a time: toggles
    # 300 ms apart once lost one in the probe.
    foreach ($b in $choices) {
      $on = [bool]($picked | Where-Object { [System.Windows.Automation.Automation]::Compare($_, $b) })
      $t = $b.GetCurrentPattern($pattern)
      if (($t.Current.ToggleState -eq 'On') -ne $on) {
        $t.Toggle()
        if (-not (Wait-Until { ($t.Current.ToggleState -eq 'On') -eq $on } 2000)) { return 'answer_failed' }
        Start-Sleep -Milliseconds 700
      }
    }
    $go = @(Find-All $card $CT::Button $null | Where-Object { @('Next', 'Submit') -contains [string]$_.Current.Name })
    if ($go.Count -ne 1) { return 'answer_failed' }
    $target = $go[0]
  }
  for ($try = 0; $try -lt 3; $try++) {
    Start-Sleep -Milliseconds 400
    # The card has moved on if it is gone or shows another question.
    $moved = { $c = Find-QuestionCards $w; $c.Count -eq 0 -or -not (Test-SameQuestion (Get-QuestionState $c[0]) $req) }
    if ($try -and (& $moved)) { return 'answered' }
    try { Invoke-El $target } catch { return 'answer_failed' }
    if (Wait-Until $moved 3000) { return 'answered' }
    [Console]::Error.WriteLine("retry $($try + 1)")
  }
  return 'answer_failed'
}

$req = [Console]::In.ReadToEnd() | ConvertFrom-Json
$w = Get-DesktopWindow
Mark 'window'
if (-not $w) { Done 'no_window' }

$winPattern = $w.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern)
$wasMinimized = $winPattern.Current.WindowVisualState -eq [System.Windows.Automation.WindowVisualState]::Minimized
$code = 'no_window'
$text = $null
$origTitle = $null
try {
  # A minimized window defers fills and queues clicks until restored (VERIFY.md 2).
  if ($wasMinimized) { $winPattern.SetWindowVisualState([System.Windows.Automation.WindowVisualState]::Normal); Mark 'restore' }
  # With the display off Chromium treats the window as hidden: no UI updates, so switches,
  # fills and the Tab all fail (seen live 2026-10-11). Its render window is not a reliable
  # sign: it stayed while the display was off 21:31-21:57 on 2026-10-12, and every switch
  # then failed. Messages and power requests don't wake it reliably; a zero mouse move does.
  # So every run nudges once. It counts as input, so it is reported and the daemon's away
  # check ignores it (DESIGN.md 3.5).
  $hwnd = [IntPtr]$w.Current.NativeWindowHandle
  $before = [NativeTab]::LastInput()
  $quietMs = [long][NativeTab]::Tick() - [long]$before
  if ($quietMs -lt 0) { $quietMs += 4294967296 }  # the tick count wraps every ~49.7 days
  [NativeTab]::Nudge() | Out-Null
  $OwnInput = @($before, [NativeTab]::LastInput())
  # After a quiet minute the display may have been off; give it a moment to come on.
  if ($quietMs -ge 60000) { Start-Sleep -Milliseconds 1500 }
  if (-not (Wait-Until { [NativeTab]::FindRender($hwnd) -ne [IntPtr]::Zero } 1500)) {
    for ($try = 0; $try -lt 3; $try++) {
      [NativeTab]::Nudge() | Out-Null
      $OwnInput = @($before, [NativeTab]::LastInput())
      if (Wait-Until { [NativeTab]::FindRender($hwnd) -ne [IntPtr]::Zero } 3000) { break }
    }
  }
  Mark 'wake'
  # Chromium may expose an empty tree on the first query; wait for the composer.
  Wait-Until { (Find-All $w $CT::Edit 'Prompt').Count } | Out-Null
  $origTitle = Get-OpenTitle $w
  Close-Tips $w
  Mark 'ready'
  $code = Switch-To $w $req.title ([Math]::Max(5000, (Left-Ms 15000)))
  Mark 'switch'
  if ($code -eq 'ok') {
    if ($req.action -eq 'prompt') {
      $res = Send-Prompt $w $req.text ([bool]$req.interject)
      if ($res -is [hashtable]) { $code = $res.code; $text = $res.text } else { $code = $res }
    }
    elseif ($req.action -eq 'approve') { $code = Approve $w ([bool]$req.allow) $req.expect }
    elseif ($req.action -eq 'answer') { $code = Answer $w $req }
    elseif ($req.action -eq 'suggest') { $res = Read-Suggestion $w; $code = $res.code; $text = $res.text }
    elseif ($req.action -eq 'accept') { $res = Send-Suggestion $w $req.expect; $code = $res.code; $text = $res.text }
    else { $code = 'bad_request' }
    Mark $req.action
  }
} catch {
  $code = 'error_' + $_.Exception.GetType().Name
} finally {
  try {
    if ($origTitle -and $origTitle -ne $req.title) {
      # A switch that timed out can still land later (seen in a 2026-10-12 test): let it,
      # or the restore finds the old title still showing and the late switch wins.
      if ($code -eq 'switch_failed') { Wait-Until { (Get-OpenTitle $w) -eq $req.title } 5000 | Out-Null }
      Switch-To $w $origTitle | Out-Null
      Mark 'back'
    }
  } catch {}
  try { if ($wasMinimized) { $winPattern.SetWindowVisualState([System.Windows.Automation.WindowVisualState]::Minimized) } } catch {}
  # Everything from the nudge to here counts as ccmonitor's own input: once, 25 s after a
  # nudge, the daemon saw input it didn't know about (2026-10-12 09:38), possibly from the
  # display coming on. A touch by the user within these few seconds is absorbed too.
  if ($OwnInput) { $OwnInput = @($OwnInput[0], [NativeTab]::LastInput()) }
}
Done $code $text
