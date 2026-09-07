import pytest

# gamefilter 通过 PySide6 取图，没装就整模块跳过，
# 否则收集阶段 ImportError 会打断整轮 pytest。
pytest.importorskip("PySide6.QtGui")

from zzz_ddtool.gamefilter import FilterClickError, _refuse_dismantle  # noqa: E402
from zzz_ddtool.layout import FILTER_BTN  # noqa: E402


def test_funnel_is_left_of_dismantle() -> None:
    assert FILTER_BTN[0] < 210


def test_old_funnel_coord_is_refused() -> None:
    try:
        _refuse_dismantle(224, 930, 1920, 1080)
    except FilterClickError:
        return
    raise AssertionError("旧漏斗坐标必须被拒绝")


def test_new_funnel_coord_is_allowed() -> None:
    _refuse_dismantle(*FILTER_BTN, 1920, 1080)
