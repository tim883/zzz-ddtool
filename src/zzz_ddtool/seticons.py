from __future__ import annotations

from zzz_ddtool.paths import assets_dir


def set_icon_path(name: str):
    return assets_dir() / "sets" / f"{name}.png"


def _fit_font(name: str, size: int):
    """在 size×size 的块里放得下**完整**套装名的最大字号。

    自上而下收缩（下限 6pt），保证 5~6 字的套装名也不会超出块宽被裁掉。
    """
    from PySide6.QtGui import QFont, QFontMetrics

    limit = size - max(4, size // 10)
    pt = max(10, size // 6)
    while pt > 6:
        font = QFont("Microsoft YaHei", pt)
        font.setBold(True)
        if QFontMetrics(font).horizontalAdvance(name) <= limit:
            return font
        pt -= 1
    font = QFont("Microsoft YaHei", 6)
    font.setBold(True)
    return font


def paint_set_icon(name: str, size: int = 72):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap

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
    painter.setFont(_fit_font(name, size))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, name)
    painter.end()
    return pix
