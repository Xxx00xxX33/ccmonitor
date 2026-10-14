"""Hook command: forward one hook payload to the local ccmonitor daemon.

Registered for every hook event ccmonitor listens to. It must never slow down or
break a session: stdlib only, short timeouts, always exit 0. Stdout of a
UserPromptSubmit hook becomes model context, so the only thing ever printed is
the daemon's away-line reply, and only after checking its exact shape.

A daemon that is restarting (a few seconds without a listener) would otherwise lose
the notice of a turn that ends meanwhile, so events that produce notices retry the
connection for a few seconds. A mark file records when the daemon was first found
down; once it has been down longer than DOWN_SECONDS nothing waits, so a stopped
daemon never slows sessions down. The mark holds no content, only its time.
"""
import json
import os
import socket
import sys
import time

MAX_BYTES = 4 * 1024 * 1024
REPLY_SECONDS = 1.5
RETRY_EVENTS = {"Stop", "StopFailure", "PermissionRequest", "PreToolUse"}  # the ones that become notices
RETRY_SECONDS = 3.5  # all attempts together; hooks are registered with a 5 s timeout
RETRY_EVERY = 0.25
DOWN_SECONDS = 30


def down_mark() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ccmonitor", "relay-down")


def may_wait() -> bool:
    """Whether a daemon that refused us is worth waiting for, and mark it down if it's new."""
    mark = down_mark()
    try:
        return time.time() - os.path.getmtime(mark) < DOWN_SECONDS
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        open(mark, "w").close()
    except OSError:
        pass
    return True


def connect(port: int, event) -> socket.socket:
    started = time.monotonic()
    try:
        return socket.create_connection(("127.0.0.1", port), timeout=0.5)
    except OSError:  # refused, or on Windows a timeout: nobody is listening
        if event not in RETRY_EVENTS or not may_wait():
            raise
    deadline = started + RETRY_SECONDS
    while True:
        time.sleep(RETRY_EVERY)
        left = deadline - time.monotonic()
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=max(0.05, min(0.5, left)))
        except OSError:
            if left <= 0.5:
                raise


def event_of(data: bytes):
    try:
        return json.loads(data.decode("utf-8")).get("hook_event_name")
    except Exception:
        return None


def read_reply(s: socket.socket) -> bytes:
    deadline = time.monotonic() + REPLY_SECONDS
    chunks = []
    while (left := deadline - time.monotonic()) > 0:
        s.settimeout(left)
        chunk = s.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    else:
        return b""  # daemon too slow: inject nothing rather than a partial reply
    return b"".join(chunks)


def valid_reply(raw: bytes) -> bytes:
    try:
        data = json.loads(raw.decode("utf-8"))
        out = data["hookSpecificOutput"]
        if (set(data) == {"hookSpecificOutput"} and set(out) == {"hookEventName", "additionalContext"}
                and out["hookEventName"] == "UserPromptSubmit" and isinstance(out["additionalContext"], str)):
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    return b""


def slim(data: bytes) -> bytes:
    """A PostToolUse payload carries the tool's whole result (a file read, a command's
    output), which the daemon never uses: drop it before sending."""
    try:
        p = json.loads(data.decode("utf-8"))
        if p.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure"):
            for k in ("tool_response", "error"):
                p.pop(k, None)
            return json.dumps(p, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    return data


def main() -> None:
    try:
        data = slim(sys.stdin.buffer.read(MAX_BYTES))
        port = int(os.environ.get("CCMONITOR_PORT", "47821"))
        with connect(port, event_of(data)) as s:
            if os.path.exists(down_mark()):
                try:
                    os.remove(down_mark())
                except OSError:
                    pass
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            reply = read_reply(s)
        if reply:
            out = valid_reply(reply)
            if out:
                sys.stdout.buffer.write(out)
                sys.stdout.buffer.flush()
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
