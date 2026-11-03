"""The background daemon: clipboard monitor + tray icon + Quick Paste window + IPC server."""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_NAME, __version__
from .clipboard import ClipboardMonitor
from .config import Settings, default_db_path, socket_path
from .db import ClipStore
from .ipc import is_running
from .paste import Paster
from .ui.icons import app_icon
from .ui.quickpaste import QuickPasteWindow
from .ui.settings_dialog import SettingsDialog

log = logging.getLogger(__name__)


def choose_platform() -> None:
    """Prefer XWayland on Wayland sessions.

    Wayland only lets the focused window read the clipboard, so a background clipboard manager
    cannot see copies through the native Wayland backend (GNOME/KDE have no data-control protocol).
    Through XWayland the compositor mirrors the clipboard and Qt is notified via XFixes.
    """
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


class DittoDaemon:
    def __init__(self, app: QApplication, db_path: str, settings: Settings, sock: str, show_tray: bool = True):
        self.app = app
        self.settings = settings
        self.store = ClipStore(db_path)
        self.monitor = ClipboardMonitor(self.store, settings)
        self.paster = Paster(settings)
        self.paster.start()
        self.window = QuickPasteWindow(self.store, settings=settings)
        self.window.setWindowIcon(app_icon())
        self.window.pasteRequested.connect(self.paste)
        self.window.copyRequested.connect(self.copy_only)
        self.window.settingsRequested.connect(self.open_settings)
        self.monitor.clipCaptured.connect(self._on_capture)
        self.tray = None
        if show_tray and settings.show_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._make_tray()

        self.sock_path = sock
        self.server = QLocalServer()
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(sock)  # stale socket from a crashed instance
        if not self.server.listen(sock):
            raise RuntimeError(f"cannot listen on {sock}: {self.server.errorString()}")
        self.server.newConnection.connect(self._on_connection)

        self._db_version = self.store.data_version()
        self._poll = QTimer(interval=1000)
        self._poll.timeout.connect(self._check_external_changes)
        self._poll.start()
        self._maint = QTimer(interval=60 * 60 * 1000)
        self._maint.timeout.connect(self._maintenance)
        self._maint.start()
        self._maintenance()
        log.info("ditto-linux %s running; db=%s socket=%s platform=%s", __version__, db_path, sock,
                 QApplication.platformName())

    # ------------------------------------------------------------------ tray
    def _make_tray(self):
        self.tray = QSystemTrayIcon(app_icon(), self.app)
        self.tray.setToolTip("Ditto")
        menu = QMenu()
        menu.addAction("Show Ditto", self.window.show_at_cursor)
        self.act_pause = QAction("Disconnect from Clipboard", menu, checkable=True)
        self.act_pause.toggled.connect(self.set_paused)
        menu.addAction(self.act_pause)
        menu.addSeparator()
        menu.addAction("Options…", self.open_settings)
        menu.addAction("Delete Clipboard History…", self._confirm_clear)
        menu.addSeparator()
        menu.addAction("Exit", self.quit)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.window.toggle() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def set_paused(self, paused: bool):
        self.monitor.paused = paused
        if self.tray:
            self.tray.setIcon(app_icon(paused))
            self.tray.setToolTip("Ditto (disconnected)" if paused else "Ditto")

    def _confirm_clear(self):
        if QMessageBox.question(None, "Ditto", "Delete all clips that are not in groups or protected?") \
                == QMessageBox.StandardButton.Yes:
            self.store.clear_history()
            self.window.refresh()

    def open_settings(self):
        dlg = SettingsDialog(self.settings, str(self.store.path), self.window if self.window.isVisible() else None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            dlg.apply().save()
            self._maintenance()
            self.paster.start()
            if self.settings.show_tray and not self.tray and QSystemTrayIcon.isSystemTrayAvailable():
                self._make_tray()
            elif self.tray:
                self.tray.setVisible(self.settings.show_tray)

    # ------------------------------------------------------------------ actions
    def _on_capture(self, _clip_id: int, _created: bool):
        self._db_version = self.store.data_version()
        if self.window.isVisible():
            self.window.refresh()

    def _check_external_changes(self):
        v = self.store.data_version()
        if v != self._db_version:
            self._db_version = v
            if self.window.isVisible():
                self.window.refresh()

    def _maintenance(self):
        try:
            removed = self.store.cleanup(self.settings.max_clips, self.settings.expire_days)
            if removed:
                log.info("auto-deleted %d old clips", removed)
        except Exception:
            log.exception("maintenance failed")

    def paste(self, clip_id: int, plain: bool = False, simulate: bool | None = None) -> bool:
        if not self.monitor.set_clip(clip_id, plain):
            return False
        self.store.mark_pasted(clip_id, self.settings.move_duplicate_to_top)
        self.window.hide()
        if self.settings.auto_paste if simulate is None else simulate:
            # give the window manager time to return focus to the previous window
            QTimer.singleShot(max(0, self.settings.paste_delay_ms),
                              lambda: threading.Thread(target=self.paster.paste, daemon=True).start())
        return True

    def copy_only(self, clip_id: int) -> bool:
        ok = self.monitor.set_clip(clip_id)
        if ok:
            self.store.mark_pasted(clip_id, move_to_top=False)
        return ok

    def quit(self):
        self.paster.stop()
        self.server.close()
        QLocalServer.removeServer(self.sock_path)
        self.store.close()
        self.app.quit()

    # ------------------------------------------------------------------ IPC
    def _on_connection(self):
        while self.server.hasPendingConnections():
            conn: QLocalSocket = self.server.nextPendingConnection()
            conn.setProperty("buf", b"")
            conn.readyRead.connect(lambda c=conn: self._on_ready(c))
            conn.disconnected.connect(conn.deleteLater)

    def _on_ready(self, conn: QLocalSocket):
        buf = conn.property("buf") + bytes(conn.readAll())
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                reply = self.handle(json.loads(line))
            except Exception as e:  # report errors to the client instead of crashing
                log.exception("IPC command failed")
                reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            conn.write((json.dumps(reply) + "\n").encode())
            conn.flush()
        conn.setProperty("buf", buf)

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True, "version": __version__, "pid": os.getpid()}
        if cmd == "status":
            return {"ok": True, "paused": self.monitor.paused, "platform": QApplication.platformName(),
                    "visible": self.window.isVisible(), **self.store.stats()}
        if cmd == "show":
            self.window.show_at_cursor()
            return {"ok": True}
        if cmd == "hotkey":
            # global Ctrl+`: open, or (Ctrl still held) move to the next clip and paste on release
            self.window.hotkey()
            return {"ok": True, "visible": self.window.isVisible()}
        if cmd == "toggle":
            self.window.toggle()
            return {"ok": True, "visible": self.window.isVisible()}
        if cmd == "hide":
            self.window.hide()
            return {"ok": True}
        if cmd in ("paste", "copy"):
            clip_id = int(req["id"])
            if self.store.get(clip_id) is None:
                return {"ok": False, "error": f"no clip with id {clip_id}"}
            # "copy" only places the clip on the clipboard; "paste" also sends Ctrl+V if enabled
            simulate = req.get("simulate") if cmd == "paste" else False
            ok = self.paste(clip_id, bool(req.get("plain")), simulate=simulate)
            return {"ok": ok} if ok else {"ok": False, "error": "clip has no data"}
        if cmd == "capture":
            clip_id = self.monitor.capture()
            return {"ok": True, "id": clip_id}
        if cmd == "clipboard":
            text = self.app.clipboard().text()
            return {"ok": True, "text": text}
        if cmd == "pause":
            self.set_paused(True)
            return {"ok": True}
        if cmd == "resume":
            self.set_paused(False)
            return {"ok": True}
        if cmd == "refresh":
            self._check_external_changes()
            return {"ok": True}
        if cmd == "reload-settings":
            new = Settings.load()
            self.settings.__dict__.update(new.__dict__)
            self.paster.start()
            return {"ok": True}
        if cmd == "quit":
            QTimer.singleShot(0, self.quit)
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def run_daemon(db_path: str | None = None, show_window: bool = False, tray: bool = True) -> int:
    choose_platform()
    sock = socket_path()
    if is_running(sock):
        # Launching again (e.g. from the app menu) opens the window of the running instance.
        from .ipc import request
        try:
            request("show", path=sock)
        except (ConnectionError, OSError, ValueError):
            pass
        print(f"{APP_NAME} is already running; showing its window.", file=sys.stderr)
        return 1
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("Ditto")
    app.setDesktopFileName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())
    settings = Settings.load()
    from .desktop import ensure_gnome_shortcut
    ensure_gnome_shortcut(settings)
    daemon = DittoDaemon(app, str(db_path or default_db_path()), settings, sock, show_tray=tray)
    # Ctrl+C / SIGTERM: Python only handles signals while the interpreter runs, so wake it periodically.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: QTimer.singleShot(0, daemon.quit))
    tick = QTimer(interval=250)
    tick.timeout.connect(lambda: None)
    tick.start()
    if show_window:
        daemon.window.show_at_cursor()
    return app.exec()
