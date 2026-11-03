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
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cmd = args.command or "daemon"
    if cmd == "daemon":
        from .app import run_daemon  # Qt is only imported when needed
        return run_daemon(args.db, show_window=getattr(args, "show", False), tray=not getattr(args, "no_tray", False))
    if cmd in ("show", "toggle", "hotkey", "hide", "pause", "resume", "status", "quit"):
        return _ipc(cmd)
    if cmd in ("paste", "copy"):
        return _ipc(cmd, id=args.id, plain=args.plain)
    handlers = {
        "list": cmd_list, "get": cmd_get, "add": cmd_add, "delete": cmd_delete, "clear": cmd_clear,
        "group": cmd_group, "sticky": cmd_flag, "never-delete": cmd_flag, "mask": cmd_flag, "pin": cmd_pin, "export": cmd_export,
        "import": cmd_import, "stats": cmd_stats, "cleanup": cmd_cleanup,
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
