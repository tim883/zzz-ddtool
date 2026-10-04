import pytest
from PySide6.QtGui import QColor, QImage

from zzz_ddtool import gamefilter as gf
from zzz_ddtool.ocr import OcrLine


def line(text, x, y, half_width=45):
    return OcrLine(text=text, score=0.99, left=x - half_width, right=x + half_width, top=y - 12, bottom=y + 12)


def fake_panel(monkeypatch, *, selected=(), fail_click=False, hide_counts=False, paged=False):
    options = [line("暴击率", 1425, 620), line("暴击伤害", 1700, 620), line("攻击力百分比", 1425, 680)]
    counts = [line(str(i), 1380 + i * 100, 820, half_width=15) for i in range(1, 5)]
    state = {"open": False, "selected": set(map(str, selected)), "page": 0}
    clicks = []

    def grab():
        image = QImage(1920, 1080, QImage.Format.Format_RGB32)
        image.fill(QColor(25, 25, 25))
        for item in options + counts:
            if item.text in state["selected"]:
                for y in range(item.top - 2, item.bottom + 3):
                    for x in range(item.left - 14, item.right + 15):
                        image.setPixelColor(x, y, QColor(0, 140, 255))
        return image, 1

    def read(image, region):
        lines = [line(gf.SUBSTAT_TITLE, 1400, 510), line("点击收起" if state["open"] else "点击展开", 1560, 565)]
        # 主属性同名项目不应被点击。
        lines.extend([line(gf.MAINSTAT_TITLE, 1400, 300), line("点击展开", 1560, 350), line("暴击率", 1425, 400)])
        if state["open"]:
            if not paged or state["page"] == 0:
                lines += options
            if not hide_counts and (not paged or state["page"] > 0):
                lines += counts
        return lines

    def click(hwnd, x, y):
        clicks.append((x, y))
        if y == 565:
            state["open"] = True
            return
        for item in options + counts:
            if (x, y) == ((item.left + item.right) // 2, (item.top + item.bottom) // 2):
                if not fail_click:
                    state["selected"] ^= {item.text}
                return

    monkeypatch.setattr(gf, "_grab", grab)
    monkeypatch.setattr(gf, "read_rect", read)
    monkeypatch.setattr(gf, "click_client", click)
    monkeypatch.setattr(gf, "wheel_client", lambda *a: state.update(page=state["page"] + 1))
    monkeypatch.setattr(gf.time, "sleep", lambda *a: None)
    return state, clicks


def test_selects_stats_and_all_higher_counts_clears_default_one(monkeypatch):
    state, clicks = fake_panel(monkeypatch, selected=(1, 2, 3, 4))
    gf.apply_substat_filter(1, 1920, 1080, names=("暴击率", "暴击伤害"), counts=(2, 3, 4))
    assert state["selected"] == {"暴击率", "暴击伤害", "2", "3", "4"}
    assert (1425, 400) not in clicks
    assert len(clicks) == 4  # 展开、两个属性、取消数量 1；不会反转已选数量。


def test_scrolls_for_counts_and_normalizes_percent(monkeypatch):
    state, clicks = fake_panel(monkeypatch, paged=True)
    gf.apply_substat_filter(1, 1920, 1080, names=("攻击力%",), counts=(1, 2, 3, 4))
    assert state["selected"] == {"攻击力百分比", "1", "2", "3", "4"}
    assert state["page"] == 1


def test_failed_selection_stops_before_scanning(monkeypatch):
    fake_panel(monkeypatch, fail_click=True)
    with pytest.raises(gf.FilterClickError, match="未确认选中"):
        gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1, 2, 3, 4))


def test_missing_count_stops_instead_of_using_partial_condition(monkeypatch):
    fake_panel(monkeypatch, hide_counts=True)
    with pytest.raises(gf.FilterClickError, match="找不到选项"):
        gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1, 2, 3, 4))


@pytest.mark.parametrize("counts", [(), (0, 1, 2, 3, 4), (1,), (2,), (2, 4)])
def test_unsafe_exact_count_conditions_refused_before_input(monkeypatch, counts):
    clicks = []
    monkeypatch.setattr(gf, "click_client", lambda *a: clicks.append(a))
    with pytest.raises(gf.FilterClickError):
        gf.apply_game_filter(1, 1920, 1080, slots=(4,), sets=(), mainstats=(), substats=("暴击率",), substat_counts=counts)
    assert not clicks


def test_mainstat_or_zero_count_cannot_be_selected():
    with pytest.raises(gf.FilterClickError):
        gf._validate_substat_filter(("穿透率",), (1, 2, 3, 4))


def test_apply_game_filter_applies_substats_before_closing(monkeypatch):
    calls = []
    monkeypatch.setattr(gf, "click_client", lambda *a: None)
    monkeypatch.setattr(gf, "_click_reset", lambda *a: calls.append("reset"))
    monkeypatch.setattr(gf, "_click_slot", lambda *a: calls.append("slot"))
    monkeypatch.setattr(gf, "apply_sets_mainstats", lambda *a, **kw: calls.append("main"))
    monkeypatch.setattr(gf, "apply_substat_filter", lambda *a, **kw: calls.append((kw["names"], kw["counts"])))
    monkeypatch.setattr(gf, "press_key", lambda *a: calls.append("close"))
    monkeypatch.setattr(gf.time, "sleep", lambda *a: None)
    gf.apply_game_filter(1, 1920, 1080, slots=(4,), sets=(), mainstats=("生命值%",), substats=("暴击率",), substat_counts=(1, 2, 3, 4))
    assert calls == ["reset", "slot", "main", (("暴击率",), (1, 2, 3, 4)), "close"]
