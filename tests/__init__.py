"""No test may read or write the real storage directory (%USERPROFILE%\.ccmonitor) or
its token. Code that falls back to paths.DATA_DIR (the daemon's relay-mode.json and
topics.json, config.json looked up without a path) lands in a throwaway folder instead;
set here, before any test module imports ccmonitor.paths."""
import atexit
import os
import shutil
import tempfile

_home = tempfile.mkdtemp(prefix="ccmonitor-tests-")
os.environ["CCMONITOR_HOME"] = _home
atexit.register(shutil.rmtree, _home, ignore_errors=True)
