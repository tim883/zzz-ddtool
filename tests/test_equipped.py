import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from zzz_ddtool.equipped import tile_has_portrait, tile_is_empty, tile_is_selected

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
