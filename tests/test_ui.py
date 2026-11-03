"""Quick Paste window tests driven by real key events (offscreen Qt platform)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*.warning=false")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

from ditto_linux import formats as fmt  # noqa: E402
from ditto_linux.config import Settings  # noqa: E402
from ditto_linux.db import ClipStore  # noqa: E402
from ditto_linux.ui.quickpaste import QuickPasteWindow  # noqa: E402
from ditto_linux.ui.settings_dialog import SettingsDialog  # noqa: E402

app = QApplication.instance() or QApplication([])


def png_bytes(w=8, h=6):
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("blue"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


class QuickPasteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ClipStore(Path(self.tmp.name) / "Ditto.db")
        self.ids = [self.store.add_text(t)[0] for t in ("alpha one", "beta two", "gamma three")]
        self.w = QuickPasteWindow(self.store)
        self.pasted = []
        self.w.pasteRequested.connect(lambda cid, plain: self.pasted.append((cid, plain)))
        self.w.show_at_cursor()
        QTest.qWaitForWindowExposed(self.w)

    def tearDown(self):
        self.w.close()
        self.w.deleteLater()
        self.store.close()
        self.tmp.cleanup()

    def type_search(self, text):
        self.w.search.setFocus()
        QTest.keyClicks(self.w.search, text)
        self.w._search_timer.stop()
        self.w.refresh()

    def row_ids(self):
        return [self.w.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.w.list.count())]

    def test_lists_most_recent_first_with_numbers(self):
        self.assertEqual(self.row_ids(), self.ids[::-1])
        self.assertTrue(self.w.list.item(0).text().startswith("1. gamma"))

    def test_search_filters_and_enter_pastes(self):
        self.type_search("beta")
        self.assertEqual(self.row_ids(), [self.ids[1]])
        QTest.keyClick(self.w.search, Qt.Key.Key_Return)
        self.assertEqual(self.pasted, [(self.ids[1], False)])

    def test_shift_enter_pastes_plain(self):
        QTest.keyClick(self.w.search, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.pasted, [(self.ids[2], True)])

    def test_arrow_keys_from_search_move_selection(self):
        self.w.search.setFocus()
        QTest.keyClick(self.w.search, Qt.Key.Key_Down)
        QTest.keyClick(self.w.list, Qt.Key.Key_Return)
        self.assertEqual(self.pasted[-1][0], self.ids[1])

    def test_ctrl_number_quick_paste(self):
        QTest.keyClick(self.w, Qt.Key.Key_3, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.pasted, [(self.ids[0], False)])

    def test_delete_key(self):
        self.w.list.setFocus()
        self.w.list.setCurrentRow(0)
        QTest.keyClick(self.w.list, Qt.Key.Key_Delete)
        self.assertIsNone(self.store.get(self.ids[2]))
        self.assertEqual(self.w.list.count(), 2)

    def test_escape_clears_search_then_hides(self):
        self.type_search("zzz")
        self.assertEqual(self.w.list.count(), 0)
        QTest.keyClick(self.w.search, Qt.Key.Key_Escape)
        self.assertEqual(self.w.search.text(), "")
        self.assertTrue(self.w.isVisible())
        QTest.keyClick(self.w.search, Qt.Key.Key_Escape)
        self.assertFalse(self.w.isVisible())

    def test_enter_group_and_back(self):
        g = self.store.create_group("Work")
        self.store.move_to_group([self.ids[0]], g)
        self.w.refresh(keep_selection=False)
        self.assertEqual(self.row_ids()[0], g)
        QTest.keyClick(self.w.search, Qt.Key.Key_Return)  # Enter on a group opens it
        self.assertEqual(self.row_ids(), [self.ids[0]])
        self.assertTrue(self.w.back_btn.isVisibleTo(self.w))
        self.assertIn("Work", self.w.status.text())
        QTest.keyClick(self.w.search, Qt.Key.Key_Backspace)
        self.assertEqual(self.row_ids()[0], g)
        self.assertEqual(self.pasted, [])

    def test_new_group_and_move(self):
        with mock.patch.object(QInputDialog, "getText", return_value=("Snips", True)):
            QTest.keyClick(self.w, Qt.Key.Key_F7, Qt.KeyboardModifier.ControlModifier)
        g = self.store.groups()[0]
        self.assertEqual(g.text, "Snips")
        self.w.list.setCurrentRow(self.row_ids().index(self.ids[1]))
        with mock.patch.object(QInputDialog, "getItem", return_value=("Snips", True)):
            self.w.move_selected_to_group()
        self.assertEqual([c.id for c in self.store.list(g.id)], [self.ids[1]])

    def test_delete_group_asks_confirmation(self):
        g = self.store.create_group("G")
        self.w.refresh(keep_selection=False)
        self.w.list.setCurrentRow(0)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.w.delete_selected()
        self.assertIsNotNone(self.store.get(g))
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.w.delete_selected()
        self.assertIsNone(self.store.get(g))

    def test_sticky_and_description(self):
        self.w.list.setCurrentRow(2)  # alpha one
        self.w.toggle_sticky()
        self.assertEqual(self.row_ids()[0], self.ids[0])
        self.assertIn("📌", self.w.list.item(0).text())
        self.w.list.setCurrentRow(0)
        with mock.patch.object(QInputDialog, "getText", return_value=("Greeting", True)):
            QTest.keyClick(self.w.list, Qt.Key.Key_F2)
        self.assertIn("Greeting", self.w.list.item(0).text())
        self.w.toggle_never_delete()
        self.assertTrue(self.store.get(self.ids[0]).never_delete)

    def test_edit_clip(self):
        self.w.list.setCurrentRow(0)
        with mock.patch("ditto_linux.ui.quickpaste.EditClipDialog") as dlg:
            dlg.return_value.exec.return_value = 1
            dlg.return_value.text.return_value = "edited text"
            QTest.keyClick(self.w.list, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.store.get_formats(self.ids[2])[fmt.TEXT], b"edited text")

    def test_preview_text_html_image(self):
        hid, _ = self.store.add_clip({fmt.TEXT: b"h", fmt.HTML: b"<b>bold html</b>"})
        iid, _ = self.store.add_clip({fmt.PNG: png_bytes()})
        self.w.refresh(keep_selection=False)
        QTest.keyClick(self.w, Qt.Key.Key_F3)
        self.assertTrue(self.w.preview.isVisible())
        self.w.list.setCurrentRow(0)  # image
        self.assertEqual(self.w.preview.currentIndex(), 1)
        self.assertFalse(self.w.list.item(0).icon().isNull(), "image clips get a thumbnail")
        self.w.list.setCurrentRow(1)  # html
        self.assertIn("bold html", self.w.preview_text.toPlainText())
        self.w.list.setCurrentRow(2)
        self.assertEqual(self.w.preview_text.toPlainText(), "gamma three")

    def test_context_menu_builds_for_each_kind(self):
        from PyQt6.QtWidgets import QMenu
        self.store.create_group("G")
        self.w.refresh(keep_selection=False)
        with mock.patch.object(QMenu, "exec", return_value=None):
            for row in range(self.w.list.count()):
                self.w.list.setCurrentRow(row)
                self.w._context_menu(self.w.list.visualItemRect(self.w.list.item(row)).center())

    def test_settings_dialog_apply(self):
        s = Settings()
        dlg = SettingsDialog(s, "/tmp/x.db")
        dlg.max_clips.setValue(42)
        dlg.expire.setValue(7)
        dlg.auto_paste.setChecked(False)
        dlg.tool.setCurrentText("xdotool")
        dlg.apply()
        self.assertEqual((s.max_clips, s.expire_days, s.auto_paste, s.paste_tool), (42, 7, False, "xdotool"))


class DittoShortcutTests(QuickPasteTests.__base__):
    """Ditto's default Quick Paste keys (ActionEnums::GetDefaultShortCutKeyA)."""
    setUp = QuickPasteTests.setUp
    tearDown = QuickPasteTests.tearDown
    row_ids = QuickPasteTests.row_ids
    type_search = QuickPasteTests.type_search

    def key(self, k, mods=Qt.KeyboardModifier.NoModifier, widget=None):
        QTest.keyClick(widget or self.w.list, k, mods)

    def test_ctrl_numpad_position_paste(self):
        self.key(Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier)
        self.assertEqual(self.pasted, [(self.ids[1], False)])

    def test_ctrl_n_new_clip(self):
        with mock.patch("ditto_linux.ui.quickpaste.EditClipDialog") as dlg:
            dlg.return_value.exec.return_value = 1
            dlg.return_value.text.return_value = "brand new clip"
            self.key(Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.store.list()[0].text, "brand new clip")

    def test_f7_group_from_selection(self):
        self.w.list.selectAll()
        with mock.patch.object(QInputDialog, "getText", return_value=("All", True)):
            self.key(Qt.Key.Key_F7)
        g = self.store.groups()[0]
        self.assertEqual(sorted(c.id for c in self.store.list(g.id)), sorted(self.ids))

    def test_ctrl_g_groups_only(self):
        self.store.create_group("Only me")
        self.key(Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.w.list.count(), 1)
        self.key(Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.w.list.count(), 4)

    def test_alt_c_cancels_filter(self):
        self.type_search("beta")
        QTest.keyClick(self.w.search, Qt.Key.Key_C, Qt.KeyboardModifier.AltModifier)
        self.assertEqual(self.w.search.text(), "")

    def test_shift_escape_closes(self):
        self.type_search("beta")
        QTest.keyClick(self.w.search, Qt.Key.Key_Escape, Qt.KeyboardModifier.ShiftModifier)
        self.assertFalse(self.w.isVisible())

    def test_ctrl_c_copies_without_paste(self):
        copied = []
        self.w.copyRequested.connect(copied.append)
        self.w.list.setCurrentRow(1)
        self.key(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(copied, [self.ids[1]])
        self.assertEqual(self.pasted, [])
        self.assertTrue(self.w.isVisible())

    def test_ctrl_space_persistent(self):
        self.key(Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.w.persistent)
        self.assertIn("kept open", self.w.status.text())

    def test_f3_then_n_p_w(self):
        self.key(Qt.Key.Key_F3)
        self.w.list.setFocus()
        self.w.list.setCurrentRow(0)
        self.key(Qt.Key.Key_N)
        self.assertEqual(self.w.list.currentRow(), 1)
        self.assertEqual(self.w.preview_text.toPlainText(), "beta two")
        self.key(Qt.Key.Key_P)
        self.assertEqual(self.w.list.currentRow(), 0)
        before = self.w.preview_text.lineWrapMode()
        self.key(Qt.Key.Key_W)
        self.assertNotEqual(self.w.preview_text.lineWrapMode(), before)
        self.assertEqual(self.w.search.text(), "", "N/P/W are commands while the description shows")

    def test_typing_in_list_goes_to_search(self):
        self.w.list.setFocus()
        QTest.keyClicks(self.w.list, "gam")
        self.assertEqual(self.w.search.text(), "gam")

    def test_alt_enter_properties(self):
        with mock.patch.object(QMessageBox, "exec", return_value=0):
            self.key(Qt.Key.Key_Return, Qt.KeyboardModifier.AltModifier)
        self.assertIn(("ID", self.ids[2]), self.w.last_properties)

    def test_ctrl_f2_compare(self):
        from PyQt6.QtWidgets import QDialog
        self.w.list.selectAll()
        self.w.list.item(2).setSelected(False)
        with mock.patch.object(QDialog, "exec", return_value=0):
            self.key(Qt.Key.Key_F2, Qt.KeyboardModifier.ControlModifier)
        self.assertIn("gamma three", self.w.last_compare)
        self.assertIn("beta two", self.w.last_compare)

    def test_sticky_never_delete_move_keys(self):
        self.w.list.setCurrentRow(2)
        self.key(Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.store.get(self.ids[0]).sticky)
        self.w.list.setCurrentRow(0)
        self.key(Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.store.get(self.ids[0]).never_delete)
        g = self.store.create_group("Dest")
        self.w.refresh()
        self.w.list.setCurrentRow(self.row_ids().index(self.ids[1]))
        with mock.patch.object(QInputDialog, "getItem", return_value=("Dest", True)):
            self.key(Qt.Key.Key_M, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual([c.id for c in self.store.list(g)], [self.ids[1]])

    def test_pin_title_hide_and_reorder_keys(self):
        self.w.list.setCurrentRow(self.row_ids().index(self.ids[0]))
        from ditto_linux.ui.quickpaste import PinDialog

        def accept(dlg):
            dlg.title.setText("Bank PIN")
            dlg.hidden.setChecked(True)
            return 1
        with mock.patch.object(PinDialog, "exec", accept):
            self.key(Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
        clip = self.store.get(self.ids[0])
        self.assertTrue(clip.sticky and clip.hidden)
        self.assertEqual(self.row_ids()[0], self.ids[0])
        label = self.w.list.item(0).text()
        self.assertIn("Bank PIN", label)
        self.assertNotIn("alpha", label + self.w.list.item(0).toolTip())
        # preview never shows hidden content
        self.key(Qt.Key.Key_F3)
        self.assertNotIn("alpha", self.w.preview_text.toPlainText())
        self.key(Qt.Key.Key_F3)
        # Ctrl+H shows / hides it again
        self.key(Qt.Key.Key_H, Qt.KeyboardModifier.ControlModifier)
        self.assertFalse(self.store.get(self.ids[0]).hidden)
        self.key(Qt.Key.Key_H, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.store.get(self.ids[0]).hidden)
        # pin a second clip, then move the first pin back to the top with Ctrl+Up
        self.w.list.setCurrentRow(self.row_ids().index(self.ids[1]))
        self.key(Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.row_ids()[:2], [self.ids[1], self.ids[0]])
        self.w.list.setCurrentRow(1)
        self.key(Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.row_ids()[:2], [self.ids[0], self.ids[1]])
        self.assertEqual(self.w.list.currentRow(), 0, "selection follows the moved pin")
        self.key(Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.row_ids()[:2], [self.ids[1], self.ids[0]])
        # Enter pastes the hidden clip normally
        QTest.keyClick(self.w.list, Qt.Key.Key_Return)
        self.assertEqual(self.pasted[-1], (self.ids[0], False))

    def test_f5_refresh_and_shortcut_help(self):
        self.store.add_text("external")
        self.key(Qt.Key.Key_F5)
        self.assertEqual(self.w.list.count(), 4)
        with mock.patch.object(QMessageBox, "exec", return_value=0):
            self.w.show_shortcuts()


class HotkeyCycleTests(QuickPasteTests.__base__):
    """Hold Ctrl, press ` again to move down, release Ctrl to paste (Ditto's quick paste cycling)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ClipStore(Path(self.tmp.name) / "Ditto.db")
        self.ids = [self.store.add_text(t)[0] for t in ("a", "b", "c", "d")]
        self.w = QuickPasteWindow(self.store)
        self.pasted = []
        self.w.pasteRequested.connect(lambda cid, plain: (self.pasted.append(cid), self.w.hide()))
        self.ctrl = True
        self.w._modifiers = lambda: (Qt.KeyboardModifier.ControlModifier if self.ctrl
                                     else Qt.KeyboardModifier.NoModifier)

    def tearDown(self):
        self.w.close()
        self.w.deleteLater()
        self.store.close()
        self.tmp.cleanup()

    def wait_ticks(self, ms=150):
        QTest.qWait(ms)

    def test_double_press_then_release_pastes_previous_clip(self):
        self.w.hotkey()                      # Ctrl+` : open, newest clip selected
        self.assertTrue(self.w.isVisible())
        self.assertEqual(self.w.list.currentRow(), 0)
        self.wait_ticks()
        self.w.hotkey()                      # ` again with Ctrl held: next clip
        self.assertEqual(self.w.list.currentRow(), 1)
        self.wait_ticks()
        self.assertEqual(self.pasted, [], "nothing is pasted while Ctrl is held")
        self.ctrl = False                    # release Ctrl
        self.wait_ticks()
        self.assertEqual(self.pasted, [self.ids[2]])

    def test_more_presses_go_further(self):
        for _ in range(4):
            self.w.hotkey()
            self.wait_ticks(40)
        self.assertEqual(self.w.list.currentRow(), 3)
        self.ctrl = False
        self.wait_ticks()
        self.assertEqual(self.pasted, [self.ids[0]])

    def test_single_press_and_release_keeps_window_open(self):
        self.w.hotkey()
        self.ctrl = False
        self.wait_ticks()
        self.assertTrue(self.w.isVisible())
        self.assertEqual(self.pasted, [])
        self.assertFalse(self.w._hotkey_watch.isActive())

    def test_ctrl_key_release_event_also_pastes(self):
        self.w.hotkey()
        self.w.hotkey()
        self.w._hotkey_watch.stop()          # rely on the key event alone
        QTest.keyRelease(self.w.list, Qt.Key.Key_Control)
        self.assertEqual(self.pasted, [self.ids[2]])

    def test_escape_cancels_cycle(self):
        self.w.hotkey()
        self.w.hotkey()
        self.w.hide()
        self.ctrl = False
        self.wait_ticks()
        self.assertEqual(self.pasted, [])


class PasteKeystrokeTests(unittest.TestCase):
    def test_tool_arguments(self):
        from ditto_linux import paste
        self.assertEqual(paste._ydotool_args("ctrl+v")[-4:], ["29:1", "47:1", "47:0", "29:0"])
        self.assertEqual(paste._ydotool_args("ctrl+shift+v")[-6:], ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"])
        self.assertEqual(paste._wtype_args("ctrl+v"), ["-M", "ctrl", "v", "-m", "ctrl"])
        self.assertEqual(paste._wtype_args("shift+insert"), ["-M", "shift", "-k", "Insert", "-m", "shift"])
        self.assertEqual(paste._xdotool_args("ctrl+shift+v"), ["key", "--clearmodifiers", "ctrl+shift+v"])
        self.assertEqual(paste._ydotool_args("bogus")[-4:], ["29:1", "47:1", "47:0", "29:0"])

    def test_none_and_fallback(self):
        from ditto_linux.paste import Paster
        p = Paster(Settings(paste_tool="none"))
        self.assertEqual(p.candidates(), [])
        self.assertFalse(p.paste())
        with mock.patch("ditto_linux.paste.shutil.which", return_value="/bin/x"), \
                mock.patch("ditto_linux.paste.subprocess.run") as run, \
                mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}):
            run.side_effect = [mock.Mock(returncode=1, stderr=b"no virtual keyboard"),
                               mock.Mock(returncode=0, stderr=b"")]
            p = Paster(Settings(paste_tool="auto"))
            p._ensure_ydotoold = lambda: True
            self.assertEqual(p.candidates(), ["wtype", "ydotool", "xdotool"])
            self.assertTrue(p.paste())
            self.assertEqual(run.call_args_list[1].args[0][0], "ydotool")
            self.assertIn("YDOTOOL_SOCKET", run.call_args_list[1].kwargs["env"])


class SettingsPersistenceTests(unittest.TestCase):
    def test_save_load_and_bad_values(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"DITTO_CONFIG": f"{d}/s.json"}):
            self.assertEqual(Settings.load().max_clips, 500)
            s = Settings(max_clips=7, auto_paste=False)
            s.save()
            self.assertEqual(Settings.load().max_clips, 7)
            Path(f"{d}/s.json").write_text('{"max_clips": "oops", "unknown": 1, "expire_days": 3}')
            loaded = Settings.load()
            self.assertEqual((loaded.max_clips, loaded.expire_days), (500, 3))
            Path(f"{d}/s.json").write_text("not json")
            self.assertEqual(Settings.load().max_clips, 500)


if __name__ == "__main__":
    unittest.main()
