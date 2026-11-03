"""Stand-in for "another application" in end-to-end tests.

    cliptool.py set-text TEXT [--secret]   own the clipboard with TEXT until killed / ownership lost
    cliptool.py set-html HTML TEXT         own the clipboard with HTML + plain-text alternative
    cliptool.py set-image W H              own the clipboard with a W x H red PNG image
    cliptool.py read [MIME]                print the clipboard (text, or hex-length of MIME data)

Prints "READY" once it owns the clipboard so tests can synchronise.
"""
import sys

from PyQt6.QtCore import QMimeData, QTimer
from PyQt6.QtGui import QClipboard, QColor, QImage
from PyQt6.QtWidgets import QApplication


def main() -> int:
    app = QApplication(sys.argv[:1])
    cb = app.clipboard()
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "read":
        md = cb.mimeData(QClipboard.Mode.Clipboard)
        if args:
            data = bytes(md.data(args[0])) if md.hasFormat(args[0]) else b""
            if args[0] == "image" and md.hasImage():
                img = md.imageData()
                print(f"IMAGE {img.width()}x{img.height()}")
                return 0
            sys.stdout.write(data.decode("utf-8", errors="replace"))
        else:
            sys.stdout.write("FORMATS " + ",".join(sorted(md.formats())) + "\n")
            sys.stdout.write(md.text())
        sys.stdout.flush()
        return 0

    md = QMimeData()
    if cmd == "set-text":
        md.setText(args[0])
        if "--secret" in args:
            md.setData("x-kde-passwordManagerHint", b"secret")
    elif cmd == "set-html":
        md.setHtml(args[0])
        md.setText(args[1])
    elif cmd == "set-image":
        img = QImage(int(args[0]), int(args[1]), QImage.Format.Format_RGB32)
        img.fill(QColor("red"))
        md.setImageData(img)
    else:
        print(f"unknown command {cmd}", file=sys.stderr)
        return 2
    cb.setMimeData(md, QClipboard.Mode.Clipboard)
    # Exit when another client takes ownership, like a real app would stop serving.
    cb.dataChanged.connect(lambda: QTimer.singleShot(0, app.quit) if not cb.ownsClipboard() else None)
    QTimer.singleShot(0, lambda: (print("READY", flush=True)))
    QTimer.singleShot(60_000, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
