"""截图保留：纯按数量，从最旧的轮次整轮删。

前提：排错靠 debug 重跑复现，不靠翻旧图回溯，所以不做
「失败永远保留」这类特殊策略——留着只会把正常的图挤掉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zzz_ddtool import retain
from zzz_ddtool import paths
from zzz_ddtool.retain import clear_captures, count_captures, retain_captures


@pytest.fixture
def fake_captures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把截图根目录指到临时目录，别碰真实 log/captures。"""
    root = tmp_path / "captures"
    root.mkdir()
    monkeypatch.setattr(paths, "captures_dir", lambda: root)
    return root


def _make_round(root: Path, name: str, count: int) -> Path:
    folder = root / name
    folder.mkdir()
    for i in range(count):
        (folder / f"{i:04d}-识别盘面.png").write_bytes(b"x")
    return folder


def test_under_cap_keeps_everything(fake_captures: Path) -> None:
    _make_round(fake_captures, "20260907-100000", 3)
    assert count_captures() == 3
    assert retain_captures(max_files=10) == []


def test_over_cap_drops_oldest_round_whole(fake_captures: Path) -> None:
    """超了整轮删最旧的，不留半截轮次。"""
    _make_round(fake_captures, "20260907-100000", 5)
    _make_round(fake_captures, "20260907-110000", 5)
    _make_round(fake_captures, "20260907-120000", 5)

    removed = retain_captures(max_files=10)
    left = sorted(p.name for p in fake_captures.iterdir())

    # 删掉最早那轮 5 张，剩 10 张
    assert len(removed) == 5
    assert left == ["20260907-110000", "20260907-120000"]
    assert count_captures() == 10


def test_no_special_treatment_for_failures(fake_captures: Path) -> None:
    """失败图没有特权——排错靠重跑，不靠留旧图。

    这条是防回归：哪天又加回「失败永远保留」，这个测试会挂。
    """
    old = _make_round(fake_captures, "20260907-100000", 3)
    (old / "0009-进强化页.png").write_bytes(b"x")  # 假设这是失败那张
    _make_round(fake_captures, "20260907-110000", 3)

    retain_captures(max_files=3)
    left = sorted(p.name for p in fake_captures.iterdir())
    assert left == ["20260907-110000"]
    assert not (fake_captures / "20260907-100000").exists()


def test_clear_captures_empties_everything(fake_captures: Path) -> None:
    _make_round(fake_captures, "20260907-100000", 4)
    _make_round(fake_captures, "20260907-110000", 4)
    assert clear_captures() == 8
    assert count_captures() == 0
    assert not list(fake_captures.iterdir())


def test_count_on_missing_dir_is_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "captures_dir", lambda: tmp_path / "nope")
    assert count_captures() == 0
    assert retain_captures(max_files=10) == []


def test_set_max_files_sticks_for_the_save_path(
    fake_captures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """日志设置里填的上限要一直生效，不能只在点确定那一下管用。

    capture.save_image / trace.finish 调的是 retain_captures()（不带参数），
    所以上限必须记在模块里；否则用户填的 500 会被下一次写图裁回默认 300。
    """
    monkeypatch.setattr(retain, "_max_files", retain.MAX_CAPTURE_FILES)
    _make_round(fake_captures, "20260907-100000", 5)
    _make_round(fake_captures, "20260907-110000", 5)
    assert retain.get_max_files() == retain.MAX_CAPTURE_FILES

    retain.set_max_files(10)
    assert retain.get_max_files() == 10
    assert retain_captures() == []  # 10 张 == 上限，不删

    retain.set_max_files(5)
    assert len(retain_captures()) == 5  # 不带参数也按改后的上限删
    assert count_captures() == 5


def test_set_max_files_floor_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retain, "_max_files", retain.MAX_CAPTURE_FILES)
    retain.set_max_files(0)
    assert retain.get_max_files() == 1
