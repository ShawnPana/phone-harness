"""ADB over a WebSocket bridge: the customer side of a boat phone's ADB gate.

Copied from phone-cloud (src/phone_cloud/adb_bridge.py), not imported; keep
the two identical. `python -m phone_harness.adb_bridge serve …` is the local
daemon `phone-harness cloud start` runs for a bridge-transport phone.

A boat sandbox is reachable only through boat's HTTPS hosting, which carries
HTTP and WebSocket but no raw TCP. The sandbox service therefore exposes its
ADB gate as `GET /adb/<code>` (WebSocket upgrade), one WebSocket per ADB TCP
connection, copying bytes both ways: every binary frame's payload goes to the
gate socket as-is, every gate read comes back as one binary frame. The gate
itself still requires `adb shell unlock <code>` as the first stream, exactly
as the shlut worker's gate does, so nothing here is an authority.

This module is the other end of that contract, stdlib only, and host-agnostic:
`Relay` listens on plain TCP ports and turns each accepted connection into one
bridge WebSocket, whether it runs next to the API or inside a CLI.

Wire facts (agreed 2026-09-25): frames are binary and at most 64 KiB, never
fragmented, no text frames; the service answers 403 before the upgrade for an
unknown or expired code and 502 while the phone is not booted; EOF or error on
either side closes the other; RFC 6455 ping/pong, the client may ping every
30 s; the service checks `Origin` on upgrades.
"""
import base64
import hashlib
import os
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
from urllib.parse import urlsplit

FRAME_LIMIT = 64 * 1024      # 64 KiB inclusive, the agreed maximum each way; a full frame uses the 8-byte length form
UPGRADE_LIMIT = 16 * 1024
PING_INTERVAL = 30
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TEXT, _BINARY, _CLOSE, _PING, _PONG = 0x1, 0x2, 0x8, 0x9, 0xA


def mint_code():
    """A session's unlock code in the shape the API validates: ph_ + 26 base32 chars."""
    return "ph_" + base64.b32encode(secrets.token_bytes(20)).decode().lower()[:26]


class BridgeRefused(Exception):
    """The service answered the upgrade with an HTTP status: 403 (code unknown or
    expired), 502 (phone not booted) or anything else; `status` says which."""

    def __init__(self, status, reason=""):
        super().__init__(f"bridge refused the upgrade: {status} {reason}".strip())
        self.status = status


class Bridge:
    """One WebSocket to `GET /adb/<code>`; binary frames only."""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""
        self.closed = False
        self.send_lock = threading.Lock()

    @classmethod
    def connect(cls, url, origin, *, timeout=15, ssl_context=None):
        u = urlsplit(url)
        if u.scheme not in ("ws", "wss") or not u.hostname or u.username or u.password or u.fragment:
            raise ValueError("invalid bridge URL")
        if not isinstance(origin, str) or not origin.startswith("https://") or any(c in origin for c in "\r\n "):
            raise ValueError("invalid bridge origin")
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if u.scheme == "wss":
                sock = (ssl_context or ssl.create_default_context()).wrap_socket(sock, server_hostname=u.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
            if port not in (80, 443):
                host += f":{port}"
            target = (u.path or "/") + (f"?{u.query}" if u.query else "")
            request = (f"GET {target} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n"
                       f"User-Agent: phone-cloud-adb-bridge/1\r\n\r\n")
            sock.sendall(request.encode("ascii"))
            head = b""
            deadline = time.monotonic() + timeout
            while b"\r\n\r\n" not in head:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("bridge closed during the upgrade")
                head += chunk
                if len(head) > UPGRADE_LIMIT:
                    raise ValueError("bridge upgrade response too large")
            header, _, rest = head.partition(b"\r\n\r\n")
            lines = header.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ", 2)
            status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            if status != 101:
                raise BridgeRefused(status, parts[2] if len(parts) == 3 else "")
            fields = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
            expected = base64.b64encode(hashlib.sha1(key.encode() + _GUID).digest()).decode()
            if (fields.get("upgrade", "").lower() != "websocket" or fields.get("sec-websocket-accept") != expected):
                raise ConnectionError("bridge upgrade response is not a WebSocket accept")
            bridge = cls(sock)
            bridge.buffer = rest
            sock.settimeout(None)
            return bridge
        except Exception:
            sock.close()
            raise

    # --- frames ---------------------------------------------------------------

    def _send_frame(self, opcode, payload=b""):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(head + mask + masked)

    def send(self, payload):
        """One binary frame per call, split at the frame limit."""
        for i in range(0, len(payload), FRAME_LIMIT):
            self._send_frame(_BINARY, payload[i:i + FRAME_LIMIT])

    def ping(self):
        self._send_frame(_PING, b"")

    def _read_exact(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("bridge disconnected")
            self.buffer += chunk
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def receive(self):
        """The next binary payload, or None when the service closed the socket.
        Pings are answered here; pongs and empty frames are skipped."""
        while True:
            b0, b1 = struct.unpack("!BB", self._read_exact(2))
            fin, opcode, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            if n > FRAME_LIMIT:
                raise ValueError("bridge frame exceeds the agreed limit")
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == _BINARY:
                if not fin:
                    raise ValueError("bridge sent a fragmented frame")
                if payload:
                    return payload
            elif opcode == _CLOSE:
                self.closed = True
                return None
            elif opcode == _PING:
                self._send_frame(_PONG, payload)
            elif opcode == _PONG:
                pass
            elif opcode == _TEXT:
                raise ValueError("bridge sent a text frame")
            else:
                raise ValueError(f"bridge sent an unsupported opcode {opcode}")

    def close(self, code=1000):
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(_CLOSE, struct.pack("!H", code))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def abort(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def _note(where, exc):
    """Errors in a pump thread are expected at the end of a connection (EOF,
    reset); print the unexpected ones so a daemon log explains a dead link."""
    if (isinstance(exc, (ConnectionResetError, BrokenPipeError)) or (isinstance(exc, OSError) and not str(exc))
            or (isinstance(exc, ConnectionError) and "disconnected" in str(exc))):
        return                                  # the far end hung up: how every connection ends
    print(f"adb-bridge: {where}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def pump(tcp, bridge):
    """Copy bytes both ways until either side ends; returns when both are closed.

    TCP → bridge: every read becomes one binary frame (the bridge splits at the
    limit). Bridge → TCP: every payload is written as-is. TCP EOF sends the
    bridge a normal close; bridge close shuts the TCP side. A pinger keeps the
    hosting path alive on quiet connections.
    """
    done = threading.Event()

    def to_bridge():
        try:
            while not done.is_set():
                data = tcp.recv(FRAME_LIMIT)
                if not data:
                    break
                bridge.send(data)
        except (OSError, ValueError) as e:
            _note("tcp→bridge", e)
        finally:
            bridge.close()
            done.set()

    def to_tcp():
        try:
            while not done.is_set():
                payload = bridge.receive()
                if payload is None:
                    break
                tcp.sendall(payload)
        except (OSError, ValueError) as e:
            _note("bridge→tcp", e)
        finally:
            try:
                tcp.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            done.set()

    def pinger():
        while not done.wait(PING_INTERVAL):
            try:
                bridge.ping()
            except OSError:
                done.set()

    threads = [threading.Thread(target=f, daemon=True, name=f"adb-bridge-{f.__name__}") for f in (to_bridge, to_tcp, pinger)]
    for t in threads:
        t.start()
    done.wait()
    bridge.abort()
    try:
        tcp.close()
    except OSError:
        pass
    for t in threads[:2]:
        t.join(timeout=5)


class Grant:
    def __init__(self, port, bridge_url, origin, expires_at):
        self.port, self.bridge_url, self.origin, self.expires_at = port, bridge_url, origin, expires_at
        self.connections = 0


class Relay:
    """Plain TCP ports, one per live grant, each connection bridged lazily.

    `bind` is the interface customers reach; `ports` the inclusive range the
    API advertises (session_adb.capability accepts 22220–22283). A grant is
    registered when the API mints a session's ADB capability and dropped when
    it is revoked or the session ends; a connection to a port with no live
    grant is closed at once. Nothing is dialed until a customer connects, so an
    unused grant costs the sandbox nothing.
    """

    def __init__(self, bind="0.0.0.0", ports=(22220, 22283), *, connector=None, clock=time.time, max_connections=16):
        lo, hi = ports
        if not (0 < lo <= hi < 65536):
            raise ValueError("invalid relay port range")
        self.bind, self.lo, self.hi = bind, lo, hi
        self.connector = connector or Bridge.connect
        self.clock = clock
        self.max_connections = max_connections
        self.grants = {}          # port -> Grant
        self.listeners = {}       # port -> socket
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    # --- grants ---------------------------------------------------------------

    def allocate(self, bridge_url, origin, expires_at):
        """Reserve a free port for a grant and start listening on it. -> port"""
        with self.lock:
            self._expire_locked()
            for port in range(self.lo, self.hi + 1):
                if port in self.grants:
                    continue
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    listener.bind((self.bind, port))
                    listener.listen(16)
                except OSError:
                    listener.close()
                    continue
                self.grants[port] = Grant(port, bridge_url, origin, expires_at)
                self.listeners[port] = listener
                threading.Thread(target=self._serve, args=(port, listener), daemon=True, name=f"adb-relay-{port}").start()
                return port
        raise RuntimeError("no free ADB relay port")

    def release(self, port):
        with self.lock:
            self._release_locked(port)

    def _release_locked(self, port):
        self.grants.pop(port, None)
        listener = self.listeners.pop(port, None)
        if listener:
            try:
                listener.close()
            except OSError:
                pass

    def _expire_locked(self):
        now = self.clock()
        for port in [p for p, g in self.grants.items() if g.expires_at <= now]:
            self._release_locked(port)

    def grant_for(self, port):
        with self.lock:
            self._expire_locked()
            return self.grants.get(port)

    def live_ports(self):
        with self.lock:
            self._expire_locked()
            return sorted(self.grants)

    # --- connections ------------------------------------------------------------

    def _serve(self, port, listener):
        while not self.stopped.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return                      # listener closed: grant released
            threading.Thread(target=self._bridge_one, args=(port, conn), daemon=True, name=f"adb-relay-conn-{port}").start()

    def _bridge_one(self, port, conn):
        grant = self.grant_for(port)
        if grant is None or grant.connections >= self.max_connections:
            conn.close()
            return
        grant.connections += 1
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                bridge = self.connector(grant.bridge_url, grant.origin)
            except Exception as e:
                _note("connect", e)
                conn.close()                # 403/502/network: the customer sees a reset, adb says "connection refused"
                return
            pump(conn, bridge)
        finally:
            grant.connections -= 1

    def close(self):
        self.stopped.set()
        with self.lock:
            for port in list(self.grants):
                self._release_locked(port)


# --- the local daemon --------------------------------------------------------

def serve(argv=None):
    """Listen on one loopback port and bridge every ADB connection until the
    grant expires or the process is stopped. Prints the port on stdout once
    listening, so the parent can record it."""
    import argparse
    import signal
    parser = argparse.ArgumentParser(prog="phone_harness.adb_bridge serve")
    parser.add_argument("--url", required=True, help="bridge URL including the code: wss://…/adb/<code>")
    parser.add_argument("--origin", required=True, help="the sandbox's own https origin, sent as Origin")
    parser.add_argument("--expires", type=float, required=True, help="unix time the grant ends")
    parser.add_argument("--port", type=int, default=0, help="loopback port to listen on (0 = pick one)")
    args = parser.parse_args(argv)
    ports = (args.port, args.port) if args.port else (1, 65535)
    relay = Relay("127.0.0.1", ports)
    if not args.port:
        # An ephemeral port: bind 0 through the OS, then register it.
        probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
        relay.lo = relay.hi = port
    port = relay.allocate(args.url, args.origin, args.expires)
    print(port, flush=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.wait(1):
        if time.time() >= args.expires:
            break
    relay.close()
    return 0


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "serve":
        raise SystemExit(serve(_sys.argv[2:]))
    raise SystemExit("usage: python -m phone_harness.adb_bridge serve --url … --origin … --expires …")
