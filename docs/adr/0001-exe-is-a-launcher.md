# ccmonitor.exe 是启动器，不是冻结打包

ccmonitor 的 exe 只是一个用 Windows 自带 `csc.exe` 编出来的几 KB 小程序：它用本机的 `pythonw.exe` 运行仓库里的界面代码，界面和守护进程在同一个进程里。这样合并 PR 后用 `ccmonitor.exe --replace` 重启就用上新代码，部署流程不变，项目也仍然只依赖标准库；代价是 exe 绑定了这台机器上的 Python 和仓库路径，不能拷到别的机器单独运行。

## Considered Options

- **PyInstaller 冻结打包**：exe 自带 Python 和代码，能单独分发；但每次合并都得重新打包，否则跑的是旧代码。ccmonitor 只在一台机器上用、改得很勤，这个代价不值。
- **原生界面（C# 等）+ 独立的 Python 守护进程**：界面本身更省，但常驻变成两个进程，还要一套控制协议；"退出 ccmonitor"也不再是结束一个进程那么简单。

## Consequences

- exe 不进 git，每台机器用 `python -m ccmonitor.build_exe` 构建一次；只有 Python 或仓库换了位置才需要重建。
- 指向存储目录的 `ccmonitor.ini` 放在 exe 旁边（仓库的 `bin\`），所以从 worktree 运行时读不到它，落回默认目录。
- 没有无界面的入口：测试修复分支时，从 worktree 运行界面并加 `--replace`，接管正在运行的实例。
