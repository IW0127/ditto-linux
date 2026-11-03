"""Log file setup so problems can be looked at later (``ditto-linux logs``).

The log lives in ``$XDG_STATE_HOME/ditto-linux/ditto.log`` (default ``~/.local/state``), is rotated at
1 MB with 3 old copies, and never contains clip contents, only clip ids.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from . import APP_NAME, __version__

FORMAT = "%(asctime)s %(levelname)s %(name)s[%(process)d]: %(message)s"
_configured = False


def log_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local/state") / APP_NAME


def log_path() -> Path:
    override = os.environ.get("DITTO_LOG")
    return Path(override) if override else log_dir() / "ditto.log"


def setup_logging(verbose: bool = False, daemon: bool = False) -> Path | None:
    """Log to the rotating file always; to stderr at INFO for the daemon, WARNING for commands."""
    global _configured
    root = logging.getLogger()
    if _configured:
        return log_path()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else (logging.INFO if daemon else logging.WARNING))
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG if verbose else logging.INFO)
        fh.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(fh)
        os.chmod(path, 0o600)
    except OSError as e:
        path = None
        root.warning("cannot write log file: %s", e)
    if daemon:
        install_exception_hooks()
        enable_crash_dump()
    _configured = True
    return path


def install_exception_hooks() -> None:
    """Uncaught exceptions (main thread, threads, Qt slots) go to the log instead of vanishing."""
    log = logging.getLogger(APP_NAME)

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("uncaught exception (ditto-linux %s)", __version__, exc_info=(exc_type, exc, tb))

    def thread_hook(args):
        log.critical("uncaught exception in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook


_crash_fh = None


def enable_crash_dump() -> None:
    """Hard crashes (segfaults in Qt etc.) write a traceback to crash.log; reported on the next start."""
    global _crash_fh
    import faulthandler
    try:
        path = log_dir() / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _crash_fh = open(path, "a", encoding="utf-8")
        faulthandler.enable(file=_crash_fh, all_threads=True)
    except OSError:
        pass


def install_qt_message_handler() -> None:
    """Route Qt's own warnings into the log."""
    from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    log = logging.getLogger("qt")
    levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
              QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}

    def handler(mode, context, message):
        log.log(levels.get(mode, logging.WARNING), "%s", message)

    qInstallMessageHandler(handler)


def tail(n: int = 50) -> list[str]:
    try:
        with open(log_path(), encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except OSError:
        return []
