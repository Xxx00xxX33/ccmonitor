"""Acknowledge one forward request (docs/helper-session.md).

Usage: python relay_ack.py <id> <delivered|queued|error:reason>
(or a JSON object on stdin, as the tests do).
"""
import json
import os
import socket
import sys


def main() -> int:
    if len(sys.argv) >= 3:
        msg = {"ccmonitor": "ack", "id": int(sys.argv[1]), "result": " ".join(sys.argv[2:])}
    else:
        msg = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    port = int(os.environ.get("CCMONITOR_PORT", "47821"))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            s.shutdown(socket.SHUT_WR)
            s.recv(1)
    except OSError:
        print("ccmonitor daemon not reachable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
