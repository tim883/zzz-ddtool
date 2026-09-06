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
    """格子中心(cx,cy)左半区域有深红块 = 已打弃置标。

    标定依据（2026-09-06 `_tile-tagged.png` vs `_tile-notag.png` 像素对比）：
    - 带标格：左半(含中下)深红像素数千，3×3 分布集中在左列全高 + 中下；
    - 无标格：仅右下品质框带红（约 560 像素，落在右半 186..279），
      左半深红像素为 0。
    因此扫描区限定在**左半 + 全高**（cx-56..cx+4, cy-68..cy+68），
    彻底避开右下品质框，带标/无标干净区分。阈值 100 余量充足。
    """
    red = 0
    w = image.width()
    h = image.height()
    for x in range(cx - 56, cx + 4):
        if not (0 <= x < w):
            continue
        for y in range(cy - 68, cy + 68):
            if not (0 <= y < h):
                continue
            color = image.pixelColor(x, y)
            if color.red() > 130 and color.green() < 75 and color.blue() < 75:
                red += 1
                if red >= 100:
                    return True
    return False

