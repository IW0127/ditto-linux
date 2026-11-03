"""Options dialog (subset of Ditto's Options sheet that makes sense on Linux)."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QVBoxLayout,
)

from .. import __version__
from ..config import Settings
from ..desktop import pretty
from ..logs import log_path


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, db_path: str, parent=None, on_shortcut=None, on_update=None,
                 on_log=None, recent_apps: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Ditto Options")
        self.settings = settings
        self.max_clips = QSpinBox(maximum=1_000_000, value=settings.max_clips, specialValueText="Unlimited")
        self.expire = QSpinBox(maximum=36500, value=settings.expire_days, specialValueText="Never", suffix=" days")
        self.max_mb = QSpinBox(minimum=1, maximum=2048, value=max(1, settings.max_clip_bytes // (1024 * 1024)),
                               suffix=" MB")
        self.images = QCheckBox("Save images", checked=settings.save_images)
        self.html = QCheckBox("Save HTML", checked=settings.save_html)
        self.dup = QCheckBox("Move duplicate copies to the top", checked=settings.move_duplicate_to_top)
        self.auto_paste = QCheckBox("Paste into the previous window after choosing a clip",
                                    checked=settings.auto_paste)
        self.tool = QComboBox()
        self.tool.addItems(["auto", "xdotool", "wtype", "ydotool", "none"])
        self.tool.setCurrentText(settings.paste_tool)
        self.shortcut = QComboBox()
        self.shortcut.addItems(["ctrl+v", "ctrl+shift+v", "shift+insert"])
        self.shortcut.setCurrentText(settings.paste_shortcut)
        self.delay = QSpinBox(minimum=0, maximum=3000, value=settings.paste_delay_ms, suffix=" ms")
        self.hide_focus = QCheckBox("Hide the Quick Paste window when it loses focus",
                                    checked=settings.hide_on_focus_loss)
        self.tray = QCheckBox("Show tray icon", checked=settings.show_tray)
        self.ignore = QLineEdit(settings.ignore_apps, objectName="ignoreApps",
                                placeholderText="e.g. keepassxc, bitwarden, 1password")
        self.ignore.setToolTip("Copies from these apps are not saved (comma separated, * wildcards).\n"
                               "Works for X11 apps and apps running through XWayland (many Electron apps);\n"
                               "native Wayland apps cannot be identified.")
        recent = ", ".join(recent_apps or []) or "none yet"
        self.recent_apps = QLabel(f"Recently copied from: {recent}", wordWrap=True,
                                  styleSheet="color: palette(placeholder-text);")
        self.separator = QComboBox(objectName="separator")
        self._seps = [("newline", "New line"), ("space", "Space"), ("comma", "Comma"), ("tab", "Tab"),
                      ("none", "Nothing")]
        self.separator.addItems([label for _k, label in self._seps])
        keys = [k for k, _l in self._seps]
        self.separator.setCurrentIndex(keys.index(settings.multi_paste_separator)
                                       if settings.multi_paste_separator in keys else 0)
        self.check_shortcut = QCheckBox("Check the global shortcut when Ditto starts",
                                        checked=settings.check_shortcut)
        self.auto_update = QCheckBox("Check for updates once a day", checked=settings.auto_update_check)
        self.report = QComboBox(objectName="autoReport")
        self.report.addItems(["Ask", "Always", "Never"])
        self.report.setCurrentText(settings.auto_report.capitalize() if settings.auto_report in
                                   ("ask", "always", "never") else "Ask")

        def row(label: str, button_text: str, callback):
            box = QHBoxLayout()
            box.addWidget(QLabel(label, wordWrap=True), 1)
            if callback:
                b = QPushButton(button_text)
                b.clicked.connect(callback)
                box.addWidget(b)
            return box

        form = QFormLayout()
        form.addRow("Maximum saved copies:", self.max_clips)
        form.addRow("Delete copies after:", self.expire)
        form.addRow("Ignore copies larger than:", self.max_mb)
        form.addRow(self.images)
        form.addRow(self.html)
        form.addRow(self.dup)
        form.addRow(self.auto_paste)
        form.addRow("Paste keystroke tool:", self.tool)
        form.addRow("Paste keystroke:", self.shortcut)
        form.addRow("Delay before pasting:", self.delay)
        form.addRow("Between clips pasted together:", self.separator)
        form.addRow(self.hide_focus)
        form.addRow("Don't save copies from:", self.ignore)
        form.addRow("", self.recent_apps)
        form.addRow(self.tray)
        form.addRow("Global shortcut:", row(pretty(settings.hotkey), "Check / Change…", on_shortcut))
        form.addRow(self.check_shortcut)
        form.addRow("Version:", row(__version__, "Check for Updates…", on_update))
        form.addRow(self.auto_update)
        form.addRow("Log file:", row(str(log_path()), "Open", on_log))
        form.addRow("Report problems on GitHub:", self.report)
        form.addRow("Database:", QLabel(db_path, wordWrap=True))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def apply(self) -> Settings:
        s = self.settings
        s.max_clips = self.max_clips.value()
        s.expire_days = self.expire.value()
        s.max_clip_bytes = self.max_mb.value() * 1024 * 1024
        s.save_images = self.images.isChecked()
        s.save_html = self.html.isChecked()
        s.move_duplicate_to_top = self.dup.isChecked()
        s.auto_paste = self.auto_paste.isChecked()
        s.paste_tool = self.tool.currentText()
        s.paste_shortcut = self.shortcut.currentText()
        s.paste_delay_ms = self.delay.value()
        s.hide_on_focus_loss = self.hide_focus.isChecked()
        s.show_tray = self.tray.isChecked()
        s.ignore_apps = self.ignore.text().strip()
        s.multi_paste_separator = self._seps[self.separator.currentIndex()][0]
        s.check_shortcut = self.check_shortcut.isChecked()
        s.auto_update_check = self.auto_update.isChecked()
        s.auto_report = self.report.currentText().lower()
        return s
