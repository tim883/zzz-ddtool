"""日志初始化必须"失败也让程序活着"：共享根不可写时不能因此起不来。"""

from __future__ import annotations

from pathlib import Path

from zzz_ddtool import logutil


def test_setup_survives_unwritable_log_dir(tmp_path, monkeypatch) -> None:
    """日志目录建不出来时不抛异常，logger 仍可用（只是没有文件 handler）。"""
    monkeypatch.setattr(logutil, "logs_dir", lambda: tmp_path / "readonly" / "text")

    def boom(self, *args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "mkdir", boom)
    monkeypatch.setattr(logutil, "_configured", False)

    logutil.setup()  # 不得抛

    assert logutil.get_logger("zzz_ddtool.probe").name == "zzz_ddtool.probe"
