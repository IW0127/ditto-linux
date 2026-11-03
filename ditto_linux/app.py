"""The background daemon: clipboard monitor + tray icon + Quick Paste window + IPC server."""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QDesktopServices
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_NAME, __version__
from . import desktop, report, transforms, update
from . import formats as fmt
from .logs import install_qt_message_handler, log_path
from .clipboard import ClipboardMonitor
from .config import Settings, default_db_path, socket_path
from .db import ClipStore
from .ipc import is_running
from .paste import Paster
from .ui.dialogs import ReportDialog, ShortcutDialog, UpdateDialog, exec_front
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


class _Worker(QObject):
    """Runs blocking work (network, pkexec) in a thread and reports back on the Qt thread."""
    done = pyqtSignal(object, object)   # callback, result-or-exception

    def __init__(self):
        super().__init__()
        self.done.connect(lambda cb, result: cb(result))

    def run(self, fn, callback):
        def target():
            try:
                result = fn()
            except Exception as e:  # handed to the callback
                result = e
            self.done.emit(callback, result)
        threading.Thread(target=target, daemon=True, name=getattr(fn, "__name__", "worker")).start()


UPDATE_INTERVAL_S = 24 * 3600


class _ProblemBridge(QObject):
    """Moves problems reported from any thread (logging) onto the Qt thread."""
    problem = pyqtSignal(object)


class DittoDaemon:
    def __init__(self, app: QApplication, db_path: str, settings: Settings, sock: str, show_tray: bool = True,
                 argv: list[str] | None = None):
        self.app = app
        self.argv = list(argv if argv is not None else sys.argv[1:])
        self.worker = _Worker()
        self._update_busy = False
        self._report_busy = False
        self._bridge = _ProblemBridge()
        self._bridge.problem.connect(self.on_problem)
        self.problem_handler = report.ProblemHandler(self._bridge.problem.emit)
        logging.getLogger().addHandler(self.problem_handler)
        self.settings = settings
        self.store = ClipStore(db_path)
        self.monitor = ClipboardMonitor(self.store, settings)
        self.paster = Paster(settings)
        self.paster.start()
        self.window = QuickPasteWindow(self.store, settings=settings)
        self.window.setWindowIcon(app_icon())
        self.window.pasteRequested.connect(self.paste)
        self.window.pasteManyRequested.connect(lambda ids, plain, tr: self.paste_many(ids, plain, tr))
        self.window.copyRequested.connect(self.copy_only)
        self.window.settingsRequested.connect(self.open_settings)
        self.window.updateRequested.connect(lambda: self.check_updates(manual=True))
        self.window.shortcutRequested.connect(lambda: self.ask_shortcut(manual=True))
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
        self._update_timer = QTimer(interval=60 * 60 * 1000)
        self._update_timer.timeout.connect(self.check_updates)
        self._update_timer.start()
        QTimer.singleShot(int(os.environ.get("DITTO_UPDATE_FIRST_CHECK_MS", "60000")), self.check_updates)
        dump = report.take_crash_dump()
        if dump:
            log.error("Ditto crashed last time:\n%s", dump)
            QTimer.singleShot(3000, lambda: self.on_problem(report.from_crash_dump(dump)))
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
        menu.addAction("Check Shortcut…", lambda: self.ask_shortcut(manual=True))
        menu.addAction("Check for Updates…", lambda: self.check_updates(manual=True))
        menu.addAction("Open Log File", self.open_log)
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
        dlg = SettingsDialog(self.settings, str(self.store.path), self.window if self.window.isVisible() else None,
                             on_shortcut=lambda: self.ask_shortcut(manual=True),
                             on_update=lambda: self.check_updates(manual=True), on_log=self.open_log,
                             recent_apps=self.store.recent_sources())
        if exec_front(dlg) == QDialog.DialogCode.Accepted:
            dlg.apply().save()
            log.info("settings saved")
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

    # ------------------------------------------------------------------ shortcut
    def ask_shortcut(self, manual: bool = False, report: desktop.Report | None = None):
        """Check the global shortcut and, if something is wrong, ask the user how to fix it."""
        if not manual and not self.settings.check_shortcut:
            return
        report = report or desktop.check(self.settings)
        log.info("shortcut check: %s %s%s", report.state, desktop.pretty(report.binding),
                 "".join(f"; conflict: {c.description}" for c in report.conflicts))
        if report.state == "unsupported":
            if manual:
                QMessageBox.information(None, "Ditto shortcut", "\n".join(report.notes))
            return
        if report.state == "ok":
            if manual:
                extra = ("\n\nNote: " + "; ".join(report.notes)) if report.notes else ""
                QMessageBox.information(None, "Ditto shortcut",
                                        f"{desktop.pretty(report.binding)} opens Ditto. Everything is fine.{extra}")
            return
        dlg = ShortcutDialog(report)
        exec_front(dlg)
        if dlg.dont_check:
            self.settings.check_shortcut = False
            self.settings.save()
        try:
            if dlg.choice == "take_over":
                new = desktop.resolve(self.settings, report.binding, take_over=True)
            elif dlg.choice == "use_key":
                new = desktop.resolve(self.settings, dlg.key, take_over=False)
                if new.state == "conflict":
                    self.ask_shortcut(manual=True, report=new)
                    return
            else:
                log.info("shortcut problem left as is (%s)", report.state)
                return
        except (OSError, ValueError) as e:
            log.exception("could not change the shortcut")
            QMessageBox.warning(None, "Ditto shortcut", f"Could not change the shortcut: {e}")
            return
        log.info("shortcut after fix: %s %s", new.state, desktop.pretty(new.binding))

    # ------------------------------------------------------------------ updates
    def check_updates(self, manual: bool = False):
        if self._update_busy:
            return
        if not manual:
            if (not self.settings.auto_update_check or os.environ.get("DITTO_NO_UPDATE_CHECK")
                    or time.time() - self.settings.last_update_check < UPDATE_INTERVAL_S):
                return
        self._update_busy = True
        self.worker.run(update.latest, lambda result: self._on_update_checked(result, manual))

    def _on_update_checked(self, result, manual: bool):
        self._update_busy = False
        if isinstance(result, Exception):
            log.warning("%s", result)
            if manual:
                QMessageBox.warning(None, "Ditto update", str(result))
            return
        self.settings.last_update_check = time.time()
        self.settings.save()
        log.info("update check: latest %s, running %s", result.version, __version__)
        if not update.is_newer(result.version):
            if manual:
                QMessageBox.information(None, "Ditto update", f"Ditto {__version__} is up to date.")
            return
        if not manual and result.version == self.settings.skipped_version:
            log.info("update %s skipped by the user", result.version)
            return
        kind = update.install_kind()
        dlg = UpdateDialog(result, kind)
        exec_front(dlg)
        log.info("update %s: user chose %s", result.version, dlg.choice)
        if dlg.choice == "skip":
            self.settings.skipped_version = result.version
            self.settings.save()
        elif dlg.choice == "open":
            QDesktopServices.openUrl(QUrl(result.url))
        elif dlg.choice == "install":
            self.install_update(result, kind)

    def install_update(self, release, kind: str):
        self._update_busy = True
        if self.tray:
            self.tray.showMessage("Ditto", f"Downloading Ditto {release.version}…")

        def work():
            path = update.download(release, kind)
            update.install(path, kind, gui=True)
            return release.version
        self.worker.run(work, self._on_update_installed)

    def _on_update_installed(self, result):
        self._update_busy = False
        if isinstance(result, Exception):
            log.error("update failed: %s", result)
            QMessageBox.warning(None, "Ditto update", f"The update failed:\n{result}\n\nLog: {log_path()}")
            return
        log.info("updated to %s; restarting", result)
        self.restart(after_update=True)

    def restart(self, after_update: bool = False):
        """Replace this process with a fresh daemon (same PID, so service managers keep tracking it)."""
        argv = update.restart_argv(self.argv) if after_update else update.self_argv(self.argv)
        log.info("restarting: %s %s", argv[0], " ".join(self.argv))
        self.close()
        logging.shutdown()
        os.execv(argv[0], argv)

    # ------------------------------------------------------------------ problem reports
    def report_mode(self) -> str:
        if os.environ.get("DITTO_NO_REPORT"):
            return "never"
        return self.settings.auto_report if self.settings.auto_report in ("ask", "always", "never") else "ask"

    def on_problem(self, problem: report.Problem):
        """An error happened: file a GitHub issue (always), ask first (ask) or only log it (never)."""
        mode = self.report_mode()
        log.warning("problem %s: %s (report mode: %s)", problem.fingerprint, problem.title, mode)
        if mode == "never" or self._report_busy:
            return
        if not report.should_send(problem):
            log.info("problem %s was reported recently; not reporting again yet", problem.fingerprint)
            return
        if mode == "always":
            self._submit_problem(problem, fallback_browser=False)
            return
        self._report_busy = True
        try:
            can_submit = report.gh_ready()
            dlg = ReportDialog(problem, report.sanitize(problem.details), can_submit)
            exec_front(dlg)
        finally:
            self._report_busy = False
        log.info("problem %s: user chose %s", problem.fingerprint, dlg.choice)
        if dlg.choice in ("always", "never"):
            self.settings.auto_report = dlg.choice
            self.settings.save()
        if dlg.choice in ("report", "always"):
            if can_submit:
                self._submit_problem(problem, fallback_browser=True)
            else:
                QDesktopServices.openUrl(QUrl(report.new_issue_url(problem)))

    def _submit_problem(self, problem: report.Problem, fallback_browser: bool):
        self._report_busy = True

        def work():
            if not report.gh_ready():
                raise report.ReportError("the GitHub CLI (gh) is not installed or not logged in")
            return report.submit(problem)

        def done(result):
            self._report_busy = False
            if isinstance(result, Exception):
                log.warning("could not report problem %s: %s", problem.fingerprint, result)
                if fallback_browser:
                    QDesktopServices.openUrl(QUrl(report.new_issue_url(problem)))
                return
            if self.tray:
                self.tray.showMessage("Ditto", f"Problem reported: {result}")
        self.worker.run(work, done)

    def open_log(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path())))

    def paste(self, clip_id: int, plain: bool = False, simulate: bool | None = None) -> bool:
        log.info("paste clip %s (plain=%s)", clip_id, plain)
        if not self.monitor.set_clip(clip_id, plain):
            log.warning("clip %s has no data to paste", clip_id)
            return False
        self.store.mark_pasted(clip_id, self.settings.move_duplicate_to_top)
        self.window.hide()
        if self.settings.auto_paste if simulate is None else simulate:
            # give the window manager time to return focus to the previous window
            QTimer.singleShot(max(0, self.settings.paste_delay_ms),
                              lambda: self.worker.run(self.paster.paste, self._on_pasted))
        return True

    def paste_many(self, ids: list[int], plain: bool = False, transform: str = "",
                   simulate: bool | None = None) -> bool:
        """Paste several clips as one text (Ditto's multi-paste), optionally transformed (Special Paste)."""
        ids = [int(i) for i in ids]
        if len(ids) == 1 and not transform:
            return self.paste(ids[0], plain, simulate)
        texts = []
        for cid in ids:
            data = self.store.get_formats(cid)
            if fmt.TEXT in data:
                texts.append(data[fmt.TEXT].decode("utf-8", errors="replace"))
            elif fmt.URIS in data:
                texts.append("\n".join(u for u in data[fmt.URIS].decode(errors="replace").split()))
        if not texts:
            log.warning("paste of clips %s: none of them has text", ids)
            return False
        sep = transforms.SEPARATORS.get(self.settings.multi_paste_separator, "\n")
        text = transforms.apply(transform, sep.join(texts))
        log.info("paste %d clip(s) %s%s", len(texts), ids, f" as {transform}" if transform else "")
        self.monitor.set_formats({fmt.TEXT: text.encode("utf-8")}, plain_text=True)
        for cid in ids:
            self.store.mark_pasted(cid, move_to_top=False)
        self.window.hide()
        if self.settings.auto_paste if simulate is None else simulate:
            QTimer.singleShot(max(0, self.settings.paste_delay_ms),
                              lambda: self.worker.run(self.paster.paste, self._on_pasted))
        return True

    def _on_pasted(self, ok):
        if ok is True:
            return
        log.warning("auto-paste did not work; the clip is on the clipboard (press Ctrl+V)")
        if not getattr(self, "_paste_warned", False):
            self._paste_warned = True
            problems = [m for good, m in self.paster.diagnose() if not good]
            msg = "Auto-paste did not work, the clip is on the clipboard: press Ctrl+V.\n" + (
                problems[0] if problems else "Run 'ditto-linux doctor' for details.")
            if self.tray:
                self.tray.showMessage("Ditto", msg, QSystemTrayIcon.MessageIcon.Warning, 10000)
            else:
                QMessageBox.warning(None, "Ditto", msg)

    def copy_only(self, clip_id: int) -> bool:
        ok = self.monitor.set_clip(clip_id)
        if ok:
            self.store.mark_pasted(clip_id, move_to_top=False)
        return ok

    def close(self):
        """Stop timers, helpers and the IPC server and close the database."""
        logging.getLogger().removeHandler(self.problem_handler)
        for t in (self._poll, self._maint, self._update_timer):
            t.stop()
        self.window._search_timer.stop()
        self.window.hide()
        self.paster.stop()
        self.server.close()
        QLocalServer.removeServer(self.sock_path)
        self.store.close()

    def quit(self):
        log.info("ditto-linux %s stopping", __version__)
        self.close()
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
        if cmd not in ("ping", "status", "clipboard"):
            log.debug("ipc: %s", cmd)
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
        if cmd in ("paste", "copy") and (req.get("ids") or req.get("transform")):
            ids = [int(i) for i in (req.get("ids") or [req["id"]])]
            missing = [i for i in ids if self.store.get(i) is None]
            if missing:
                return {"ok": False, "error": f"no clip with id {missing[0]}"}
            try:
                ok = self.paste_many(ids, bool(req.get("plain")), req.get("transform") or "",
                                     simulate=req.get("simulate") if cmd == "paste" else False)
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": ok} if ok else {"ok": False, "error": "the clips have no text"}
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
        if cmd == "restart":
            after_update = bool(req.get("update"))
            QTimer.singleShot(100, lambda: self.restart(after_update=after_update))
            return {"ok": True}
        if cmd == "check-shortcut":
            QTimer.singleShot(0, lambda: self.ask_shortcut(manual=True))
            return {"ok": True}
        if cmd == "check-updates":
            QTimer.singleShot(0, lambda: self.check_updates(manual=True))
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
    install_qt_message_handler()
    settings = Settings.load()
    shortcut = desktop.ensure_gnome_shortcut(settings)
    log.info("starting ditto-linux %s (%s install, python %s, log %s); shortcut %s: %s", __version__,
             update.install_kind(), sys.version.split()[0], log_path(), desktop.pretty(settings.hotkey), shortcut)
    daemon = DittoDaemon(app, str(db_path or default_db_path()), settings, sock, show_tray=tray)
    if shortcut in ("taken", "missing"):
        QTimer.singleShot(1500, daemon.ask_shortcut)
    # Ctrl+C / SIGTERM: Python only handles signals while the interpreter runs, so wake it periodically.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: QTimer.singleShot(0, daemon.quit))
    tick = QTimer(interval=250)
    tick.timeout.connect(lambda: None)
    tick.start()
    if show_window:
        daemon.window.show_at_cursor()
    return app.exec()
