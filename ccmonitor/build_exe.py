"""Builds bin\\ccmonitor.exe, the launcher (ADR 0001), with Windows' own C# compiler.

    python -m ccmonitor.build_exe [--out PATH]

The exe runs this machine's pythonw on this checkout's ccmonitor.gui, passing its
arguments on, so merged code is used without rebuilding. Rebuild only when Python or the
repository moves. The tray's three icons (gui.TRAY_COLORS, #67: running, disabled, error)
are drawn by icons.py and embedded as icon groups 1-3; group 1, the running colour, is
the exe's own icon. The exe is not in git.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import icons

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "bin" / "ccmonitor.exe"

SOURCE = r'''using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

[assembly: System.Reflection.AssemblyTitle("ccmonitor")]
[assembly: System.Reflection.AssemblyProduct("ccmonitor")]

static class Launcher
{
    const string Pythonw = @"%(pythonw)s";
    const string Repo = @"%(repo)s";

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr hwnd, string text, string caption, uint type);

    static int Fail(string text)
    {
        MessageBoxW(IntPtr.Zero, text + "\n\n重新构建：python -m ccmonitor.build_exe", "ccmonitor", 0x10);
        return 1;
    }

    static string Quote(string a)
    {
        if (a.Length > 0 && a.IndexOfAny(new char[] { ' ', '\t', '"' }) < 0) return a;
        return "\"" + a.Replace("\"", "\\\"") + "\"";
    }

    static int Main(string[] args)
    {
        if (!File.Exists(Pythonw)) return Fail("找不到 Python：" + Pythonw);
        if (!Directory.Exists(Path.Combine(Repo, "ccmonitor"))) return Fail("找不到 ccmonitor 仓库：" + Repo);
        StringBuilder line = new StringBuilder("-m ccmonitor.gui");
        foreach (string a in args) line.Append(' ').Append(Quote(a));
        ProcessStartInfo start = new ProcessStartInfo(Pythonw, line.ToString());
        start.WorkingDirectory = Repo;
        start.UseShellExecute = false;
        try { Process.Start(start); }
        catch (Exception e) { return Fail("无法启动 ccmonitor：" + e.Message); }
        return 0;
    }
}
'''


def find_csc() -> Path:
    """csc.exe of the .NET Framework 4.x that ships with Windows."""
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for framework in ("Framework64", "Framework"):
        csc = windir / "Microsoft.NET" / framework / "v4.0.30319" / "csc.exe"
        if csc.exists():
            return csc
    raise FileNotFoundError("csc.exe not found under %WINDIR%\\Microsoft.NET")


def pythonw() -> Path:
    path = Path(sys.executable).with_name("pythonw.exe")
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    return path


def source(pythonw_path: Path, repo: Path) -> str:
    def verbatim(p: Path) -> str:
        return str(p).replace('"', '""')
    return SOURCE % {"pythonw": verbatim(pythonw_path), "repo": verbatim(repo)}


def state_colours() -> list[str]:
    """Running first: Explorer shows the first icon group as the exe's icon."""
    from .controller import DISABLED, ERROR, RUNNING
    from .gui import TRAY_COLORS  # the tray's colours, so the exe matches the icons in the tray
    return [TRAY_COLORS[s] for s in (RUNNING, DISABLED, ERROR)]


def build(out: Path = DEFAULT_OUT) -> Path:
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        cs, res = Path(tmp) / "launcher.cs", Path(tmp) / "icons.res"
        cs.write_text(source(pythonw(), ROOT), encoding="utf-8")
        res.write_bytes(icons.res(state_colours()))
        result = subprocess.run([str(find_csc()), "/nologo", "/target:winexe", "/optimize+",
                                 "/codepage:65001", f"/win32res:{res}", f"/out:{out}", str(cs)],

                                capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"csc failed:\n{result.stdout}{result.stderr}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build bin\\ccmonitor.exe (the launcher) with csc.exe")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    out = build(args.out)
    print(f"built {out}\n  runs {pythonw()} -m ccmonitor.gui in {ROOT}")


if __name__ == "__main__":
    main()
