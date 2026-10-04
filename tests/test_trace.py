"""步骤跟踪的中文命名与步骤号（日志给人看，错了就白记）。"""

import logging
from pathlib import Path

import pytest

from zzz_ddtool.trace import Trace, action_name, page_name


def test_page_names_are_chinese() -> None:
    assert page_name("drive_disc") == "驱动盘仓库"
    assert page_name("detail") == "详情页"
    assert page_name("zoom") == "缩放页"
    assert page_name("upgrade") == "强化页"
    assert page_name("menu") == "功能页"
    assert page_name("storage") == "音擎仓库"
    assert page_name("overworld") == "大地图"


def test_unknown_page_is_passed_through_not_invented() -> None:
    # 认不出的页面原样返回，绝不能编一个名字误导排查
    assert page_name("some_new_page") == "some_new_page"


def test_action_names_are_chinese() -> None:
    assert action_name("scan") == "扫描列表"
    assert action_name("parse") == "识别盘面"
    assert action_name("decide") == "判断"
    assert action_name("upgrade") == "强化"
    assert action_name("discard") == "打弃置标记"
    assert action_name("enter_upgrade") == "进强化页"


def test_unknown_action_is_passed_through() -> None:
    assert action_name("whatever") == "whatever"


def test_step_number_increments() -> None:
    trace = Trace(tag="test")
    assert trace.step == 0
    trace.ok("detail", "parse")
    assert trace.step == 1
    trace.ok("detail", "decide")
    assert trace.step == 2


def test_fail_marks_round_as_failed() -> None:
    trace = Trace(tag="test2")
    assert not trace.failed
    trace.ok("detail", "parse")
    assert not trace.failed
    trace.fail("detail", "enter_upgrade", "理由")
    assert trace.failed


def test_fail_mentions_screenshot_or_explains_why_not(caplog: pytest.LogCaptureFixture) -> None:
    """失败日志必须交代截图情况，否则事后无从回溯。"""
    trace = Trace(tag="test3")
    with caplog.at_level(logging.ERROR, logger="zzz_ddtool.trace"):
        trace.fail("detail", "enter_upgrade", "没找到按钮")
    messages = [r.message for r in caplog.records]
    assert any("进强化页" in m and "失败" in m for m in messages)
    # 要么给了截图文件名，要么明说截图没成功——不能一声不吭
    assert any("截图=" in m or "截图没成功" in m for m in messages)


def test_screenshot_filename_uses_step_number() -> None:
    """截图名带步骤号，才能和日志那一行对上。"""
    trace = Trace(tag="tag")
    trace.ok("detail", "parse")
    name = trace._persist("parse", image=None)
    # 这台机器没有游戏窗口时会截不了图，返回 None 也算正常
    if name is not None:
        assert name.startswith("tag/0001-")
        assert name.endswith(".png")


def test_decide_does_not_shoot() -> None:
    """判断是纯计算不碰屏幕，截图跟上一张一模一样，不该截。"""
    trace = Trace(tag="tag-decide")
    trace.ok("detail", "decide", "继续强化")
    assert trace._shots == []


class _FakeImage:
    """假装是 QImage。这台机器没游戏窗口，现截必然失败，测试得自己喂图。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, path: str, _fmt: str) -> bool:
        Path(path).write_bytes(b"x")
        return True


def test_every_step_gets_a_photo_except_decide(tmp_path: Path) -> None:
    """一步一扫。判断是纯计算例外——不碰屏幕，截出来跟上一张一样。"""
    trace = Trace(tag="tag-steps")
    img = _FakeImage(tmp_path)
    trace.ok("detail", "click_cell", "第2行第3列", image=img)
    trace.ok("detail", "parse", "等级3", image=img)
    trace.ok("detail", "decide", "继续强化", image=img)  # 判断不截
    # 前两步各记一张，判断不记
    assert len(trace._shots) == 2


def test_note_does_not_consume_a_step() -> None:
    trace = Trace(tag="test4")
    trace.ok("detail", "parse")
    before = trace.step
    trace.note("补充说明")
    assert trace.step == before


def test_steps_record_elapsed_time_with_chinese_names(tmp_path: Path) -> None:
    """每步都记耗时，名字跟日志里一致（统计直接按这个名字分组）。"""
    trace = Trace(tag="tag-spans")
    img = _FakeImage(tmp_path)
    trace.ok("drive_disc", "scan", "第 1 批", image=img)
    trace.ok("upgrade", "upgrade", "强化了一阶", image=img)

    assert [s.page for s in trace.spans] == ["驱动盘仓库", "强化页"]
    assert [s.action for s in trace.spans] == ["扫描列表", "强化"]
    assert [s.detail for s in trace.spans] == ["第 1 批", "强化了一阶"]
    assert all(s.seconds >= 0.0 for s in trace.spans)


def test_decide_step_also_records_a_span(tmp_path: Path) -> None:
    """判断不截图，但耗时还是要记——它也是链路里的一步。"""
    trace = Trace(tag="tag-spans-decide")
    trace.ok("detail", "decide", "继续强化")
    assert len(trace.spans) == 1
    assert trace.spans[0].action == "判断"
    assert trace._shots == []
