from __future__ import annotations

from zzz_ddtool.paths import assets_dir


def set_icon_path(name: str):
    return assets_dir() / "sets" / f"{name}.png"


def paint_set_icon(name: str, size: int = 72):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

    path = set_icon_path(name)
    if path.is_file():
        pix = QPixmap(str(path))
        if not pix.isNull():
            return pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

    hue = sum(ord(char) for char in name) % 360
    pix = QPixmap(size, size)
    pix.fill(QColor.fromHsv(hue, 90, 230))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#1a1a1a"))
    font = QFont("Microsoft YaHei", max(10, size // 6))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, name[:2])
    painter.end()
    return pix
