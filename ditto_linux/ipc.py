"""Tiny JSON-lines protocol over a Unix socket for controlling the running daemon."""
from __future__ import annotations

import json
import socket

from .config import socket_path


class DaemonNotRunning(ConnectionError):
    pass


def request(cmd: str, timeout: float = 5.0, path: str | None = None, **args) -> dict:
    """Send one command to the daemon and return its JSON reply."""
    path = path or socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        s.close()
        raise DaemonNotRunning(f"ditto-linux daemon is not running ({path})") from e
    try:
        s.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        raise ConnectionError("daemon closed the connection without replying")
    return json.loads(buf)


def is_running(path: str | None = None) -> bool:
    try:
        return request("ping", timeout=2.0, path=path).get("ok", False)
    except (ConnectionError, OSError, ValueError):
        return False
