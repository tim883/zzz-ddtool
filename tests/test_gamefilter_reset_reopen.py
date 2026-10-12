"""筛选面板打开的重试回归：读不到「重置」就再点一次漏斗把它开回来。

背景（2026-10-12 真机）：漏斗按钮是**开/关切换**。面板本来就开着时（上一轮没关就点了
开始），开头那一下会把它关掉，接着找「重置」读空、整轮中断。失败轮 ``0002-导航到仓库.png``
左侧网格已被压暗（p95 119~129），正常轮是 218~242，证实任务开始时面板就是开着的。

做法只用"读没读到重置"，不依赖任何亮度阈值。替身替换点击与 OCR；不操作真实游戏。
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import zzz_ddtool.gamefilter as gf
from zzz_ddtool.gamefilter import FilterClickError


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch(monkeypatch, results: list[object]):
    """按顺序给 _try_click_reset 返回结果；返回 (漏斗点击次数, 记录容器)。"""
    seq = iter(results)
    calls = {"funnel": 0}

    monkeypatch.setattr(gf, "input_context", lambda *a, **kw: _Ctx())
    monkeypatch.setattr(gf, "wait_release", lambda *a, **kw: None)
    monkeypatch.setattr(gf, "click_client", lambda *a, **kw: Mock())

    def funnel(hwnd, fx, fy):
        calls["funnel"] += 1

    def try_reset(hwnd, width, height):
        value = next(seq)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(gf, "_click_funnel", funnel)
    monkeypatch.setattr(gf, "_try_click_reset", try_reset)
    return calls


def test_normal_path_clicks_funnel_once(monkeypatch):
    """面板本来关着：点 1 次漏斗就读到「重置」，不再补点。"""
    calls = _patch(monkeypatch, [Mock()])
    release = gf._open_panel_and_reset(1, 1920, 1080, fx=149, fy=930, allow_reopen=True)
    assert release is not None
    assert calls["funnel"] == 1


def test_reopens_panel_when_reset_not_found(monkeypatch):
    """读不到「重置」（说明第一下把已开的面板关掉了）：再点一次漏斗并成功。"""
    calls = _patch(monkeypatch, [None, Mock()])
    release = gf._open_panel_and_reset(1, 1920, 1080, fx=149, fy=930, allow_reopen=True)
    assert release is not None
    assert calls["funnel"] == 2


def test_raises_when_still_not_found_after_reopen(monkeypatch):
    """补点后仍读不到：抛出，不再无限重试。"""
    calls = _patch(monkeypatch, [None, None])
    with pytest.raises(FilterClickError, match="没找到「重置」"):
        gf._open_panel_and_reset(1, 1920, 1080, fx=149, fy=930, allow_reopen=True)
    assert calls["funnel"] == 2


def test_reset_game_filter_does_not_reopen(monkeypatch):
    """reset_game_filter 读不到时不补点：它紧接着就 Esc 关面板，补点没有意义。"""
    calls = _patch(monkeypatch, [None])
    monkeypatch.setattr(gf, "scale_point", lambda x, y, *a: (x, y))
    monkeypatch.setattr(gf, "_refuse_dismantle", lambda *a, **kw: None)
    monkeypatch.setattr(gf, "press_key", lambda *a, **kw: Mock())
    with pytest.raises(FilterClickError, match="没找到「重置」"):
        gf.reset_game_filter(1, 1920, 1080)
    assert calls["funnel"] == 1
