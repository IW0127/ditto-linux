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
