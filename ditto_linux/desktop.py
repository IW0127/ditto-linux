"""One-time desktop integration: register the Ctrl+` shortcut on GNOME.

Wayland does not let applications grab global hotkeys, so the shortcut is registered as a GNOME
custom keybinding that runs ``ditto-linux hotkey``. This runs once per user (first daemon start) and
never overrides a binding the user already has on that key.
"""
from __future__ import annotations

import ast
import logging
import os
import shutil
import subprocess

from .config import Settings

log = logging.getLogger(__name__)

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
KEY_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/ditto-linux/"
BINDING = "<Control>grave"


def _gs(*args: str) -> str:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=5, check=True).stdout.strip()


def _custom_paths() -> list[str]:
    raw = _gs("get", MEDIA_KEYS, "custom-keybindings").replace("@as ", "")
    return list(ast.literal_eval(raw))


def _upgrade_command() -> bool:
    """Older versions bound the key to ``ditto-linux show``; ``hotkey`` adds Ctrl+` cycling."""
    schema = f"{MEDIA_KEYS}.custom-keybinding:{KEY_PATH}"
    cmd = _gs("get", schema, "command").strip("'")
    if cmd.endswith(" show") and "ditto-linux" in cmd:
        _gs("set", schema, "command", cmd[: -len("show")] + "hotkey")
        log.info("updated GNOME shortcut command to %shotkey", cmd[: -len("show")])
        return True
    return False


def ensure_gnome_shortcut(settings: Settings) -> str:
    """Returns what happened: 'registered', 'exists', 'upgraded', 'taken', 'skipped'."""
    if os.environ.get("DITTO_NO_DESKTOP_INTEGRATION"):
        return "skipped"
    if "GNOME" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() or not shutil.which("gsettings"):
        return "skipped"
    if settings.gnome_shortcut_done:
        try:
            return "upgraded" if KEY_PATH in _custom_paths() and _upgrade_command() else "skipped"
        except (OSError, subprocess.SubprocessError, ValueError, SyntaxError):
            return "skipped"
    result = "skipped"
    try:
        paths = _custom_paths()
        if KEY_PATH in paths:
            result = "upgraded" if _upgrade_command() else "exists"
        else:
            for p in paths:
                if _gs("get", f"{MEDIA_KEYS}.custom-keybinding:{p}", "binding").strip("'") == BINDING:
                    result = "taken"
                    break
            else:
                exe = shutil.which("ditto-linux") or "ditto-linux"
                schema = f"{MEDIA_KEYS}.custom-keybinding:{KEY_PATH}"
                _gs("set", schema, "name", "Ditto")
                _gs("set", schema, "command", f"{exe} hotkey")
                _gs("set", schema, "binding", BINDING)
                _gs("set", MEDIA_KEYS, "custom-keybindings", str(paths + [KEY_PATH]))
                result = "registered"
                log.info("registered GNOME shortcut Ctrl+` -> %s hotkey", exe)
    except (OSError, subprocess.SubprocessError, ValueError, SyntaxError) as e:
        log.warning("could not register the GNOME shortcut: %s", e)
        return "skipped"
    settings.gnome_shortcut_done = True
    try:
        settings.save()
    except OSError:
        pass
    return result
