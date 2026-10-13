import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from zzz_ddtool.equipped import tile_has_portrait, tile_is_empty, tile_is_selected
from zzz_ddtool.traversal import tail_slots_confirmed

FIXTURES = Path(__file__).parent / "fixtures"

# 头像识别当前在 Runner 中停用；相关用例仅保护保留函数的历史样本行为，
# 不作为已装备跳过功能的验收，也不证明标定样本之外的识别可靠性。
# 标为 legacy，默认不运行；修改保留函数时显式用 -m legacy 验证。


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.legacy
def test_blank_tile_is_not_equipped(qapp: QApplication) -> None:
    image = QImage(120, 120, QImage.Format.Format_RGB32)
    image.fill(QColor(40, 40, 40))
    assert tile_has_portrait(image, 0, 0, 120, 120) is False


@pytest.mark.legacy
def test_colorful_corner_counts_as_portrait(qapp: QApplication) -> None:
    image = QImage(120, 120, QImage.Format.Format_RGB32)
    image.fill(QColor(40, 40, 40))
    painter = QPainter(image)
    painter.setBrush(QColor(220, 140, 120))
    painter.setPen(QColor(220, 140, 120))
    painter.drawEllipse(78, 8, 34, 34)
    painter.end()
    assert tile_has_portrait(image, 0, 0, 120, 120) is True


@pytest.mark.legacy
def test_archived_equipped_tile(qapp: QApplication) -> None:
    sample = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "驱动盘仓库" / "裁剪" / "已装备头像格.png"
    assert sample.is_file(), f"归档样本缺失：{sample}"
    image = QImage(str(sample))
    assert not image.isNull()
    assert tile_has_portrait(image, 0, 0, image.width(), image.height()) is True


@pytest.mark.legacy
@pytest.mark.parametrize(
    "name,expected",
    [
        ("头像判据-有头像-133401-r0c8.png", True),
        ("头像判据-有头像-132833-r0c0.png", True),
        ("头像判据-有头像-161653-r1c0.png", True),
        ("头像判据-无头像-133401-r3c6.png", False),
        ("头像判据-无头像-114200-r1c6.png", False),
    ],
)
def test_portrait_criteria_on_real_tiles(qapp: QApplication, name, expected) -> None:
    """真机格位标定：8 个有头像 / 9 个无头像里挑出的代表样本（2026-10-05）。

    旧判据（右上角窗口里「饱和度>40」占比 ≥0.12）在这批样本上 7/8 漏判——因为窗口
    只盖住头像左半边又混进盘面立绘；保留判据（亮且非黄占比 ≥0.10，且头像内圈平均
    亮度 ≥48）仅在这些标定样本上检查预期输出，没有独立答案集，不能据此启用
    生产头像过滤。无头像样本覆盖灰色盘面角与黄色装饰角。
    原图（1920×1080 真机截图的格位裁剪）在 tests/fixtures/驱动盘仓库/裁剪/。
    """
    sample = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "驱动盘仓库" / "裁剪" / name
    image = QImage(str(sample))
    assert not image.isNull()
    # 裁剪取的是「图标中心 ±(70, 92)」，格位方框在裁剪内的位置固定为 (14, 24)-(126, 160)。
    assert tile_has_portrait(image, 14, 24, 126, 160) is expected


# ---------------------------------------------------------------- 锁标 / 红牌三态（2026-10-05）


def _blank_tile() -> QImage:
    image = QImage(240, 260, QImage.Format.Format_RGB32)
    image.fill(QColor(10, 10, 10))
    return image


def test_lock_badge_three_states(qapp: QApplication) -> None:
    """灰锁判据三态：有标 / 无标 / 拿不准。

    阈值带按真机实测取：有标灰像素 297~373、无标 20~34；≥120 判有、≤60 判没有、
    61~119 拿不准（中间带无样本，按保守处理，由调用方决定重读还是忽略）。
    """
    from zzz_ddtool.equipped import tile_has_lock_badge

    image = _blank_tile()
    assert tile_has_lock_badge(image, 120, 130) is False

    painter = QPainter(image)
    painter.fillRect(144, 142, 37, 35, QColor(194, 194, 194))  # 1295 灰像素
    painter.end()
    assert tile_has_lock_badge(image, 120, 130) is True

    mid = _blank_tile()
    painter = QPainter(mid)
    painter.fillRect(150, 150, 10, 8, QColor(194, 194, 194))  # 80 灰像素：中间带
    painter.end()
    assert tile_has_lock_badge(mid, 120, 130) is None


def test_discard_badge_three_states(qapp: QApplication) -> None:
    """红牌判据三态：有标 / 无标 / 拿不准（1~49 红像素算拿不准）。"""
    from zzz_ddtool.equipped import tile_has_discard_badge

    image = _blank_tile()
    assert tile_has_discard_badge(image, 120, 130) is False

    painter = QPainter(image)
    painter.fillRect(140, 145, 30, 30, QColor(203, 0, 0))  # 900 红像素
    painter.end()
    assert tile_has_discard_badge(image, 120, 130) is True

    mid = _blank_tile()
    painter = QPainter(mid)
    painter.fillRect(150, 155, 5, 5, QColor(203, 0, 0))  # 25 红像素：中间带
    painter.end()
    assert tile_has_discard_badge(mid, 120, 130) is None


@pytest.mark.parametrize("width,height", [(1920, 1080), (1280, 720)])
def test_tile_is_selected_on_real_fixture(qapp: QApplication, width, height) -> None:
    """实拍夹具：选中框在黄↔黄绿闪现动画下也要识别，且不误判邻格与未选中图。"""
    before = QImage(str(FIXTURES / "grid-before-autoscroll.png")).scaled(width, height)
    after = QImage(str(FIXTURES / "grid-after-autoscroll.png")).scaled(width, height)
    assert tile_is_selected(after, 166, 616)
    assert not tile_is_selected(after, 301, 616)
    assert not tile_is_selected(before, 166, 616)


# ---------------------------------------------------------------- 空位占位识别

# 阈值来自真机标定：占位格彩色/亮
# 像素均为 0%，有盘格 6~10% / 30~37%；中间带一律判「拿不准」，由调用方走失败路径。


def test_blank_tile_reads_as_empty(qapp: QApplication) -> None:
    image = QImage(300, 300, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    assert tile_is_empty(image, 150, 150) is True


def test_colorful_tile_reads_as_occupied(qapp: QApplication) -> None:
    image = QImage(300, 300, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    painter = QPainter(image)
    painter.fillRect(92, 98, 116, 102, QColor(240, 200, 60))
    painter.end()
    assert tile_is_empty(image, 150, 150) is False


def test_partly_lit_tile_is_undecided(qapp: QApplication) -> None:
    """中间带不猜：亮像素占比落在两档阈值之间必须判「拿不准」。"""
    image = QImage(300, 300, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    painter = QPainter(image)
    painter.fillRect(130, 130, 44, 44, QColor(120, 120, 120))
    painter.end()
    assert tile_is_empty(image, 150, 150) is None


def test_tile_outside_image_is_undecided(qapp: QApplication) -> None:
    image = QImage(60, 60, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    assert tile_is_empty(image, 5, 5) is None


def test_real_list_tail_only_last_slot_is_empty(qapp: QApplication) -> None:
    """真机图重放：驱动仓库 [152/3000] 末行 8 件，第 9 格是空位，其余 35 格有盘。"""
    image = QImage(str(FIXTURES / "list-tail-empty-slot.png"))
    assert not image.isNull()
    origin_y, pitch = 267.5, 174.0
    empty: list[tuple[int, int]] = []
    for row in range(4):
        for col in range(9):
            cx = 166 + 135 * col
            cy = int(round(origin_y + pitch * row))
            verdict = tile_is_empty(image, cx, cy)
            assert verdict is not None, f"格位 {(row, col)} 未知，不能忽略"
            if verdict is True:
                empty.append((row, col))
    assert empty == [(3, 8)]


def test_real_small_list_three_rows_empty_tail(qapp: QApplication) -> None:
    """真机图重放：谶羽之誓 1 号位 [23/3000] 只有 3 行（9+9+5），

    第 3 行第 6~9 格与整个第 4 行都是游戏画出来的空位占位；其余 23 格有盘。
    这张图对应 E6「1~3 行小列表」场景（一屏装得下，游戏根本不滚动）。
    """
    image = QImage(str(FIXTURES / "list-small-three-rows.png"))
    assert not image.isNull()
    origin_y, pitch = 268.5, 174.5
    empty: list[tuple[int, int]] = []
    occupied: set[tuple[int, int]] = set()
    for row in range(4):
        for col in range(9):
            cx = 166 + 135 * col
            cy = int(round(origin_y + pitch * row))
            verdict = tile_is_empty(image, cx, cy)
            assert verdict is not None, f"格位 {(row, col)} 未知，不能忽略"
            if verdict is True:
                empty.append((row, col))
            elif verdict is False:
                occupied.add((row, col))
    assert empty == [(2, 5), (2, 6), (2, 7), (2, 8)] + [(3, col) for col in range(9)]
    assert len(occupied) == 23


def test_real_small_list_tail_slots_confirmed(qapp: QApplication) -> None:
    """同一张真机图：23 件占满前 3 行、末行 5 件，末尾占位构成连续尾段 → 确认到底。"""
    image = QImage(str(FIXTURES / "list-small-three-rows.png"))
    assert not image.isNull()
    origin_y, pitch = 268.5, 174.5
    occupied, empty = set(), set()
    for row in range(4):
        for col in range(9):
            cx = 166 + 135 * col
            cy = int(round(origin_y + pitch * row))
            verdict = tile_is_empty(image, cx, cy)
            assert verdict is not None, f"格位 {(row, col)} 未知，不能当作空位"
            (empty if verdict is True else occupied).add((row, col))
    assert occupied == {(row, col) for row in range(3) for col in range(9) if row < 2 or col < 5}
    assert empty == {(row, col) for row in range(4) for col in range(9)} - occupied
    assert tail_slots_confirmed(occupied, empty, rows=4, cols=9)
    # 抽掉一个空位（模拟没确认到）就必须拒绝，不能当到底
    assert not tail_slots_confirmed(occupied, empty - {(2, 8)}, rows=4, cols=9)


def test_real_single_row_list_tail(qapp: QApplication) -> None:
    """真机图重放：谶羽之誓 4 号位 + 攻击力% [3/3000]，只有首行 3 件，其余 33 格空位。"""
    image = QImage(str(FIXTURES / "list-single-row-three-discs.png"))
    assert not image.isNull()
    occupied: set[tuple[int, int]] = set()
    empty: set[tuple[int, int]] = set()
    for row in range(4):
        for col in range(9):
            cx = 166 + 135 * col
            cy = int(round(268.5 + 174.5 * row))
            verdict = tile_is_empty(image, cx, cy)
            assert verdict is not None, f"格位 {(row, col)} 未知，不能当作空位"
            if verdict is False:
                occupied.add((row, col))
            else:
                empty.add((row, col))
    assert occupied == {(0, 0), (0, 1), (0, 2)}
    assert len(empty) == 33
    assert tail_slots_confirmed(occupied, empty, rows=4, cols=9)
