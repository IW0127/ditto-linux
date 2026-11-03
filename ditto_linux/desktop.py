"""Global shortcut management on GNOME: register, check, repair and resolve conflicts.

Wayland does not let applications grab global hotkeys, so the shortcut is a GNOME custom keybinding
that runs ``ditto-linux hotkey``. On every start Ditto checks it:

* never registered -> registered (unless the key is already used by something else);
* broken (command points at a launcher that no longer exists, or the old ``show``) -> repaired;
* changed by the user in GNOME Settings -> the new key is adopted;
* deleted after it was registered, or the key is also used by another shortcut, a clipboard
  extension or CopyQ -> reported, and the daemon asks the user in a popup what to do.
"""
from __future__ import annotations

import ast
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from .config import Settings

log = logging.getLogger(__name__)

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
KEY_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/ditto-linux/"
OUR_SCHEMA = f"{MEDIA_KEYS}.custom-keybinding:{KEY_PATH}"
BINDING = "<Control>grave"
BUILTIN_SCHEMAS = (
    "org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings", "org.gnome.mutter.keybindings",
    "org.gnome.mutter.wayland.keybindings", MEDIA_KEYS,
)
CLIPBOARD_EXTENSION_WORDS = ("clipboard", "copyq", "pano", "gpaste")
_ERRORS = (OSError, subprocess.SubprocessError, ValueError, SyntaxError)


def _gs(*args: str) -> str:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=5, check=True).stdout.strip()


def _parse_list(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("@as "):
        raw = raw[4:]
    if not raw:
        return []
    value = ast.literal_eval(raw)
    return [v for v in value] if isinstance(value, (list, tuple)) else [value]


def _custom_paths() -> list[str]:
    return _parse_list(_gs("get", MEDIA_KEYS, "custom-keybindings"))


# ---------------------------------------------------------------------- accelerators
_MOD_ALIASES = {"ctrl": "control", "primary": "control", "control": "control", "shift": "shift",
                "alt": "alt", "mod1": "alt", "super": "super", "meta": "super", "mod4": "super", "hyper": "hyper"}
_KEY_ALIASES = {"above_tab": "grave", "quoteleft": "grave", "`": "grave"}


def normalize(accel: str) -> str:
    """'<Primary>Above_Tab' and '<Ctrl>grave' both become '<control>grave' (order-independent)."""
    mods = sorted({_MOD_ALIASES.get(m.lower(), m.lower()) for m in re.findall(r"<([^>]+)>", accel)})
    key = re.sub(r"<[^>]+>", "", accel).strip().lower()
    key = _KEY_ALIASES.get(key, key)
    return "".join(f"<{m}>" for m in mods) + key


_PRETTY_MODS = {"control": "Ctrl", "shift": "Shift", "alt": "Alt", "super": "Super", "hyper": "Hyper"}
_PRETTY_KEYS = {"grave": "`", "space": "Space", "minus": "-", "equal": "=", "comma": ",", "period": ".",
                "slash": "/", "backslash": "\\", "semicolon": ";", "apostrophe": "'", "bracketleft": "[",
                "bracketright": "]"}


def pretty(accel: str) -> str:
    """'<Control>grave' -> 'Ctrl+`'."""
    if not accel:
        return "(none)"
    mods = [_PRETTY_MODS.get(_MOD_ALIASES.get(m.lower(), m.lower()), m) for m in re.findall(r"<([^>]+)>", accel)]
    key = re.sub(r"<[^>]+>", "", accel)
    low = _KEY_ALIASES.get(key.lower(), key.lower())
    key = _PRETTY_KEYS.get(low, key.upper() if len(key) == 1 else key)
    return "+".join(mods + [key])


def from_portable(text: str) -> str:
    """'Ctrl+Shift+V' / 'Ctrl+`' (Qt's QKeySequence text) -> '<Control><Shift>v' (GNOME syntax)."""
    text = text.strip()
    if text.startswith("<"):
        return text                     # already GNOME syntax
    parts = [p for p in re.split(r"\+(?!$)", text) if p]
    if not parts:
        raise ValueError("empty shortcut")
    mods = {"ctrl": "<Control>", "control": "<Control>", "shift": "<Shift>", "alt": "<Alt>",
            "meta": "<Super>", "super": "<Super>", "win": "<Super>"}
    out = ""
    for p in parts[:-1]:
        if p.lower() not in mods:
            raise ValueError(f"unknown modifier {p!r}")
        out += mods[p.lower()]
    if not out:
        raise ValueError("a global shortcut needs Ctrl, Alt or Super")
    key = parts[-1]
    key = {v: k for k, v in _PRETTY_KEYS.items()}.get(key, key)
    return out + (key.lower() if len(key) == 1 else key)


# ---------------------------------------------------------------------- detection
@dataclass
class Conflict:
    kind: str          # builtin | custom | extension | app
    description: str   # shown to the user
    schema: str = ""
    key: str = ""
    value: list[str] = field(default_factory=list)
    path: str = ""
    fixable: bool = True

    def to_dict(self) -> dict:
        return {"kind": self.kind, "description": self.description, "schema": self.schema, "key": self.key,
                "path": self.path, "fixable": self.fixable}


@dataclass
class Report:
    state: str                      # ok | missing | broken | conflict | unsupported
    binding: str = ""               # our accelerator (or the one we want when missing)
    command: str = ""
    conflicts: list[Conflict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"state": self.state, "binding": self.binding, "shortcut": pretty(self.binding),
                "command": self.command, "conflicts": [c.to_dict() for c in self.conflicts], "notes": self.notes}


def supported() -> bool:
    return "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() and bool(shutil.which("gsettings"))


def expected_command() -> str:
    exe = shutil.which("ditto-linux") or "/usr/bin/ditto-linux"
    return f"{exe} hotkey"


def command_ok(cmd: str) -> bool:
    parts = cmd.split()
    return len(parts) >= 2 and parts[-1] == "hotkey" and os.path.isfile(parts[0]) and os.access(parts[0], os.X_OK)


def find_conflicts(accel: str) -> list[Conflict]:
    want = normalize(accel)
    found: list[Conflict] = []
    for schema in BUILTIN_SCHEMAS:
        try:
            out = _gs("list-recursively", schema)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) < 3 or parts[1] == "custom-keybindings":
                continue
            raw = parts[2].strip()
            try:
                values = _parse_list(raw) if raw.startswith(("[", "@as")) else [ast.literal_eval(raw)]
            except (ValueError, SyntaxError):
                continue
            values = [v for v in values if isinstance(v, str)]
            if any(v and normalize(v) == want for v in values):
                found.append(Conflict("builtin", f"GNOME shortcut “{parts[1]}” ({parts[0]})",
                                      schema=parts[0], key=parts[1], value=values))
    try:
        paths = _custom_paths()
    except _ERRORS:
        paths = []
    for p in paths:
        if p == KEY_PATH:
            continue
        schema = f"{MEDIA_KEYS}.custom-keybinding:{p}"
        try:
            binding = _gs("get", schema, "binding").strip("'")
        except (OSError, subprocess.SubprocessError):
            continue
        if binding and normalize(binding) == want:
            try:
                name = _gs("get", schema, "name").strip("'") or p
            except (OSError, subprocess.SubprocessError):
                name = p
            found.append(Conflict("custom", f"custom shortcut “{name}”", schema=schema, key="binding",
                                  value=[binding], path=p))
    if want == normalize(BINDING):
        # other clipboard managers grab Ctrl+` themselves
        try:
            exts = _parse_list(_gs("get", "org.gnome.shell", "enabled-extensions"))
        except _ERRORS:
            exts = []
        for ext in exts:
            if any(w in ext.lower() for w in CLIPBOARD_EXTENSION_WORDS):
                found.append(Conflict("extension", f"GNOME extension “{ext}” (another clipboard manager)",
                                      schema="org.gnome.shell", key="enabled-extensions", value=[ext]))
        if shutil.which("pgrep"):
            r = subprocess.run(["pgrep", "-x", "copyq"], capture_output=True, text=True)
            if r.returncode == 0:
                found.append(Conflict("app", "CopyQ is running and may grab the same key (quit it)", fixable=False))
    return found


def check(settings: Settings | None = None) -> Report:
    if not supported():
        return Report("unsupported", notes=["not a GNOME session: bind `ditto-linux hotkey` in your desktop's "
                                            "keyboard settings"])
    accel = (settings.hotkey if settings else "") or BINDING
    try:
        paths = _custom_paths()
        binding = command = ""
        if KEY_PATH in paths:
            binding = _gs("get", OUR_SCHEMA, "binding").strip("'")
            command = _gs("get", OUR_SCHEMA, "command").strip("'")
    except _ERRORS as e:
        return Report("unsupported", notes=[f"cannot read GNOME shortcuts: {e}"])
    rep = Report("ok", binding=binding or accel, command=command, conflicts=find_conflicts(binding or accel))
    if not binding:
        rep.state = "missing"
    elif not command_ok(command):
        rep.state = "broken"
    elif any(c.fixable for c in rep.conflicts):
        rep.state = "conflict"
    elif rep.conflicts:
        rep.notes += [c.description for c in rep.conflicts]
    return rep


# ---------------------------------------------------------------------- changes
def install(accel: str = BINDING, command: str | None = None) -> None:
    command = command or expected_command()
    paths = _custom_paths()
    _gs("set", OUR_SCHEMA, "name", "Ditto")
    _gs("set", OUR_SCHEMA, "command", command)
    _gs("set", OUR_SCHEMA, "binding", accel)
    if KEY_PATH not in paths:
        _gs("set", MEDIA_KEYS, "custom-keybindings", str(paths + [KEY_PATH]))
    log.info("GNOME shortcut %s -> %s", pretty(accel), command)


def free(conflict: Conflict, accel: str) -> bool:
    """Remove ``accel`` from the conflicting binding. Returns False if it cannot be changed."""
    want = normalize(accel)
    if not conflict.fixable:
        return False
    if conflict.kind == "builtin":
        keep = [v for v in conflict.value if normalize(v) != want]
        _gs("set", conflict.schema, conflict.key, str(keep) if keep else "@as []")
    elif conflict.kind == "custom":
        _gs("set", conflict.schema, "binding", "")
    elif conflict.kind == "extension":
        exts = [e for e in _parse_list(_gs("get", "org.gnome.shell", "enabled-extensions")) if e not in conflict.value]
        _gs("set", "org.gnome.shell", "enabled-extensions", str(exts) if exts else "@as []")
    else:
        return False
    log.warning("freed %s from %s (was %s)", pretty(accel), conflict.description, conflict.value or conflict.path)
    return True


def resolve(settings: Settings, accel: str | None = None, take_over: bool = False) -> Report:
    """Make the shortcut work: repair our binding and, with ``take_over``, free the key from others.

    With ``accel`` the shortcut moves to that key (saved in the settings).
    """
    accel = accel or settings.hotkey or BINDING
    if take_over:
        for c in find_conflicts(accel):
            try:
                free(c, accel)
            except _ERRORS as e:
                log.warning("could not free %s: %s", c.description, e)
    install(accel)
    settings.hotkey = accel
    settings.gnome_shortcut_done = True
    try:
        settings.save()
    except OSError:
        pass
    return check(settings)


def ensure_gnome_shortcut(settings: Settings) -> str:
    """Startup check. Returns 'registered', 'exists', 'upgraded', 'adopted', 'taken', 'missing' or 'skipped'.

    'taken' (the key is used by something else) and 'missing' (deleted after registration) are left
    for the daemon to ask the user about.
    """
    if os.environ.get("DITTO_NO_DESKTOP_INTEGRATION") or not supported():
        return "skipped"
    try:
        rep = check(settings)
        if rep.state == "unsupported":
            return "skipped"
        fixable = [c for c in rep.conflicts if c.fixable]
        if rep.state == "missing":
            if settings.gnome_shortcut_done:
                log.warning("Ditto's GNOME shortcut was removed")
                return "missing"
            if fixable:
                log.warning("%s is already used by: %s", pretty(rep.binding),
                            "; ".join(c.description for c in fixable))
                return "taken"
            install(rep.binding)
            result = "registered"
        elif rep.state == "broken":
            log.warning("GNOME shortcut command %r is broken; repairing", rep.command)
            install(rep.binding)
            result = "upgraded"
        else:
            result = "exists"
        if normalize(rep.binding) != normalize(settings.hotkey or BINDING):
            log.info("shortcut changed in GNOME Settings to %s; adopting it", pretty(rep.binding))
            settings.hotkey = rep.binding
            result = "adopted" if result == "exists" else result
        if result in ("exists", "adopted") and fixable:
            log.warning("%s is also used by: %s", pretty(rep.binding), "; ".join(c.description for c in fixable))
            result = "taken"
    except _ERRORS as e:
        log.warning("could not check the GNOME shortcut: %s", e)
        return "skipped"
    settings.gnome_shortcut_done = True
    try:
        settings.save()
    except OSError:
        pass
    return result
