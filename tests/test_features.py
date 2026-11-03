"""Multi-paste, Special Paste and ignoring apps."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*.warning=false")

from PyQt6.QtCore import QMimeData, Qt  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ditto_linux import formats as fmt, transforms  # noqa: E402
from ditto_linux.clipboard import ClipboardMonitor, ignore_patterns, ignored_by, source_name  # noqa: E402
from ditto_linux.config import Settings  # noqa: E402
from ditto_linux.db import ClipStore  # noqa: E402
from ditto_linux.ui.quickpaste import QuickPasteWindow  # noqa: E402
from tests.support import IsolatedEnv  # noqa: E402

app = QApplication.instance() or QApplication([])


class TransformTests(unittest.TestCase):
    def test_each_transform(self):
        t = transforms.apply
        self.assertEqual(t("upper", "Hello wörld"), "HELLO WÖRLD")
        self.assertEqual(t("lower", "Hello WÖRLD"), "hello wörld")
        self.assertEqual(t("title", "don't stop me-now 2day"), "Don't Stop Me-Now 2Day")
        self.assertEqual(t("sentence", "HELLO THERE. how are you?\nfine"), "Hello there. How are you?\nFine")
        self.assertEqual(t("invert", "aBc"), "AbC")
        self.assertEqual(t("trim", "  a  \n   b \n"), "a\nb")
        self.assertEqual(t("one_line", "line one\n  line two\n\nthree"), "line one line two three")
        self.assertEqual(t("squeeze", "a    b\n\n\n\nc\t\td  "), "a b\n\nc d")
        self.assertEqual(t("slug", "Héllo, World! 2026"), "hello-world-2026")
        self.assertEqual(t("", "same"), "same")
        with self.assertRaises(ValueError):
            t("nope", "x")
        self.assertEqual(len(transforms.TRANSFORMS), 9)


class IgnoreAppTests(unittest.TestCase):
    owner = {"pid": 42, "names": ["bitwarden", "electron"], "wm_class": ["bitwarden", "Bitwarden"]}

    def test_matching(self):
        self.assertEqual(ignore_patterns(" KeePassXC, bit* ,\n1password"), ["keepassxc", "bit*", "1password"])
        self.assertEqual(ignored_by(self.owner, ["bit*"]), "bit*")
        self.assertEqual(ignored_by(self.owner, ["bitwarden"]), "bitwarden")
        self.assertIsNone(ignored_by(self.owner, ["keepassxc"]))
        self.assertIsNone(ignored_by(None, ["bitwarden"]), "unknown source is never ignored")
        self.assertIsNone(ignored_by(self.owner, []))
        self.assertEqual(source_name(self.owner), "bitwarden")
        self.assertEqual(source_name(None), "")

    def test_monitor_skips_ignored_app_and_records_source(self):
        with tempfile.TemporaryDirectory() as d:
            store = ClipStore(Path(d) / "Ditto.db")
            owner = {"pid": 42, "names": ["bitwarden"], "wm_class": []}
            mon = ClipboardMonitor(store, Settings(ignore_apps="bitwarden"), owner_lookup=lambda: owner)
            md = QMimeData()
            md.setText("secret password")
            QGuiApplication.clipboard().setMimeData(md)
            self.assertIsNone(mon.capture())
            self.assertEqual(store.count(), 0)
            owner["names"] = ["gedit"]
            md = QMimeData()
            md.setText("normal text")
            QGuiApplication.clipboard().setMimeData(md)
            cid = mon.capture()
            self.assertEqual(store.get(cid).source, "gedit")
            self.assertEqual(store.recent_sources(), ["gedit"])
            store.set_hidden(cid, True)
            self.assertEqual(store.get(cid).source, "gedit", "hiding keeps the source")
            store.set_hidden(cid, False)
            self.assertEqual((store.get(cid).hidden, store.get(cid).source), (False, "gedit"))
            mon.owner_lookup = lambda: {"pid": os.getpid(), "names": ["us"], "wm_class": []}
            md = QMimeData()
            md.setText("written by Ditto itself")
            QGuiApplication.clipboard().setMimeData(md)
            self.assertIsNone(mon.capture())
            store.close()


class MultiAndSpecialPasteWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ClipStore(Path(self.tmp.name) / "Ditto.db")
        self.ids = [self.store.add_text(t)[0] for t in ("one", "two", "three")]
        self.w = QuickPasteWindow(self.store)
        self.single, self.many = [], []
        self.w.pasteRequested.connect(lambda cid, plain: self.single.append((cid, plain)))
        self.w.pasteManyRequested.connect(lambda ids, plain, tr: self.many.append((ids, plain, tr)))
        self.w.show_at_cursor()
        QTest.qWaitForWindowExposed(self.w)

    def tearDown(self):
        self.w.close()
        self.w.deleteLater()
        self.store.close()
        self.tmp.cleanup()

    def test_select_several_and_enter(self):
        self.w.list.setFocus()
        self.w.list.setCurrentRow(0)
        QTest.keyClick(self.w.list, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyClick(self.w.list, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
        self.assertIn("3 selected", self.w.status.text())
        QTest.keyClick(self.w.list, Qt.Key.Key_Return)
        self.assertEqual(self.many, [(self.ids[::-1], False, "")], "list order: newest first")
        self.assertEqual(self.single, [])

    def test_single_selection_still_pastes_normally(self):
        self.w.list.setCurrentRow(1)
        QTest.keyClick(self.w.list, Qt.Key.Key_Return)
        self.assertEqual((self.single, self.many), ([(self.ids[1], False)], []))

    def test_special_paste_menu_and_action(self):
        self.w.list.setCurrentRow(0)
        menu = self.w._special_paste_menu()
        labels = [a.text().replace("&", "") for a in menu.actions()]
        self.assertEqual(labels[0], "UPPERCASE")
        self.assertEqual(len(labels), len(transforms.TRANSFORMS))
        with mock.patch("ditto_linux.ui.quickpaste.QMenu.exec") as ex:
            QTest.keyClick(self.w.list, Qt.Key.Key_V,
                           Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        ex.assert_called_once()
        menu.actions()[0].trigger()
        self.assertEqual(self.many, [([self.ids[2]], True, "upper")])

    def test_properties_show_source(self):
        cid, _ = self.store.add_clip({fmt.TEXT: b"from app"}, source="gedit")
        self.w.refresh()
        self.w.list.setCurrentRow(0)
        with mock.patch("ditto_linux.ui.quickpaste.QMessageBox.exec", return_value=0):
            self.w.show_properties()
        self.assertIn(("Copied from", "gedit"), self.w.last_properties)


class DaemonPasteManyTests(unittest.TestCase):
    def setUp(self):
        from ditto_linux.app import DittoDaemon
        self.iso = IsolatedEnv()
        self.addCleanup(self.iso.cleanup)
        p = mock.patch.dict(os.environ, self.iso.env)
        p.start()
        self.addCleanup(p.stop)
        self.settings = Settings.load()
        self.d = DittoDaemon(app, str(self.iso.db), self.settings, self.iso.socket, show_tray=False, argv=[])
        self.addCleanup(self.d.close)
        self.a = self.d.store.add_text("first line")[0]
        self.b = self.d.store.add_text("  second line ")[0]

    def test_joined_and_transformed(self):
        self.assertTrue(self.d.paste_many([self.a, self.b], transform="upper", simulate=False))
        self.assertEqual(app.clipboard().text(), "FIRST LINE\n  SECOND LINE ")
        self.settings.multi_paste_separator = "comma"
        self.d.paste_many([self.a, self.b], transform="trim", simulate=False)
        self.assertEqual(app.clipboard().text(), "first line,   second line")
        self.assertIsNotNone(self.d.store.get(self.a).last_paste)
        self.assertIsNone(self.d.monitor.capture(), "our own clipboard write is not recorded again")
        self.assertEqual(self.d.store.count(), 2)

    def test_single_without_transform_is_a_normal_paste(self):
        with mock.patch.object(self.d, "paste", return_value=True) as paste:
            self.d.paste_many([self.a])
        paste.assert_called_once_with(self.a, False, None)

    def test_ipc(self):
        r = self.d.handle({"cmd": "copy", "id": self.a, "ids": [self.b, self.a], "transform": "one_line"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(app.clipboard().text(), "second line first line")
        self.assertFalse(self.d.handle({"cmd": "copy", "ids": [999]})["ok"])
        self.assertIn("unknown transform", self.d.handle({"cmd": "copy", "ids": [self.a], "transform": "x"})["error"])
        img = self.d.store.add_clip({fmt.PNG: b"\x89PNG fake"})[0]
        self.assertFalse(self.d.handle({"cmd": "copy", "ids": [img, img], "transform": "upper"})["ok"])

    def test_settings_dialog_fields(self):
        from ditto_linux.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self.settings, "db", recent_apps=["gedit", "code"])
        self.assertIn("gedit, code", dlg.recent_apps.text())
        dlg.ignore.setText(" keepassxc, bit* ")
        dlg.separator.setCurrentIndex(1)
        s = dlg.apply()
        self.assertEqual((s.ignore_apps, s.multi_paste_separator), ("keepassxc, bit*", "space"))


if __name__ == "__main__":
    unittest.main()
