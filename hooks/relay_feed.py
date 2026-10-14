"""Feed for the helper session's Monitor tool (docs/helper-session.md).

Connects to the daemon's helper port, identifies the helper session, then prints
one JSON line per forward request: {"id": n, "to": "local_...", "text": "..."}.
Exits when the daemon closes the connection, so the Monitor ends and is re-armed.
"""
import json
import os
import socket
import sys


def main() -> int:
    port = int(os.environ.get("CCMONITOR_FEED_PORT", int(os.environ.get("CCMONITOR_PORT", "47821")) + 1))
    hello = {"session_id": os.environ.get("CLAUDE_CODE_SESSION_ID", "")}
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
    except OSError:
        print("ccmonitor daemon not reachable", file=sys.stderr)
        return 1
    with s:
        s.settimeout(None)
        s.sendall((json.dumps(hello) + "\n").encode("utf-8"))
        for line in s.makefile("rb"):
            if line.strip():
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
