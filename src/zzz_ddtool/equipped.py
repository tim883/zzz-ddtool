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



# 空位占位（列表末尾不足 9 件时游戏画的空格子）的判据阈值。
# 2026-10-02 真机标定（log/captures/list-e5-scroll20.png，驱动仓库 [152/3000]，末行 8 件）：
# - 占位格图标区 (cx±58, cy±52) 实测彩色像素 0%、亮像素 0%（整块黑底 + 灰叉）
# - 有盘格同区域实测彩色像素 6~10%、亮像素 30~37%（两帧共 72 格全测）
# 两档阈值：双低=确认空位，任一偏高=确认有盘，中间带=拿不准。
EMPTY_TILE_COLORFUL_MAX = 0.02
EMPTY_TILE_BRIGHT_MAX = 0.10
EMPTY_TILE_COLORFUL_FULL = 0.04
EMPTY_TILE_BRIGHT_FULL = 0.20


def tile_is_empty(image, cx: int, cy: int) -> bool | None:
    """图标区是否为游戏画出来的空位占位。

    True=已确认这里没有盘（合法空位），False=已确认有盘，None=拿不准。
    只采样图标区域，不看「等级」标签——spec §5.5 第 259 行明确禁止只凭
    「没 OCR 到等级」宣布空位；拿不准的位置由调用方按失败路径处理。
    """
    width, height = image.width(), image.height()
    x0, x1 = cx - 58, cx + 58
    y0, y1 = cy - 52, cy + 50
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        return None
    colorful = bright = total = 0
    for y in range(y0, y1, 3):
        for x in range(x0, x1, 3):
            color = image.pixelColor(x, y)
            r, g, b = color.red(), color.green(), color.blue()
            top, low = max(r, g, b), min(r, g, b)
            total += 1
            if top > 90:
                bright += 1
            if top - low > 60 and top > 70:
                colorful += 1
    if total == 0:
        return None
    colorful_ratio = colorful / total
    bright_ratio = bright / total
    if colorful_ratio <= EMPTY_TILE_COLORFUL_MAX and bright_ratio <= EMPTY_TILE_BRIGHT_MAX:
        return True
    if colorful_ratio >= EMPTY_TILE_COLORFUL_FULL or bright_ratio >= EMPTY_TILE_BRIGHT_FULL:
        return False
    return None
