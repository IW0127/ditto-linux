"""Clipboard monitoring and writing through Qt (X11 / XWayland via xcb, or native Wayland)."""
from __future__ import annotations

import fnmatch
import logging
import os
import time

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QClipboard, QGuiApplication, QImage

from . import formats as fmt
from .config import Settings
from .db import ClipStore

log = logging.getLogger(__name__)

# Hints set by password managers (KeePassXC, KDE) asking clipboard managers not to record.
SECRET_HINTS = ("x-kde-passwordManagerHint", "application/x-nspasswordmanager")


def mime_to_formats(md: QMimeData, settings: Settings) -> dict[str, bytes]:
    if md is None:
        return {}
    for hint in SECRET_HINTS:
        if md.hasFormat(hint) and bytes(md.data(hint)).strip() == b"secret":
            return {}
    out: dict[str, bytes] = {}
    if md.hasText() and md.text():
        out[fmt.TEXT] = md.text().encode("utf-8")
    if settings.save_html and md.hasHtml() and md.html():
        out[fmt.HTML] = md.html().encode("utf-8")
    if md.hasUrls():
        urls = [u.toString() for u in md.urls() if u.isValid()]
        if urls:
            out[fmt.URIS] = ("\r\n".join(urls) + "\r\n").encode()
    if settings.save_images and md.hasImage():
        img = md.imageData()
        if isinstance(img, QImage) and not img.isNull():
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            buf.close()
            out[fmt.PNG] = bytes(ba)
    total = sum(len(v) for v in out.values())
    if total > settings.max_clip_bytes:
        log.info("skipping clip of %d bytes (limit %d)", total, settings.max_clip_bytes)
        return {}
    return out


def ignore_patterns(setting: str) -> list[str]:
    return [p.strip().lower() for p in setting.replace("\n", ",").split(",") if p.strip()]


def ignored_by(owner: dict | None, patterns: list[str]) -> str | None:
    """The pattern from the 'ignore apps' list that matches the clipboard owner, or None."""
    if not owner or not patterns:
        return None
    names = [n.lower() for n in owner.get("names", []) + owner.get("wm_class", []) if n]
    for pat in patterns:
        for n in names:
            if fnmatch.fnmatchcase(n, pat) or n == pat:
                return pat
    return None


def source_name(owner: dict | None) -> str:
    if not owner:
        return ""
    names = owner.get("names") or owner.get("wm_class") or []
    return names[0] if names else ""


def formats_to_mime(formats: dict[str, bytes], plain_text: bool = False) -> QMimeData:
    md = QMimeData()
    if fmt.TEXT in formats:
        md.setText(formats[fmt.TEXT].decode("utf-8", errors="replace"))
    if plain_text:
        return md
    if fmt.HTML in formats:
        md.setHtml(formats[fmt.HTML].decode("utf-8", errors="replace"))
    if fmt.URIS in formats:
        urls = [QUrl(u) for u in formats[fmt.URIS].decode(errors="replace").split() if u and not u.startswith("#")]
        md.setUrls(urls)
        if fmt.TEXT not in formats:
            md.setText("\n".join(u.toString() for u in urls))
    for key in (fmt.PNG, fmt.BMP):
        if key in formats:
            img = QImage.fromData(formats[key])
            if not img.isNull():
                md.setImageData(img)
                break
    return md


class ClipboardMonitor(QObject):
    clipCaptured = pyqtSignal(int, bool)  # clip id, newly created

    def __init__(self, store: ClipStore, settings: Settings, parent=None, owner_lookup=None):
        super().__init__(parent)
        self.store = store
        self.settings = settings
        if owner_lookup is None and QGuiApplication.platformName() == "xcb":
            from .ui.x11 import clipboard_owner as owner_lookup
        self.owner_lookup = owner_lookup or (lambda: None)
        self.paused = False
        self.clipboard: QClipboard = QGuiApplication.clipboard()
        self._own: tuple[float, dict[str, bytes]] | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)  # coalesce bursts (apps often set formats one by one)
        self._timer.timeout.connect(self.capture)
        self.clipboard.dataChanged.connect(self._timer.start)

    def capture(self) -> int | None:
        if self.paused:
            return None
        formats = mime_to_formats(self.clipboard.mimeData(QClipboard.Mode.Clipboard), self.settings)
        if not formats:
            return None
        if self._own and time.monotonic() - self._own[0] < 3 and self._is_own(formats):
            return None  # our own write (paste from history) must not be re-recorded
        owner = self.owner_lookup()
        if owner and owner.get("pid") == os.getpid():
            return None
        pattern = ignored_by(owner, ignore_patterns(self.settings.ignore_apps))
        if pattern:
            log.info("not saving a copy from %s (ignored app '%s')", source_name(owner), pattern)
            return None
        try:
            clip_id, created = self.store.add_clip(formats, self.settings.move_duplicate_to_top,
                                                   source=source_name(owner))
        except Exception:  # never let a bad clip kill the daemon
            log.exception("failed to save clip")
            return None
        if created and (self.settings.max_clips or self.settings.expire_days):
            self.store.cleanup(self.settings.max_clips, self.settings.expire_days)
        log.debug("captured clip %s (new=%s)", clip_id, created)
        self.clipCaptured.emit(clip_id, created)
        return clip_id

    def set_formats(self, formats: dict[str, bytes], plain_text: bool = False) -> None:
        """Put arbitrary data on the clipboard without recording it as a new clip."""
        self._own = (time.monotonic(), formats)
        self.clipboard.setMimeData(formats_to_mime(formats, plain_text), QClipboard.Mode.Clipboard)

    def set_clip(self, clip_id: int, plain_text: bool = False) -> bool:
        formats = self.store.get_formats(clip_id)
        if not formats:
            return False
        self._own = (time.monotonic(), formats)
        self.clipboard.setMimeData(formats_to_mime(formats, plain_text), QClipboard.Mode.Clipboard)
        return True

    def _is_own(self, captured: dict[str, bytes]) -> bool:
        own = self._own[1]
        key = fmt.TEXT if fmt.TEXT in captured else next(iter(captured))
        if key in (fmt.PNG, fmt.BMP):
            return fmt.PNG in own or fmt.BMP in own
        return own.get(key) == captured.get(key)
