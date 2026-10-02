import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from zzz_ddtool.equipped import tile_has_portrait, tile_is_empty, tile_is_selected
from zzz_ddtool.traversal import tail_slots_confirmed

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


def test_blank_tile_is_not_equipped(qapp: QApplication) -> None:
    image = QImage(120, 120, QImage.Format.Format_RGB32)
    image.fill(QColor(40, 40, 40))
    assert tile_has_portrait(image, 0, 0, 120, 120) is False


def test_colorful_corner_counts_as_portrait(qapp: QApplication) -> None:
    image = QImage(120, 120, QImage.Format.Format_RGB32)
    image.fill(QColor(40, 40, 40))
    painter = QPainter(image)
    painter.setBrush(QColor(220, 140, 120))
    painter.setPen(QColor(220, 140, 120))
    painter.drawEllipse(78, 8, 34, 34)
    painter.end()
    assert tile_has_portrait(image, 0, 0, 120, 120) is True


def test_user_equipped_tile_if_present(qapp: QApplication) -> None:
    sample = Path(__file__).resolve().parents[1] / "手册" / "素材" / "已装备-头像格.png"
    if not sample.is_file():
        pytest.skip("no equipped tile crop")
    image = QImage(str(sample))
    assert not image.isNull()
    assert tile_has_portrait(image, 0, 0, image.width(), image.height()) is True


@pytest.mark.parametrize("width,height", [(1920, 1080), (1280, 720)])
def test_tile_is_selected_on_real_fixture(qapp: QApplication, width, height) -> None:
    """实拍夹具：选中框在黄↔黄绿闪现动画下也要识别，且不误判邻格与未选中图。"""
    before = QImage(str(FIXTURES / "grid-before-autoscroll.png")).scaled(width, height)
    after = QImage(str(FIXTURES / "grid-after-autoscroll.png")).scaled(width, height)
    assert tile_is_selected(after, 166, 616)
    assert not tile_is_selected(after, 301, 616)
    assert not tile_is_selected(before, 166, 616)


# ---------------------------------------------------------------- 空位占位识别

# 阈值来自 2026-10-02 真机标定（log/captures/list-e5-scroll20.png）：占位格彩色/亮
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
            if tile_is_empty(image, cx, cy) is True:
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
            if verdict is True:
                empty.append((row, col))
            elif verdict is False:
                occupied.add((row, col))
    assert empty == [(2, 5), (2, 6), (2, 7), (2, 8)] + [(3, col) for col in range(9)]
    assert len(occupied) == 23


def test_real_small_list_tail_slots_confirmed(qapp: QApplication) -> None:
    """同一张真机图：23 件占满前 3 行、末行 5 件，末尾占位构成连续尾段 → 确认到底。"""
    image = QImage(str(FIXTURES / "list-small-three-rows.png"))
    origin_y, pitch = 268.5, 174.5
    occupied, empty = set(), set()
    for row in range(4):
        for col in range(9):
            cx = 166 + 135 * col
            cy = int(round(origin_y + pitch * row))
            (occupied if tile_is_empty(image, cx, cy) is False else empty).add((row, col))
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
            if tile_is_empty(image, cx, cy) is False:
                occupied.add((row, col))
            else:
                empty.add((row, col))
    assert occupied == {(0, 0), (0, 1), (0, 2)}
    assert len(empty) == 33
    assert tail_slots_confirmed(occupied, empty, rows=4, cols=9)
