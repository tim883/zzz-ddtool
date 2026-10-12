"""日志初始化必须"失败也让程序活着"：日志目录建不出来时不能因此起不来。

新契约：一轮一个 ``ddtool.log``，文件挂在当前轮次上；建不出来只降级成
"不写文件"，不抛异常，也不改调用方。切轮次/退出时才关文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zzz_ddtool import loground, logutil


@pytest.fixture
def sandbox_round(tmp_path: Path):
    """在 tmp 上开一轮真实轮次：断言不依赖会话轮次还在不在，也不写会话沙箱。"""
    root = tmp_path / "log_test"
    root.mkdir()
    round_ = loground.use_test_context("logutil", root=root)
    yield round_
    loground.close_active()


def test_setup_writes_into_current_round(sandbox_round) -> None:
    """正常路径：文件记录就是本轮的 ddtool.log，不含 tag/ 之类的中间层。"""
    logutil.setup()
    assert logutil.current_log_path() == sandbox_round.log_path

    logutil.get_logger("zzz_ddtool.probe").error("写进本轮")
    assert "写进本轮" in sandbox_round.log_path.read_text(encoding="utf-8")


def test_setup_survives_unwritable_log_dir(sandbox_round, monkeypatch) -> None:
    """日志目录建不出来时不抛异常，降级成没有文件 handler，logger 仍可用。"""
    logutil.setup()
    assert logutil.current_log_path() == sandbox_round.log_path  # 先确认正常时确实挂了文件

    def boom(self, *args, **kwargs):
        raise PermissionError("read-only")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", boom)
        logutil.setup()  # 不得抛
        assert logutil.current_log_path() is None
        assert logutil.get_logger("zzz_ddtool.probe").name == "zzz_ddtool.probe"

    # 可写之后能重新挂上文件：证明刚才是降级，不是把日志整个关掉
    logutil.setup()
    assert logutil.current_log_path() == sandbox_round.log_path
