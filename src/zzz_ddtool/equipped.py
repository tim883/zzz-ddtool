from __future__ import annotations


def tile_is_selected(image, cx: int, cy: int) -> bool:
    """选中框在盘图两侧的亮黄色竖边；避开稀有度的底部色条。"""
    from zzz_ddtool.layout import scale_point

    hits = total = 0
    for dx in (-62, 60):
        for dy in range(-35, 51, 5):
            x, y = scale_point(cx + dx, cy + dy, image.width(), image.height())
            if not (0 <= x < image.width() and 0 <= y < image.height()):
                continue
            color = image.pixelColor(x, y)
            total += 1
            # 选中框会在黄绿色/黄色之间闪动，两种动画颜色都应命中。
            if color.red() > 100 and color.green() > 140 and color.blue() < 80:
                hits += 1
    return total > 0 and hits / total >= 0.6


def grid_scroll_position(image) -> tuple[int, int] | None:
    """只读滚动条滑块位置，不执行 OCR；小列表没有滑块时返回 None。"""
    from zzz_ddtool.layout import scale_point

    x, top = scale_point(1365, 233, image.width(), image.height())
    _, bottom = scale_point(1365, 870, image.width(), image.height())
    runs = []
    start = None
    for y in range(top, min(bottom, image.height())):
        color = image.pixelColor(x, y)
        channels = (color.red(), color.green(), color.blue())
        bright = min(channels) > 70 and max(channels) - min(channels) < 25
        if bright and start is None:
            start = y
        elif not bright and start is not None:
            runs.append((start, y - 1))
            start = None
    if start is not None:
        runs.append((start, bottom - 1))
    minimum = max(5, round(20 * image.height() / 1080))
    runs = [run for run in runs if run[1] - run[0] >= minimum]
    return max(runs, key=lambda run: run[1] - run[0], default=None)


def grid_did_scroll(before, after) -> bool:
    old, new = grid_scroll_position(before), grid_scroll_position(after)
    tolerance = max(2, round(3 * after.height() / 1080))
    return old != new if old is None or new is None else max(abs(a - b) for a, b in zip(old, new)) > tolerance


def grid_scroll_limits(image) -> tuple[bool, bool]:
    """滑块到轨道端点时边界行可处理；无滑块代表列表无需滚动。"""
    from zzz_ddtool.layout import scale_point

    position = grid_scroll_position(image)
    if position is None:
        return True, True
    _, top = scale_point(1365, 233, image.width(), image.height())
    _, bottom = scale_point(1365, 870, image.width(), image.height())
    margin = max(3, round(7 * image.height() / 1080))
    return position[0] <= top + margin, position[1] >= bottom - margin


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

