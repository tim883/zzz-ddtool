"""步骤跟踪的中文命名、步骤号与截图落点（日志给人看，错了就白记）。

落点变了：图存**当前轮次**的 ``captures/``，不再拼 ``<tag>/`` 一层；
日志里的「截图=」只写文件名，同一轮目录里一看就对上。
"""

import logging
from pathlib import Path

import pytest

from zzz_ddtool.trace import Trace, action_name, page_name
from zzz_ddtool import trace as trace_module


@pytest.fixture(autouse=True)
def isolated_capture(tmp_path, monkeypatch):
    """只用临时目录和内存图片，不在日志回归中抓真实桌面或写会话沙箱。"""
    monkeypatch.setattr(trace_module.loground, "captures_dir", lambda: tmp_path / "captures")
    monkeypatch.setattr(trace_module, "grab_game_image", lambda: (_FakeImage(tmp_path), "fake", 1))


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


def test_screenshot_lands_in_current_captures_without_a_tag_dir(tmp_path) -> None:
    """截图名带步骤号，才能和日志那一行对上；目录就是本轮 captures，不再拼 tag。"""
    trace = Trace(tag="tag")
    trace.step_start("detail", "parse")
    name = trace._persist("parse", image=_FakeImage(tmp_path))
    assert trace.dir == tmp_path / "captures"
    assert name == "0001-识别盘面.png"
    assert trace._shots == [tmp_path / "captures" / "0001-识别盘面.png"]
    assert trace._shots[0].read_bytes() == b"x"


def test_log_line_names_only_the_screenshot_file(tmp_path, caplog) -> None:
    """日志里的「截图=」只写文件名，不写一长串路径。"""
    trace = Trace(tag="tag-name")
    with caplog.at_level(logging.INFO, logger="zzz_ddtool.trace"):
        trace.ok("detail", "parse", "等级3", image=_FakeImage(tmp_path))
    messages = [r.getMessage() for r in caplog.records if "截图=" in r.getMessage()]
    assert len(messages) == 1
    assert messages[0].endswith("截图=0001-识别盘面.png")
    assert "captures" not in messages[0]


def test_tag_defaults_to_the_round_id(tmp_path) -> None:
    """不给 tag 时用当前轮次编号：日志、统计、性能共用一个身份。"""
    root = tmp_path / "log_test"
    root.mkdir()
    round_ = trace_module.loground.use_test_context("trace", root=root)
    try:
        assert Trace().tag == round_.id
    finally:
        trace_module.loground.close_active()


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


def test_steps_record_elapsed_time_with_chinese_names(tmp_path: Path, monkeypatch) -> None:
    """每步都记耗时，名字跟日志里一致（统计直接按这个名字分组）。"""
    from types import SimpleNamespace

    now = [100.0]
    monkeypatch.setattr(trace_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    trace = Trace(tag="tag-spans")
    img = _FakeImage(tmp_path)
    now[0] += 0.25
    trace.ok("drive_disc", "scan", "第 1 批", image=img)
    now[0] += 0.75
    trace.ok("upgrade", "upgrade", "强化了一阶", image=img)

    assert [s.page for s in trace.spans] == ["驱动盘仓库", "强化页"]
    assert [s.action for s in trace.spans] == ["扫描列表", "强化"]
    assert [s.detail for s in trace.spans] == ["第 1 批", "强化了一阶"]
    assert [s.seconds for s in trace.spans] == pytest.approx([0.25, 0.75])


def test_decide_step_also_records_a_span(tmp_path: Path) -> None:
    """判断不截图，但耗时还是要记——它也是链路里的一步。"""
    trace = Trace(tag="tag-spans-decide")
    trace.ok("detail", "decide", "继续强化")
    assert len(trace.spans) == 1
    assert trace.spans[0].action == "判断"
    assert trace._shots == []


def test_finish_logs_one_summary_and_does_not_recycle(tmp_path, monkeypatch, caplog) -> None:
    """收尾只记一条摘要；回收只由正式入口触发，trace 不反手清生产日志。"""
    from zzz_ddtool import retain

    calls: list[int] = []
    monkeypatch.setattr(retain, "retain_rounds", lambda *a, **k: calls.append(1) or [])

    trace = Trace(tag="tag-finish")
    trace.ok("detail", "parse", "等级3", image=_FakeImage(tmp_path))
    with caplog.at_level(logging.INFO, logger="zzz_ddtool.trace"):
        trace.finish()

    assert calls == []
    summaries = [r.getMessage() for r in caplog.records if "本轮结束" in r.getMessage()]
    assert len(summaries) == 1
    assert "共 1 步" in summaries[0]


def test_trace_accepts_explicit_dir(tmp_path: Path) -> None:
    """§2.1：final 测量脚本可用显式目录把 Runner 的 Trace 指向本次证据目录。"""
    target = tmp_path / "evidence" / "run-1"
    trace = Trace(tag="evidence-run", dir=target)
    assert trace.dir == target
    assert target.is_dir()
