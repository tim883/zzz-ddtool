from __future__ import annotations


def tile_is_selected(image, cx: int, cy: int) -> bool:
    """选中框在格位左右两侧的高亮竖边；避开稀有度的底部色条。

    2026-10-02 真机标定（1920×1080，log/captures/标定-锚点-点击后.png）：
    点中第 3 行第 5 列后扫描该格：
    - 左右竖线中心在 x = cx-62 / cx+64，线宽约 6px（横扫 y=cy 得 x:641-647 / 767-773）
    - 上下边在 y = cy-56 / cy+54（竖扫 x=cx 得 y:558-564 / 666-677）
    - 竖线在 y ∈ [cy-45, cy+45] 内连续

    旧实现只在 dx=-62 / +60 两条固定线上按 0.6 占比判决：真机右边线中心是
    cx+64，采样线落在 cx+60 已经偏出线外，实测占比 19/38=0.5 → 恒判未选中。
    改为在左右候选带内各取「黄色占比最高」的一列，两侧都达标（≥0.7）才判选中，
    对线宽、缩放与 1px 偏移都稳。左右基本对称是选中框的固有几何，用它兜住
    误命中（半列间距内的其它竖直元素不会同时满足两侧）。
    阈值取 0.7 而非 0.8：选中框有黄↔黄绿闪现动画，
    tests/fixtures/grid-before-autoscroll.png 捕获的即是动画帧，右线占比实测
    0.79；同图邻格因候选带扫到相邻框线左侧可达 0.79，但另一侧为空，不误判。
    """
    from zzz_ddtool.layout import scale_point

    width, height = image.width(), image.height()

    def column_ratio(design_x: int) -> float:
        hits = total = 0
        for dy in range(-45, 46, 5):
            x, y = scale_point(design_x, cy + dy, width, height)
            if not (0 <= x < width and 0 <= y < height):
                continue
            total += 1
            color = image.pixelColor(x, y)
            # 选中框会在黄绿色/黄色之间闪动，两种动画颜色都应命中。
            if color.red() > 100 and color.green() > 140 and color.blue() < 80:
                hits += 1
        return hits / total if total else 0.0

    left = max(range(cx - 80, cx - 44), key=column_ratio)
    right = max(range(cx + 44, cx + 81), key=column_ratio)
    if column_ratio(left) < 0.7 or column_ratio(right) < 0.7:
        return False
    return abs((cx - left) - (right - cx)) <= 16


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

