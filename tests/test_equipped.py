import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from zzz_ddtool.equipped import tile_has_portrait, tile_is_selected

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
