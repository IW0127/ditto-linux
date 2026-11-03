"""Paths and persistent settings (XDG compliant)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import APP_NAME


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME


def default_db_path() -> Path:
    return Path(os.environ.get("DITTO_DB") or data_dir() / "Ditto.db")


def socket_path() -> str:
    """Unix socket used for single-instance control (``ditto-linux show`` etc.)."""
    override = os.environ.get("DITTO_SOCKET")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP_NAME}-{os.getuid()}"
    os.makedirs(runtime, mode=0o700, exist_ok=True)
    return os.path.join(runtime, f"{APP_NAME}.sock")


@dataclass
class Settings:
    max_clips: int = 500            # 0 = unlimited (Ditto: "Maximum number of saved copies")
    expire_days: int = 0            # 0 = never (Ditto: "Automatically delete copies after N days")
    max_clip_bytes: int = 50 * 1024 * 1024
    save_images: bool = True
    save_html: bool = True
    auto_paste: bool = True         # simulate Ctrl+V into the previous window after choosing a clip
    paste_tool: str = "auto"        # auto | wtype | xdotool | ydotool | none
    paste_shortcut: str = "ctrl+v"  # ctrl+v | ctrl+shift+v (terminals) | shift+insert
    paste_delay_ms: int = 250       # wait for focus to return to the target window
    hide_on_focus_loss: bool = True # Ditto hides Quick Paste when it loses focus (Ctrl+Space toggles)
    move_duplicate_to_top: bool = True
    show_tray: bool = True
    lines_per_row: int = 1
    ignore_apps: str = ""           # reserved, comma separated
    gnome_shortcut_done: bool = False  # first-run Ctrl+` registration already attempted

    @classmethod
    def path(cls) -> Path:
        return Path(os.environ.get("DITTO_CONFIG") or config_dir() / "settings.json")

    @classmethod
    def load(cls) -> "Settings":
        p = cls.path()
        s = cls()
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError):
            return s
        known = {f.name: f.type for f in fields(cls)}
        for key, value in raw.items():
            if key in known:
                default = getattr(s, key)
                try:
                    setattr(s, key, type(default)(value))
                except (TypeError, ValueError):
                    pass
        return s

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(p)
