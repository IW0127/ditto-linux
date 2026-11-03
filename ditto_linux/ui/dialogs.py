"""Popups shown by the daemon: shortcut conflicts and available updates."""
from __future__ import annotations

import html

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QGuiApplication, QKeySequence
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QKeySequenceEdit, QLabel, QPushButton, QTextBrowser, QVBoxLayout,
)

from .. import __version__
from .. import desktop


def exec_front(dlg: QDialog) -> int:
    """exec() a dialog from the background daemon so it opens in front (GNOME focus-stealing prevention)."""
    if QGuiApplication.platformName() == "xcb":
        from .x11 import activate_window
        for delay in (50, 250):
            QTimer.singleShot(delay, lambda: dlg.isVisible() and activate_window(int(dlg.winId())))
    dlg.raise_()
    dlg.activateWindow()
    return dlg.exec()


class ShortcutDialog(QDialog):
    """Explains what is wrong with the global shortcut and offers to fix it.

    After exec(): ``choice`` is 'take_over', 'use_key' (with ``key`` in GNOME syntax) or 'later';
    ``dont_check`` is True when the user asked not to be asked again at startup.
    """

    def __init__(self, report: desktop.Report, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ditto shortcut")
        self.setObjectName("ShortcutDialog")
        self.choice = "later"
        self.key = report.binding
        self.dont_check = False
        name = desktop.pretty(report.binding)
        fixable = [c for c in report.conflicts if c.fixable]
        others = [c for c in report.conflicts if not c.fixable]
        if report.state == "missing" and not fixable:
            text = f"<p>Ditto's global shortcut <b>{html.escape(name)}</b> is not set up.</p>"
        elif report.state == "broken":
            text = (f"<p>Ditto's shortcut <b>{html.escape(name)}</b> runs a command that no longer works:"
                    f"<br><code>{html.escape(report.command)}</code></p>")
        else:
            text = f"<p><b>{html.escape(name)}</b> is also used by:</p>"
        if report.conflicts:
            text += "<ul>" + "".join(f"<li>{html.escape(c.description)}</li>" for c in report.conflicts) + "</ul>"
        if fixable:
            text += (f"<p>Ditto can take <b>{html.escape(name)}</b> over (the other shortcut is removed), or you can "
                     "choose a different key for Ditto.</p>")
        elif others:
            text += "<p>Ditto cannot change that itself.</p>"
        self.message = QLabel(text, wordWrap=True, objectName="shortcutMessage")
        self.message.setMinimumWidth(420)

        self.take_btn = QPushButton(f"Use {name} for Ditto" if fixable else f"Fix {name}", objectName="takeOver")
        self.take_btn.setDefault(True)
        self.take_btn.clicked.connect(lambda: self._done("take_over"))
        self.key_edit = QKeySequenceEdit(QKeySequence(name if name != "(none)" else ""), objectName="keyEdit")
        if hasattr(self.key_edit, "setMaximumSequenceLength"):   # Qt >= 6.5
            self.key_edit.setMaximumSequenceLength(1)
        self.key_btn = QPushButton("Use this key", objectName="useKey")
        self.key_btn.clicked.connect(self._use_key)
        self.later_btn = QPushButton("Not now", objectName="later")
        self.later_btn.clicked.connect(lambda: self._done("later"))
        self.dont = QCheckBox("Don't check the shortcut when Ditto starts", objectName="dontCheck")
        self.error = QLabel("", objectName="keyError", styleSheet="color: #c62828;")

        keyrow = QHBoxLayout()
        keyrow.addWidget(QLabel("Other key:"))
        keyrow.addWidget(self.key_edit, 1)
        keyrow.addWidget(self.key_btn)
        buttons = QHBoxLayout()
        buttons.addWidget(self.dont)
        buttons.addStretch(1)
        buttons.addWidget(self.later_btn)
        buttons.addWidget(self.take_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.message)
        lay.addLayout(keyrow)
        lay.addWidget(self.error)
        lay.addLayout(buttons)

    def _use_key(self):
        text = self.key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        try:
            self.key = desktop.from_portable(text)
        except ValueError as e:
            self.error.setText(f"{e}." if text else "Press the new key combination first.")
            return
        self._done("use_key")

    def _done(self, choice: str):
        self.choice = choice
        self.dont_check = self.dont.isChecked()
        self.accept()


class ReportDialog(QDialog):
    """'Ditto hit a problem'. After exec(): ``choice`` is 'report', 'always', 'never' or 'later'."""

    def __init__(self, problem, details: str, can_submit: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ditto problem")
        self.setObjectName("ReportDialog")
        self.resize(560, 420)
        self.choice = "later"
        how = ("It will be filed as an issue on GitHub with your GitHub account."
               if can_submit else "The GitHub issue page opens pre-filled; press Submit there.")
        intro = QLabel(f"<p><b>Ditto hit a problem:</b> {html.escape(problem.title)}</p>"
                       f"<p>Report it so it can be fixed? {how} The report contains the error, system "
                       "information and recent log lines (no clipboard contents; your user name and home "
                       "folder are hidden).</p>", wordWrap=True)
        text = QTextBrowser(objectName="details")
        text.setPlainText(details)
        self.report_btn = QPushButton("Report", objectName="report")
        self.report_btn.setDefault(True)
        self.report_btn.clicked.connect(lambda: self._done("report"))
        always = QPushButton("Always report", objectName="always")
        always.clicked.connect(lambda: self._done("always"))
        never = QPushButton("Never report", objectName="never")
        never.clicked.connect(lambda: self._done("never"))
        later = QPushButton("Not now", objectName="later")
        later.clicked.connect(lambda: self._done("later"))
        row = QHBoxLayout()
        row.addWidget(never)
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(always)
        row.addWidget(self.report_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(text, 1)
        lay.addLayout(row)

    def _done(self, choice: str):
        self.choice = choice
        self.accept()


class UpdateDialog(QDialog):
    """'Ditto X is available'. After exec(): ``choice`` is 'install', 'open', 'skip' or 'later'."""

    def __init__(self, release, kind: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ditto update")
        self.setObjectName("UpdateDialog")
        self.resize(520, 420)
        self.choice = "later"
        title = QLabel(f"<h3>Ditto {html.escape(release.version)} is available</h3>"
                       f"<p>You have {html.escape(__version__)}.</p>", wordWrap=True)
        notes = QTextBrowser(objectName="notes")
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(release.notes or "")
        self.install_btn = QPushButton("Install and restart" if kind != "git" else "Open download page",
                                       objectName="install")
        self.install_btn.setDefault(True)
        self.install_btn.clicked.connect(lambda: self._done("install" if kind != "git" else "open"))
        skip = QPushButton("Skip this version", objectName="skip")
        skip.clicked.connect(lambda: self._done("skip"))
        later = QPushButton("Later", objectName="later")
        later.clicked.connect(lambda: self._done("later"))
        row = QHBoxLayout()
        row.addWidget(skip)
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(self.install_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(notes, 1)
        if kind == "git":
            lay.addWidget(QLabel("This copy runs from a git checkout: update it with <code>git pull</code>."))
        lay.addLayout(row)

    def _done(self, choice: str):
        self.choice = choice
        self.accept()
