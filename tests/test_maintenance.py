"""Auto-update, global shortcut checks/repairs, popups and the log file."""
import functools
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*.warning=false")

from PyQt6.QtGui import QKeySequence  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ditto_linux import __version__, desktop, update  # noqa: E402
from ditto_linux.config import Settings  # noqa: E402
from tests.support import ROOT, IsolatedEnv  # noqa: E402

app = QApplication.instance() or QApplication([])


# ====================================================================== fake GNOME settings
class FakeGnome:
    """In-memory gsettings: values are stored in gsettings' printed (GVariant text) form."""

    def __init__(self, builtin=None, custom=None, extensions=()):
        self.values = {}
        for (schema, key), value in (builtin or {}).items():
            self.values[(schema, key)] = str(value)
        paths = []
        for path, (name, binding, command) in (custom or {}).items():
            schema = f"{desktop.MEDIA_KEYS}.custom-keybinding:{path}"
            self.values[(schema, "name")] = repr(name)
            self.values[(schema, "binding")] = repr(binding)
            self.values[(schema, "command")] = repr(command)
            paths.append(path)
        self.values[(desktop.MEDIA_KEYS, "custom-keybindings")] = str(paths) if paths else "@as []"
        self.values[("org.gnome.shell", "enabled-extensions")] = str(list(extensions)) if extensions else "@as []"

    def gs(self, *args):
        if args[0] == "get":
            return self.values.get((args[1], args[2]), "''")
        if args[0] == "set":
            v = args[3]
            self.values[(args[1], args[2])] = v if v.startswith(("[", "@as")) else repr(v)
            return ""
        if args[0] == "list-recursively":
            return "\n".join(f"{s} {k} {v}" for (s, k), v in self.values.items() if s == args[1])
        raise AssertionError(args)

    def get(self, schema, key):
        return desktop._parse_list(self.values[(schema, key)]) if self.values.get((schema, key), "").startswith(
            ("[", "@as")) else self.values.get((schema, key), "''").strip("'")

    def ours(self, key):
        return self.get(desktop.OUR_SCHEMA, key)


class ShortcutTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = Path(self.tmp.name) / "ditto-linux"
        self.exe.write_text("#!/bin/sh\n")
        self.exe.chmod(0o755)
        self.settings_file = Path(self.tmp.name) / "s.json"
        patches = [
            mock.patch.dict(os.environ, {"DITTO_CONFIG": str(self.settings_file), "XDG_CURRENT_DESKTOP": "GNOME"}),
            mock.patch("ditto_linux.desktop.supported", return_value=True),
            mock.patch("ditto_linux.desktop.expected_command", return_value=f"{self.exe} hotkey"),
            mock.patch("ditto_linux.desktop.shutil.which", return_value=None),   # no pgrep -> no CopyQ check
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("DITTO_NO_DESKTOP_INTEGRATION", None)
        self.addCleanup(self.tmp.cleanup)

    def use(self, fake: FakeGnome):
        p = mock.patch("ditto_linux.desktop._gs", side_effect=fake.gs)
        p.start()
        self.addCleanup(p.stop)
        return fake


class AcceleratorTests(unittest.TestCase):
    def test_normalize_aliases(self):
        self.assertEqual(desktop.normalize("<Control>grave"), desktop.normalize("<Primary>Above_Tab"))
        self.assertEqual(desktop.normalize("<Shift><Ctrl>v"), desktop.normalize("<Control><Shift>V"))
        self.assertNotEqual(desktop.normalize("<Alt>grave"), desktop.normalize("<Control>grave"))

    def test_pretty_and_from_portable(self):
        self.assertEqual(desktop.pretty("<Control>grave"), "Ctrl+`")
        self.assertEqual(desktop.pretty("<Control><Alt>v"), "Ctrl+Alt+V")
        self.assertEqual(desktop.from_portable("Ctrl+`"), "<Control>grave")
        self.assertEqual(desktop.from_portable("Ctrl+Alt+V"), "<Control><Alt>v")
        self.assertEqual(desktop.from_portable("Meta+Space"), "<Super>space")
        self.assertEqual(desktop.from_portable("<Super>v"), "<Super>v")
        for bad in ("", "V", "Foo+V"):
            with self.assertRaises(ValueError):
                desktop.from_portable(bad)


class ShortcutCheckTests(ShortcutTestBase):
    def test_fresh_install_registers(self):
        g = self.use(FakeGnome(custom={"/x/": ("Screenshot", "<Super>s", "shot")}))
        s = Settings(gnome_shortcut_done=False)
        self.assertEqual(desktop.ensure_gnome_shortcut(s), "registered")
        self.assertEqual(g.ours("binding"), "<Control>grave")
        self.assertEqual(g.ours("command"), f"{self.exe} hotkey")
        self.assertIn(desktop.KEY_PATH, g.get(desktop.MEDIA_KEYS, "custom-keybindings"))
        self.assertIn("/x/", g.get(desktop.MEDIA_KEYS, "custom-keybindings"))
        self.assertEqual(desktop.check(s).state, "ok")
        self.assertTrue(Settings.load().gnome_shortcut_done)

    def test_builtin_conflict_is_reported_then_taken_over(self):
        wm = "org.gnome.desktop.wm.keybindings"
        g = self.use(FakeGnome(builtin={(wm, "switch-group"): ["<Primary>Above_Tab", "<Alt>Above_Tab"]}))
        s = Settings()
        self.assertEqual(desktop.ensure_gnome_shortcut(s), "taken")
        self.assertEqual(g.ours("binding"), "", "nothing changed before the user agreed")
        rep = desktop.check(s)
        self.assertEqual(rep.state, "missing")
        self.assertEqual([c.kind for c in rep.conflicts], ["builtin"])
        after = desktop.resolve(s, take_over=True)
        self.assertEqual(after.state, "ok")
        self.assertEqual(g.get(wm, "switch-group"), ["<Alt>Above_Tab"], "only our key is removed")
        self.assertEqual(g.ours("binding"), "<Control>grave")

    def test_custom_and_extension_conflicts_taken_over(self):
        g = self.use(FakeGnome(custom={"/copyq/": ("CopyQ", "<Ctrl>grave", "copyq show")},
                               extensions=["clipboard-indicator@tudmotu.com", "dock@ubuntu.com"]))
        s = Settings(gnome_shortcut_done=False)
        rep = desktop.check(s)
        self.assertEqual(sorted(c.kind for c in rep.conflicts), ["custom", "extension"])
        self.assertEqual(desktop.ensure_gnome_shortcut(s), "taken")
        self.assertEqual(desktop.resolve(s, take_over=True).state, "ok")
        self.assertEqual(g.get(f"{desktop.MEDIA_KEYS}.custom-keybinding:/copyq/", "binding"), "")
        self.assertEqual(g.get("org.gnome.shell", "enabled-extensions"), ["dock@ubuntu.com"])

    def test_conflict_with_existing_binding(self):
        self.use(FakeGnome(custom={desktop.KEY_PATH: ("Ditto", "<Control>grave", f"{self.exe} hotkey"),
                                   "/other/": ("Other", "<Control>grave", "other")}))
        s = Settings(gnome_shortcut_done=True)
        self.assertEqual(desktop.check(s).state, "conflict")
        self.assertEqual(desktop.ensure_gnome_shortcut(s), "taken")

    def test_broken_command_is_repaired(self):
        g = self.use(FakeGnome(custom={desktop.KEY_PATH: ("Ditto", "<Control>grave",
                                                          "/home/gone/.local/bin/ditto-linux hotkey")}))
        s = Settings(gnome_shortcut_done=True)
        self.assertEqual(desktop.check(s).state, "broken")
        self.assertEqual(desktop.ensure_gnome_shortcut(s), "upgraded")
        self.assertEqual(g.ours("command"), f"{self.exe} hotkey")

    def test_old_show_command_is_repaired(self):
        g = self.use(FakeGnome(custom={desktop.KEY_PATH: ("Ditto", "<Control>grave", f"{self.exe} show")}))
        self.assertEqual(desktop.ensure_gnome_shortcut(Settings(gnome_shortcut_done=True)), "upgraded")
        self.assertEqual(g.ours("command"), f"{self.exe} hotkey")

    def test_key_changed_in_gnome_settings_is_adopted(self):
        self.use(FakeGnome(custom={desktop.KEY_PATH: ("Ditto", "<Super>v", f"{self.exe} hotkey")}))
        s = Settings(gnome_shortcut_done=True)
        self.assertEqual(desktop.ensure_gnome_shortcut(s), "adopted")
        self.assertEqual(Settings.load().hotkey, "<Super>v")

    def test_removed_after_registration_asks(self):
        g = self.use(FakeGnome())
        self.assertEqual(desktop.ensure_gnome_shortcut(Settings(gnome_shortcut_done=True)), "missing")
        self.assertEqual(g.ours("binding"), "")

    def test_move_to_another_key(self):
        g = self.use(FakeGnome(custom={desktop.KEY_PATH: ("Ditto", "<Control>grave", f"{self.exe} hotkey")}))
        s = Settings()
        rep = desktop.resolve(s, "<Control><Alt>v")
        self.assertEqual((rep.state, g.ours("binding")), ("ok", "<Control><Alt>v"))
        self.assertEqual(Settings.load().hotkey, "<Control><Alt>v")

    def test_disabled_or_not_gnome(self):
        self.use(FakeGnome())
        with mock.patch.dict(os.environ, {"DITTO_NO_DESKTOP_INTEGRATION": "1"}):
            self.assertEqual(desktop.ensure_gnome_shortcut(Settings()), "skipped")
        with mock.patch("ditto_linux.desktop.supported", return_value=False):
            self.assertEqual(desktop.check(Settings()).state, "unsupported")


# ====================================================================== update server
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class UpdateServer:
    """Serves a fake GitHub 'latest release' API response plus its files."""

    def __init__(self, version="9.0.0", files=None, bad_sum=False, no_sums=False):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=root))
        base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        files = files or {f"ditto-linux_{version}_all.deb": b"fake deb",
                          f"ditto-linux-{version}.tar.gz": make_tarball(version)}
        sums = ""
        for name, data in files.items():
            (root / name).write_bytes(data)
            digest = hashlib.sha256(data).hexdigest() if not bad_sum else "0" * 64
            sums += f"{digest}  ./{name}\n"
        assets = list(files)
        if not no_sums:
            (root / "SHA256SUMS").write_text(sums)
            assets.append("SHA256SUMS")
        release = {"tag_name": f"v{version}", "body": "## What's new\n- things", "html_url": f"{base}/release",
                   "assets": [{"name": n, "browser_download_url": f"{base}/{n}"} for n in assets]}
        (root / "latest.json").write_text(json.dumps(release))
        self.url = f"{base}/latest.json"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.dir.cleanup()


def make_tarball(version: str) -> bytes:
    """A release tarball whose install.sh records that it ran."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        script = b'#!/bin/sh\necho "installed $(dirname "$0")" > "$XDG_DATA_HOME/installed-marker"\n'
        info = tarfile.TarInfo(f"ditto-linux-{version}/install.sh")
        info.size, info.mode = len(script), 0o755
        t.addfile(info, io.BytesIO(script))
    return buf.getvalue()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": f"{self.tmp.name}/cache",
                                         "XDG_DATA_HOME": f"{self.tmp.name}/data"})
        p.start()
        self.addCleanup(p.stop)

    def serve(self, **kw):
        srv = UpdateServer(**kw)
        self.addCleanup(srv.close)
        p = mock.patch.dict(os.environ, {"DITTO_UPDATE_URL": srv.url})
        p.start()
        self.addCleanup(p.stop)
        return srv

    def test_versions(self):
        self.assertTrue(update.is_newer("1.10.0", "1.9.9"))
        self.assertTrue(update.is_newer("v2.0", "1.99.99"))
        self.assertFalse(update.is_newer("1.1.0", "1.1.0"))
        self.assertFalse(update.is_newer("1.0.9", "1.1.0"))

    def test_check_newer_and_up_to_date(self):
        self.serve(version="9.0.0")
        rel = update.check()
        self.assertEqual((rel.version, rel.tag), ("9.0.0", "v9.0.0"))
        self.assertIn("ditto-linux_9.0.0_all.deb", rel.assets)
        self.serve(version=__version__)
        self.assertIsNone(update.check())

    def test_network_error(self):
        with mock.patch.dict(os.environ, {"DITTO_UPDATE_URL": "http://127.0.0.1:9/nothing"}):
            with self.assertRaises(update.UpdateError):
                update.latest(timeout=2)

    def test_download_verifies_checksum(self):
        self.serve()
        rel = update.latest()
        path = update.download(rel, "deb")
        self.assertEqual(path.read_bytes(), b"fake deb")
        self.serve(bad_sum=True)
        with self.assertRaisesRegex(update.UpdateError, "checksum mismatch"):
            update.download(update.latest(), "deb")
        self.serve(no_sums=True)
        with self.assertRaisesRegex(update.UpdateError, "no checksum"):
            update.download(update.latest(), "deb")

    def test_install_deb_uses_pkexec_or_sudo(self):
        deb = Path(self.tmp.name) / "x.deb"
        with mock.patch("ditto_linux.update.os.geteuid", return_value=1000), \
                mock.patch("ditto_linux.update.shutil.which", return_value="/usr/bin/pkexec"):
            self.assertEqual(update.deb_command(deb, gui=True)[:3], ["pkexec", "apt-get", "install"])
            self.assertEqual(update.deb_command(deb, gui=False)[0], "sudo")
            with mock.patch("ditto_linux.update.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "", "")
                update.install(deb, "deb", gui=True)
                self.assertEqual(run.call_args.args[0][-1], str(deb))
                run.return_value = subprocess.CompletedProcess([], 126, "", "dismissed")
                with self.assertRaisesRegex(update.UpdateError, "exit 126"):
                    update.install(deb, "deb", gui=True)

    def test_install_tarball_runs_bundled_installer(self):
        self.serve(version="9.1.0")
        rel = update.latest()
        path = update.download(rel, "tarball")
        update.install(path, "tarball")
        marker = Path(self.tmp.name) / "data" / "installed-marker"
        self.assertIn("ditto-linux/app/ditto-linux-9.1.0", marker.read_text())

    def test_git_checkout_is_not_touched(self):
        self.assertEqual(update.install_kind(ROOT / "ditto_linux"), "git" if (ROOT / ".git").exists() else "tarball")
        self.assertEqual(update.install_kind(Path("/usr/lib/ditto-linux/ditto_linux")), "deb")
        with self.assertRaises(update.UpdateError):
            update.install(Path("x"), "git")

    def test_restart_argv(self):
        with mock.patch("ditto_linux.update.shutil.which", return_value=None), \
                mock.patch("ditto_linux.update.os.path.exists", return_value=False):
            argv = update.restart_argv(["daemon"])
        self.assertEqual((argv[0], argv[-2:]), (sys.executable, [str(ROOT), "daemon"]))
        r = subprocess.run([*argv[:-1], "--version"], cwd="/", capture_output=True, text=True, timeout=30)
        self.assertIn(__version__, r.stdout)


# ====================================================================== popups
class DialogTests(unittest.TestCase):
    def test_shortcut_dialog_choices(self):
        from ditto_linux.ui.dialogs import ShortcutDialog
        rep = desktop.Report("conflict", binding="<Control>grave",
                             conflicts=[desktop.Conflict("custom", "custom shortcut “CopyQ”")])
        dlg = ShortcutDialog(rep)
        self.assertIn("CopyQ", dlg.message.text())
        dlg.take_btn.click()
        self.assertEqual(dlg.choice, "take_over")

        dlg = ShortcutDialog(rep)
        dlg.key_edit.setKeySequence(QKeySequence("Ctrl+Alt+V"))
        dlg.dont.setChecked(True)
        dlg.key_btn.click()
        self.assertEqual((dlg.choice, dlg.key, dlg.dont_check), ("use_key", "<Control><Alt>v", True))

        dlg = ShortcutDialog(rep)
        dlg.key_edit.setKeySequence(QKeySequence("V"))
        dlg.key_btn.click()
        self.assertEqual(dlg.choice, "later")
        self.assertIn("Ctrl, Alt or Super", dlg.error.text())
        dlg.later_btn.click()
        self.assertEqual(dlg.choice, "later")

    def test_update_dialog_choices(self):
        from ditto_linux.ui.dialogs import UpdateDialog
        rel = update.Release("9.0.0", "v9.0.0", notes="- new things")
        for kind, expected in (("deb", "install"), ("git", "open")):
            dlg = UpdateDialog(rel, kind)
            dlg.install_btn.click()
            self.assertEqual(dlg.choice, expected)
        dlg = UpdateDialog(rel, "deb")
        dlg.findChild(type(dlg.install_btn), "skip").click()
        self.assertEqual(dlg.choice, "skip")


# ====================================================================== daemon decisions
class DaemonDecisionTests(unittest.TestCase):
    def setUp(self):
        from ditto_linux.app import DittoDaemon
        self.iso = IsolatedEnv()
        self.addCleanup(self.iso.cleanup)
        p = mock.patch.dict(os.environ, self.iso.env)
        p.start()
        self.addCleanup(p.stop)
        self.settings = Settings.load()
        self.daemon = DittoDaemon(app, str(self.iso.db), self.settings, self.iso.socket, show_tray=False,
                                  argv=["daemon", "--no-tray"])
        self.addCleanup(self.daemon.server.close)
        self.addCleanup(self.daemon.store.close)
        self.addCleanup(lambda: __import__("logging").getLogger().removeHandler(self.daemon.problem_handler))
        self.addCleanup(self.daemon.paster.stop)

    def test_shortcut_popup_take_over(self):
        from ditto_linux.ui import dialogs
        rep = desktop.Report("conflict", binding="<Control>grave", conflicts=[desktop.Conflict("custom", "Other")])
        self.settings.check_shortcut = True

        def fake_exec(dlg):
            dlg.choice, dlg.dont_check = "take_over", False
            return 1
        with mock.patch("ditto_linux.app.exec_front", fake_exec), \
                mock.patch("ditto_linux.app.desktop.resolve",
                           return_value=desktop.Report("ok", binding="<Control>grave")) as resolve:
            self.daemon.ask_shortcut(report=rep)
        resolve.assert_called_once_with(self.settings, "<Control>grave", take_over=True)
        self.assertIsInstance(dialogs.ShortcutDialog(rep), dialogs.ShortcutDialog)

    def test_shortcut_popup_dont_check_again(self):
        rep = desktop.Report("missing", binding="<Control>grave")
        self.settings.check_shortcut = True

        def fake_exec(dlg):
            dlg.choice, dlg.dont_check = "later", True
            return 1
        with mock.patch("ditto_linux.app.exec_front", fake_exec), \
                mock.patch("ditto_linux.app.desktop.resolve") as resolve:
            self.daemon.ask_shortcut(report=rep)
        resolve.assert_not_called()
        self.assertFalse(Settings.load().check_shortcut)
        with mock.patch("ditto_linux.app.exec_front") as ex:
            self.daemon.ask_shortcut()                  # automatic check is now off
        ex.assert_not_called()

    def test_update_popup_skip_and_install(self):
        rel = update.Release("9.0.0", "v9.0.0")
        choices = iter(["skip", "install"])

        def fake_exec(dlg):
            dlg.choice = next(choices)
            return 1
        with mock.patch("ditto_linux.app.exec_front", fake_exec), \
                mock.patch.object(self.daemon, "install_update") as inst:
            self.daemon._on_update_checked(rel, manual=False)
            self.assertEqual(Settings.load().skipped_version, "9.0.0")
            self.daemon._on_update_checked(rel, manual=False)     # skipped: no popup
            self.daemon._on_update_checked(rel, manual=True)      # manual check still offers it
        inst.assert_called_once()
        self.assertGreater(Settings.load().last_update_check, 0)

    def test_automatic_check_respects_settings(self):
        with mock.patch.object(self.daemon.worker, "run") as run:
            self.daemon.check_updates()                 # disabled in the test environment
            run.assert_not_called()
            self.daemon.check_updates(manual=True)
            run.assert_called_once()

    def test_failed_update_is_logged_and_shown(self):
        with mock.patch("ditto_linux.app.QMessageBox.warning") as warn:
            self.daemon._on_update_installed(update.UpdateError("installation failed (exit 126)"))
        self.assertIn("exit 126", warn.call_args.args[2])

    def test_restart_after_update_execs_launcher(self):
        with mock.patch("ditto_linux.app.os.execv") as execv, \
                mock.patch("ditto_linux.app.update.restart_argv", return_value=["/usr/bin/ditto-linux", "daemon"]):
            self.daemon._on_update_installed("9.0.0")
        execv.assert_called_once_with("/usr/bin/ditto-linux", ["/usr/bin/ditto-linux", "daemon"])


# ====================================================================== log file + CLI
class LogAndCliTests(unittest.TestCase):
    def setUp(self):
        self.iso = IsolatedEnv()
        self.addCleanup(self.iso.cleanup)

    def test_log_path_and_uncaught_exception_logged(self):
        self.assertEqual(self.iso.cli("logs", "--path").stdout.strip(), str(self.iso.log))
        code = ("from ditto_linux.logs import setup_logging; setup_logging(daemon=True); "
                "import logging; logging.getLogger('t').info('hello log'); raise RuntimeError('boom 42')")
        subprocess.run([sys.executable, "-c", code], env=self.iso.env, cwd=ROOT, capture_output=True, timeout=30)
        text = self.iso.log.read_text()
        self.assertIn("hello log", text)
        self.assertIn("uncaught exception", text)
        self.assertIn("RuntimeError: boom 42", text)
        self.assertEqual(self.iso.log.stat().st_mode & 0o777, 0o600)
        self.assertIn("boom 42", self.iso.cli("logs", "-n", "20").stdout)

    def test_shortcut_command_outside_gnome(self):
        self.iso.env["XDG_CURRENT_DESKTOP"] = "KDE"
        r = self.iso.cli("shortcut", "--json")
        self.assertEqual(json.loads(r.stdout)["state"], "unsupported")
        self.assertEqual(self.iso.cli("shortcut", "--fix", check=False).returncode, 1)
        self.assertEqual(self.iso.cli("shortcut", "--set", "V", check=False).returncode, 1)

    def test_update_check_command(self):
        srv = UpdateServer(version="9.0.0")
        self.addCleanup(srv.close)
        self.iso.env["DITTO_UPDATE_URL"] = srv.url
        out = self.iso.cli("update", "--check").stdout
        self.assertIn("latest: 9.0.0", out)
        self.iso.env["DITTO_UPDATE_URL"] = "http://127.0.0.1:9/none"
        self.assertEqual(self.iso.cli("update", "--check", check=False).returncode, 1)

    def test_doctor(self):
        out = self.iso.cli("doctor").stdout
        self.assertIn(f"version:    {__version__}", out)
        self.assertIn(str(self.iso.log), out)


if __name__ == "__main__":
    unittest.main()


# ====================================================================== problem reports
class ReportTests(unittest.TestCase):
    def setUp(self):
        from ditto_linux import report
        self.report = report
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def problem(self, msg="boom", line=10):
        try:
            exec(compile("\n" * line + f"raise ValueError({msg!r})", "/x/ditto_linux/app.py", "exec"))
        except ValueError:
            return self.report.from_exception(sys.exc_info(), "paste")

    def test_fingerprint_ignores_values_and_line_numbers(self):
        a, b = self.problem("boom 1", 10), self.problem("boom 2", 20)
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertTrue(a.title.startswith("ValueError: boom 1"))
        try:
            raise KeyError("x")
        except KeyError:
            c = self.report.from_exception(sys.exc_info())
        self.assertNotEqual(a.fingerprint, c.fingerprint)
        dump = "Fatal Python error: Segmentation fault\n\nCurrent thread:\n  File \"/x/ditto_linux/app.py\", line 3 in paste\n"
        self.assertTrue(self.report.from_crash_dump(dump).title.startswith("Crash: Fatal Python error"))

    def test_body_is_sanitized_and_tagged(self):
        home = str(Path.home())
        p = self.report.Problem("t", f'File "{home}/secret/app.py"\ntoken ghp_{"a" * 30}', "abc123")
        body = self.report.build_body(p)
        self.assertNotIn(home + "/", body)
        self.assertIn("~/secret/app.py", body)
        self.assertNotIn("ghp_aaaa", body)
        self.assertIn("ditto-fingerprint: abc123", body)
        self.assertIn(f"| Ditto | {__version__} |", body)
        url = self.report.new_issue_url(p)
        self.assertTrue(url.startswith(f"https://github.com/{self.report.REPO}/issues/new?"))
        self.assertLess(len(url), 20000)

    issue_state = "OPEN"

    def fake_gh(self, existing=None):
        calls = []

        def run(*args, input=None, timeout=30):
            calls.append((args, input))
            if args[:2] == ("issue", "list"):
                return subprocess.CompletedProcess(args, 0, json.dumps(existing or []), "")
            if args[:2] == ("issue", "view"):
                return subprocess.CompletedProcess(args, 0, self.issue_state + "\n", "")
            if args[:2] == ("issue", "create"):
                return subprocess.CompletedProcess(args, 0, "https://github.com/o/r/issues/7\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        p = mock.patch.object(self.report, "_gh", side_effect=run)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_submit_creates_once_then_comments(self):
        calls = self.fake_gh()
        prob = self.problem()
        self.assertTrue(self.report.should_send(prob))
        self.assertEqual(self.report.submit(prob), "https://github.com/o/r/issues/7")
        create = [c for c in calls if c[0][:2] == ("issue", "create")][0]
        self.assertIn("--label", create[0])
        self.assertIn(prob.fingerprint, create[1])
        self.assertFalse(self.report.should_send(prob), "not again within a day")
        import time as _t
        self.assertTrue(self.report.should_send(prob, now=_t.time() + 25 * 3600))
        self.report.submit(prob)
        self.assertEqual(calls[-1][0][:3], ("issue", "comment", "7"))
        self.assertIn("seen 2 times", calls[-1][1])

    def test_closed_issue_gets_a_new_one(self):
        calls = self.fake_gh()
        prob = self.problem()
        self.report.submit(prob)
        self.issue_state = "CLOSED"
        self.report.submit(prob)
        self.assertEqual([c[0][1] for c in calls if c[0][0] == "issue" and c[0][1] in ("create", "comment")],
                         ["create", "create"])

    def test_submit_reuses_open_issue_found_on_github(self):
        calls = self.fake_gh(existing=[{"number": 3, "url": "https://github.com/o/r/issues/3"}])
        self.report.submit(self.problem())
        self.assertEqual(calls[-1][0][:3], ("issue", "comment", "3"))
        self.assertFalse(any(c[0][:2] == ("issue", "create") for c in calls))

    def test_handler_catches_logged_exceptions_only(self):
        import logging
        got = []
        h = self.report.ProblemHandler(got.append)
        lg = logging.getLogger("ditto_linux.test_handler")
        lg.addHandler(h)
        lg.propagate = False
        try:
            lg.error("plain error without traceback")
            try:
                1 / 0
            except ZeroDivisionError:
                lg.exception("IPC command failed")
        finally:
            lg.removeHandler(h)
        self.assertEqual(len(got), 1)
        self.assertIn("ZeroDivisionError", got[0].title)
        self.assertIn("IPC command failed", got[0].title)

    def test_crash_dump_taken_once(self):
        f = self.report.crash_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("Fatal Python error: Aborted\n")
        self.assertIn("Aborted", self.report.take_crash_dump())
        self.assertEqual(self.report.take_crash_dump(), "")


class DaemonReportTests(DaemonDecisionTests):
    """How the daemon reacts to a problem in each report mode."""

    def setUp(self):
        super().setUp()
        from ditto_linux import report
        self.report = report
        self.addCleanup(lambda: __import__("logging").getLogger().removeHandler(self.daemon.problem_handler))
        p = mock.patch.dict(os.environ, {"DITTO_NO_REPORT": ""})
        p.start()
        self.addCleanup(p.stop)
        self.prob = report.Problem("ValueError: x", "Traceback...\nValueError: x", "fp1")

    def test_never_only_logs(self):
        self.settings.auto_report = "never"
        with mock.patch("ditto_linux.app.exec_front") as ex, mock.patch.object(self.daemon.worker, "run") as run:
            self.daemon.on_problem(self.prob)
        ex.assert_not_called()
        run.assert_not_called()

    def test_always_submits_without_asking(self):
        self.settings.auto_report = "always"
        with mock.patch("ditto_linux.app.exec_front") as ex, mock.patch.object(self.daemon.worker, "run") as run:
            self.daemon.on_problem(self.prob)
        ex.assert_not_called()
        run.assert_called_once()

    def test_ask_then_always(self):
        self.settings.auto_report = "ask"

        def fake_exec(dlg):
            dlg.choice = "always"
            return 1
        with mock.patch("ditto_linux.app.exec_front", fake_exec), \
                mock.patch("ditto_linux.app.report.gh_ready", return_value=True), \
                mock.patch.object(self.daemon.worker, "run") as run:
            self.daemon.on_problem(self.prob)
        run.assert_called_once()
        self.assertEqual(Settings.load().auto_report, "always")

    def test_ask_without_gh_opens_prefilled_page(self):
        self.settings.auto_report = "ask"

        def fake_exec(dlg):
            dlg.choice = "report"
            return 1
        with mock.patch("ditto_linux.app.exec_front", fake_exec), \
                mock.patch("ditto_linux.app.report.gh_ready", return_value=False), \
                mock.patch("ditto_linux.app.QDesktopServices.openUrl") as open_url:
            self.daemon.on_problem(self.prob)
        self.assertIn("/issues/new?", open_url.call_args.args[0].toString())

    def test_logged_exception_reaches_the_daemon(self):
        import logging
        self.settings.auto_report = "always"
        with mock.patch.object(self.daemon, "_submit_problem") as sub:
            try:
                raise RuntimeError("from a slot")
            except RuntimeError:
                logging.getLogger("ditto_linux.clipboard").exception("failed to save clip")
            app.processEvents()
        sub.assert_called_once()
        self.assertIn("RuntimeError", sub.call_args.args[0].title)

    def test_recent_problem_not_reported_twice(self):
        self.settings.auto_report = "always"
        with mock.patch("ditto_linux.app.report.should_send", return_value=False), \
                mock.patch.object(self.daemon.worker, "run") as run:
            self.daemon.on_problem(self.prob)
        run.assert_not_called()


class ReportDialogTests(unittest.TestCase):
    def test_choices(self):
        from ditto_linux import report
        from ditto_linux.ui.dialogs import ReportDialog
        p = report.Problem("ValueError: x", "details", "fp")
        for name in ("report", "always", "never", "later"):
            dlg = ReportDialog(p, "details", can_submit=True)
            dlg.findChild(type(dlg.report_btn), name).click()
            self.assertEqual(dlg.choice, name)
