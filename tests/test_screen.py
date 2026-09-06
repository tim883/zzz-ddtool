import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from zzz_ddtool.layout import scale_point
from zzz_ddtool.screen import MENU, OVERWORLD, STORAGE, classify_image


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


def _blank(width: int = 1920, height: int = 1080) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(30, 32, 36))
    return image


def _paint_menu(image: QImage) -> QImage:
    painter = QPainter(image)
    painter.fillRect(80, 28, 90, 46, QColor(196, 32, 8))
    centers = (317, 430, 554, 671, 782, 891, 1007, 1122, 1250, 1375, 1481, 1594)
    for cx in centers:
        painter.fillRect(cx - 18, 948, 36, 36, QColor(240, 240, 240))
    painter.end()
    return image


def test_classify_menu(qapp: QApplication) -> None:
    assert classify_image(_paint_menu(_blank())) == MENU


def test_classify_overworld(qapp: QApplication) -> None:
    image = _blank()
    painter = QPainter(image)
    painter.fillRect(40, 990, 160, 40, QColor(250, 250, 250))
    painter.end()
    assert classify_image(image) == OVERWORLD


def test_classify_storage_keeps_back_without_dock(qapp: QApplication) -> None:
    image = _blank()
    painter = QPainter(image)
    painter.fillRect(80, 28, 90, 46, QColor(196, 32, 8))
    painter.end()
    assert classify_image(image) == STORAGE


def test_warehouse_point_scales() -> None:
    assert scale_point(1122, 970, 1920, 1080) == (1122, 970)
    x, y = scale_point(1122, 970, 2560, 1440)
    assert (x, y) == (1496, 1293)
