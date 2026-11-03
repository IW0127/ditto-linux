"""Command line interface: ``ditto-linux [command]``."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from . import APP_NAME, __version__
from . import formats as fmt
from .config import Settings, default_db_path
from .db import NO_PARENT, ClipStore, dumps_export
from .ipc import DaemonNotRunning, request


def _store(args) -> ClipStore:
    return ClipStore(args.db or default_db_path())


def _notify_daemon():
    try:
        request("refresh", timeout=1.0)
    except (ConnectionError, OSError, ValueError):
        pass


def _ipc(cmd: str, **kw) -> int:
    try:
        reply = request(cmd, **kw)
    except DaemonNotRunning as e:
        print(f"error: {e}. Start it with '{APP_NAME} daemon'.", file=sys.stderr)
        return 2
    if not reply.get("ok"):
        print(f"error: {reply.get('error', 'failed')}", file=sys.stderr)
        return 1
    extra = {k: v for k, v in reply.items() if k != "ok"}
    if extra:
        print(json.dumps(extra, indent=2))
    return 0


def cmd_list(args) -> int:
    store = _store(args)
    clips = store.list(args.group, search=args.search or "", limit=args.limit)
    if args.json:
        print(json.dumps([c.to_dict() for c in clips], indent=2))
        return 0
    for c in clips:
        when = dt.datetime.fromtimestamp(c.date).strftime("%Y-%m-%d %H:%M") if c.date else "-"
        flags = (("G" if c.is_group else " ") + ("S" if c.sticky else " ") + ("N" if c.never_delete else " ")
                 + ("H" if c.hidden else " "))
        text = " ".join(c.title.split())
        print(f"{c.id:>6}  {flags}  {when}  {text[:100]}")
    return 0


def cmd_get(args) -> int:
    store = _store(args)
    clip = store.get(args.id)
    if clip is None:
        print(f"error: no clip with id {args.id}", file=sys.stderr)
        return 1
    data = store.get_formats(args.id)
    mime = args.format or (fmt.TEXT if fmt.TEXT in data else next(iter(data), None))
    if mime not in data:
        print(f"error: clip {args.id} has no {mime} data (has: {', '.join(data) or 'none'})", file=sys.stderr)
        return 1
    out = sys.stdout.buffer
    out.write(data[mime])
    out.flush()
    return 0


def cmd_add(args) -> int:
    text = args.text if args.text is not None else sys.stdin.read()
    if not text:
        print("error: nothing to add", file=sys.stderr)
        return 1
    clip_id, created = _store(args).add_text(text)
    _notify_daemon()
    print(clip_id)
    return 0


def cmd_delete(args) -> int:
    n = _store(args).delete(args.ids)
    _notify_daemon()
    print(f"deleted {n}")
    return 0 if n else 1


def cmd_clear(args) -> int:
    n = _store(args).clear_history()
    _notify_daemon()
    print(f"deleted {n}")
    return 0


def cmd_group(args) -> int:
    store = _store(args)
    if args.group_cmd == "create":
        print(store.create_group(args.name, args.parent))
    elif args.group_cmd == "list":
        for g in store.groups():
            print(f"{g.id:>6}  {g.text}")
    elif args.group_cmd == "move":
        store.move_to_group(args.ids, args.group)
    _notify_daemon()
    return 0


def cmd_flag(args) -> int:
    store = _store(args)
    if store.get(args.id) is None:
        print(f"error: no clip with id {args.id}", file=sys.stderr)
        return 1
    value = not args.off
    if args.flag == "sticky":
        store.set_sticky(args.id, value)
    elif args.flag == "mask":
        store.set_hidden(args.id, value)
    else:
        store.set_never_delete(args.id, value)
    _notify_daemon()
    return 0


def cmd_pin(args) -> int:
    store = _store(args)
    if store.get(args.id) is None:
        print(f"error: no clip with id {args.id}", file=sys.stderr)
        return 1
    if args.off:
        store.set_sticky(args.id, False)
    else:
        store.pin(args.id, title=args.title, hidden=True if args.hide else None)
        if args.up:
            store.move_sticky(args.id, -args.up)
    _notify_daemon()
    return 0


def cmd_logs(args) -> int:
    from .logs import log_path, tail
    if args.path:
        print(log_path())
        return 0
    lines = tail(args.lines)
    if not lines:
        print(f"no log yet ({log_path()})", file=sys.stderr)
    sys.stdout.writelines(lines)
    if args.follow:
        import time
        with open(log_path(), encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            try:
                while True:
                    line = f.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.3)
            except KeyboardInterrupt:
                pass
    return 0


def cmd_shortcut(args) -> int:
    from . import desktop
    settings = Settings.load()
    if args.set:
        try:
            accel = desktop.from_portable(args.set)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not desktop.supported():
            print("error: changing the shortcut is only supported on GNOME", file=sys.stderr)
            return 1
        rep = desktop.resolve(settings, accel, take_over=args.take_over)
    elif args.fix or args.take_over:
        if not desktop.supported():
            print("error: fixing the shortcut is only supported on GNOME", file=sys.stderr)
            return 1
        rep = desktop.resolve(settings, None, take_over=args.take_over)
    else:
        rep = desktop.check(settings)
    if args.json:
        print(json.dumps(rep.to_dict(), indent=2))
    else:
        print(f"shortcut: {desktop.pretty(rep.binding)}   state: {rep.state}")
        if rep.command:
            print(f"command:  {rep.command}")
        for c in rep.conflicts:
            print(f"conflict: {c.description}" + ("" if c.fixable else "  (cannot be changed by Ditto)"))
        for n in rep.notes:
            print(f"note:     {n}")
        if rep.state in ("missing", "broken"):
            print(f"fix with: {APP_NAME} shortcut --fix")
        elif rep.state == "conflict":
            print(f"fix with: {APP_NAME} shortcut --take-over   (or --set 'Ctrl+Alt+V')")
    return 0 if rep.state in ("ok", "unsupported") else 1


def cmd_update(args) -> int:
    from . import update
    try:
        rel = update.latest()
    except update.UpdateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    kind = update.install_kind()
    newer = update.is_newer(rel.version)
    print(f"installed: {__version__} ({kind})   latest: {rel.version}   {rel.url}")
    if not newer:
        print("Ditto is up to date.")
        return 0
    if args.check:
        return 0
    if kind == "git":
        print("This is a git checkout: update it with 'git pull'.")
        return 0
    if not args.yes and sys.stdin.isatty():
        if input(f"Install Ditto {rel.version} now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return 0
    try:
        path = update.download(rel, kind)
        print(f"downloaded and verified {path.name}")
        update.install(path, kind, gui=not sys.stdin.isatty())
    except update.UpdateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Ditto {rel.version} installed.")
    try:
        request("restart", update=True, timeout=2)
        print("Restarted the running Ditto.")
    except (ConnectionError, OSError, ValueError):
        pass
    return 0


def cmd_report(args) -> int:
    from . import report
    details = args.message or (sys.stdin.read() if not sys.stdin.isatty() and args.stdin else "")
    title = args.title or "Problem report"
    problem = report.Problem(title=title, details=details or "(reported by hand)",
                             fingerprint=report.fingerprint(title + "\n" + details) if details else
                             __import__("hashlib").sha1(title.encode()).hexdigest()[:12])
    if args.dry_run:
        print(f"title: {report.sanitize(title)}\n")
        print(report.build_body(problem))
        return 0
    if not args.browser and report.gh_ready():
        try:
            print(report.submit(problem))
            return 0
        except report.ReportError as e:
            print(f"error: {e}", file=sys.stderr)
    url = report.new_issue_url(problem)
    print(f"Open this page to file the issue:\n{url}")
    import webbrowser
    webbrowser.open(url)
    return 0


def cmd_paste_test(args) -> int:
    """Send the paste keystroke to whatever window is focused after a countdown."""
    import time
    from .paste import Paster
    settings = Settings.load()
    paster = Paster(settings)
    problems = [m for ok, m in paster.diagnose() if not ok]
    for m in problems:
        print(f"✗ {m}")
    print(f"Copy some text, then click into a text field. Pasting with {settings.paste_shortcut} in", end="", flush=True)
    for i in range(args.seconds, 0, -1):
        print(f" {i}", end="", flush=True)
        time.sleep(1)
    print()
    try:
        ok = paster.paste()
    finally:
        paster.stop()
    print("✓ keystroke sent: the text should have appeared." if ok else
          "✗ could not send the keystroke; see the problems above and 'ditto-linux logs'.")
    return 0 if ok else 1


def cmd_doctor(args) -> int:
    import os
    import shutil
    from . import desktop, update
    from .ipc import is_running
    from .logs import log_path
    settings = Settings.load()
    print(f"version:    {__version__} ({update.install_kind()} install, python {sys.version.split()[0]})")
    print(f"session:    {os.environ.get('XDG_SESSION_TYPE', '?')} / {os.environ.get('XDG_CURRENT_DESKTOP', '?')}")
    print(f"daemon:     {'running' if is_running() else 'not running'}")
    print(f"database:   {args.db or default_db_path()}")
    print(f"settings:   {Settings.path()}")
    print(f"log:        {log_path()}")
    from .paste import Paster
    print("auto-paste:")
    for ok, msg in Paster(settings).diagnose():
        print(f"  {'✓' if ok else '✗'} {msg}")
    rep = desktop.check(settings)
    print(f"shortcut:   {desktop.pretty(rep.binding)} ({rep.state})")
    for c in rep.conflicts:
        print(f"  conflict: {c.description}")
    print(f"updates:    {'daily check on' if settings.auto_update_check else 'off'}")
    from . import report
    print(f"reports:    {settings.auto_report}; gh {'ready' if report.gh_ready() else 'not available'}")
    return 0


def cmd_export(args) -> int:
    text = dumps_export(_store(args))
    if args.file == "-":
        print(text)
    else:
        Path(args.file).write_text(text)
    return 0


def cmd_import(args) -> int:
    data = json.loads(Path(args.file).read_text() if args.file != "-" else sys.stdin.read())
    n = _store(args).import_json(data)
    _notify_daemon()
    print(f"imported {n}")
    return 0


def cmd_stats(args) -> int:
    print(json.dumps(_store(args).stats(), indent=2))
    return 0


def cmd_cleanup(args) -> int:
    s = Settings.load()
    store = _store(args)
    n = store.cleanup(args.max if args.max is not None else s.max_clips,
                      args.days if args.days is not None else s.expire_days)
    if args.vacuum:
        store.vacuum()
    _notify_daemon()
    print(f"deleted {n}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP_NAME, description="Ditto clipboard manager for Linux")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--db", help="database file (default: %(default)s)", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("daemon", help="run the clipboard monitor (default when no command is given)")
    d.add_argument("--show", action="store_true", help="open the Quick Paste window on start")
    d.add_argument("--no-tray", action="store_true", help="do not create a tray icon")

    for name, help_ in (("show", "open the Quick Paste window"), ("toggle", "show or hide the window"),
                        ("hotkey", "what the global Ctrl+` shortcut runs: open, or select next while "
                                   "Ctrl is held and paste when it is released"),
                        ("hide", "hide the window"), ("pause", "stop recording copies"),
                        ("resume", "start recording copies again"), ("status", "daemon status"),
                        ("quit", "stop the daemon")):
        sub.add_parser(name, help=help_)

    for name, help_ in (("paste", "put a clip on the clipboard and paste it into the focused window"),
                        ("copy", "put a clip on the clipboard")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("id", type=int)
        sp.add_argument("--plain", action="store_true", help="plain text only")

    sp = sub.add_parser("list", help="list clips (most recent first)")
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp.add_argument("-s", "--search")
    sp.add_argument("-g", "--group", type=int, default=NO_PARENT, help="list clips inside a group id")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("get", help="write a clip's data to stdout")
    sp.add_argument("id", type=int)
    sp.add_argument("-f", "--format", help="MIME type, e.g. text/plain, text/html, image/png")

    sp = sub.add_parser("add", help="add a text clip (from argument or stdin)")
    sp.add_argument("text", nargs="?")

    sp = sub.add_parser("delete", help="delete clips by id")
    sp.add_argument("ids", type=int, nargs="+")

    sub.add_parser("clear", help="delete all clips that are not grouped or protected")

    sp = sub.add_parser("group", help="manage groups")
    gsub = sp.add_subparsers(dest="group_cmd", required=True)
    g = gsub.add_parser("create")
    g.add_argument("name")
    g.add_argument("--parent", type=int, default=NO_PARENT)
    gsub.add_parser("list")
    g = gsub.add_parser("move", help="move clips into a group (-1 = no group)")
    g.add_argument("group", type=int)
    g.add_argument("ids", type=int, nargs="+")

    sp = sub.add_parser("pin", help="pin a clip at the top, optionally with a title and hidden content")
    sp.add_argument("id", type=int)
    sp.add_argument("-t", "--title", help="title shown instead of the content")
    sp.add_argument("--hide", action="store_true", help="hide the content (passwords); only the title is shown")
    sp.add_argument("--up", type=int, metavar="N", help="move the pin N places up (negative: down)")
    sp.add_argument("--off", action="store_true", help="unpin")

    for flag in ("sticky", "never-delete", "mask"):
        sp = sub.add_parser(flag, help=f"set the {flag} flag on a clip")
        sp.add_argument("id", type=int)
        sp.add_argument("--off", action="store_true")
        sp.set_defaults(flag=flag)

    sub.add_parser("restart", help="restart the running daemon")
    sp = sub.add_parser("logs", help="show the log file (for troubleshooting)")
    sp.add_argument("-n", "--lines", type=int, default=50)
    sp.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    sp.add_argument("--path", action="store_true", help="only print where the log is")
    sp = sub.add_parser("shortcut", help="check / fix the global shortcut (GNOME)")
    sp.add_argument("--fix", action="store_true", help="repair Ditto's shortcut")
    sp.add_argument("--take-over", action="store_true", help="also remove the key from conflicting shortcuts")
    sp.add_argument("--set", metavar="KEY", help="use another key, e.g. 'Ctrl+Alt+V' or '<Super>v'")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("update", help="check for a new release and install it")
    sp.add_argument("--check", action="store_true", help="only check, do not install")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sub.add_parser("doctor", help="show version, paths, shortcut and paste setup")
    sp = sub.add_parser("paste-test", help="send the paste keystroke after a countdown (checks auto-paste)")
    sp.add_argument("-s", "--seconds", type=int, default=5)
    sp = sub.add_parser("report", help="report a problem as a GitHub issue (with logs and system info)")
    sp.add_argument("-t", "--title")
    sp.add_argument("-m", "--message", help="what happened")
    sp.add_argument("--stdin", action="store_true", help="read the details from stdin")
    sp.add_argument("--dry-run", action="store_true", help="only print the report")
    sp.add_argument("--browser", action="store_true", help="open the GitHub page instead of using gh")
    sp = sub.add_parser("export", help="export all clips to JSON")
    sp.add_argument("file", nargs="?", default="-")
    sp = sub.add_parser("import", help="import clips from a JSON export")
    sp.add_argument("file")
    sub.add_parser("stats", help="database statistics")
    sp = sub.add_parser("cleanup", help="apply auto-delete rules now")
    sp.add_argument("--max", type=int)
    sp.add_argument("--days", type=int)
    sp.add_argument("--vacuum", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command or "daemon"
    from .logs import setup_logging
    setup_logging(verbose=args.verbose, daemon=cmd == "daemon")
    if cmd == "daemon":
        from .app import run_daemon  # Qt is only imported when needed
        return run_daemon(args.db, show_window=getattr(args, "show", False), tray=not getattr(args, "no_tray", False))
    if cmd in ("show", "toggle", "hotkey", "hide", "pause", "resume", "status", "quit", "restart"):
        return _ipc(cmd)
    if cmd in ("paste", "copy"):
        return _ipc(cmd, id=args.id, plain=args.plain)
    handlers = {
        "list": cmd_list, "get": cmd_get, "add": cmd_add, "delete": cmd_delete, "clear": cmd_clear,
        "group": cmd_group, "sticky": cmd_flag, "never-delete": cmd_flag, "mask": cmd_flag, "pin": cmd_pin, "export": cmd_export,
        "import": cmd_import, "stats": cmd_stats, "cleanup": cmd_cleanup, "logs": cmd_logs,
        "shortcut": cmd_shortcut, "update": cmd_update, "doctor": cmd_doctor, "report": cmd_report, "paste-test": cmd_paste_test,
    }
    try:
        return handlers[cmd](args)
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
