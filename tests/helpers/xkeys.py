"""Hold / release keys on an X server through XTest (no xdotool needed).

Reads commands from stdin, one per line: ``down <keysym>``, ``up <keysym>``, ``quit``.
Prints ``READY`` once connected and ``OK`` after each command. Every key still held on exit is released.
"""
import ctypes
import ctypes.util
import sys


def main() -> int:
    x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    xtst = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        print("cannot open display", file=sys.stderr)
        return 1
    held: set[int] = set()

    def send(code: int, press: bool):
        xtst.XTestFakeKeyEvent(dpy, code, int(press), 0)
        x11.XSync(dpy, 0)
        (held.add if press else held.discard)(code)

    print("READY", flush=True)
    try:
        for line in sys.stdin:
            parts = line.split()
            if not parts or parts[0] == "quit":
                break
            code = x11.XKeysymToKeycode(dpy, x11.XStringToKeysym(parts[1].encode()))
            send(code, parts[0] == "down")
            print("OK", flush=True)
    finally:
        for code in list(held):
            send(code, False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
