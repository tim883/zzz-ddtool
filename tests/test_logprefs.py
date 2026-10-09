"""日志设置的持久化（user/log-settings.json）：日志级别 + 截图张数上限。

防的就是这个：以前两样都只是进程内状态，关掉就丢——界面里填的
3000 张、开的详细模式，下次打开又变回 300 / 常规。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from PySide6.QtWidgets import QApplication  # noqa: E402

from zzz_ddtool import logprefs, logutil, paths, retain  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def save_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把设置文件指到临时目录，别碰真实的 user/log-settings.json。"""
    target = tmp_path / "user" / "log-settings.json"
    monkeypatch.setattr(logprefs, "log_settings_file", lambda: target)
    return target


@pytest.fixture
def fake_captures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """截图根目录指到临时目录，别让 apply_settings 去删真实 log/captures。"""
    root = tmp_path / "captures"
    root.mkdir()
    monkeypatch.setattr(paths, "captures_dir", lambda: root)
    return root


@pytest.fixture(autouse=True)
def _restore_runtime_state(monkeypatch: pytest.MonkeyPatch):
    """级别和上限都是模块级状态，测完还原，别污染后面的用例。"""
    level = logutil.level()
    keep = retain.get_max_files()
    yield
    logutil.set_level(level)
    retain.set_max_files(keep)


def test_missing_file_means_defaults(save_file: Path) -> None:
    assert not save_file.exists()
    assert logprefs.load() == (logprefs.DEFAULT_LEVEL, logprefs.DEFAULT_MAX_FILES)
    # 没存过时走的就是 retain 那份默认，别让两边数字分家
    assert logprefs.DEFAULT_MAX_FILES == retain.MAX_CAPTURE_FILES


def test_saved_settings_come_back(save_file: Path) -> None:
    logprefs.save(logging.WARNING, 1234)
    assert json.loads(save_file.read_text(encoding="utf-8")) == {"level": logging.WARNING, "max_files": 1234}
    assert logprefs.load() == (logging.WARNING, 1234)


@pytest.mark.parametrize("raw", ["{ 不是 json", "", "[]", '{"level": null, "max_files": null}'])
def test_broken_file_falls_back(save_file: Path, raw: str) -> None:
    """读坏了不能反过来把程序带崩，一律回默认档。"""
    save_file.parent.mkdir(parents=True, exist_ok=True)
    save_file.write_text(raw, encoding="utf-8")
    assert logprefs.load() == (logprefs.DEFAULT_LEVEL, logprefs.DEFAULT_MAX_FILES)


def test_unknown_level_falls_back(save_file: Path) -> None:
    """界面只有三档，存了别的级别（手改文件）当没存过。"""
    save_file.parent.mkdir(parents=True, exist_ok=True)
    save_file.write_text(json.dumps({"level": logging.ERROR, "max_files": 500}), encoding="utf-8")
    assert logprefs.load() == (logprefs.DEFAULT_LEVEL, 500)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, logprefs.MIN_KEEP_FILES),
        (99999, logprefs.MAX_KEEP_FILES),
        ("abc", logprefs.DEFAULT_MAX_FILES),
    ],
)
def test_keep_is_clamped(save_file: Path, value: object, expected: int) -> None:
    save_file.parent.mkdir(parents=True, exist_ok=True)
    save_file.write_text(json.dumps({"level": logging.INFO, "max_files": value}), encoding="utf-8")
    assert logprefs.load() == (logprefs.DEFAULT_LEVEL, expected)


def test_restore_applies_saved_settings(save_file: Path) -> None:
    """关掉再开还在——持久化的全部意义。"""
    logprefs.save(logging.DEBUG, 50)
    assert logprefs.restore() == (logging.DEBUG, 50)
    assert logutil.level() == logging.DEBUG
    assert retain.get_max_files() == 50

    # 假装这是「新开的一次程序」：内存全刷回默认，只重新读盘
    logutil.set_level(logging.INFO)
    retain.set_max_files(retain.MAX_CAPTURE_FILES)
    assert logprefs.load() == (logging.DEBUG, 50)


def test_restore_does_not_retain_captures(save_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动时只装设置，不删老截图——回收等下一次存图时才该发生。"""
    calls: list[int] = []
    monkeypatch.setattr(retain, "retain_captures", lambda *a, **k: calls.append(1) or [])

    logprefs.save(logging.DEBUG, 50)
    logprefs.restore()

    assert calls == []


def test_dialog_applies_and_persists(qapp, save_file: Path, fake_captures: Path) -> None:
    """从界面里点确定，改完得能下次启动还是它。"""
    from zzz_ddtool.logsettings import LogSettingsDialog

    dialog = LogSettingsDialog()
    dialog.level_combo.set_level(logging.DEBUG)
    dialog.keep_spin.setValue(1234)
    dialog.apply_settings()

    assert logprefs.load() == (logging.DEBUG, 1234)
    assert logutil.level() == logging.DEBUG
    assert retain.get_max_files() == 1234

    # 重新打开界面：显示的是存下来的值，不是 300 / 常规
    reopened = LogSettingsDialog()
    assert reopened.keep_spin.value() == 1234
    assert reopened.level_combo.currentData() == logging.DEBUG


def test_dialog_spin_range_matches_prefs(qapp, save_file: Path) -> None:
    """spinbox 的上下限和 logprefs 里的数必须同一个来源。"""
    from zzz_ddtool.logsettings import LogSettingsDialog

    dialog = LogSettingsDialog()
    assert (dialog.keep_spin.minimum(), dialog.keep_spin.maximum()) == (
        logprefs.MIN_KEEP_FILES,
        logprefs.MAX_KEEP_FILES,
    )
