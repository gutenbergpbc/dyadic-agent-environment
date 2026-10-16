"""Root-owned narrow process manager. Application code only runs as uid 1100."""

import json
import os
import signal
import socket
import struct
import subprocess
import time

proc = None
os.makedirs("/control", mode=0o700, exist_ok=True)
log = open("/control/app.log", "ab", buffering=0)


def start():
    global proc
    if proc is None or proc.poll() is not None:
        proc = subprocess.Popen(
            ["python", "-u", "/workspace/app.py"],
            cwd="/workspace",
            user=1100,
            group=1200,
            extra_groups=[],
            start_new_session=True,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/home/app",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdout=log,
            stderr=log,
        )


def stop():
    global proc
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    proc = None


start()
sock = socket.socket(socket.AF_UNIX)
sock.bind("/run/appctl.sock")
os.chown("/run/appctl.sock", 0, 1200)
os.chmod("/run/appctl.sock", 0o660)
sock.listen(8)
while True:
    conn, _ = sock.accept()
    with conn:
        conn.settimeout(5)
        try:
            pid, uid, gid = struct.unpack(
                "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            command = conn.recv(100).decode().strip()
            if command not in ("start", "stop", "restart", "status"):
                conn.sendall(b'{"error":"allowed: start stop restart status"}')
                continue
            if command in ("stop", "restart"):
                stop()
            if command in ("start", "restart"):
                start()
            event = {
                "time": time.time(),
                "uid": uid,
                "pid": pid,
                "action": command,
                "running": bool(proc and proc.poll() is None),
            }
            with open("/control/service-actions.jsonl", "a") as out:
                out.write(json.dumps(event) + "\n")
            conn.sendall(json.dumps(event).encode())
        except Exception as exc:
            try:
                conn.sendall(json.dumps({"error": repr(exc)}).encode())
            except OSError:
                pass
