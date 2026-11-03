"""End-to-end tests: real daemon process, real X11 clipboard (private X server), real CLI.

Nothing here touches the user's clipboard, database or settings.
"""
import os
import subprocess
import sys
import time
import unittest

from tests.support import HELPER, IsolatedEnv, PrivateXServer, wait_for, x11_env

XSERVER = PrivateXServer()


def setUpModule():
    if not PrivateXServer.available():
        raise unittest.SkipTest("needs Xwayland (in a Wayland session) or Xvfb")
    XSERVER.start()


def tearDownModule():
    XSERVER.stop()


class DaemonE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iso = IsolatedEnv()
        cls.env = x11_env(cls.iso.env, XSERVER.display)
        cls.iso.env = cls.env
        cls.log = open(cls.iso.tmp / "daemon.log", "w+")
        cls.daemon = subprocess.Popen([sys.executable, "-m", "ditto_linux", "-v", "daemon", "--no-tray"],
                                      env=cls.env, stdout=cls.log, stderr=subprocess.STDOUT)
        wait_for(lambda: cls.iso.cli("status", check=False).returncode == 0 or cls.daemon.poll() is not None,
                 timeout=20, what="daemon to answer")
        if cls.daemon.poll() is not None:
            raise RuntimeError("daemon died:\n" + cls.daemon_log())
        cls.owners = []

    @classmethod
    def tearDownClass(cls):
        for p in cls.owners:
            if p.poll() is None:
                p.kill()
        if cls.daemon.poll() is None:
            cls.iso.cli("quit", check=False)
            try:
                cls.daemon.wait(10)
            except subprocess.TimeoutExpired:
                cls.daemon.kill()
        cls.log.close()
        cls.iso.cleanup()

    @classmethod
    def daemon_log(cls) -> str:
        cls.log.flush()
        cls.log.seek(0)
        return cls.log.read()

    def tearDown(self):
        self.assertIsNone(self.daemon.poll(), "daemon crashed:\n" + self.daemon_log())
        self.assertNotIn("Traceback", self.daemon_log(), "daemon logged an exception")

    # -------------------------------------------------------------- helpers
    def own(self, *args):
        """Start a separate process that owns the clipboard (an 'other application')."""
        p = subprocess.Popen([sys.executable, str(HELPER), *args], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.owners.append(p)
        line = p.stdout.readline()
        self.assertEqual(line.strip(), "READY", p.stderr.read() if p.poll() is not None else line)
        return p

    def read_clipboard(self, *args) -> str:
        r = subprocess.run([sys.executable, str(HELPER), "read", *args], env=self.env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def clips(self, *extra):
        return self.iso.cli_json("list", "--json", "-n", "100", *extra)

    def wait_top(self, text, timeout=10):
        return wait_for(lambda: (self.clips() or [{}])[0].get("text") == text and self.clips()[0],
                        timeout=timeout, what=f"clip {text!r} at top")

    # -------------------------------------------------------------- tests
    def test_01_status(self):
        st = self.iso.cli_json("status")
        self.assertEqual(st["platform"], "xcb")
        self.assertFalse(st["paused"])
        self.assertEqual(st["path"], str(self.iso.db))

    def test_02_captures_text_from_other_app(self):
        self.own("set-text", "Hello from another app")
        clip = self.wait_top("Hello from another app")
        out = self.iso.cli("get", clip["id"]).stdout
        self.assertEqual(out, "Hello from another app")

    def test_03_captures_html_and_unicode(self):
        self.own("set-html", "<b>Bold</b> ünïcødé ✓", "Bold ünïcødé ✓")
        clip = self.wait_top("Bold ünïcødé ✓")
        html = self.iso.cli("get", clip["id"], "-f", "text/html").stdout
        self.assertIn("<b>Bold</b>", html)

    def test_04_captures_image(self):
        self.own("set-image", "40", "30")
        clip = self.wait_top("[Image]")
        r = subprocess.run([sys.executable, "-m", "ditto_linux", "get", str(clip["id"]), "-f", "image/png"],
                           env=self.env, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith(b"\x89PNG"), r.stdout[:16])

    def test_05_duplicate_copy_moves_to_top_without_new_entry(self):
        self.own("set-text", "dup-entry")
        first = self.wait_top("dup-entry")
        self.own("set-text", "something else")
        self.wait_top("something else")
        before = len(self.clips())
        self.own("set-text", "dup-entry")
        again = self.wait_top("dup-entry")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.clips()), before)

    def test_06_copy_from_history_sets_clipboard_and_is_not_rerecorded(self):
        self.own("set-text", "history item A")
        a = self.wait_top("history item A")
        self.own("set-text", "history item B")
        self.wait_top("history item B")
        count = len(self.clips())
        self.iso.cli("copy", a["id"])
        wait_for(lambda: "history item A" in self.read_clipboard(), what="clipboard to contain clip A")
        time.sleep(0.5)
        self.assertEqual(len(self.clips()), count, "pasting from history must not create a new clip")
        self.assertEqual(self.clips()[0]["id"], a["id"], "pasted clip moves to the top")

    def test_07_plain_text_paste_strips_html(self):
        self.own("set-html", "<i>rich</i> text", "rich text")
        clip = self.wait_top("rich text")
        self.iso.cli("copy", clip["id"])
        wait_for(lambda: "text/html" in self.read_clipboard(), what="html on clipboard")
        self.iso.cli("copy", clip["id"], "--plain")
        out = wait_for(lambda: (lambda o: "text/html" not in o.splitlines()[0] and o)(self.read_clipboard()),
                       what="plain-text-only clipboard")
        self.assertTrue(out.rstrip().endswith("rich text"))
        time.sleep(0.4)
        self.assertEqual(sum(c["text"] == "rich text" for c in self.clips()), 1)

    def test_08_image_roundtrip_to_clipboard(self):
        self.own("set-image", "17", "9")
        clip = self.wait_top("[Image]")
        self.iso.cli("copy", clip["id"])
        wait_for(lambda: self.read_clipboard("image").strip() == "IMAGE 17x9", what="image back on clipboard")

    def test_09_pause_and_resume(self):
        self.iso.cli("pause")
        self.assertTrue(self.iso.cli_json("status")["paused"])
        self.own("set-text", "secret while paused")
        time.sleep(1.0)
        self.assertNotIn("secret while paused", [c["text"] for c in self.clips()])
        self.iso.cli("resume")
        self.own("set-text", "after resume")
        self.wait_top("after resume")

    def test_10_password_manager_hint_is_ignored(self):
        self.own("set-text", "hunter2", "--secret")
        time.sleep(1.0)
        self.assertNotIn("hunter2", [c["text"] for c in self.clips()])

    def test_11_cli_changes_visible_to_daemon(self):
        cid = int(self.iso.cli("add", "added via cli").stdout)
        self.iso.cli("copy", cid)
        wait_for(lambda: "added via cli" in self.read_clipboard(), what="cli clip on clipboard")
        self.iso.cli("delete", cid)
        r = self.iso.cli("copy", cid, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no clip", r.stderr)

    def test_12_window_show_toggle_hide(self):
        self.iso.cli("show")
        wait_for(lambda: self.iso.cli_json("status")["visible"], what="window visible")
        self.iso.cli("hide")
        wait_for(lambda: not self.iso.cli_json("status")["visible"], what="window hidden")
        self.assertTrue(self.iso.cli_json("toggle")["visible"])
        self.iso.cli("hide")

    def test_13_groups_and_flags_through_cli(self):
        gid = int(self.iso.cli("group", "create", "Snippets").stdout)
        cid = int(self.iso.cli("add", "grouped snippet").stdout)
        self.iso.cli("group", "move", gid, cid)
        inside = self.clips("-g", str(gid))
        self.assertEqual([c["id"] for c in inside], [cid])
        self.iso.cli("sticky", cid)
        self.iso.cli("never-delete", cid)
        clip = [c for c in self.clips() if c["id"] == cid][0]
        self.assertTrue(clip["never_delete"])

    def test_14_export_import(self):
        out = self.iso.tmp / "export.json"
        self.iso.cli("export", out)
        other = IsolatedEnv()
        try:
            r = other.cli("import", out)
            self.assertIn("imported", r.stdout)
            texts = [c["text"] for c in other.cli_json("list", "--json", "-n", "500")]
            self.assertIn("Hello from another app", texts)
        finally:
            other.cleanup()

    def test_15_unknown_ipc_command_is_reported(self):
        from ditto_linux.ipc import request
        reply = request("bogus", path=self.iso.socket)
        self.assertFalse(reply["ok"])
        self.assertIn("unknown command", reply["error"])

    def test_16_second_instance_refuses(self):
        r = subprocess.run([sys.executable, "-m", "ditto_linux", "daemon", "--no-tray"], env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already running", r.stderr)
        wait_for(lambda: self.iso.cli_json("status")["visible"], what="second launch shows window")
        self.iso.cli("hide")

    def test_99_quit_cleans_up(self):
        self.iso.cli("quit")
        self.assertEqual(self.daemon.wait(10), 0, self.daemon_log())
        self.assertFalse(os.path.exists(self.iso.socket))
        r = self.iso.cli("status", check=False)
        self.assertEqual(r.returncode, 2)
        # restart so tearDown/tearDownClass checks still hold
        type(self).daemon = subprocess.Popen([sys.executable, "-m", "ditto_linux", "daemon", "--no-tray"],
                                             env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        wait_for(lambda: self.iso.cli("status", check=False).returncode == 0, timeout=20, what="restart")
        self.assertGreater(self.iso.cli_json("status")["clips"], 5, "history persists across restarts")


class HotkeyCycleE2E(unittest.TestCase):
    """Real daemon + real X keyboard state: Ctrl+` , ` again while Ctrl is held, release Ctrl -> paste.

    Rootless Xwayland ignores XTest for keyboard state, so this runs on a nested Xephyr server.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        if not shutil.which("Xephyr"):
            raise unittest.SkipTest("needs Xephyr to emulate holding Ctrl")
        for n in range(260, 300):
            if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
                break
        cls.display = f":{n}"
        cls.xephyr = subprocess.Popen(["Xephyr", cls.display, "-screen", "640x480", "-nolisten", "tcp"],
                                      env={**os.environ, "DISPLAY": XSERVER.display},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(lambda: os.path.exists(f"/tmp/.X11-unix/X{n}") or cls.xephyr.poll() is not None,
                 timeout=15, what="Xephyr socket")
        if cls.xephyr.poll() is not None:
            raise unittest.SkipTest("Xephyr did not start")
        time.sleep(0.3)
        cls.iso = IsolatedEnv()
        cls.env = cls.iso.env = x11_env(cls.iso.env, cls.display)
        cls.log = open(cls.iso.tmp / "daemon.log", "w+")
        cls.daemon = subprocess.Popen([sys.executable, "-m", "ditto_linux", "-v", "daemon", "--no-tray"],
                                      env=cls.env, stdout=cls.log, stderr=subprocess.STDOUT)
        wait_for(lambda: cls.iso.cli("status", check=False).returncode == 0 or cls.daemon.poll() is not None,
                 timeout=20, what="daemon to answer")
        cls.keys = subprocess.Popen([sys.executable, str(HELPER.parent / "xkeys.py")], env=cls.env, text=True,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert cls.keys.stdout.readline().strip() == "READY"

    @classmethod
    def tearDownClass(cls):
        cls.keys.stdin.write("quit\n")
        cls.keys.stdin.flush()
        cls.keys.wait(5)
        cls.iso.cli("quit", check=False)
        try:
            cls.daemon.wait(10)
        except subprocess.TimeoutExpired:
            cls.daemon.kill()
        cls.xephyr.terminate()
        cls.xephyr.wait(5)
        cls.log.close()
        cls.iso.cleanup()

    def tearDown(self):
        self.log.flush()
        self.log.seek(0)
        out = self.log.read()
        self.assertIsNone(self.daemon.poll(), "daemon crashed:\n" + out)
        self.assertNotIn("Traceback", out)

    def key(self, cmd):
        self.keys.stdin.write(cmd + "\n")
        self.keys.stdin.flush()
        self.assertEqual(self.keys.stdout.readline().strip(), "OK")

    def visible(self):
        return self.iso.cli_json("status")["visible"]

    def _clip(self):
        from ditto_linux.ipc import request
        return request("clipboard", path=self.iso.socket)["text"]

    def test_double_press_then_release_pastes_second_clip(self):
        for text in ("cycle one", "cycle two", "cycle three"):
            self.iso.cli("add", text)
        self.key("down Control_L")
        self.assertTrue(self.iso.cli_json("hotkey")["visible"])
        time.sleep(0.3)
        self.iso.cli("hotkey")                         # ` again while Ctrl is held
        time.sleep(0.4)
        self.assertTrue(self.visible(), "must not paste while Ctrl is held")
        self.key("up Control_L")
        wait_for(lambda: not self.visible(), what="window closes on Ctrl release")
        wait_for(lambda: self._clip() == "cycle two", what="second clip pasted")

    def test_single_press_and_release_only_opens(self):
        self.key("down Control_L")
        self.iso.cli("hotkey")
        self.key("up Control_L")
        time.sleep(0.4)
        self.assertTrue(self.visible())
        self.iso.cli("hide")


if __name__ == "__main__":
    unittest.main()
