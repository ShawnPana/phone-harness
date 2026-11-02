"""Loaded into every Python process of a watched test (PYTHONPATH lists this folder), so the
detached sender and any child a script starts are watched too.

Logs each network attempt and process start to NETWORK_WATCH_LOG, and refuses anything off this
machine: a stray send fails the test and reaches nobody. Never logs a payload, a header or the environment.
"""
import json
import os
import sys

LOG = os.environ.get("NETWORK_WATCH_LOG")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def note(kind, **fields):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({"kind": kind, "pid": os.getpid(), **fields}) + "\n")


def watch(event, args):
    if event == "socket.connect" and isinstance(args[1], tuple):
        note("connect", host=str(args[1][0]))
        if args[1][0] not in LOOPBACK:
            raise PermissionError("network_watch: off this machine")
    elif event == "socket.getaddrinfo":
        note("connect", host=str(args[0]))
        if args[0] not in LOOPBACK:
            raise PermissionError("network_watch: off this machine")
    elif event == "subprocess.Popen":
        argv = [str(a) for a in args[1] or []]
        note("spawn", sender=len(argv) > 2 and argv[1] == "-c" and "urllib.request" in argv[2])


sys.addaudithook(watch)
note("start")
