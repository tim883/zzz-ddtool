from __future__ import annotations


def tile_is_selected(image, cx: int, cy: int) -> bool:
    """选中框在格位左右两侧的高亮竖边；避开稀有度的底部色条。

    真机标定（1920×1080）：
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


# 格位「已装备」头像判据：右上角圆形角色头像。
# 2026-10-05 用 4 帧真机截图、17 个格位（8 个有头像、9 个没有）逐一目视标定：
# - 头像圆心约在格位图标中心 (cx+42, cy-50)、半径约 25px（1920×1080 设计坐标）；
# - 判据一：头像窗口里「亮(pixel max>130)且非黄」像素占比——有头像 0.172~0.465、无头像 0~0.031；
# - 判据二：头像内圈（8×8 设计像素）非黄像素平均亮度——有头像 55.9~182.4、无头像 20.3~41.0；
# - 阈值取两档之间 0.10 / 48，两条同时成立才算有头像。
# 旧判据（右上角 58%~98% 窗口里「饱和度>40」占比 ≥0.12）**会漏掉大部分头像**：
# 实测 7/8 个有头像的格位被漏判（0.043~0.112 < 0.12），因为窗口只盖住头像左半边、
# 又混进盘面立绘；也解释过为什么「跳过已装备」实际挡不住。
# 黄色装饰角必须排除：实测它能把亮度类判据推到 0.22/121 的假阳性水平。
PORTRAIT_BRIGHT_MIN = 130
PORTRAIT_BRIGHT_FRACTION = 0.10
PORTRAIT_INNER_MEAN = 48.0


def _is_status_yellow(color) -> bool:
    """盘面「S 徽章 / 黄色装饰角」的判据：高红高绿低蓝。"""
    return color.red() > 180 and color.green() > 150 and color.blue() < 90


def tile_has_portrait(image, left: int, top: int, right: int, bottom: int) -> bool:
    """True if the tile's top-right corner has a circular character head (equipped).

    入参是格位方框（调用方按图标中心 cx±56、cy±68 传）。头像窗口按同一套
    相对几何取：x ∈ [left+78, left+118]、y ∈ [top-2, top+38]（1080p 实测），
    内圈取 x ∈ [left+90, left+106]、y ∈ [top+10, top+26]。
    """
    width = right - left
    height = bottom - top
    if width < 20 or height < 20:
        return False
    sx = width / 112.0
    sy = height / 136.0

    def _px(design_x: float, design_y: float) -> tuple[int, int]:
        return left + round(design_x * sx), top + round(design_y * sy)

    x0, y0 = _px(78, -2)
    x1, y1 = _px(118, 38)
    img_w, img_h = image.width(), image.height()
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img_w, x1), min(img_h, y1)
    if x1 <= x0 or y1 <= y0:
        return False

    bright = total = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            color = image.pixelColor(x, y)
            if _is_status_yellow(color):
                continue
            total += 1
            if max(color.red(), color.green(), color.blue()) > PORTRAIT_BRIGHT_MIN:
                bright += 1
    if total == 0 or bright / total < PORTRAIT_BRIGHT_FRACTION:
        return False

    ix0, iy0 = _px(90, 10)
    ix1, iy1 = _px(106, 26)
    ix0, iy0 = max(0, ix0), max(0, iy0)
    ix1, iy1 = min(img_w, ix1), min(img_h, iy1)
    values = [
        max(image.pixelColor(x, y).red(), image.pixelColor(x, y).green(), image.pixelColor(x, y).blue())
        for y in range(iy0, iy1)
        for x in range(ix0, ix1)
        if not _is_status_yellow(image.pixelColor(x, y))
    ]
    if not values:
        return False
    return sum(values) / len(values) >= PORTRAIT_INNER_MEAN


def tile_has_discard_badge(image, cx: int, cy: int) -> bool | None:
    """格子右下角的纯红矩形小牌 = 已打弃置标。

    实拍标定（1920×1080）：
    - 红牌 RGB(203,0,0) 纯红，约 16×16，位于格子右下角
    - 相对格子中心偏移约 (dx=+34, dy=+28)
    - 扫描区 (cx+20..cx+50, cy+15..cy+45)
    - 有标红像素 245+，无标 0；阈值 50 余量充足

    返回 True（有红牌）/ False（没有）/ **None（拿不准，1~49 像素）**。拿不准时
    调用方按配置决定：该状态配了跳过就重读、耗尽停止；没配就不阻塞。
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
    return False if red == 0 else None


# 格位「已锁定」标记：右下角与弃置标同槽位的**浅灰实心圆 + 黑色挂锁**。
# 样本 tests/fixtures/驱动盘仓库/列表与侧栏.png：
# - 圆盘 RGB(194,194,194)，直径约 26px，圆心相对格子图标中心约 (dx=+41, dy=+28)；
# - 与紧随其后的详情帧 OCR「T 解除锁定」对照 43/43 一致；未锁定格无标记（灰像素 20~34）。
# - 有标格灰像素 297~373、无标 20~34：≥120 判有、≤60 判没有，61~119 算**拿不准**（中间带无样本，保守处理）。
LOCK_BADGE_GREY = 194
LOCK_BADGE_GREY_TOLERANCE = 22
LOCK_BADGE_NEUTRAL_TOLERANCE = 12
LOCK_BADGE_MIN_HITS = 120
LOCK_BADGE_MAX_NEGATIVE_HITS = 60


def tile_has_lock_badge(image, cx: int, cy: int) -> bool | None:
    """格子右下角的浅灰圆盘挂锁 = 已锁定。

    与弃置标共用同一个槽位（弃置是红色圆盘），所以颜色判据必须分开：这里只数
    「接近 194 的中性灰」像素，红盘不会被算成锁定。

    返回 True（有锁标）/ False（没有）/ **None（拿不准）**。拿不准时调用方按配置
    决定：该状态配了跳过就重读、耗尽停止；没配跳过就不阻塞（盘照常点开）。
    """
    hits = 0
    w = image.width()
    h = image.height()
    for x in range(cx + 24, cx + 61):
        if not (0 <= x < w):
            continue
        for y in range(cy + 12, cy + 47):
            if not (0 <= y < h):
                continue
            color = image.pixelColor(x, y)
            r, g, b = color.red(), color.green(), color.blue()
            if (
                abs(r - LOCK_BADGE_GREY) <= LOCK_BADGE_GREY_TOLERANCE
                and abs(r - g) <= LOCK_BADGE_NEUTRAL_TOLERANCE
                and abs(g - b) <= LOCK_BADGE_NEUTRAL_TOLERANCE
            ):
                hits += 1
    if hits >= LOCK_BADGE_MIN_HITS:
        return True
    if hits <= LOCK_BADGE_MAX_NEGATIVE_HITS:
        return False
    return None




# 空位占位（列表末尾不足 9 件时游戏画的空格子）的判据阈值。
# 真机标定（驱动仓库 [152/3000]，末行 8 件）：
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
