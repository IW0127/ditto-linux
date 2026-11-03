"""Shared helpers for the test-suite: isolated environments, a private X server, the daemon."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "tests" / "helpers" / "cliptool.py"


def wait_for(pred, timeout: float = 10.0, interval: float = 0.05, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last={last!r})")


class IsolatedEnv:
    """Temporary XDG dirs, database, settings and socket so tests never touch the user's data."""

    def __init__(self, settings: dict | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="ditto-test-"))
        self.db = self.tmp / "Ditto.db"
        self.config = self.tmp / "settings.json"
        self.socket = str(self.tmp / "ditto.sock")
        base = {"auto_paste": False, "show_tray": False, "gnome_shortcut_done": True,
                "auto_update_check": False, "check_shortcut": False, "auto_report": "never"}
        base.update(settings or {})
        self.config.write_text(json.dumps(base))
        self.env = dict(os.environ)
        self.env.update({
            "DITTO_DB": str(self.db), "DITTO_CONFIG": str(self.config), "DITTO_SOCKET": self.socket,
            "XDG_CONFIG_HOME": str(self.tmp / "config"), "XDG_DATA_HOME": str(self.tmp / "data"),
            "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "DITTO_NO_DESKTOP_INTEGRATION": "1",
            "XDG_STATE_HOME": str(self.tmp / "state"), "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "DITTO_NO_UPDATE_CHECK": "1", "DITTO_NO_REPORT": "1", "DITTO_REPORT_REPO": "example/test",
        })
        self.env.pop("DITTO_LOG", None)
        self.env.pop("DITTO_UPDATE_URL", None)
        self.log = self.tmp / "state" / "ditto-linux" / "ditto.log"

    def cli(self, *args, input: str | None = None, check: bool = True, timeout: float = 30):
        r = subprocess.run([sys.executable, "-m", "ditto_linux", *map(str, args)], env=self.env, cwd=ROOT,
                           input=input, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise AssertionError(f"ditto-linux {' '.join(map(str, args))} failed ({r.returncode}):\n"
                                 f"stdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def cli_json(self, *args):
        return json.loads(self.cli(*args).stdout)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class PrivateXServer:
    """A throw-away X server (rootless Xwayland, or Xvfb) so clipboard tests are hermetic."""

    def __init__(self):
        self.proc = None
        self.display = None

    @staticmethod
    def available() -> str | None:
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("Xwayland"):
            return "Xwayland"
        if shutil.which("Xvfb"):
            return "Xvfb"
        return None

    def start(self) -> str:
        kind = self.available()
        if not kind:
            raise RuntimeError("neither Xwayland (inside a Wayland session) nor Xvfb is available")
        for n in range(150, 250):
            if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
                break
        else:
            raise RuntimeError("no free X display number")
        cmd = ([kind, f":{n}", "-rootless", "-noreset"] if kind == "Xwayland"
               else [kind, f":{n}", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"])
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.display = f":{n}"
        wait_for(lambda: os.path.exists(f"/tmp/.X11-unix/X{n}") or self.proc.poll() is not None,
                 timeout=15, what="X server socket")
        if self.proc.poll() is not None:
            raise RuntimeError(f"{kind} exited with {self.proc.returncode}")
        time.sleep(0.3)
        return self.display

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def x11_env(env: dict, display: str) -> dict:
    e = dict(env)
    e["DISPLAY"] = display
    e["QT_QPA_PLATFORM"] = "xcb"
    e.pop("WAYLAND_DISPLAY", None)
    return e
