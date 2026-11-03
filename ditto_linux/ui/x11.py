"""Force-activate a window on X11 / XWayland.

Window managers (notably GNOME's Mutter) apply focus-stealing prevention to windows shown by a
background process: when the Quick Paste window is opened from a global shortcut it would be mapped
*behind* the active window. Sending ``_NET_ACTIVE_WINDOW`` with source indication 2 ("pager", i.e.
a request made on behalf of the user) is the standard EWMH way to ask the window manager to raise and
focus it anyway.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os

log = logging.getLogger(__name__)

_X11 = None


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("xclient", _XClientMessageEvent), ("pad", ctypes.c_long * 24)]


def _lib():
    global _X11
    if _X11 is None:
        name = ctypes.util.find_library("X11")
        if not name:
            _X11 = False
            return None
        lib = ctypes.CDLL(name)
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XInternAtom.restype = ctypes.c_ulong
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
                                   ctypes.POINTER(_XEvent)]
        lib.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        _X11 = lib
    return _X11 or None


def activate_window(win_id: int) -> bool:
    """Ask the window manager to raise and focus ``win_id``. Returns False when not possible."""
    lib = _lib()
    if lib is None or not os.environ.get("DISPLAY"):
        return False
    dpy = lib.XOpenDisplay(None)
    if not dpy:
        return False
    try:
        root = lib.XDefaultRootWindow(dpy)
        ev = _XEvent()
        ev.xclient.type = 33  # ClientMessage
        ev.xclient.send_event = 1
        ev.xclient.display = dpy
        ev.xclient.window = win_id
        ev.xclient.message_type = lib.XInternAtom(dpy, b"_NET_ACTIVE_WINDOW", 0)
        ev.xclient.format = 32
        ev.xclient.data[0] = 2  # source indication: pager (user request)
        ev.xclient.data[1] = 0  # CurrentTime
        ev.xclient.data[2] = 0
        mask = (1 << 19) | (1 << 20)  # SubstructureNotifyMask | SubstructureRedirectMask
        lib.XSendEvent(dpy, root, 0, mask, ctypes.byref(ev))
        lib.XRaiseWindow(dpy, win_id)
        lib.XFlush(dpy)
        return True
    except Exception:
        log.exception("X11 activation failed")
        return False
    finally:
        lib.XCloseDisplay(dpy)


# ---------------------------------------------------------------------- clipboard owner (ignore apps)
class _XResClientIdSpec(ctypes.Structure):
    _fields_ = [("client", ctypes.c_ulong), ("mask", ctypes.c_uint)]


class _XResClientIdValue(ctypes.Structure):
    _fields_ = [("spec", _XResClientIdSpec), ("length", ctypes.c_long), ("value", ctypes.c_void_p)]


class _XClassHint(ctypes.Structure):
    _fields_ = [("res_name", ctypes.c_char_p), ("res_class", ctypes.c_char_p)]


_XRES = None
_owner_dpy = None
# processes that own the clipboard on behalf of others (Wayland apps seen through XWayland)
PROXY_OWNERS = {"xwayland", "gnome-shell", "mutter", "kwin_wayland", "kwin_x11", "weston", "sway", "xorg"}
_INTERPRETERS = {"python", "python3", "node", "electron", "java", "sh", "bash", "perl", "ruby", "gjs"}


def _xres():
    global _XRES
    if _XRES is None:
        name = ctypes.util.find_library("XRes")
        if not name:
            _XRES = False
            return None
        lib = ctypes.CDLL(name)
        lib.XResQueryClientIds.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_XResClientIdSpec),
                                           ctypes.POINTER(ctypes.c_long),
                                           ctypes.POINTER(ctypes.POINTER(_XResClientIdValue))]
        lib.XResGetClientPid.argtypes = [ctypes.POINTER(_XResClientIdValue)]
        lib.XResGetClientPid.restype = ctypes.c_int
        lib.XResClientIdsDestroy.argtypes = [ctypes.c_long, ctypes.POINTER(_XResClientIdValue)]
        _XRES = lib
    return _XRES or None


def process_names(pid: int) -> list[str]:
    """Names an app can be matched by: process name, executable, script (for python/node apps)."""
    names: list[str] = []
    try:
        names.append(open(f"/proc/{pid}/comm").read().strip())
    except OSError:
        pass
    try:
        names.append(os.path.basename(os.readlink(f"/proc/{pid}/exe")))
    except OSError:
        pass
    try:
        argv = [a for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if a]
        if argv:
            names.append(os.path.basename(argv[0].decode(errors="replace")))
            base = names[-1].lower().rstrip("0123456789.")
            if base in _INTERPRETERS:
                script, args = "", iter(argv[1:])
                for arg in args:
                    if arg == b"-c":
                        break                      # inline code: no script name
                    if arg == b"-m":
                        script = next(args, b"").decode(errors="replace")
                        break
                    if not arg.startswith(b"-"):
                        script = arg.decode(errors="replace")
                        break
                if script:
                    names.insert(0, os.path.splitext(os.path.basename(script))[0])
    except OSError:
        pass
    seen, out = set(), []
    for n in names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def clipboard_owner() -> dict | None:
    """Who owns the X11 CLIPBOARD selection: {'pid', 'names', 'wm_class'} or None when unknown.

    Unknown means: no X11, the owner is a proxy for a native Wayland app, or the query failed.
    """
    global _owner_dpy
    lib = _lib()
    if lib is None or not os.environ.get("DISPLAY"):
        return None
    try:
        if not _owner_dpy:
            _owner_dpy = lib.XOpenDisplay(None)
            if not _owner_dpy:
                return None
            lib.XGetSelectionOwner.restype = ctypes.c_ulong
            lib.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            lib.XGetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XClassHint)]
            lib.XFree.argtypes = [ctypes.c_void_p]
        dpy = _owner_dpy
        win = lib.XGetSelectionOwner(dpy, lib.XInternAtom(dpy, b"CLIPBOARD", 0))
        if not win:
            return None
        wm_class: list[str] = []
        hint = _XClassHint()
        if lib.XGetClassHint(dpy, win, ctypes.byref(hint)):
            wm_class = [v.decode(errors="replace") for v in (hint.res_name, hint.res_class) if v]
        pid = 0
        xres = _xres()
        if xres:
            spec = _XResClientIdSpec(client=win, mask=2)  # XRES_CLIENT_ID_PID_MASK
            num = ctypes.c_long(0)
            ids = ctypes.POINTER(_XResClientIdValue)()
            if xres.XResQueryClientIds(dpy, 1, ctypes.byref(spec), ctypes.byref(num), ctypes.byref(ids)) == 0:
                for i in range(num.value):
                    p = xres.XResGetClientPid(ctypes.byref(ids[i]))
                    if p > 0:
                        pid = p
                        break
                xres.XResClientIdsDestroy(num, ids)
        names = process_names(pid) if pid else []
        if not names and not wm_class:
            return None
        if names and names[0].lower() in PROXY_OWNERS or any(n.lower() in PROXY_OWNERS for n in names[:2]):
            return None
        return {"pid": pid, "names": names, "wm_class": wm_class}
    except Exception:
        log.debug("clipboard owner query failed", exc_info=True)
        return None
