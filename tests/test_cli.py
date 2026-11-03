"""CLI behaviour without a running daemon (exit codes, error messages, data commands)."""
import json
import unittest

from tests.support import IsolatedEnv


class CliTests(unittest.TestCase):
    def setUp(self):
        self.iso = IsolatedEnv()

    def tearDown(self):
        self.iso.cleanup()

    def test_version_and_help(self):
        self.assertIn("ditto-linux", self.iso.cli("--version").stdout)
        self.assertIn("paste", self.iso.cli("--help").stdout)

    def test_daemon_commands_fail_cleanly_without_daemon(self):
        for cmd in (["show"], ["status"], ["copy", "1"], ["quit"]):
            r = self.iso.cli(*cmd, check=False)
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn("not running", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_add_list_get_delete(self):
        a = int(self.iso.cli("add", "first clip").stdout)
        b = int(self.iso.cli("add", input="multi\nline\n").stdout)
        rows = self.iso.cli_json("list", "--json")
        self.assertEqual([r["id"] for r in rows], [b, a])
        self.assertEqual(self.iso.cli("get", b).stdout, "multi\nline\n")
        self.assertIn("first clip", self.iso.cli("list").stdout)
        self.assertEqual(len(self.iso.cli_json("list", "--json", "-s", "multi")), 1)
        self.assertIn("deleted 1", self.iso.cli("delete", a).stdout)
        r = self.iso.cli("delete", a, check=False)
        self.assertEqual(r.returncode, 1)

    def test_errors_are_friendly(self):
        r = self.iso.cli("get", 999, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no clip", r.stderr)
        cid = self.iso.cli("add", "x").stdout.strip()
        r = self.iso.cli("get", cid, "-f", "image/png", check=False)
        self.assertIn("has no image/png", r.stderr)
        r = self.iso.cli("group", "move", cid, cid, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a group", r.stderr)
        r = self.iso.cli("add", input="", check=False)
        self.assertEqual(r.returncode, 1)
        for res in (r,):
            self.assertNotIn("Traceback", res.stderr)

    def test_groups_flags_cleanup_stats_export(self):
        g = self.iso.cli("group", "create", "Work").stdout.strip()
        ids = [self.iso.cli("add", f"c{i}").stdout.strip() for i in range(6)]
        self.iso.cli("group", "move", g, ids[0])
        self.assertIn("Work", self.iso.cli("group", "list").stdout)
        self.iso.cli("never-delete", ids[1])
        self.iso.cli("sticky", ids[2])
        self.assertIn("deleted 1", self.iso.cli("cleanup", "--max", "2", "--vacuum").stdout)
        stats = self.iso.cli_json("stats")
        self.assertEqual((stats["clips"], stats["groups"]), (5, 1))
        export = json.loads(self.iso.cli("export").stdout)
        self.assertEqual(export["format"], "ditto-linux-export")
        self.iso.cli("sticky", ids[2], "--off")
        self.assertIn("deleted", self.iso.cli("clear").stdout)
        self.assertEqual(self.iso.cli_json("stats")["clips"], 2)  # grouped + never-delete survive


    def test_pin_title_hide(self):
        a = self.iso.cli("add", "s3cr3t-token").stdout.strip()
        b = self.iso.cli("add", "plain").stdout.strip()
        self.iso.cli("pin", a, "--title", "API token", "--hide")
        out = self.iso.cli("list").stdout
        self.assertIn("API token", out)
        self.assertNotIn("s3cr3t", out)
        self.iso.cli("pin", b)
        self.assertEqual(self.iso.cli_json("list", "--json")[0]["id"], int(b))
        self.iso.cli("pin", a, "--up", "1")
        rows = self.iso.cli_json("list", "--json")
        self.assertEqual(rows[0]["id"], int(a))
        self.assertTrue(rows[0]["hidden"] and rows[0]["sticky"])
        self.iso.cli("mask", a, "--off")
        self.assertIn("API token", self.iso.cli("list", "-s", "s3cr3t").stdout)  # searchable again
        self.iso.cli("pin", a, "--off")
        self.assertFalse(self.iso.cli_json("list", "--json")[1]["sticky"])


if __name__ == "__main__":
    unittest.main()
