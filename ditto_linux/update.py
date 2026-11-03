"""Auto-update from the project's GitHub releases.

``check()`` asks the GitHub API for the latest release; ``download()`` fetches the right asset for this
install (``.deb`` for the package, ``.tar.gz`` for a per-user install) and verifies it against the
release's ``SHA256SUMS``; ``install()`` applies it (``pkexec apt-get install`` for the package, the
bundled ``install.sh`` for the tarball). A git checkout is never touched: it only gets notified.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_NAME, __version__
from .config import data_dir

log = logging.getLogger(__name__)

REPO = "IW0127/ditto-linux"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
PACKAGE_DIR = Path(__file__).resolve().parent


def api_url() -> str:
    return os.environ.get("DITTO_UPDATE_URL") or f"https://api.github.com/repos/{REPO}/releases/latest"


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text.split("-")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass
class Release:
    version: str
    tag: str
    notes: str = ""
    url: str = RELEASES_PAGE
    assets: dict[str, str] = field(default_factory=dict)   # name -> download url

    def to_dict(self) -> dict:
        return {"version": self.version, "tag": self.tag, "url": self.url, "assets": sorted(self.assets),
                "current": __version__, "newer": is_newer(self.version)}


class UpdateError(RuntimeError):
    pass


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest(timeout: float = 15) -> Release:
    try:
        with _open(api_url(), timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise UpdateError(f"could not check for updates: {e}") from e
    tag = data.get("tag_name") or ""
    if not tag:
        raise UpdateError("no release found")
    return Release(version=tag.lstrip("vV"), tag=tag, notes=data.get("body") or "",
                   url=data.get("html_url") or RELEASES_PAGE,
                   assets={a["name"]: a["browser_download_url"] for a in data.get("assets", [])
                           if a.get("name") and a.get("browser_download_url")})


def check(timeout: float = 15) -> Release | None:
    """The latest release if it is newer than this version, else None."""
    rel = latest(timeout)
    log.info("update check: latest %s, running %s", rel.version, __version__)
    return rel if is_newer(rel.version) else None


def install_kind(package_dir: Path = PACKAGE_DIR) -> str:
    """'deb' (system package), 'git' (source checkout) or 'tarball' (per-user install)."""
    if str(package_dir).startswith("/usr/"):
        return "deb"
    if (package_dir.parent / ".git").exists():
        return "git"
    return "tarball"


def asset_for(rel: Release, kind: str) -> str:
    want = ".deb" if kind == "deb" else ".tar.gz"
    for name in rel.assets:
        if name.endswith(want):
            return name
    raise UpdateError(f"release {rel.tag} has no {want} file")


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / APP_NAME / "updates"


def download(rel: Release, kind: str, dest: Path | None = None, timeout: float = 60) -> Path:
    name = asset_for(rel, kind)
    dest = dest or cache_dir()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    try:
        with _open(rel.assets[name], timeout) as r, open(target, "wb") as f:
            shutil.copyfileobj(r, f)
        sums = {}
        if "SHA256SUMS" in rel.assets:
            with _open(rel.assets["SHA256SUMS"], timeout) as r:
                for line in r.read().decode().splitlines():
                    parts = line.split()
                    if len(parts) == 2:
                        sums[parts[1].lstrip("*").removeprefix("./")] = parts[0].lower()
    except OSError as e:
        raise UpdateError(f"download failed: {e}") from e
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if name not in sums:
        target.unlink(missing_ok=True)
        raise UpdateError("the release has no checksum for the download; not installing it")
    if sums[name] != digest:
        target.unlink(missing_ok=True)
        raise UpdateError(f"checksum mismatch for {name}; not installing it")
    log.info("downloaded %s (sha256 %s)", target, digest)
    return target


def deb_command(path: Path, gui: bool) -> list[str]:
    apt = ["apt-get", "install", "-y", "--allow-downgrades", str(path)]
    if os.geteuid() == 0:
        return apt
    if gui and shutil.which("pkexec"):
        return ["pkexec", *apt]           # graphical password prompt
    return ["sudo", *apt]


def install(path: Path, kind: str, gui: bool = True) -> None:
    """Apply a downloaded update. Raises UpdateError on failure."""
    if kind == "deb":
        cmd = deb_command(path, gui)
        log.info("installing update: %s", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=gui, text=True)
        if r.returncode != 0:
            raise UpdateError(f"installation failed (exit {r.returncode}): {(r.stderr or '').strip()[-400:]}")
    elif kind == "tarball":
        root = data_dir() / "app"
        root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path) as t:
            top = {m.name.split("/")[0] for m in t.getmembers()}
            if len(top) != 1:
                raise UpdateError("unexpected archive layout")
            try:
                t.extractall(root, filter="data")
            except TypeError:               # Python without extraction filters
                t.extractall(root)
        installer = root / top.pop() / "install.sh"
        log.info("installing update with %s", installer)
        r = subprocess.run(["bash", str(installer)], capture_output=True, text=True)
        if r.returncode != 0:
            raise UpdateError(f"installer failed (exit {r.returncode}): {r.stderr.strip()[-400:]}")
    else:
        raise UpdateError("this is a source checkout; update it with 'git pull'")
    log.info("update installed from %s", path)


def launcher() -> str:
    return shutil.which(APP_NAME) or "/usr/bin/ditto-linux"


def self_argv(args: list[str]) -> list[str]:
    """Run this same copy of ditto_linux again (independent of the current directory)."""
    return [sys.executable, "-c", 'import sys; sys.path.insert(0, sys.argv.pop(1)); from ditto_linux.cli import main; sys.exit(main())', str(PACKAGE_DIR.parent), *args]


def restart_argv(args: list[str]) -> list[str]:
    """How to start the (possibly updated) daemon again with the same arguments."""
    exe = launcher()
    if os.path.exists(exe):
        return [exe, *args]
    return self_argv(args)
