import json
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage

from zzz_ddtool import gamefilter as gf
from zzz_ddtool.ocr import OcrLine

BASE = Path(__file__).resolve().parents[1]
MANIFEST = BASE / "tests" / "fixtures" / "filter-replay" / "manifest.json"


def line(text, x, y, half_width=45, half_height=12):
    return OcrLine(text=text, score=0.99, left=x - half_width, right=x + half_width,
                   top=y - half_height, bottom=y + half_height)


@pytest.fixture(autouse=True)
def _no_release_timing(monkeypatch):
    """§4.2 释放预算在 gamefilter 内由 wait_release 消费；本组用例只验动作/坐标，替换掉计时。"""
    monkeypatch.setattr(gf, "wait_release", lambda *args, **kwargs: None)


def fake_panel(monkeypatch, *, selected=(), fail_click=False, hide_counts=False, paged=False,
               bottom_band=False, partial_first=False, tall_dead_zone=False, four_options=False):
    options = [line("暴击率", 1425, 620), line("暴击伤害", 1700, 620), line("攻击力百分比", 1425, 680)]
    if four_options:
        # 数量按钮只在 ≤ 已选词条数时可用（2026-10-11 真机）：需要数量 3/4 的场景必须有 4 个词条目标。
        options.append(line("攻击力", 1700, 680))
    state = {"open": False, "selected": set(map(str, selected)), "page": 0, "scrolls": 0, "log": []}

    def counts():
        # bottom_band=True 时数字行初始 y=960（在面板搜索区内，但底边 972 落在底部
        # 「点击无效带」里），每滚一次上移 60px（972 → 912，进入可点区）；真实实现
        # 「一次滑到底」= 一次调用把 page 推到 1，数字行随即进入可点区。
        base = 960 if bottom_band else 820
        return [line(str(i), 1380 + i * 100, base - state["page"] * 60, half_width=15) for i in range(1, 5)]

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
            visible = options
            if partial_first and state["page"] == 0:
                # S02：初帧只有部分目标露出，滚到底后完整可见
                visible = [item for item in options if item.text == "攻击力百分比"]
            if tall_dead_zone and state["page"] == 0:
                # S03：中心在可点区内但底边越过禁区（bottom=940 > 920），不得点击
                visible = [
                    line("暴击率", 1425, 820, half_height=120) if item.text == "暴击率" else item
                    for item in options
                ]
            lines += visible
            if not hide_counts and (not paged or state["page"] > 0):
                lines += counts()
        return lines

    def click(hwnd, x, y):
        clicks.append((x, y))
        state["log"].append(("click", x, y))
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
        state["log"].append(("scroll", state["page"]))

    reads = {"n": 0}
    real_read_panel = gf._read_panel

    def counting_read_panel():
        reads["n"] += 1
        return real_read_panel()

    monkeypatch.setattr(gf, "_grab", grab)
    monkeypatch.setattr(gf, "read_rect", read)
    monkeypatch.setattr(gf, "click_client", click)
    monkeypatch.setattr(gf, "wheel_client", scroll)
    monkeypatch.setattr(gf, "_scroll_panel_to_bottom", scroll)
    monkeypatch.setattr(gf, "_read_panel", counting_read_panel)
    # §4.2 释放预算在 gamefilter 内由 wait_release 消费；本组用例只验动作/坐标，替换掉计时。
    monkeypatch.setattr(gf, "wait_release", lambda *args, **kwargs: None)
    monkeypatch.setattr(gf.time, "sleep", lambda *a: None)
    state["panel_reads"] = reads
    return state, clicks


def test_selects_stats_and_all_higher_counts_clears_default_one(monkeypatch):
    state, clicks = fake_panel(monkeypatch, selected=(1, 2, 3, 4), four_options=True)
    gf.apply_substat_filter(1, 1920, 1080, names=("暴击率", "暴击伤害", "攻击力%", "攻击力"),
                            counts=(2, 3, 4))
    assert state["selected"] == {"暴击率", "暴击伤害", "攻击力百分比", "攻击力", "2", "3", "4"}
    assert (1425, 400) not in clicks
    assert len(clicks) == 6  # 展开、四个属性、取消数量 1；不会反转已选数量。


def test_scrolls_for_counts_and_normalizes_percent(monkeypatch):
    state, clicks = fake_panel(monkeypatch, paged=True)
    gf.apply_substat_filter(1, 1920, 1080, names=("攻击力%",), counts=(1,))
    assert state["selected"] == {"攻击力百分比", "1"}
    assert state["page"] == 1


def test_failed_selection_stops_before_scanning(monkeypatch):
    fake_panel(monkeypatch, fail_click=True)
    with pytest.raises(gf.FilterClickError, match="未确认选中"):
        gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1,))


def test_missing_count_stops_instead_of_using_partial_condition(monkeypatch):
    fake_panel(monkeypatch, hide_counts=True)
    with pytest.raises(gf.FilterClickError, match="副属性目标未就位"):
        gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1,))


@pytest.mark.parametrize(
    "case",
    ["ready_no_rescan", "position_before_selection", "bottom_dead_zone", "still_missing",
     "selection_failure", "substat_reference_manifest"],
)
def test_substat_acceptance(monkeypatch, case: str) -> None:
    """spec §8.2 S01—S06：就位契约、禁区标准、观察上限与判读共用。"""
    if case == "ready_no_rescan":  # S01：已就位 → 不滚、不重复面板 OCR、只点该点的
        state, clicks = fake_panel(
            monkeypatch, selected=("暴击率", "暴击伤害", "攻击力百分比", "攻击力", "3", "4"),
            four_options=True,
        )
        gf.apply_substat_filter(1, 1920, 1080, names=("暴击率", "暴击伤害", "攻击力%", "攻击力"),
                                counts=(3, 4))
        assert state["scrolls"] == 0
        assert state["panel_reads"]["n"] == 2  # 展开阶段两帧（初检+确认展开）；完成段不再读面板
        assert state["selected"] == {"暴击率", "暴击伤害", "攻击力百分比", "攻击力", "3", "4"}
        option_clicks = [item for item in clicks if item[1] != 565]  # 排除展开那一击
        assert option_clicks == []  # 已满足的目标一个都不点
    elif case == "position_before_selection":  # S02：先一次滑到底，再开始选择
        state, clicks = fake_panel(monkeypatch, partial_first=True)
        gf.apply_substat_filter(1, 1920, 1080, names=("暴击率", "攻击力%"), counts=(2,))
        assert state["scrolls"] == 1
        first_click = next(
            index for index, item in enumerate(state["log"]) if item[0] == "click" and item[2] != 565
        )
        first_scroll = next(index for index, item in enumerate(state["log"]) if item[0] == "scroll")
        assert first_scroll < first_click  # 所有选项点击之前先就位
        assert state["selected"] == {"暴击率", "攻击力百分比", "2"}
    elif case == "bottom_dead_zone":  # S03：底部禁区按 bottom>920 判定，不用中心
        state, clicks = fake_panel(monkeypatch, bottom_band=True, tall_dead_zone=True)
        gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1,))
        assert (1480, 960) not in clicks  # 初始数字行压在禁区，中心判断不能替代
        assert (1425, 820) not in clicks  # 高框底边 940 越过禁区，即使中心在区内也不点
        assert state["selected"] == {"暴击率", "1"}
    elif case == "still_missing":  # S04：滚到底仍缺 → 只观察不再滚，不选部分条件
        state, clicks = fake_panel(monkeypatch, hide_counts=True)
        presses: list = []
        monkeypatch.setattr(gf, "press_key", lambda *a: presses.append(a))
        with pytest.raises(gf.FilterClickError, match="副属性目标未就位"):
            gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1,))
        assert state["scrolls"] == 1  # 不追加无限滚动
        assert state["panel_reads"]["n"] == 2 + 3  # 展开 2 帧 + 滚后就位观察最多 3 帧
        assert [item for item in clicks if item[1] != 565] == []  # 不选择部分条件
        assert presses == []  # 不关闭为成功
    elif case == "substat_reference_manifest":  # S06：真图 + 判读函数逐项比对
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cases = [item for item in data["substat_cases"]]
        assert cases, "清单缺 substat_cases"
        failures: list[str] = []
        for item in cases:
            path = BASE / item["image"]
            assert path.exists(), f"清单引用的副属性样本不存在：{item['image']}"
            image = QImage(str(path))
            assert not image.isNull(), f"副属性样本不可读：{item['image']}"
            lines = gf._panel_lines(image)
            ready, found = gf._inspect_substat_targets(
                image, lines, names=item["names"], counts=item["counts"]
            )
            if ready != item["expected_ready"]:
                failures.append(f"{item['id']}: 就位期望 {item['expected_ready']!r}，实得 {ready!r}")
            keys = sorted(found, key=lambda key: (isinstance(key, int), key))
            if keys != sorted(item["expected_keys"], key=lambda key: (isinstance(key, int), key)):
                failures.append(f"{item['id']}: 键期望 {item['expected_keys']}，实得 {keys}")
            if ready is True:
                for key, line in found.items():
                    if line.bottom > image.height() - gf._PANEL_CLICK_BOTTOM_GAP:
                        failures.append(f"{item['id']}: 已就位却把 {key} 判在点击禁区")
        assert not failures, "副属性样本不符：\n" + "\n".join(failures)
        # 生产执行必须复用同一个判读函数（不接受用就位值替身）
        state, _clicks = fake_panel(monkeypatch, selected=("暴击率", "3"))
        calls: list = []
        real_inspect = gf._inspect_substat_targets

        def spy(*args, **kwargs):
            calls.append(1)
            return real_inspect(*args, **kwargs)

        monkeypatch.setattr(gf, "_inspect_substat_targets", spy)
        gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1,))
        assert calls, "apply_substat_filter 没有调用共享判读函数"
    else:  # S05：点击后判读仍 False → 不重做面板 OCR、不继续余项
        state, clicks = fake_panel(monkeypatch, fail_click=True)
        with pytest.raises(gf.FilterClickError, match="未确认选中"):
            gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1,))
        assert state["panel_reads"]["n"] == 2  # 不用重做面板 OCR 覆盖失败
        assert len([item for item in clicks if item[1] != 565]) == 1  # 不继续余项


@pytest.mark.parametrize("counts", [(), (0, 1, 2, 3, 4), (2,), (1, 2), (2, 4)])
def test_unsafe_exact_count_conditions_refused_before_input(monkeypatch, counts):
    clicks = []
    monkeypatch.setattr(gf, "click_client", lambda *a: clicks.append(a))
    with pytest.raises(gf.FilterClickError):
        gf.apply_game_filter(1, 1920, 1080, slots=(4,), sets=(), mainstats=(), substats=("暴击率",), substat_counts=counts)
    assert not clicks


def test_mainstat_or_zero_count_cannot_be_selected():
    with pytest.raises(gf.FilterClickError):
        gf._validate_substat_filter(("穿透率",), (1, 2, 3, 4))


def test_substats_evidence_reuses_last_frame(monkeypatch):
    """§8.4：完成即发一次 substats_selected（Esc 前），复用最后检查帧，未启用时不多拍。"""
    state, _clicks = fake_panel(monkeypatch, selected=(1,), four_options=True)
    real_grab = gf._grab
    grabs = {"n": 0}

    def counting_grab():
        grabs["n"] += 1
        return real_grab()

    monkeypatch.setattr(gf, "_grab", counting_grab)
    names = ("暴击率", "暴击伤害", "攻击力%", "攻击力")
    counts = (2, 3, 4)

    def reset_state() -> None:
        state["selected"] = {"1"}
        state["open"] = False
        state["page"] = 0

    reset_state()
    before = grabs["n"]
    gf.apply_substat_filter(1, 1920, 1080, names=names, counts=counts)
    plain = grabs["n"] - before
    reset_state()
    events: list = []
    before = grabs["n"]
    with gf._capture_filter_evidence(events.append):
        gf.apply_substat_filter(1, 1920, 1080, names=names, counts=counts)
    assert grabs["n"] - before == plain  # 同起点对比：复用最后检查帧，取证不多拍
    assert len(events) == 1
    event = events[0]
    assert event.event == "substats_selected" and event.stage_index is None
    assert list(event.targets) == [*names, 1, 2, 3, 4]
    assert len(event.boxes) == len(event.targets)
    assert all(box[2] > box[0] and box[3] > box[1] for box in event.boxes)  # 真实框不是占位


def test_no_substats_evidence_on_failure(monkeypatch):
    fake_panel(monkeypatch, fail_click=True)
    events: list = []
    with gf._capture_filter_evidence(events.append), pytest.raises(gf.FilterClickError):
        gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1,))
    assert events == []


def test_apply_game_filter_applies_substats_before_closing(monkeypatch):
    calls = []
    monkeypatch.setattr(gf, "click_client", lambda *a: None)
    monkeypatch.setattr(gf, "_click_reset", lambda *a: calls.append("reset"))
    monkeypatch.setattr(gf, "_click_slot", lambda *a: calls.append("slot"))
    monkeypatch.setattr(gf, "apply_sets_mainstats", lambda *a, **kw: calls.append("main"))
    monkeypatch.setattr(gf, "apply_substat_filter", lambda *a, **kw: calls.append((kw["names"], kw["counts"])))
    monkeypatch.setattr(gf, "press_key", lambda *a: calls.append("close"))
    monkeypatch.setattr(gf.time, "sleep", lambda *a: None)
    gf.apply_game_filter(1, 1920, 1080, slots=(4,), sets=(), mainstats=("生命值%",), substats=("暴击率",), substat_counts=(1,))
    assert calls == ["reset", "slot", "main", (("暴击率",), (1,)), "close"]


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
    gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1,))
    # 展开前读 1 次 + 点击展开后确认 1 次 + 点「暴击率」后校验 1 次 = 3。
    # 旧实现会在 apply_substat_filter 开头再抓一帧 → 变成 4。
    assert len(grabs) == 3


def test_bottom_band_option_is_scrolled_before_click(monkeypatch):
    """压在面板底部「点击无效带」上的选项，必须先滑动、不能直接点（点了游戏不响应）。"""
    state, clicks = fake_panel(monkeypatch, bottom_band=True)
    gf.apply_substat_filter(1, 1920, 1080, names=("暴击率",), counts=(1,))
    assert state["scrolls"] >= 1
    assert state["selected"] == {"暴击率", "1"}
    # 数字行初始 y=1000 落在无效带里，滑动之前不得点它。
    assert not any(y >= 920 for _x, y in clicks), clicks
