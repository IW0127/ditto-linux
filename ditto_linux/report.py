"""Automatic problem reports as GitHub issues.

When Ditto hits an error (an uncaught exception, a logged exception, or a hard crash caught by
``faulthandler`` on the previous run) it builds a report and, depending on the ``auto_report`` setting:

* ``always`` – files it with the GitHub CLI (``gh``) if it is installed and logged in;
* ``ask``    – asks first; without ``gh`` it opens a pre-filled "new issue" page instead;
* ``never``  – only logs it.

No token is embedded in the app. The same problem (same fingerprint) is filed once; repeats add at most one
comment per day to the existing issue. Reports contain the error, a traceback, system information and
the last log lines, with the home directory and user name masked. Clip contents are never included
(they are never logged in the first place).
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .logs import log_dir, tail

log = logging.getLogger(__name__)

REPO = os.environ.get("DITTO_REPORT_REPO", "IW0127/ditto-linux")
LABELS = ("bug", "auto-report")
COMMENT_INTERVAL_S = 24 * 3600
MAX_BODY = 60000


@dataclass
class Problem:
    title: str
    details: str        # traceback / crash dump (unsanitized)
    fingerprint: str

    def to_dict(self) -> dict:
        return {"title": self.title, "fingerprint": self.fingerprint}


# ---------------------------------------------------------------------- building
def fingerprint(text: str) -> str:
    """Stable id for 'the same problem': exception type + function names, not line numbers or values."""
    frames = re.findall(r'File "([^"]+)", line \d+, in (\S+)', text)
    exc = ""
    for line in reversed(text.strip().splitlines()):
        m = re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)\b|Fatal Python error)", line)
        if m:
            exc = m.group(1)
            break
    key = exc + "|" + "|".join(f"{Path(f).name}:{fn}" for f, fn in frames if "ditto_linux" in f or not frames)
    if key == "|":
        key = re.sub(r"\d+", "N", text.strip().splitlines()[-1] if text.strip() else "")
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def from_exception(exc_info, where: str = "") -> Problem:
    text = "".join(traceback.format_exception(*exc_info))
    exc_type = exc_info[0].__name__ if exc_info[0] else "Error"
    msg = str(exc_info[1]).splitlines()[0][:100] if exc_info[1] else ""
    title = f"{exc_type}: {msg}" if msg else exc_type
    if where:
        title += f" (in {where})"
    return Problem(title=title, details=text, fingerprint=fingerprint(text))


def from_crash_dump(dump: str) -> Problem:
    first = next((l for l in dump.splitlines() if l.startswith("Fatal Python error")), "Fatal error")
    return Problem(title=f"Crash: {first[:110]}", details=dump, fingerprint=fingerprint(dump))


def sanitize(text: str) -> str:
    home = str(Path.home())
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    if home and home != "/":
        text = text.replace(home, "~")
    if user and len(user) > 2:
        text = re.sub(rf"\b{re.escape(user)}\b", "<user>", text)
    text = re.sub(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "<token>", text)
    hostname = platform.node()
    if hostname and len(hostname) > 2:
        text = text.replace(hostname, "<host>")
    return text


def system_info() -> dict:
    from . import update
    info = {"Ditto": __version__, "Install": update.install_kind(), "Python": sys.version.split()[0],
            "Session": f"{os.environ.get('XDG_SESSION_TYPE', '?')} / {os.environ.get('XDG_CURRENT_DESKTOP', '?')}",
            "Kernel": platform.release()}
    try:
        from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
        info["Qt / PyQt"] = f"{QT_VERSION_STR} / {PYQT_VERSION_STR}"
    except ImportError:
        pass
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                info["OS"] = line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return info


def build_body(problem: Problem, log_lines: int = 40) -> str:
    info = "\n".join(f"| {k} | {v} |" for k, v in system_info().items())
    logs = "".join(tail(log_lines))
    body = (f"Automatic report from Ditto for Linux.\n\n"
            f"| | |\n|---|---|\n{info}\n\n"
            f"### Error\n```\n{problem.details.strip()}\n```\n\n"
            f"<details><summary>Last log lines</summary>\n\n```\n{logs.strip()}\n```\n</details>\n\n"
            f"<!-- ditto-fingerprint: {problem.fingerprint} -->\n")
    body = sanitize(body)
    return body if len(body) <= MAX_BODY else body[: MAX_BODY - 20] + "\n…(truncated)\n"


def new_issue_url(problem: Problem) -> str:
    """Pre-filled 'new issue' page (no account token needed; the user presses Submit)."""
    body = build_body(problem, log_lines=15)
    if len(body) > 6000:
        body = body[:6000] + "\n…(truncated)\n"
    q = urllib.parse.urlencode({"title": sanitize(problem.title), "body": body, "labels": "bug"})
    return f"https://github.com/{REPO}/issues/new?{q}"


# ---------------------------------------------------------------------- state
def _state_path() -> Path:
    return log_dir() / "reports.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1))


def should_send(problem: Problem, state: dict | None = None, now: float | None = None) -> bool:
    entry = (state if state is not None else load_state()).get(problem.fingerprint)
    return not entry or (now or time.time()) - entry.get("last", 0) >= COMMENT_INTERVAL_S


# ---------------------------------------------------------------------- GitHub
def _gh(*args: str, input: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], input=input, capture_output=True, text=True, timeout=timeout)


def gh_ready() -> bool:
    if not shutil.which("gh"):
        return False
    try:
        return _gh("auth", "status", timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class ReportError(RuntimeError):
    pass


def submit(problem: Problem) -> str:
    """File (or update) the GitHub issue for ``problem``. Returns the issue URL."""
    state = load_state()
    entry = state.get(problem.fingerprint, {})
    body = build_body(problem)
    title = sanitize(problem.title)[:200]
    number = entry.get("issue")
    try:
        if number:
            # an issue closed since (fixed?) means this is a new occurrence: open a fresh issue
            r = _gh("issue", "view", str(number), "--repo", REPO, "--json", "state", "-q", ".state")
            if r.returncode != 0 or r.stdout.strip().upper() != "OPEN":
                number = None
                entry = {}
        if not number:
            r = _gh("issue", "list", "--repo", REPO, "--state", "open", "--search",
                    f"ditto-fingerprint {problem.fingerprint} in:body", "--json", "number,url", "--limit", "1")
            found = json.loads(r.stdout or "[]") if r.returncode == 0 else []
            if found:
                number, url = found[0]["number"], found[0]["url"]
        if number:
            count = entry.get("count", 1) + 1
            r = _gh("issue", "comment", str(number), "--repo", REPO, "--body-file", "-",
                    input=f"Happened again (seen {count} times).\n\n{body}")
            if r.returncode != 0:
                raise ReportError(r.stderr.strip() or "gh issue comment failed")
            url = entry.get("url") or f"https://github.com/{REPO}/issues/{number}"
        else:
            count = 1
            args = ["issue", "create", "--repo", REPO, "--title", title, "--body-file", "-"]
            r = _gh(*args, *[a for lab in LABELS for a in ("--label", lab)], input=body)
            if r.returncode != 0:              # e.g. a label is missing: try without labels
                r = _gh(*args, input=body)
            if r.returncode != 0:
                raise ReportError(r.stderr.strip() or "gh issue create failed")
            url = r.stdout.strip().splitlines()[-1]
            number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url.rstrip("/").rsplit("/", 1)[-1].isdigit() else 0
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise ReportError(f"could not reach GitHub: {e}") from e
    state[problem.fingerprint] = {"issue": number, "url": url, "count": count, "last": time.time(),
                                  "title": title}
    save_state(state)
    log.info("problem %s reported: %s", problem.fingerprint, url)
    return url


class ProblemHandler(logging.Handler):
    """Calls ``callback(Problem)`` for every logged exception (ERROR or worse with a traceback)."""

    def __init__(self, callback):
        super().__init__(level=logging.ERROR)
        self.callback = callback
        self._busy = False

    def emit(self, record: logging.LogRecord) -> None:
        if not record.exc_info or record.name.startswith(__name__) or self._busy:
            return
        self._busy = True
        try:
            where = record.getMessage().splitlines()[0][:60] if record.levelno < logging.CRITICAL else ""
            self.callback(from_exception(record.exc_info, where))
        except Exception:
            pass
        finally:
            self._busy = False


def crash_file() -> Path:
    return log_dir() / "crash.log"


def take_crash_dump() -> str:
    """Contents of a hard-crash dump left by the previous run (then cleared)."""
    p = crash_file()
    try:
        text = p.read_text(errors="replace").strip()
        if text:
            p.write_text("")
        return text
    except OSError:
        return ""
