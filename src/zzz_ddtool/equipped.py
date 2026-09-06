from __future__ import annotations


def tile_has_portrait(image, left: int, top: int, right: int, bottom: int) -> bool:
    """True if the tile's top-right corner has a circular character head (equipped)."""
    width = right - left
    height = bottom - top
    if width < 20 or height < 20:
        return False
    x0 = left + int(width * 0.58)
    y0 = top + int(height * 0.02)
    x1 = left + int(width * 0.98)
    y1 = top + int(height * 0.40)
    colorful = 0
    total = 0
    step = max(1, (x1 - x0) // 16)
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            color = image.pixelColor(x, y)
            total += 1
            r, g, b = color.red(), color.green(), color.blue()
            mx, mn = max(r, g, b), min(r, g, b)
            if mx - mn < 40:
                continue
            if r > 180 and g > 150 and b < 90:
                continue
            colorful += 1
    return total > 0 and colorful / total >= 0.12


def tile_has_discard_badge(image, cx: int, cy: int) -> bool:
    """格子中心右上区域的深红圆牌 = 已打弃置标（2026-09-06 实拍标定）。

    标记中心约在 (cx+40, cy-55)，区域 (cx+22..cx+60, cy-75..cy-38)。
    实测有标 ≈330 红像素、无标 0，阈值 50 余量充足。
    """
    red = 0
    for x in range(cx + 22, cx + 60, 2):
        for y in range(cy - 75, cy - 38, 2):
            color = image.pixelColor(x, y)
            if color.red() > 130 and color.green() < 75 and color.blue() < 75:
                red += 1
    return red >= 50
