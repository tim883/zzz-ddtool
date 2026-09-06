import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from zzz_ddtool.paths import captures_dir
from zzz_ddtool.screen import DRIVE_DISC, MENU, OVERWORLD, STORAGE, classify_image, is_page
from zzz_ddtool.template import assets_dir

CAPTURES = captures_dir()
PAGES = {
    "大地图页.png": OVERWORLD,
    "功能页.png": MENU,
    "仓库页.png": STORAGE,
    "驱动盘页.png": DRIVE_DISC,
}


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


def test_template_files_exist() -> None:
    expected = [
        "world/quick_wheel.png",
        "menu/back.png",
        "menu/warehouse.png",
        "storage/recycle.png",
        "storage/title.png",
        "discs/dismantle.png",
        "discs/tab.png",
        "discs/title.png",
    ]
    missing = [name for name in expected if not (assets_dir() / name).is_file()]
    assert missing == []


@pytest.mark.skipif(not (CAPTURES / "驱动盘页.png").is_file(), reason="no named captures")
def test_named_captures_hit_their_icons(qapp: QApplication) -> None:
    for filename, page in PAGES.items():
        image = QImage(str(CAPTURES / filename))
        assert not image.isNull(), filename
        assert is_page(image, page), filename
        assert classify_image(image) == page, filename


@pytest.mark.skipif(not (CAPTURES / "驱动盘页.png").is_file(), reason="no named captures")
def test_recycle_and_dismantle_do_not_cross(qapp: QApplication) -> None:
    storage = QImage(str(CAPTURES / "仓库页.png"))
    discs = QImage(str(CAPTURES / "驱动盘页.png"))
    assert is_page(storage, STORAGE)
    assert not is_page(storage, DRIVE_DISC)
    assert is_page(discs, DRIVE_DISC)
    assert not is_page(discs, STORAGE)
