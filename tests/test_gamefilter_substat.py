import pytest
from PySide6.QtGui import QColor, QImage

from zzz_ddtool import gamefilter as gf
from zzz_ddtool.ocr import OcrLine


def line(text, x, y, half_width=45):
    return OcrLine(text=text, score=0.99, left=x - half_width, right=x + half_width, top=y - 12, bottom=y + 12)


def fake_panel(monkeypatch, *, selected=(), fail_click=False, hide_counts=False, paged=False, bottom_band=False):
    options = [line("暴击率", 1425, 620), line("暴击伤害", 1700, 620), line("攻击力百分比", 1425, 680)]
    state = {"open": False, "selected": set(map(str, selected)), "page": 0, "scrolls": 0}

    def counts():
        # bottom_band=True 时数字行初始 y=960（在面板搜索区内，但底边 972 落在底部
        # 「点击无效带」里），每滚一次上移 220px；真实实现「一次滑到底」= 一次调用把
        # page 推到 1，数字行随即进入可点区。
        base = 960 if bottom_band else 820
        return [line(str(i), 1380 + i * 100, base - state["page"] * 220, half_width=15) for i in range(1, 5)]

    def items():
        return options + counts()

    clicks = []

    def grab():
        image = QImage(1920, 1080, QImage.Format.Format_RGB32)
        image.fill(QColor(25, 25, 25))
        for item in items():
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
                lines += counts()
        return lines

    def click(hwnd, x, y):
        clicks.append((x, y))
        if y == 565:
            state["open"] = True
            return
        for item in items():
            if (x, y) == ((item.left + item.right) // 2, (item.top + item.bottom) // 2):
                if not fail_click:
                    state["selected"] ^= {item.text}
                return

    def scroll(*args, **kwargs):
        state["page"] += 1
        state["scrolls"] += 1

    monkeypatch.setattr(gf, "_grab", grab)
    monkeypatch.setattr(gf, "read_rect", read)
    monkeypatch.setattr(gf, "click_client", click)
    monkeypatch.setattr(gf, "wheel_client", scroll)
    monkeypatch.setattr(gf, "_scroll_panel_to_bottom", scroll)
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


def test_reuses_open_section_frame_without_extra_grab(monkeypatch):
    """展开确认帧必须被 apply_substat_filter 复用。

    两者之间没有任何点击，再抓一帧就是重复读同一帧，白花一次全屏 OCR（约 1s）。
    """
    grabs = []
    state, clicks = fake_panel(monkeypatch, selected=(1, 2, 3, 4))
    inner = gf._grab

    def counting_grab():
        grabs.append(1)
        return inner()

    monkeypatch.setattr(gf, "_grab", counting_grab)
    gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1, 2, 3, 4))
    # 展开前读 1 次 + 点击展开后确认 1 次 + 点「暴击率」后校验 1 次 = 3。
    # 旧实现会在 apply_substat_filter 开头再抓一帧 → 变成 4。
    assert len(grabs) == 3


def test_bottom_band_option_is_scrolled_before_click(monkeypatch):
    """压在面板底部「点击无效带」上的选项，必须先滑动、不能直接点（点了游戏不响应）。"""
    state, clicks = fake_panel(monkeypatch, bottom_band=True)
    gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1, 2, 3, 4))
    assert state["scrolls"] >= 1
    assert state["selected"] == {"暴击率", "1", "2", "3", "4"}
    # 数字行初始 y=1000 落在无效带里，滑动之前不得点它。
    assert not any(y >= 920 for _x, y in clicks), clicks
