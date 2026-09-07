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
    """格子右下角的纯红矩形小牌 = 已打弃置标。

    实拍标定（log/captures/verify-discard.png, 1920×1080）：
    - 红牌 RGB(203,0,0) 纯红，约 16×16，位于格子右下角
    - 相对格子中心偏移约 (dx=+34, dy=+28)
    - 扫描区 (cx+20..cx+50, cy+15..cy+45)
    - 有标红像素 245+，无标 0；阈值 50 余量充足
    """
    red = 0
    w = image.width()
    h = image.height()
    for x in range(cx + 20, cx + 50):
        if not (0 <= x < w):
            continue
        for y in range(cy + 15, cy + 45):
            if not (0 <= y < h):
                continue
            color = image.pixelColor(x, y)
            if color.red() > 150 and color.green() < 30 and color.blue() < 30:
                red += 1
                if red >= 50:
                    return True
    return False

