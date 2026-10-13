"""主属性词条筛选的回归测试（B5）。

真机标定：
筛选面板内容比视口高，「主属性词条」区默认收起且滚到顶时在视口外；
点开「点击展开」后才出现复选框。此前代码直接点名 → 永远找不到 → 滚 8 轮中断。
"""

from __future__ import annotations

import pytest

from zzz_ddtool import gamefilter as gf
from zzz_ddtool.gamefilter import FilterClickError
from zzz_ddtool.ocr import OcrLine


def line(text: str, cx: int, cy: int, w: int = 60, h: int = 28) -> OcrLine:
    return OcrLine(
        text=text,
        score=0.99,
        left=cx - w // 2,
        top=cy - h // 2,
        right=cx + w // 2,
        bottom=cy + h // 2,
    )


@pytest.fixture(autouse=True)
def _no_release_timing(monkeypatch):
    """§4.2 释放预算在 gamefilter 内由 wait_release 消费；本组用例只验动作/坐标，替换掉计时。"""
    monkeypatch.setattr(gf, "wait_release", lambda *args, **kwargs: None)


# 面板滚到底 + 主属性区已展开（坐标取自 probe-slot4.png 的真机 OCR）
PANEL_EXPANDED = [
    line("分区", 1320, 350),
    line("分区[1]", 1425, 403, w=79),
    line("分区[4]", 1705, 460, w=78),
    line("主属性词条", 1349, 570, w=105),
    line("点击收起", 1563, 623, w=80),
    line("生命值", 1425, 678),
    line("攻击力百分比", 1706, 678, w=114),
    line("生命值百分比", 1426, 735, w=114),
    line("防御力百分比", 1706, 735, w=114),
    line("暴击率", 1425, 793),
    line("暴击伤害", 1705, 793),
    line("异常精通", 1425, 850),
    line("副属性词条", 1349, 902, w=105),
    line("点击展开", 1564, 949, w=80),
]

# 面板滚到底 + 主属性区收起：这是修复前代码看到的样子
PANEL_COLLAPSED = [
    line("分区[6]", 1705, 518, w=78),
    line("主属性词条", 1349, 570, w=105),
    line("点击展开", 1563, 623, w=80),
    line("副属性词条", 1349, 729, w=105),
    line("点击展开", 1564, 782, w=80),
]


def _stub(monkeypatch, frames, clicks, wheels, frames_are_fn=False):
    """把 OCR / 点击 / 滚轮换成替身。frames 可以是列表（固定）或可调用对象。"""
    monkeypatch.setattr(gf, "_text_lines", frames if frames_are_fn else (lambda: list(frames)))
    monkeypatch.setattr(gf, "click_client", lambda hwnd, x, y: clicks.append((x, y)))
    monkeypatch.setattr(gf, "wheel_client", lambda hwnd, x, y: wheels.append((x, y)))
    # §4.2 释放预算在 gamefilter 内由 wait_release 消费；本组用例只验动作/坐标，替换掉计时。
    monkeypatch.setattr(gf, "wait_release", lambda *args, **kwargs: None)
    monkeypatch.setattr(gf.time, "sleep", lambda seconds: None)


# ---------------------------------------------------------------- 展开主属性区


def test_open_mainstat_section_keeps_open_section(monkeypatch) -> None:
    """已展开（「点击收起」）时不该再点任何东西。"""
    clicks: list[tuple[int, int]] = []
    wheels: list[tuple[int, int]] = []
    _stub(monkeypatch, PANEL_EXPANDED, clicks, wheels)

    gf._open_mainstat_section(1, 1920, 1080)

    assert clicks == []
    assert wheels == []


def test_open_mainstat_section_clicks_main_toggle_only(monkeypatch) -> None:
    """收起态点的是主属性区的「点击展开」(1563,623)，不是副属性区那条 (1564,782)。"""
    state = {"open": False}
    clicks: list[tuple[int, int]] = []
    wheels: list[tuple[int, int]] = []

    def frames():
        return list(PANEL_EXPANDED if state["open"] else PANEL_COLLAPSED)

    def click(hwnd, x, y):
        clicks.append((x, y))
        state["open"] = True

    monkeypatch.setattr(gf, "_text_lines", frames)
    monkeypatch.setattr(gf, "click_client", click)
    monkeypatch.setattr(gf, "wheel_client", lambda hwnd, x, y: wheels.append((x, y)))
    monkeypatch.setattr(gf.time, "sleep", lambda seconds: None)

    gf._open_mainstat_section(1, 1920, 1080)

    assert clicks == [(1563, 623)]
    assert wheels == []


def test_open_mainstat_section_scrolls_until_title_visible(monkeypatch) -> None:
    """滚到顶时主属性区在视口外，必须先滚面板才看得到标题。"""
    seq = [
        [line("稀有度", 1362, 164, w=81), line("分区[1]", 1425, 445, w=79)],
        list(PANEL_EXPANDED),
    ]
    state = {"i": 0}
    clicks: list[tuple[int, int]] = []
    wheels: list[tuple[int, int]] = []

    monkeypatch.setattr(gf, "_text_lines", lambda: list(seq[min(state["i"], len(seq) - 1)]))

    def wheel(hwnd, x, y):
        wheels.append((x, y))
        state["i"] += 1

    monkeypatch.setattr(gf, "click_client", lambda hwnd, x, y: clicks.append((x, y)))
    monkeypatch.setattr(gf, "wheel_client", wheel)
    monkeypatch.setattr(gf.time, "sleep", lambda seconds: None)

    gf._open_mainstat_section(1, 1920, 1080)

    assert len(wheels) == 1
    assert clicks == []


def test_open_mainstat_section_raises_when_toggle_missing(monkeypatch) -> None:
    """标题在但开关一直认不上：按 §4.2 先向下滚重读，滚满轮数仍不行才中断。"""
    clicks: list[tuple[int, int]] = []
    wheels: list[tuple[int, int]] = []
    _stub(monkeypatch, [line("主属性词条", 1349, 570, w=105)], clicks, wheels)

    with pytest.raises(FilterClickError, match="滚动面板"):
        gf._open_mainstat_section(1, 1920, 1080)

    assert clicks == []
    assert len(wheels) == gf._MAX_PANEL_SCROLLS


def test_open_mainstat_section_scrolls_when_toggle_clipped(monkeypatch) -> None:
    """标题露出但开关被裁在底部点击无效带外（S5 轮次 2 真机）：滚一轮后开关可点即展开。"""
    clicks: list[tuple[int, int]] = []
    wheels: list[tuple[int, int]] = []
    seq = [
        [line("主属性词条", 1349, 908, w=105), line("点击展开", 1563, 962, w=80)],
        [line("主属性词条", 1349, 570, w=105), line("点击展开", 1563, 624, w=80)],
        [line("主属性词条", 1349, 570, w=105), line("点击收起", 1563, 624, w=80)],
    ]
    state = {"index": 0}

    def frames():
        index = min(state["index"], len(seq) - 1)
        state["index"] += 1
        return list(seq[index])

    _stub(monkeypatch, frames, clicks, wheels, frames_are_fn=True)

    result = gf._open_mainstat_section(1, 1920, 1080)

    assert len(wheels) == 1
    assert len(clicks) == 1
    assert result == seq[2]


def test_open_mainstat_section_scrolls_before_clicking_bottom_toggle(monkeypatch) -> None:
    frames = iter([
        [line("主属性词条", 1349, 850), line("点击展开", 1563, 960)],
        PANEL_COLLAPSED,
        PANEL_EXPANDED,
    ])
    clicks, wheels = [], []
    _stub(monkeypatch, lambda: list(next(frames)), clicks, wheels, frames_are_fn=True)

    gf._open_mainstat_section(1, 1920, 1080)

    assert wheels == [(1560, 500)]
    assert clicks == [(1563, 623)]


def test_open_mainstat_section_raises_after_scroll_limit(monkeypatch) -> None:
    clicks: list[tuple[int, int]] = []
    wheels: list[tuple[int, int]] = []
    _stub(monkeypatch, [line("稀有度", 1362, 164, w=81)], clicks, wheels)

    with pytest.raises(FilterClickError):
        gf._open_mainstat_section(1, 1920, 1080)

    assert len(wheels) == gf._MAX_PANEL_SCROLLS


# ---------------------------------------------------------------- 主属性点名


def _pick(monkeypatch, names: list[str]) -> list[tuple[int, int]]:
    clicks: list[tuple[int, int]] = []
    _stub(monkeypatch, PANEL_EXPANDED, clicks, [])
    gf._pick_mainstats(1, names, width=1920, height=1080)
    return clicks


def test_pick_mainstats_prefers_exact_over_longer_prefix(monkeypatch) -> None:
    """「生命值」必须命中 (1425,678)，不能被更长的「生命值百分比」抢走。"""
    assert _pick(monkeypatch, ["生命值"]) == [(1425, 678)]


def test_pick_mainstats_clicks_full_names(monkeypatch) -> None:
    """真机里主属性是全称：攻击力百分比 / 防御力百分比。"""
    assert _pick(monkeypatch, ["防御力百分比"]) == [(1706, 735)]
    assert _pick(monkeypatch, ["攻击力百分比"]) == [(1706, 678)]


def test_pick_mainstats_accepts_short_form(monkeypatch) -> None:
    """预设里写「防御力%」也要能认上「防御力百分比」（相似度 0.8）。"""
    assert _pick(monkeypatch, ["防御力%"]) == [(1706, 735)]


def test_pick_mainstats_clicks_multiple(monkeypatch) -> None:
    assert _pick(monkeypatch, ["暴击率", "异常精通"]) == [(1425, 793), (1425, 850)]


def test_pick_mainstats_ignores_substat_rows(monkeypatch) -> None:
    """副属性区里的同名条目不算数：主属性区没有就中断。"""
    frames = [
        line("主属性词条", 1349, 570, w=105),
        line("点击收起", 1563, 623, w=80),
        line("生命值", 1425, 678),
        line("副属性词条", 1349, 902, w=105),
        line("暴击率", 1425, 949),
    ]
    clicks: list[tuple[int, int]] = []
    _stub(monkeypatch, frames, clicks, [])

    with pytest.raises(FilterClickError):
        gf._pick_mainstats(1, ["暴击率"], width=1920, height=1080)

    assert clicks == []


def test_pick_mainstats_raises_on_unknown_name(monkeypatch) -> None:
    clicks: list[tuple[int, int]] = []
    _stub(monkeypatch, PANEL_EXPANDED, clicks, [])

    with pytest.raises(FilterClickError):
        gf._pick_mainstats(1, ["冲击力"], width=1920, height=1080)

    assert clicks == []


def test_pick_mainstats_scrolls_to_offscreen_target(monkeypatch) -> None:
    """实测失败场景：展开后只露出前两排，暴击伤害仍在视口下方。"""
    initial = [
        line("主属性词条", 1349, 746),
        line("点击收起", 1563, 799),
        line("攻击力百分比", 1425, 854),
        line("生命值百分比", 1705, 854),
        line("防御力百分比", 1425, 911),
        line("暴击率", 1705, 911),
    ]
    clicks, wheels = [], []
    _stub(monkeypatch, lambda: PANEL_EXPANDED, clicks, wheels, frames_are_fn=True)

    gf._pick_mainstats(1, ["暴击伤害"], width=1920, height=1080, lines=initial)

    assert wheels == [(1560, 500)]
    assert clicks == [(1705, 793)]


def test_pick_mainstats_scrolls_before_clicking_bottom_band(monkeypatch) -> None:
    initial = [line("主属性词条", 1349, 746), line("暴击率", 1705, 911)]
    clicks, wheels = [], []
    _stub(monkeypatch, PANEL_EXPANDED, clicks, wheels)

    gf._pick_mainstats(1, ["暴击率"], width=1920, height=1080, lines=initial)

    assert wheels == [(1560, 500)]
    assert clicks == [(1425, 793)]


def test_pick_mainstats_keeps_selected_names_when_scrolling(monkeypatch) -> None:
    initial = [line("主属性词条", 1349, 746), line("攻击力百分比", 1705, 854)]
    clicks, wheels = [], []
    _stub(monkeypatch, PANEL_EXPANDED, clicks, wheels)

    gf._pick_mainstats(1, ["攻击力%", "暴击伤害"], width=1920, height=1080, lines=initial)

    assert wheels == [(1560, 500)]
    assert clicks == [(1705, 854), (1705, 793)]


def test_pick_mainstats_uses_substat_boundary_after_main_title_scrolls_away(monkeypatch) -> None:
    initial = [line("主属性词条", 1349, 746)]
    after = [
        line("暴击伤害", 1705, 200),
        line("副属性词条", 1349, 260),
        line("暴击伤害", 1705, 400),
    ]
    clicks, wheels = [], []
    _stub(monkeypatch, after, clicks, wheels)

    gf._pick_mainstats(1, ["暴击伤害"], width=1920, height=1080, lines=initial)

    assert clicks == [(1705, 200)]
    assert len(wheels) == 1


def test_pick_mainstats_missing_name_stops_at_scroll_limit(monkeypatch) -> None:
    clicks, wheels = [], []
    _stub(monkeypatch, PANEL_EXPANDED, clicks, wheels)

    with pytest.raises(FilterClickError, match="冲击力"):
        gf._pick_mainstats(1, ["冲击力"], width=1920, height=1080)

    assert clicks == []
    assert len(wheels) == gf._MAX_PANEL_SCROLLS


def test_pick_mainstats_ignores_same_name_in_warehouse(monkeypatch) -> None:
    frames = [line("主属性词条", 1349, 570), line("暴击伤害", 800, 700)]
    clicks, wheels = [], []
    _stub(monkeypatch, frames, clicks, wheels)

    with pytest.raises(FilterClickError):
        gf._pick_mainstats(1, ["暴击伤害"], width=1920, height=1080)

    assert clicks == []


def test_pick_mainstats_scales_panel_scroll_and_click_band(monkeypatch) -> None:
    initial = [line("主属性词条", 674, 373), line("暴击率", 852, 456)]
    after = [line("主属性词条", 674, 285), line("暴击率", 713, 397)]
    clicks, wheels = [], []
    _stub(monkeypatch, after, clicks, wheels)

    gf._pick_mainstats(1, ["暴击率"], width=960, height=540, lines=initial)

    assert wheels == [(780, 250)]
    assert clicks == [(713, 397)]


def test_pick_mainstats_normalized_duplicates_are_clicked_once(monkeypatch) -> None:
    assert _pick(monkeypatch, ["攻击力%", "攻击力百分比"]) == [(1706, 678)]


# ---------------------------------------------------------------- 接线


def test_apply_sets_mainstats_mainstat_path_has_no_confirm(monkeypatch) -> None:
    """主属性是复选框：展开 → 点名，不该走套装那套弹层确认。"""
    calls: list[str] = []
    monkeypatch.setattr(gf, "_open_mainstat_section", lambda *a: calls.append("open"))
    monkeypatch.setattr(gf, "_pick_mainstats", lambda *a, **kw: calls.append("pick"))
    monkeypatch.setattr(gf, "_confirm_popup", lambda *a: calls.append("confirm"))

    gf.apply_sets_mainstats(1, 1920, 1080, sets=(), mainstats=["暴击率"])

    assert calls == ["open", "pick"]


def test_apply_sets_mainstats_reuses_expand_frame(monkeypatch) -> None:
    """展开区那一帧直接交给点名复用：整个主属性路径只该识别一帧面板。

    抓一帧全屏 OCR 实测 1.3~1.5s，多抓一次就是白扔。两函数之间没有任何点击，
    同一帧状态没变，可以安全复用。这条守住的就是「别再各抓一帧」。
    """
    reads: list[int] = []
    clicks: list[tuple[int, int]] = []

    def counting_text_lines():
        reads.append(1)
        return list(PANEL_EXPANDED)

    monkeypatch.setattr(gf, "_text_lines", counting_text_lines)
    monkeypatch.setattr(gf, "click_client", lambda hwnd, x, y: clicks.append((x, y)))
    monkeypatch.setattr(gf, "wheel_client", lambda hwnd, x, y: None)
    monkeypatch.setattr(gf.time, "sleep", lambda seconds: None)

    gf.apply_sets_mainstats(1, 1920, 1080, sets=(), mainstats=["暴击率"])

    assert len(reads) == 1, f"主属性路径识别了 {len(reads)} 帧面板，应只识别 1 帧"
    assert clicks == [(1425, 793)]


def test_apply_sets_mainstats_set_path_still_confirms(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(gf, "_find_and_click", lambda *a, **kw: calls.append("open-sets") or True)
    monkeypatch.setattr(gf, "_select_names", lambda *a, **kw: calls.append("pick-sets"))
    monkeypatch.setattr(gf, "_confirm_popup", lambda *a: calls.append("confirm"))

    gf.apply_sets_mainstats(1, 1920, 1080, sets=["折枝剑歌"], mainstats=())

    assert calls == ["open-sets", "pick-sets", "confirm"]


def test_pick_mainstats_percent_maps_to_percentage(monkeypatch) -> None:
    """预设写「生命值%」（data/stats.json 的写法）要落在「生命值百分比」上，

    不能落到裸的「生命值」上——两者在同一区里同时存在。
    """
    assert _pick(monkeypatch, ["生命值%"]) == [(1426, 735)]


def test_normalize_stat_aliases() -> None:
    assert gf._normalize_stat("攻击力%") == "攻击力百分比"
    assert gf._normalize_stat("攻击力％") == "攻击力百分比"
    assert gf._normalize_stat("攻击力百分比") == "攻击力百分比"
    assert gf._normalize_stat("暴击率") == "暴击率"


def test_mainstat_group_waits_only_after_last_click_in_each_viewport(monkeypatch):
    clicks, wheels, waits = [], [], []
    initial = [*PANEL_EXPANDED, line("攻击力百分比", 1706, 678, w=114)]
    initial = [l for l in initial if l.text != "暴击伤害"]
    _stub(monkeypatch, PANEL_EXPANDED, clicks, wheels)
    from zzz_ddtool.actionwait import InputRelease
    def click(hwnd, x, y):
        clicks.append((x, y))
        return InputRelease("click_client", len(clicks) * 1000000)
    monkeypatch.setattr(gf, "click_client", click)
    monkeypatch.setattr(gf, "wait_release", lambda role, release, **kw: waits.append((role, len(clicks), kw)))
    gf._pick_mainstats(1, ["生命值", "攻击力%", "暴击伤害"], width=1920, height=1080, lines=initial)
    main = [w for w in waits if w[0] == "mainstat_option"]
    assert main == [
        ("mainstat_option", 1, {"fallback_ms": 0, "allow_measured": False}),
        ("mainstat_option", 2, {"fallback_ms": 0, "allow_measured": False}),
        ("mainstat_option", 2, {"fallback_ms": 160}),
        ("mainstat_option", 3, {"fallback_ms": 0, "allow_measured": False}),
        ("mainstat_option", 3, {"fallback_ms": 160}),
    ]
    assert len(wheels) == 1
