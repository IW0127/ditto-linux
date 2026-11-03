"""Application icon drawn at runtime (no binary assets needed)."""
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap


def app_icon(paused: bool = False) -> QIcon:
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64, 128):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#9e9e9e" if paused else "#2f6fde"))
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(size * 0.06, size * 0.06, size * 0.88, size * 0.88)
        p.drawRoundedRect(r, size * 0.2, size * 0.2)
        p.setPen(QColor("white"))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int(size * 0.62))
        p.setFont(f)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, "D")
        p.end()
        icon.addPixmap(pm)
    return icon
