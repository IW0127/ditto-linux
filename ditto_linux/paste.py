"""Auto-paste: send the paste keystroke to the window that was focused before Ditto opened.

Tools, in order of preference:
  * ``wtype``   - wlroots compositors (Sway, Hyprland, ...)
  * ``ydotool`` - any session (GNOME/KDE Wayland included) via the kernel uinput device. Ditto starts
                  its own private ``ydotoold`` helper and stops it on exit (needs access to
                  /dev/uinput, i.e. membership of the ``input`` group on most distributions).
  * ``xdotool`` - X11 sessions
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time

from .config import Settings

log = logging.getLogger(__name__)

# Linux input event codes (linux/input-event-codes.h) for ydotool.
_EV = {"ctrl": 29, "shift": 42, "alt": 56, "v": 47, "insert": 110}
_RELEASE_MODIFIERS = ["42:0", "54:0", "29:0", "97:0", "56:0", "100:0", "125:0", "126:0"]

PASTE_SHORTCUTS = ("ctrl+v", "ctrl+shift+v", "shift+insert")


def _keys(shortcut: str) -> list[str]:
    keys = [k.strip() for k in shortcut.lower().split("+") if k.strip()]
    return keys if keys and all(k in _EV for k in keys) else ["ctrl", "v"]


def _ydotool_args(shortcut: str) -> list[str]:
    keys = _keys(shortcut)
    down = [f"{_EV[k]}:1" for k in keys]
    up = [f"{_EV[k]}:0" for k in reversed(keys)]
    return _RELEASE_MODIFIERS + down + up


def _wtype_args(shortcut: str) -> list[str]:
    *mods, key = _keys(shortcut)
    key = {"v": "v", "insert": "Insert"}[key]
    out = []
    for m in mods:
        out += ["-M", m]
    out += ["-k", key] if key == "Insert" else [key]
    for m in reversed(mods):
        out += ["-m", m]
    return out


def _xdotool_args(shortcut: str) -> list[str]:
    names = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "v": "v", "insert": "Insert"}
    return ["key", "--clearmodifiers", "+".join(names[k] for k in _keys(shortcut))]


class Paster:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._ydotoold: subprocess.Popen | None = None
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/ditto-linux-{os.getuid()}"
        self.ydotool_socket = os.path.join(runtime, "ditto-linux-ydotool.sock")
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ tool selection
    def candidates(self) -> list[str]:
        pref = self.settings.paste_tool
        if pref == "none":
            return []
        if pref in ("wtype", "ydotool", "xdotool"):
            return [pref] if shutil.which(pref) else []
        wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        order = ["wtype", "ydotool", "xdotool"] if wayland else ["xdotool", "ydotool"]
        return [t for t in order if shutil.which(t)]

    def _ydotool_usable(self) -> bool:
        return bool(shutil.which("ydotoold")) and os.access("/dev/uinput", os.R_OK | os.W_OK)

    _legacy: bool | None = None

    def ydotool_legacy(self) -> bool:
        """ydotool 0.1.x (Ubuntu 22.04) has no socket option and takes key names; it needs no daemon."""
        if self._legacy is None:
            try:
                r = subprocess.run(["ydotoold", "--help"], capture_output=True, text=True, timeout=5)
                self._legacy = "socket-path" not in f"{r.stdout}{r.stderr}"
            except (OSError, subprocess.SubprocessError):
                self._legacy = not shutil.which("ydotoold")
        return self._legacy

    def diagnose(self) -> list[tuple[bool, str]]:
        """What auto-paste needs on this system: [(ok, message)], problems explain the fix."""
        out: list[tuple[bool, str]] = []
        session = os.environ.get("XDG_SESSION_TYPE") or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11")
        out.append((self.settings.auto_paste, "auto-paste is " + ("on" if self.settings.auto_paste else
                                                                  "off (turn it on in Options)")))
        tools = self.candidates()
        if not tools:
            want = "ydotool" if session == "wayland" else "xdotool (or ydotool)"
            out.append((False, f"no paste tool installed: sudo apt install {want.split()[0]}"))
        else:
            out.append((True, f"paste tools found: {', '.join(tools)}"))
        if "ydotool" in tools:
            access = os.access("/dev/uinput", os.R_OK | os.W_OK)
            out.append((access, "/dev/uinput is writable" if access else
                        "no permission on /dev/uinput: run the fix script (adds a udev rule and the 'input' "
                        "group), then log out and in"))
            if self.ydotool_legacy():
                out.append((True, "old ydotool (0.1.x): used without a helper daemon"))
        if session == "wayland" and tools == ["xdotool"]:
            out.append((False, "Wayland session: xdotool only reaches X11 apps; install ydotool"))
        if session == "x11" and "xdotool" not in tools:
            out.append((True, "tip: on X11 'sudo apt install xdotool' works without extra permissions"))
        out.append((True, f"paste keystroke: {self.settings.paste_shortcut} "
                          "(terminals need ctrl+shift+v: change it in Options)"))
        return out

    def start(self) -> None:
        """Prepare helpers up front so the first paste is not delayed."""
        if (self.settings.auto_paste and "ydotool" in self.candidates() and self._ydotool_usable()
                and not self.ydotool_legacy()):
            self._ensure_ydotoold()

    def _ensure_ydotoold(self) -> bool:
        if self._ydotoold and self._ydotoold.poll() is None and os.path.exists(self.ydotool_socket):
            return True
        if not self._ydotool_usable():
            log.warning("ydotool needs ydotoold and read/write access to /dev/uinput (join the 'input' group)")
            return False
        try:
            os.unlink(self.ydotool_socket)
        except FileNotFoundError:
            pass
        self._ydotoold = subprocess.Popen(
            ["ydotoold", f"--socket-path={self.ydotool_socket}", "--socket-perm=0600", "--mouse-off"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not os.path.exists(self.ydotool_socket):
            if self._ydotoold.poll() is not None:
                log.warning("ydotoold exited with %s", self._ydotoold.returncode)
                return False
            time.sleep(0.05)
        time.sleep(0.3)  # let the compositor pick up the new virtual keyboard
        log.info("started ydotoold helper (pid %s)", self._ydotoold.pid)
        return os.path.exists(self.ydotool_socket)

    def stop(self) -> None:
        if self._ydotoold and self._ydotoold.poll() is None:
            self._ydotoold.terminate()
            try:
                self._ydotoold.wait(3)
            except subprocess.TimeoutExpired:
                self._ydotoold.kill()
        self._ydotoold = None

    # ------------------------------------------------------------------ paste
    def command(self, tool: str) -> tuple[list[str], dict | None]:
        shortcut = self.settings.paste_shortcut
        if tool == "ydotool" and self.ydotool_legacy():
            return ["ydotool", "key", "+".join(_keys(shortcut))], None
        if tool == "ydotool":
            env = dict(os.environ, YDOTOOL_SOCKET=self.ydotool_socket)
            return ["ydotool", "key", *_ydotool_args(shortcut)], env
        if tool == "wtype":
            return ["wtype", *_wtype_args(shortcut)], None
        return ["xdotool", *_xdotool_args(shortcut)], None

    def paste(self) -> bool:
        """Send the paste keystroke; tries each available tool until one succeeds."""
        with self._lock:
            for tool in self.candidates():
                if tool == "ydotool":
                    if not os.access("/dev/uinput", os.R_OK | os.W_OK):
                        log.warning("ydotool: no permission on /dev/uinput (see 'ditto-linux doctor')")
                        continue
                    if not self.ydotool_legacy() and not self._ensure_ydotoold():
                        continue
                cmd, env = self.command(tool)
                try:
                    r = subprocess.run(cmd, env=env, timeout=5, capture_output=True)
                except (OSError, subprocess.TimeoutExpired) as e:
                    log.warning("%s failed: %s", tool, e)
                    continue
                err = (r.stderr or b"").decode(errors="replace").strip()
                if r.returncode == 0 and "failed" not in err.lower():
                    log.debug("pasted with %s", tool)
                    return True
                log.info("%s could not paste (exit %s): %s", tool, r.returncode, err)
            log.warning("auto-paste failed: %s", "; ".join(m for ok, m in self.diagnose() if not ok)
                        or "no tool could send the keystroke")
            return False
