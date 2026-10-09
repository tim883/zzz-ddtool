"""套装图标绘制：保证在色块里画出**完整**套装名，不截断、不溢出。

回归点：旧实现 `paint_set_icon` 用 `name[:2]` 只画前两个字，
导致「拂晓生花 / 拂晓行纪」「混沌爵士 / 混沌重金属」在界面上看起来一样。
"""
import json
import os
import pathlib

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication

from zzz_ddtool.seticons import _fit_font, paint_set_icon

SETS_FILE = pathlib.Path(__file__).resolve().parents[1] / "data" / "sets.json"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


def _names() -> list[str]:
    return json.loads(SETS_FILE.read_text(encoding="utf-8"))


def _ink_span(pix):
    """深色像素（字色 #1a1a1a）的横向跨度，用来判断实际画了几个字。"""
    img = pix.toImage()
    xs = []
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.red() < 80 and c.green() < 80 and c.blue() < 80:
                xs.append(x)
    if not xs:
        return None
    return min(xs), max(xs)


def test_icon_size(qapp: QApplication) -> None:
    pix = paint_set_icon("折枝剑歌", 64)
    assert pix.width() == 64
    assert pix.height() == 64


def test_font_fits_every_set_name(qapp: QApplication) -> None:
    """每个套装名的自适应字号都必须落在块宽内（5~6 字名也不能超标）。"""
    for name in _names():
        width = QFontMetrics(_fit_font(name, 64)).horizontalAdvance(name)
        assert width <= 64 - 6, f"{name} 字号仍超标：{width}px"


def test_full_name_drawn_not_truncated(qapp: QApplication) -> None:
    """4 字及以上的名字必须画满，且不触到块边缘。"""
    for name in _names():
        if len(name) < 4:
            continue
        span = _ink_span(paint_set_icon(name, 64))
        assert span is not None, f"{name} 没画出任何文字"
        lo, hi = span
        assert hi - lo + 1 > 40, f"{name} 墨迹仅 {hi - lo + 1}px，疑似仍被截断"
        assert lo >= 2 and hi <= 61, f"{name} 文字触到边缘（{lo}~{hi}），可能溢出"
