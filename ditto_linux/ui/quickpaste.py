"""The Quick Paste window: searchable clip history, groups, preview and paste."""
from __future__ import annotations

import datetime as _dt
import logging

import difflib
import html as _html

from PyQt6.QtCore import QEvent, QKeyCombination, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QSplitter, QStackedWidget,
    QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from .. import formats as fmt
from ..db import NO_PARENT, Clip, ClipStore

log = logging.getLogger(__name__)

ROLE_ID = Qt.ItemDataRole.UserRole
ROLE_GROUP = Qt.ItemDataRole.UserRole + 1


def _one_line(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class EditClipDialog(QDialog):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Clip")
        self.resize(560, 380)
        self.editor = QPlainTextEdit(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.editor)
        lay.addWidget(buttons)

    def text(self) -> str:
        return self.editor.toPlainText()


class PinDialog(QDialog):
    """Pin a clip with a title; optionally hide its content (passwords, tokens…)."""

    def __init__(self, clip: Clip, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pin Clip")
        self.resize(420, 0)
        self.title = QLineEdit(clip.description, objectName="pinTitle",
                               placeholderText="e.g. Work VPN password")
        self.hidden = QCheckBox("Hide content (show only the title)", objectName="pinHidden")
        self.hidden.setChecked(clip.hidden)
        self.hidden.setToolTip("For passwords and other secrets: the list and preview show only the title.\n"
                               "Enter still pastes the real content.")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("Title:", self.title)
        form.addRow("", self.hidden)
        form.addRow(buttons)


class QuickPasteWindow(QWidget):
    pasteRequested = pyqtSignal(int, bool)  # clip id, plain text only
    copyRequested = pyqtSignal(int)         # put clip on the clipboard, no paste, keep window
    settingsRequested = pyqtSignal()
    updateRequested = pyqtSignal()
    shortcutRequested = pyqtSignal()

    def __init__(self, store: ClipStore, parent=None, settings=None):
        super().__init__(parent)
        self.store = store
        self.settings = settings
        self.persistent = False      # Ctrl+Space: keep window open when it loses focus
        self.groups_only = False     # Ctrl+G: show only groups
        self._dialog_open = 0
        # Ctrl+` cycling: hold Ctrl, press ` again to move down, release Ctrl to paste.
        self._hotkey_presses = 0
        self._released_ticks = 0
        self._modifiers = QGuiApplication.queryKeyboardModifiers  # replaceable in tests
        self._hotkey_watch = QTimer(self, interval=30)
        self._hotkey_watch.timeout.connect(self._check_hotkey_modifier)
        self._focus_check = QTimer(self, singleShot=True, interval=250)
        self._focus_check.timeout.connect(self._hide_if_inactive)
        self.current_group = NO_PARENT
        self.group_stack: list[tuple[int, str]] = []
        self.setWindowTitle("Ditto")
        self.setObjectName("QuickPasteWindow")
        self.resize(460, 560)
        self.setWindowFlag(Qt.WindowType.Tool, False)

        self.search = QLineEdit(placeholderText="Search clips…  (Esc to close)")
        self.search.setClearButtonEnabled(True)
        self.search.setObjectName("search")
        self.search.installEventFilter(self)
        self.back_btn = QToolButton(text="◀", toolTip="Back to parent group (Backspace)")
        self.back_btn.clicked.connect(self.go_up)
        self.back_btn.setVisible(False)
        self.menu_btn = QToolButton(text="☰", toolTip="Menu")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._build_main_menu()

        self.list = QListWidget(objectName="clipList")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setUniformItemSizes(False)
        self.list.setIconSize(QSize(48, 32))
        self.list.itemActivated.connect(lambda _i: self.paste_selected())
        self.list.currentItemChanged.connect(lambda *_: self._update_preview())
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.installEventFilter(self)

        self.preview_text = QTextBrowser(objectName="previewText")
        self.preview_text.setOpenExternalLinks(False)
        self.preview_text.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        self.preview_image = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.preview = QStackedWidget()
        self.preview.addWidget(self.preview_text)
        self.preview.addWidget(self.preview_image)
        self.preview.setVisible(False)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.list)
        split.addWidget(self.preview)
        split.setSizes([400, 160])

        self.status = QLabel(objectName="status")
        self.status.setStyleSheet("color: palette(placeholder-text); padding: 2px;")

        top = QHBoxLayout()
        top.addWidget(self.back_btn)
        top.addWidget(self.search, 1)
        top.addWidget(self.menu_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 4)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addWidget(self.status)

        self._search_timer = QTimer(self, singleShot=True, interval=150)
        self._search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self._shortcuts()
        self.refresh()

    # ------------------------------------------------------------------ setup
    def _build_main_menu(self):
        m = QMenu(self)
        m.addAction("New Clip…\tCtrl+N", self.new_clip)
        m.addAction("New Group…\tCtrl+F7", self.new_group)
        m.addAction("New Group from Selection…\tF7", self.new_group_from_selection)
        self.act_groups = m.addAction("Show Groups Only\tCtrl+G", self.toggle_groups_only)
        self.act_groups.setCheckable(True)
        self.act_preview = m.addAction("Show Description\tF3", self.toggle_preview)
        self.act_preview.setCheckable(True)
        self.act_persist = m.addAction("Keep Window Open\tCtrl+Space", self.toggle_persistent)
        self.act_persist.setCheckable(True)
        m.addAction("Refresh\tF5", self.refresh)
        m.addSeparator()
        m.addAction("Pin Selected…\tCtrl+P", self.pin_selected)
        m.addAction("Hide / Show Content\tCtrl+H", self.toggle_hidden)
        m.addSeparator()
        m.addAction("Keyboard Shortcuts…", self.show_shortcuts)
        m.addAction("Settings…", self.settingsRequested.emit)
        m.addAction("Check Shortcut…", self.shortcutRequested.emit)
        m.addAction("Check for Updates…", self.updateRequested.emit)
        self.menu_btn.setMenu(m)

    # Ditto's default Quick Paste keys (ActionEnums::GetDefaultShortCutKeyA) plus a few extras.
    SHORTCUTS = [
        ("Ctrl+` (global)", "Open Ditto"),
        ("Hold Ctrl, press ` again", "Move to the next clip; release Ctrl to paste it"),
        ("Enter / Double-click", "Paste selected clip into the previous window"),
        ("Shift+Enter", "Paste as plain text"),
        ("Ctrl+1 … Ctrl+0 (also numpad)", "Paste clip at position 1–10"),
        ("Ctrl+C", "Copy selected clip to the clipboard (no paste)"),
        ("Esc", "Clear search → leave group → close"),
        ("Shift+Esc", "Close window"),
        ("Alt+C", "Cancel search filter"),
        ("F3", "Show / hide description (preview)"),
        ("N / P", "Next / previous clip while the description is shown"),
        ("W", "Toggle word wrap in the description"),
        ("Ctrl+N", "New clip"),
        ("Ctrl+E", "Edit clip"),
        ("Alt+Enter", "Clip properties"),
        ("Ctrl+F2", "Compare two selected clips"),
        ("Ctrl+F7", "New group"),
        ("F7", "New group from selected clips"),
        ("Ctrl+G", "Show groups only"),
        ("Ctrl+Space", "Keep window open when it loses focus"),
        ("F5", "Refresh"),
        ("Delete", "Delete selected clips"),
        ("F2", "Set title (description) / rename group"),
        ("Ctrl+M", "Move selected clips to a group"),
        ("Ctrl+S", "Pin / unpin (always first)"),
        ("Ctrl+P", "Pin with a title, optionally hiding the content"),
        ("Ctrl+H", "Hide / show content (passwords: list shows only the title)"),
        ("Ctrl+Up / Ctrl+Down", "Move a pinned clip up / down"),
        ("Ctrl+Shift+Up", "Move a pinned clip to the top"),
        ("Ctrl+D", "Toggle never auto delete"),
        ("Backspace (empty search)", "Back to parent group"),
        ("Right-click", "Menu"),
    ]

    def _shortcuts(self):
        def sc(key, fn):
            s = QShortcut(QKeySequence(key) if isinstance(key, str) else QKeySequence(key), self)
            s.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            s.activated.connect(fn)

        sc("Escape", self._escape)
        sc("Shift+Escape", self.hide)
        sc("Shift+Return", lambda: self.paste_selected(plain=True))
        sc("Shift+Enter", lambda: self.paste_selected(plain=True))
        sc("Alt+Return", self.show_properties)
        sc("Alt+Enter", self.show_properties)
        sc("Alt+C", self.search.clear)
        sc("Delete", self.delete_selected)
        sc("Ctrl+C", self.copy_selected)
        sc("Ctrl+N", self.new_clip)
        sc("Ctrl+E", self.edit_selected)
        sc("Ctrl+F2", self.compare_selected)
        sc("Ctrl+F7", self.new_group)
        sc("F7", self.new_group_from_selection)
        sc("Ctrl+G", self.toggle_groups_only)
        sc("Ctrl+Space", self.toggle_persistent)
        sc("F2", self.describe_selected)
        sc("F3", self.toggle_preview)
        sc("F5", self.refresh)
        sc("Ctrl+M", self.move_selected_to_group)
        sc("Ctrl+S", self.toggle_sticky)
        sc("Ctrl+P", self.pin_selected)
        sc("Ctrl+H", self.toggle_hidden)
        sc("Ctrl+Up", lambda: self.move_pin(-1))
        sc("Ctrl+Down", lambda: self.move_pin(1))
        sc("Ctrl+Shift+Up", lambda: self.move_pin(-10**9))
        # Without a desktop-wide binding the key reaches the window itself.
        sc(QKeyCombination(Qt.KeyboardModifier.ControlModifier, Qt.Key.Key_QuoteLeft), self.hotkey)
        sc("Ctrl+D", self.toggle_never_delete)
        sc("Ctrl+F", lambda: self.search.setFocus())
        digits = [Qt.Key.Key_1, Qt.Key.Key_2, Qt.Key.Key_3, Qt.Key.Key_4, Qt.Key.Key_5,
                  Qt.Key.Key_6, Qt.Key.Key_7, Qt.Key.Key_8, Qt.Key.Key_9, Qt.Key.Key_0]
        for n, key in enumerate(digits):
            sc(QKeyCombination(Qt.KeyboardModifier.ControlModifier, key), lambda n=n: self._paste_index(n))
            sc(QKeyCombination(Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier, key),
               lambda n=n: self._paste_index(n))

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.KeyRelease and ev.key() == Qt.Key.Key_Control and self._hotkey_presses:
            self._hotkey_released()
            return False
        if obj is self.list and ev.type() == QEvent.Type.KeyPress:
            k, mods = ev.key(), ev.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            if self.preview.isVisible() and mods == Qt.KeyboardModifier.NoModifier:
                if k == Qt.Key.Key_N:
                    self._move(1)
                    return True
                if k == Qt.Key.Key_P:
                    self._move(-1)
                    return True
                if k == Qt.Key.Key_W:
                    self.toggle_word_wrap()
                    return True
            if k == Qt.Key.Key_Backspace and self.group_stack and not self.search.text():
                self.go_up()
                return True
            text = ev.text()
            if text and text.isprintable() and not (mods & (Qt.KeyboardModifier.ControlModifier
                                                           | Qt.KeyboardModifier.AltModifier)):
                # typing while the list has focus goes to the search box, like Ditto
                self.search.setFocus()
                self.search.insert(text)
                return True
        # Arrow keys / Enter in the search box drive the list, like Ditto.
        if obj is self.search and ev.type() == ev.Type.KeyPress:
            k = ev.key()
            if k in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self.list.setFocus()
                QGuiApplication.sendEvent(self.list, ev)
                return True
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.paste_selected(plain=bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier))
                return True
            if k == Qt.Key.Key_Backspace and not self.search.text() and self.group_stack:
                self.go_up()
                return True
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------------ data
    def refresh(self, keep_selection: bool = True):
        selected = self.selected_ids() if keep_selection else []
        query = self.search.text().strip()
        clips = self.store.list(self.current_group, search=query, limit=1000, groups_only=self.groups_only)
        self.list.clear()
        for i, clip in enumerate(clips):
            self.list.addItem(self._make_item(clip, i))
        restored = False
        if selected:
            for row in range(self.list.count()):
                if self.list.item(row).data(ROLE_ID) in selected:
                    self.list.setCurrentRow(row)
                    restored = True
                    break
        if not restored and self.list.count():
            self.list.setCurrentRow(0)
        path = " › ".join(name for _gid, name in self.group_stack)
        total = self.store.count()
        flags = ("  ·  groups only" if self.groups_only else "") + ("  ·  📌 kept open" if self.persistent else "")
        self.status.setText((f"{path}  ·  " if path else "") + f"{len(clips)} shown · {total} clips" + flags)
        self.back_btn.setVisible(bool(self.group_stack))

    def _make_item(self, clip: Clip, index: int) -> QListWidgetItem:
        prefix = f"{index + 1}. " if index < 10 else ""
        flags = (("📌 " if clip.sticky else "") + ("🔒 " if clip.hidden else "")
                 + ("🛡 " if clip.never_delete and not clip.is_group else ""))
        if clip.is_group:
            label = f"📁 {clip.text}"
        else:
            label = flags + _one_line(clip.title)
        item = QListWidgetItem(prefix + label)
        item.setData(ROLE_ID, clip.id)
        item.setData(ROLE_GROUP, clip.is_group)
        when = _dt.datetime.fromtimestamp(clip.date).strftime("%Y-%m-%d %H:%M:%S") if clip.date else ""
        body = "(content hidden — Enter pastes it, Ctrl+H shows it)" if clip.hidden else clip.text[:1500]
        item.setToolTip(f"#{clip.id} · {when}\n\n{body}")
        if clip.text == "[Image]" and not clip.hidden:
            data = self.store.get_formats(clip.id)
            raw = data.get(fmt.PNG) or data.get(fmt.BMP)
            if raw:
                img = QImage.fromData(raw)
                if not img.isNull():
                    item.setIcon(QIcon(QPixmap.fromImage(img.scaled(
                        96, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))))
        return item

    def selected_ids(self) -> list[int]:
        items = self.list.selectedItems() or ([self.list.currentItem()] if self.list.currentItem() else [])
        return [i.data(ROLE_ID) for i in items if i is not None]

    def _current(self) -> tuple[int, bool] | None:
        item = self.list.currentItem()
        if item is None:
            return None
        return item.data(ROLE_ID), bool(item.data(ROLE_GROUP))

    # ------------------------------------------------------------------ actions
    def paste_selected(self, plain: bool = False):
        cur = self._current()
        if not cur:
            return
        clip_id, is_group = cur
        if is_group:
            self.enter_group(clip_id)
            return
        self.pasteRequested.emit(clip_id, plain)

    def _paste_index(self, n: int):
        item = self.list.item(n)
        if item is not None:
            self.list.setCurrentItem(item)
            self.paste_selected()

    def enter_group(self, group_id: int):
        g = self.store.get(group_id)
        if not g:
            return
        self.group_stack.append((group_id, g.text))
        self.current_group = group_id
        self.search.clear()
        self.refresh(keep_selection=False)

    def go_up(self):
        if not self.group_stack:
            return
        self.group_stack.pop()
        self.current_group = self.group_stack[-1][0] if self.group_stack else NO_PARENT
        self.refresh(keep_selection=False)

    def _escape(self):
        if self.search.text():
            self.search.clear()
        elif self.group_stack:
            self.go_up()
        else:
            self.hide()

    def delete_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        if any(self.store.get(i) and self.store.get(i).is_group for i in ids):
            if self._exec_input(QMessageBox.question, self, "Delete",
                                "Delete the selected group(s) and all clips inside?") != QMessageBox.StandardButton.Yes:
                return
        row = self.list.currentRow()
        self.store.delete(ids)
        self.refresh(keep_selection=False)
        if self.list.count():
            self.list.setCurrentRow(min(row, self.list.count() - 1))

    def edit_selected(self):
        cur = self._current()
        if not cur or cur[1]:
            return
        formats = self.store.get_formats(cur[0])
        if fmt.TEXT not in formats:
            self._exec_input(QMessageBox.information, self, "Edit Clip", "Only text clips can be edited.")
            return
        dlg = EditClipDialog(formats[fmt.TEXT].decode("utf-8", errors="replace"), self)
        if self._exec(dlg) == QDialog.DialogCode.Accepted:
            self.store.update_text(cur[0], dlg.text())
            self.refresh()

    def describe_selected(self):
        cur = self._current()
        if not cur:
            return
        clip = self.store.get(cur[0])
        if clip.is_group:
            name, ok = self._exec_input(QInputDialog.getText, self, "Rename Group", "Name:", text=clip.text)
            if ok and name.strip():
                self.store.rename_group(clip.id, name.strip())
        else:
            desc, ok = self._exec_input(QInputDialog.getText, self, "Clip Description", "Description (shown instead of text):",
                                            text=clip.description)
            if ok:
                self.store.set_description(clip.id, desc)
        self.refresh()

    def toggle_sticky(self):
        for cid in self.selected_ids():
            clip = self.store.get(cid)
            if clip:
                self.store.set_sticky(cid, not clip.sticky)
        self.refresh()

    def toggle_never_delete(self):
        for cid in self.selected_ids():
            clip = self.store.get(cid)
            if clip:
                self.store.set_never_delete(cid, not clip.never_delete)
        self.refresh()

    def pin_selected(self):
        cur = self._current()
        if not cur or cur[1]:
            return
        clip = self.store.get(cur[0])
        dlg = PinDialog(clip, self)
        if self._exec(dlg) == QDialog.DialogCode.Accepted:
            self.store.pin(clip.id, title=dlg.title.text().strip(), hidden=dlg.hidden.isChecked())
            self.refresh()
            self._select_id(clip.id)

    def toggle_hidden(self):
        for cid in self.selected_ids():
            clip = self.store.get(cid)
            if clip and not clip.is_group:
                self.store.set_hidden(cid, not clip.hidden)
        self.refresh()
        self._update_preview()

    def move_pin(self, delta: int):
        cur = self._current()
        if cur and self.store.move_sticky(cur[0], delta):
            self.refresh(keep_selection=False)
            self._select_id(cur[0])

    def _select_id(self, clip_id: int):
        for row in range(self.list.count()):
            if self.list.item(row).data(ROLE_ID) == clip_id:
                self.list.setCurrentRow(row)
                return

    # ------------------------------------------------------------------ Ctrl+` cycling
    def hotkey(self):
        """The global shortcut was pressed.

        First press opens the window. Pressing it again while Ctrl is still held moves to the next
        clip, and releasing Ctrl then pastes the selected clip (Ditto's quick "paste previous" flow).
        """
        if not self.isVisible():
            self.show_at_cursor()
            self._hotkey_presses = 1
        else:
            if not self.isActiveWindow():
                self.show_at_cursor()
            self._hotkey_presses = max(self._hotkey_presses, 1) + 1
            self._move(1)
        self._released_ticks = 0
        self._hotkey_watch.start()
        log.info("hotkey press %d (row %d)", self._hotkey_presses, self.list.currentRow())

    def _check_hotkey_modifier(self):
        if not self.isVisible():
            self._reset_hotkey()
            return
        if self._modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._released_ticks = 0
            return
        self._released_ticks += 1
        if self._released_ticks >= 2:   # two readings in a row: ignore glitches
            self._hotkey_released()

    def _hotkey_released(self):
        presses = self._hotkey_presses
        self._reset_hotkey()
        if presses >= 2 and self.isVisible():
            log.info("Ctrl released after %d presses: pasting row %d", presses, self.list.currentRow())
            self.paste_selected()

    def _reset_hotkey(self):
        self._hotkey_presses = 0
        self._released_ticks = 0
        self._hotkey_watch.stop()

    def new_group(self):
        name, ok = self._exec_input(QInputDialog.getText, self, "New Group", "Group name:")
        if ok and name.strip():
            self.store.create_group(name.strip(), self.current_group)
            self.refresh()

    def move_selected_to_group(self):
        ids = self.selected_ids()
        if not ids:
            return
        groups = [g for g in self.store.groups() if g.id not in ids]
        names = ["(No group)"] + [g.text for g in groups]
        choice, ok = self._exec_input(QInputDialog.getItem, self, "Move to Group", "Group:", names, 0, False)
        if ok:
            target = NO_PARENT if choice == names[0] else groups[names.index(choice) - 1].id
            self.store.move_to_group(ids, target)
            self.refresh()

    def toggle_preview(self):
        self.preview.setVisible(not self.preview.isVisible())
        self.act_preview.setChecked(self.preview.isVisible())
        self._update_preview()

    def _update_preview(self):
        if not self.preview.isVisible():
            return
        cur = self._current()
        if not cur or cur[1]:
            self.preview_text.clear()
            self.preview.setCurrentIndex(0)
            return
        clip = self.store.get(cur[0])
        if clip and clip.hidden:
            self.preview_text.setPlainText(f"{clip.title}\n\n{Clip.MASK}\n\n"
                                           "Content hidden. Enter pastes it; Ctrl+H shows it.")
            self.preview.setCurrentIndex(0)
            return
        data = self.store.get_formats(cur[0])
        raw = data.get(fmt.PNG) or data.get(fmt.BMP)
        if raw:
            img = QImage.fromData(raw)
            pm = QPixmap.fromImage(img)
            self.preview_image.setPixmap(pm.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))
            self.preview.setCurrentIndex(1)
        elif fmt.HTML in data:
            self.preview_text.setHtml(data[fmt.HTML].decode("utf-8", errors="replace"))
            self.preview.setCurrentIndex(0)
        else:
            self.preview_text.setPlainText(data.get(fmt.TEXT, b"").decode("utf-8", errors="replace"))
            self.preview.setCurrentIndex(0)

    def _context_menu(self, pos):
        cur = self._current()
        m = QMenu(self)
        if cur:
            clip = self.store.get(cur[0])
            if clip and clip.is_group:
                m.addAction("Open Group", lambda: self.enter_group(clip.id))
                m.addAction("Rename Group…", self.describe_selected)
            elif clip:
                m.addAction("Paste\tEnter", self.paste_selected)
                m.addAction("Paste Plain Text\tShift+Enter", lambda: self.paste_selected(plain=True))
                m.addSeparator()
                m.addAction("Copy\tCtrl+C", self.copy_selected)
                m.addAction("Edit Clip…\tCtrl+E", self.edit_selected)
                m.addAction("Set Title / Description…\tF2", self.describe_selected)
                a = m.addAction("Pin (always first)\tCtrl+S", self.toggle_sticky)
                a.setCheckable(True)
                a.setChecked(clip.sticky)
                m.addAction("Pin with Title…\tCtrl+P", self.pin_selected)
                a = m.addAction("Hide Content\tCtrl+H", self.toggle_hidden)
                a.setCheckable(True)
                a.setChecked(clip.hidden)
                if clip.sticky:
                    m.addAction("Move Pin Up\tCtrl+Up", lambda: self.move_pin(-1))
                    m.addAction("Move Pin Down\tCtrl+Down", lambda: self.move_pin(1))
                    m.addAction("Move Pin to Top\tCtrl+Shift+Up", lambda: self.move_pin(-10**9))
                a = m.addAction("Never Auto Delete\tCtrl+D", self.toggle_never_delete)
                a.setCheckable(True)
                a.setChecked(clip.never_delete)
            m.addAction("Move to Group…\tCtrl+M", self.move_selected_to_group)
            m.addAction("New Group from Selection…\tF7", self.new_group_from_selection)
            m.addAction("Compare Selected\tCtrl+F2", self.compare_selected)
            m.addAction("Properties…\tAlt+Enter", self.show_properties)
            m.addSeparator()
            m.addAction("Delete\tDel", self.delete_selected)
            m.addSeparator()
        m.addAction("New Clip…\tCtrl+N", self.new_clip)
        m.addAction("New Group…\tCtrl+F7", self.new_group)
        self._dialog_open += 1
        try:
            m.exec(self.list.viewport().mapToGlobal(pos))
        finally:
            self._dialog_open -= 1

    def _move(self, delta: int):
        if self.list.count():
            row = max(0, min(self.list.count() - 1, self.list.currentRow() + delta))
            self.list.setCurrentRow(row)

    def toggle_word_wrap(self):
        wrap = self.preview_text.lineWrapMode() == QTextBrowser.LineWrapMode.NoWrap
        self.preview_text.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth if wrap
                                          else QTextBrowser.LineWrapMode.NoWrap)

    def toggle_groups_only(self):
        self.groups_only = not self.groups_only
        self.act_groups.setChecked(self.groups_only)
        self.refresh(keep_selection=False)

    def toggle_persistent(self):
        self.persistent = not self.persistent
        self.act_persist.setChecked(self.persistent)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.persistent)
        if self.isHidden():
            self.show()
        self.refresh()

    def copy_selected(self):
        if self.search.hasFocus() and self.search.hasSelectedText():
            self.search.copy()
            return
        cur = self._current()
        if cur and not cur[1]:
            self.copyRequested.emit(cur[0])

    def new_clip(self):
        dlg = EditClipDialog("", self)
        dlg.setWindowTitle("New Clip")
        if self._exec(dlg) == QDialog.DialogCode.Accepted and dlg.text():
            clip_id, _ = self.store.add_text(dlg.text())
            if self.current_group != NO_PARENT:
                self.store.move_to_group([clip_id], self.current_group)
            self.refresh(keep_selection=False)

    def new_group_from_selection(self):
        ids = [i for i in self.selected_ids() if not (self.store.get(i) and self.store.get(i).is_group)]
        if not ids:
            return
        name, ok = self._exec_input(QInputDialog.getText, self, "New Group", "Group name for the selected clips:")
        if ok and name.strip():
            gid = self.store.create_group(name.strip(), self.current_group)
            self.store.move_to_group(ids, gid)
            self.refresh()

    def compare_selected(self):
        ids = [i for i in self.selected_ids() if not self.store.get(i).is_group][:2]
        if len(ids) != 2:
            self._exec_input(QMessageBox.information, self, "Compare", "Select two clips to compare (Ctrl+click).")
            return
        a, b = (self.store.get_formats(i).get(fmt.TEXT, b"").decode("utf-8", "replace").splitlines() for i in ids)
        table = difflib.HtmlDiff(wrapcolumn=70).make_table(a, b, f"Clip #{ids[0]}", f"Clip #{ids[1]}")
        dlg = QDialog(self)
        dlg.setWindowTitle("Compare Clips")
        dlg.resize(900, 520)
        view = QTextBrowser()
        view.setHtml("<style>.diff_add{background:#cfc}.diff_chg{background:#ffc}.diff_sub{background:#fcc}"
                     "td{font-family:monospace}</style>" + table)
        lay = QVBoxLayout(dlg)
        lay.addWidget(view)
        self.last_compare = view.toPlainText()
        self._exec(dlg)

    def show_properties(self):
        cur = self._current()
        if not cur:
            return
        clip = self.store.get(cur[0])
        data = self.store.get_formats(clip.id)
        when = lambda t: _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "never"
        group = self.store.get(clip.parent_id) if clip.parent_id not in (NO_PARENT, 0) else None
        rows = [("ID", clip.id), ("Type", "Group" if clip.is_group else "Clip"), ("Copied", when(clip.date)),
                ("Last pasted", when(clip.last_paste)), ("Group", group.text if group else "(none)"),
                ("Title / description", clip.description or "(none)"), ("Pinned", "yes" if clip.sticky else "no"),
                ("Content hidden", "yes" if clip.hidden else "no"),
                ("Never auto delete", "yes" if clip.never_delete else "no")]
        rows += [(f"Format {m}", f"{len(v):,} bytes") for m, v in data.items()]
        text = "".join(f"<tr><td><b>{_html.escape(str(k))}</b></td><td>{_html.escape(str(v))}</td></tr>"
                       for k, v in rows)
        self.last_properties = rows
        box = QMessageBox(QMessageBox.Icon.NoIcon, "Clip Properties", f"<table cellpadding=3>{text}</table>",
                          QMessageBox.StandardButton.Close, self)
        self._exec(box)

    def show_shortcuts(self):
        rows = "".join(f"<tr><td><b>{_html.escape(k)}</b></td><td>{_html.escape(v)}</td></tr>"
                       for k, v in self.SHORTCUTS)
        box = QMessageBox(QMessageBox.Icon.NoIcon, "Keyboard Shortcuts",
                          f"<table cellpadding=2>{rows}</table>", QMessageBox.StandardButton.Close, self)
        self._exec(box)

    # dialogs opened from the window must not trigger hide-on-focus-loss
    def _exec(self, dlg):
        self._dialog_open += 1
        try:
            return dlg.exec()
        finally:
            self._dialog_open -= 1
            self.activateWindow()

    def _exec_input(self, fn, *args, **kw):
        self._dialog_open += 1
        try:
            return fn(*args, **kw)
        finally:
            self._dialog_open -= 1
            self.activateWindow()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self._focus_check.start()

    def _hide_if_inactive(self):
        from PyQt6.QtWidgets import QApplication
        if (self.isVisible() and not self.persistent and not self._dialog_open
                and (self.settings is None or self.settings.hide_on_focus_loss)
                and QApplication.activeWindow() is None and QApplication.activePopupWidget() is None
                and QApplication.activeModalWidget() is None
                and QGuiApplication.platformName() not in ("offscreen", "minimal")):
            self.hide()

    # ------------------------------------------------------------------ show / hide
    def show_at_cursor(self):
        self.search.clear()
        self.refresh(keep_selection=False)
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None and QGuiApplication.platformName() != "wayland":
            geo = screen.availableGeometry()
            pos = QCursor.pos()
            x = min(max(geo.left(), pos.x()), geo.right() - self.width())
            y = min(max(geo.top(), pos.y()), geo.bottom() - self.height())
            self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()
        if QGuiApplication.platformName() == "xcb":
            # Defeat focus-stealing prevention (window would open behind the active one on GNOME).
            from .x11 import activate_window
            for delay in (30, 150):
                QTimer.singleShot(delay, lambda: self.isVisible() and activate_window(int(self.winId())))

    def showEvent(self, ev):
        # a focus-loss check queued before the window was hidden must not hide it again
        self._focus_check.stop()
        super().showEvent(ev)

    def hideEvent(self, ev):
        self._reset_hotkey()
        super().hideEvent(ev)

    def toggle(self):
        if self.isVisible() and self.isActiveWindow():
            self.hide()
        else:
            self.show_at_cursor()
