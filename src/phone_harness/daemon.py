"""Daemon mode: pay the import tax once, run every snippet in ~0.1s.

A cold `phone-harness` invocation imports pyobjc + Vision and connects a
backend — ~1s on a fast Mac, 10-14s on a virtualized CI runner. Agents make
hundreds of invocations per benchmark sweep, so the cold start dominates
wall time on slow machines.

    phone-harness --serve     # hold imports + backend, listen on a socket
    PHONE_HARNESS_DAEMON=1 phone-harness <<'PY'   # ~0.1s client
    print(screen_info())
    PY

The client ships the snippet over a unix socket; the server execs it with
the same pre-imported helper globals a cold run would build, and streams
back stdout/stderr and the exit status. One snippet at a time (a lock, not
a queue): the phone is a serial device anyway.

The socket path is derived from PHONE_HARNESS_PLATFORM and
PHONE_HARNESS_SIM_DEVICE so distinct targets get distinct daemons.
"""
import contextlib
import io
import json
import os
import socket
import struct
import sys
import threading


def _sock_path():
    key = (os.environ.get("PHONE_HARNESS_PLATFORM", "ios") + "-"
           + os.environ.get("PHONE_HARNESS_SIM_DEVICE", "default"))
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return os.path.join(os.environ.get("TMPDIR", "/tmp"),
                        f"phone-harness-{safe}.sock")


def _recv_msg(conn):
    head = b""
    while len(head) < 4:
        chunk = conn.recv(4 - len(head))
        if not chunk:
            return None
        head += chunk
    (n,) = struct.unpack(">I", head)
    body = b""
    while len(body) < n:
        chunk = conn.recv(min(65536, n - len(body)))
        if not chunk:
            return None
        body += chunk
    return json.loads(body)


def _send_msg(conn, obj):
    body = json.dumps(obj).encode()
    conn.sendall(struct.pack(">I", len(body)) + body)


def serve():
    """Foreground server. Build helper globals once; exec snippets forever."""
    from . import helpers                      # the expensive part, paid once
    base = {k: v for k, v in vars(helpers).items() if not k.startswith("_")}

    path = _sock_path()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    lock = threading.Lock()
    print(f"phone-harness daemon on {path}", flush=True)

    def handle(conn):
        with conn:
            req = _recv_msg(conn)
            if not req:
                return
            if req.get("op") == "ping":
                _send_msg(conn, {"ok": True})
                return
            code = req.get("code", "")
            out = io.StringIO()
            status = 0
            with lock:                          # the phone is serial
                g = dict(base)
                g["__name__"] = "__main__"
                try:
                    with contextlib.redirect_stdout(out), \
                         contextlib.redirect_stderr(out):
                        exec(code, g)
                except SystemExit as e:
                    status = int(e.code) if isinstance(e.code, int) else 1
                except BaseException:
                    import traceback
                    out.write(traceback.format_exc())
                    status = 1
            _send_msg(conn, {"output": out.getvalue(), "status": status})

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def client_run(code):
    """Ship a snippet to the daemon. Returns exit status, prints its output.
    Raises ConnectionError if no daemon is listening (caller falls back)."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(600)
    conn.connect(_sock_path())                  # ConnectionRefusedError -> caller
    _send_msg(conn, {"code": code})
    resp = _recv_msg(conn)
    conn.close()
    if resp is None:
        raise ConnectionError("daemon hung up")
    sys.stdout.write(resp.get("output", ""))
    sys.stdout.flush()
    return resp.get("status", 0)
