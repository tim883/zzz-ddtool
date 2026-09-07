"""坐标缩放与关键矩形的一致性检查（纯逻辑，不需要游戏窗口）。"""

from zzz_ddtool import layout
from zzz_ddtool.layout import (
    DESIGN_H,
    DESIGN_W,
    DISMANTLE_FORBID,
    FILTER_BTN,
    scale_point,
    scale_rect,
)


def test_design_size_is_1080p() -> None:
    assert (DESIGN_W, DESIGN_H) == (1920, 1080)


def test_scale_point_at_design_size_is_identity() -> None:
    assert scale_point(100, 200, DESIGN_W, DESIGN_H) == (100, 200)


def test_scale_point_scales_each_axis() -> None:
    # 半分辨率：两个轴各自减半
    assert scale_point(100, 200, 960, 540) == (50, 100)


def test_scale_rect_at_design_size_is_identity() -> None:
    rect = (10, 20, 30, 40)
    assert scale_rect(*rect, DESIGN_W, DESIGN_H) == rect


def test_scale_rect_scales_each_edge() -> None:
    assert scale_rect(10, 20, 30, 40, 960, 540) == (5, 10, 15, 20)


def test_scale_point_handles_zero_size() -> None:
    # 窗口最小化时 GetClientRect 会返回 0×0，不能崩
    assert scale_point(100, 200, 0, 0) == (0, 0)


def test_filter_button_is_outside_dismantle_forbidden_zone() -> None:
    """点漏斗绝不能点到拆解蓝标上——点上去就进拆解流程了。"""
    fx, fy = FILTER_BTN
    left, top, right, bottom = DISMANTLE_FORBID
    assert not (left <= fx <= right and top <= fy <= bottom), (
        f"漏斗点击 {FILTER_BTN} 落在拆解禁区 {DISMANTLE_FORBID} 内"
    )


def test_all_named_rects_are_well_formed() -> None:
    """每个 (left, top, right, bottom) 都得左上 < 右下，且在画面内。"""
    rect_names = [
        name
        for name in dir(layout)
        if name.isupper() and isinstance(getattr(layout, name), tuple) and len(getattr(layout, name)) == 4
    ]
    assert rect_names, "没扫到任何矩形常量，检查 layout.py 是否改名"
    for name in rect_names:
        left, top, right, bottom = getattr(layout, name)
        assert right > left, f"{name} 右边界 {right} 不大于左边界 {left}"
        assert bottom > top, f"{name} 下边界 {bottom} 不大于上边界 {top}"
        assert 0 <= left < right <= DESIGN_W, f"{name} 横向越界: {left}..{right}"
        assert 0 <= top < bottom <= DESIGN_H, f"{name} 纵向越界: {top}..{bottom}"


def test_all_named_points_are_inside_screen() -> None:
    """所有点击坐标都得在 1920×1080 画面内。"""
    point_names = [
        name
        for name in dir(layout)
        if name.isupper() and isinstance(getattr(layout, name), tuple) and len(getattr(layout, name)) == 2
    ]
    for name in point_names:
        x, y = getattr(layout, name)
        assert 0 <= x <= DESIGN_W, f"{name} 横坐标 {x} 越界"
        assert 0 <= y <= DESIGN_H, f"{name} 纵坐标 {y} 越界"
