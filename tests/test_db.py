import sqlite3
import struct
import tempfile
import time
import unittest
from pathlib import Path

from ditto_linux import formats as fmt
from ditto_linux.db import NO_PARENT, ClipStore, compute_crc


class FormatTests(unittest.TestCase):
    def test_unicode_text_roundtrip(self):
        s = "héllo ✓ 日本"
        enc = fmt.encode_unicode_text(s)
        self.assertTrue(enc.endswith(b"\x00\x00"))
        self.assertEqual(fmt.decode_unicode_text(enc), s)

    def test_cf_html_roundtrip_and_offsets(self):
        html = "<b>bold</b> ü"
        data = fmt.encode_cf_html(html)
        self.assertTrue(data.startswith(b"Version:0.9"))
        self.assertEqual(fmt.decode_cf_html(data), html)
        head = data[:200].decode()
        start = int(head.split("StartHTML:")[1][:10])
        self.assertTrue(data[start:].startswith(b"<html>"))

    def test_plain_html_tolerated(self):
        self.assertEqual(fmt.decode_cf_html(b"<i>x</i>"), "<i>x</i>")

    def test_storage_names_match_windows_ditto(self):
        names = dict(fmt.to_storage({fmt.TEXT: b"a", fmt.HTML: b"<p>a</p>", fmt.PNG: b"\x89PNG"}))
        self.assertIn("CF_UNICODETEXT", names)
        self.assertIn("HTML Format", names)
        self.assertIn("PNG", names)

    def test_from_storage_windows_formats(self):
        dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"
        out = fmt.from_storage([("CF_TEXT", b"ansi\x00junk"), ("CF_DIB", dib), ("CF_HDROP", b"x")])
        self.assertEqual(out[fmt.TEXT], b"ansi")
        self.assertTrue(out[fmt.BMP].startswith(b"BM"))
        self.assertNotIn("CF_HDROP", out)

    def test_describe(self):
        self.assertEqual(fmt.describe({fmt.TEXT: b"x"}), "x")
        self.assertEqual(fmt.describe({fmt.PNG: b"1"}), "[Image]")
        self.assertEqual(fmt.describe({fmt.HTML: b"<b>hi</b>"}), "hi")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "Ditto.db"
        self.s = ClipStore(self.path)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_schema_matches_ditto(self):
        cols = [r[1] for r in self.s.conn.execute("PRAGMA table_info(Main)")]
        for c in ("lID", "lDate", "mText", "lDontAutoDelete", "CRC", "bIsGroup", "lParentID",
                  "QuickPasteText", "clipOrder", "clipGroupOrder", "stickyClipOrder", "lastPasteDate"):
            self.assertIn(c, cols)
        tables = {r[0] for r in self.s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"Main", "Data", "Types", "CopyBuffers", "MainDeletes"} <= tables)

    def test_add_and_order(self):
        a, _ = self.s.add_text("first")
        b, _ = self.s.add_text("second")
        self.assertEqual([c.id for c in self.s.list()], [b, a])
        self.assertEqual(self.s.get_formats(a), {fmt.TEXT: b"first"})

    def test_duplicate_moves_to_top(self):
        a, created = self.s.add_text("dup")
        self.s.add_text("other")
        a2, created2 = self.s.add_text("dup")
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(a, a2)
        self.assertEqual(self.s.list()[0].id, a)
        self.assertEqual(self.s.count(), 2)

    def test_duplicate_not_moved_when_disabled(self):
        a, _ = self.s.add_text("dup")
        b, _ = self.s.add_text("other")
        self.s.add_text("dup", move_duplicate_to_top=False)
        self.assertEqual(self.s.list()[0].id, b)

    def test_empty_clip_rejected(self):
        with self.assertRaises(ValueError):
            self.s.add_clip({fmt.TEXT: b""})

    def test_search_terms_and_escaping(self):
        self.s.add_text("apple banana")
        self.s.add_text("apple cherry")
        self.s.add_text("100% sure_thing")
        self.assertEqual(len(self.s.list(search="apple")), 2)
        self.assertEqual(len(self.s.list(search="apple cherry")), 1)
        self.assertEqual(len(self.s.list(search="APPLE")), 2)
        self.assertEqual(len(self.s.list(search="100%")), 1)
        self.assertEqual(len(self.s.list(search="e_b")), 0)
        self.assertEqual(len(self.s.list(search='"apple b"')), 1)
        self.assertEqual(len(self.s.list(search='unbalanced "quote')), 0)

    def test_search_description(self):
        a, _ = self.s.add_text("zzz")
        self.s.set_description(a, "my password hint")
        self.assertEqual([c.id for c in self.s.list(search="hint")], [a])
        self.assertEqual(self.s.get(a).title, "my password hint")

    def test_delete_cascades_data(self):
        a, _ = self.s.add_clip({fmt.TEXT: b"x", fmt.HTML: b"<b>x</b>"})
        self.assertEqual(self.s.delete([a]), 1)
        self.assertIsNone(self.s.get(a))
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM Data").fetchone()[0], 0)
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM MainDeletes").fetchone()[0], 0)

    def test_sticky_first(self):
        a, _ = self.s.add_text("a")
        b, _ = self.s.add_text("b")
        self.s.set_sticky(a, True)
        self.assertEqual(self.s.list()[0].id, a)
        self.assertTrue(self.s.get(a).sticky)
        self.s.set_sticky(a, False)
        self.assertEqual(self.s.list()[0].id, b)

    def test_groups(self):
        g = self.s.create_group("Work")
        a, _ = self.s.add_text("in group")
        b, _ = self.s.add_text("loose")
        self.s.move_to_group([a], g)
        self.assertEqual([c.id for c in self.s.list(g)], [a])
        top = [c.id for c in self.s.list()]
        self.assertIn(g, top)
        self.assertEqual(top[0], g, "groups are listed before clips")
        self.assertEqual([x.text for x in self.s.groups()], ["Work"])
        with self.assertRaises(ValueError):
            self.s.move_to_group([b], a)  # not a group
        self.s.rename_group(g, "Job")
        self.assertEqual(self.s.get(g).text, "Job")
        self.s.move_to_group([a], NO_PARENT)
        self.assertEqual(self.s.list(g), [])

    def test_delete_group_recursive(self):
        g = self.s.create_group("G")
        sub = self.s.create_group("Sub", g)
        a, _ = self.s.add_text("deep")
        self.s.move_to_group([a], sub)
        self.assertEqual(self.s.delete([g]), 3)
        self.assertEqual(self.s.count(include_groups=True), 0)

    def test_cleanup_max_and_protection(self):
        ids = [self.s.add_text(f"clip {i}")[0] for i in range(10)]
        self.s.set_never_delete(ids[0], True)
        self.s.set_sticky(ids[1], True)
        g = self.s.create_group("keep")
        self.s.move_to_group([ids[2]], g)
        removed = self.s.cleanup(max_clips=3)
        remaining = {c.id for c in self.s.list(limit=100) if not c.is_group}
        self.assertTrue({ids[0], ids[1], ids[2]} <= remaining)
        self.assertTrue({ids[9], ids[8], ids[7]} <= remaining)
        self.assertEqual(removed, 4)

    def test_cleanup_expire(self):
        old, _ = self.s.add_text("old")
        new, _ = self.s.add_text("new")
        self.s.conn.execute("UPDATE Main SET lDate = ? WHERE lID = ?", (int(time.time()) - 10 * 86400, old))
        self.assertEqual(self.s.cleanup(expire_days=5), 1)
        self.assertIsNone(self.s.get(old))
        self.assertIsNotNone(self.s.get(new))

    def test_clear_history(self):
        a, _ = self.s.add_text("a")
        b, _ = self.s.add_text("b")
        self.s.set_never_delete(b, True)
        g = self.s.create_group("g")
        self.assertEqual(self.s.clear_history(), 1)
        self.assertIsNone(self.s.get(a))
        self.assertIsNotNone(self.s.get(g))

    def test_update_text_and_paste_mark(self):
        a, _ = self.s.add_clip({fmt.TEXT: b"x", fmt.HTML: b"<b>x</b>"})
        b, _ = self.s.add_text("newer")
        self.s.update_text(a, "edited")
        self.assertEqual(self.s.get_formats(a), {fmt.TEXT: b"edited"})
        self.assertEqual(self.s.get(a).text, "edited")
        self.s.mark_pasted(a)
        self.assertEqual(self.s.list()[0].id, a)
        self.assertIsNotNone(self.s.get(a).last_paste)

    def test_export_import_roundtrip(self):
        g = self.s.create_group("G")
        a, _ = self.s.add_clip({fmt.TEXT: b"t", fmt.PNG: b"\x89PNGdata"})
        self.s.move_to_group([a], g)
        self.s.set_description(a, "desc")
        data = self.s.export_json()
        other = ClipStore(":memory:")
        self.assertEqual(other.import_json(data), 2)
        grp = other.groups()[0]
        inside = other.list(grp.id)
        self.assertEqual(len(inside), 1)
        self.assertEqual(other.get_formats(inside[0].id)[fmt.PNG], b"\x89PNGdata")
        self.assertEqual(inside[0].description, "desc")
        with self.assertRaises(ValueError):
            other.import_json({"format": "nope"})

    def test_opens_legacy_ditto_database(self):
        """A Windows Ditto DB from before clipOrder existed is upgraded and readable."""
        legacy = Path(self.tmp.name) / "legacy.db"
        c = sqlite3.connect(legacy)
        c.executescript("""
            CREATE TABLE Main(lID INTEGER PRIMARY KEY AUTOINCREMENT, lDate INTEGER, mText TEXT,
              lShortCut INTEGER, lDontAutoDelete INTEGER, CRC INTEGER, bIsGroup INTEGER,
              lParentID INTEGER, QuickPasteText TEXT);
            CREATE TABLE Data(lID INTEGER PRIMARY KEY AUTOINCREMENT, lParentID INTEGER,
              strClipBoardFormat TEXT, ooData BLOB);
        """)
        c.execute("INSERT INTO Main VALUES(1, 0, 'win clip', 0, 0, 0, 0, -1, '')")
        c.execute("INSERT INTO Data VALUES(1, 1, 'CF_UNICODETEXT', ?)", (fmt.encode_unicode_text("win clip"),))
        c.commit()
        c.close()
        s = ClipStore(legacy)
        self.assertEqual(s.get_formats(1), {fmt.TEXT: b"win clip"})
        self.assertEqual(s.list()[0].text, "win clip")
        s.add_text("linux clip")
        self.assertEqual(s.count(), 2)
        s.close()

    def test_crc_signed(self):
        crc = compute_crc({fmt.TEXT: b"x" * 1000})
        self.assertTrue(-(2 ** 31) <= crc < 2 ** 31)

    def test_concurrent_connections(self):
        other = ClipStore(self.path)
        v = self.s.data_version()
        other.add_text("from another process")
        self.assertNotEqual(self.s.data_version(), v)
        self.assertEqual(self.s.list()[0].text, "from another process")
        other.close()


class PinTests(unittest.TestCase):
    def setUp(self):
        self.s = ClipStore(":memory:")
        self.ids = [self.s.add_text(f"clip {i}")[0] for i in range(5)]

    def tearDown(self):
        self.s.close()

    def pinned(self, parent=NO_PARENT):
        return [c.id for c in self.s.list(parent) if c.sticky]

    def test_reorder_pins(self):
        for cid in self.ids[:4]:
            self.s.set_sticky(cid, True)
        a, b, c, d = self.ids[:4]
        self.assertEqual(self.pinned(), [d, c, b, a])         # newest pin first
        self.assertTrue(self.s.move_sticky(a, -1))
        self.assertEqual(self.pinned(), [d, c, a, b])
        self.assertTrue(self.s.move_sticky(d, 1))
        self.assertEqual(self.pinned(), [c, d, a, b])
        self.assertTrue(self.s.move_sticky(b, -10 ** 9))
        self.assertEqual(self.pinned(), [b, c, d, a])
        self.assertFalse(self.s.move_sticky(b, -1))            # already first
        self.assertFalse(self.s.move_sticky(self.ids[4], -1))  # not pinned
        self.assertEqual(self.s.list()[4].id, self.ids[4])     # unpinned clips after the pins

    def test_reorder_pins_inside_group(self):
        g = self.s.create_group("G")
        self.s.move_to_group(self.ids[:3], g)
        for cid in self.ids[:3]:
            self.s.set_sticky(cid, True)
        self.s.set_sticky(self.ids[4], True)                   # top-level pin is not affected
        self.s.move_sticky(self.ids[0], -10 ** 9)
        self.assertEqual(self.pinned(g), [self.ids[0], self.ids[2], self.ids[1]])
        self.assertIn(self.ids[4], self.pinned())

    def test_pin_with_title_and_hidden_content(self):
        cid, _ = self.s.add_text("hunter2-secret")
        self.s.pin(cid, title="Wifi password", hidden=True)
        clip = self.s.get(cid)
        self.assertTrue(clip.sticky and clip.hidden)
        self.assertEqual(clip.title, "Wifi password")
        self.assertEqual(self.s.list()[0].id, cid)
        self.assertEqual(self.s.get_formats(cid)[fmt.TEXT], b"hunter2-secret")  # pastes the real thing
        # searching finds it by title, never by its hidden content
        self.assertEqual([c.id for c in self.s.list(search="wifi")], [cid])
        self.assertEqual(self.s.list(search="hunter2"), [])
        self.s.set_description(cid, "")
        self.assertEqual(self.s.get(cid).title, clip.MASK)
        self.s.set_hidden(cid, False)
        self.assertEqual([c.id for c in self.s.list(search="hunter2")], [cid])

    def test_hidden_flag_removed_with_clip_and_survives_export(self):
        cid = self.ids[0]
        self.s.pin(cid, title="t", hidden=True)
        data = self.s.export_json()
        other = ClipStore(":memory:")
        other.import_json(data)
        c = other.list(search="t")[0]
        self.assertTrue(c.hidden and c.sticky)
        other.close()
        self.s.delete([cid])
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM ClipFlags").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
