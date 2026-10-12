"""日志设置的持久化（user/log-settings.json）：日志级别 + 历史轮数上限。

防的就是这个：以前两样都只是进程内状态，关掉就丢——界面里填的 / 开的详细模式，
下次打开又变回默认。

旧配置只有 ``max_files``（截图张数上限）。张数和轮数不是一个量纲，
300 张不能读成 300 轮，一律按新默认轮数处理。
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
    monkeypatch.setattr(paths, "log_settings_file", lambda: target)
    return target


@pytest.fixture(autouse=True)
def _restore_runtime_state(monkeypatch: pytest.MonkeyPatch):
    """级别和轮数上限都是模块级状态，测完还原，别污染后面的用例。"""
    level = logutil.level()
    rounds = retain.get_max_rounds()
    yield
    logutil.set_level(level)
    retain.set_max_rounds(rounds)


def _write(target: Path, data: object) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = data if isinstance(data, str) else json.dumps(data)
    target.write_text(raw, encoding="utf-8")


def test_missing_file_means_defaults(save_file: Path) -> None:
    assert not save_file.exists()
    assert logprefs.load() == (logprefs.DEFAULT_LEVEL, logprefs.DEFAULT_KEEP_ROUNDS)
    # 两个模块别各记一份默认值：界面/持久化读的都是 retain 那份
    assert logprefs.DEFAULT_KEEP_ROUNDS == retain.DEFAULT_MAX_ROUNDS
    assert (retain.MIN_KEEP_ROUNDS, retain.MAX_KEEP_ROUNDS) == (1, 100)


def test_saved_settings_come_back(save_file: Path) -> None:
    logprefs.save(logging.WARNING, 7)
    assert json.loads(save_file.read_text(encoding="utf-8")) == {
        "version": logprefs.CONFIG_VERSION,
        "level": logging.WARNING,
        "max_rounds": 7,
    }
    assert logprefs.load() == (logging.WARNING, 7)


def test_current_format_without_level_keeps_saved_rounds(save_file: Path) -> None:
    """有版本号和 max_rounds、没写 level：轮数照读，级别回默认档。"""
    _write(save_file, {"version": logprefs.CONFIG_VERSION, "max_rounds": 7})
    assert logprefs.load() == (logprefs.DEFAULT_LEVEL, 7)


@pytest.mark.parametrize("raw", ["{ 不是 json", "", "[]", '{"level": null, "max_files": null}'])
def test_broken_file_falls_back(save_file: Path, raw: str) -> None:
    """读坏了不能反过来把程序带崩，一律回默认档。"""
    _write(save_file, raw)
    assert logprefs.load() == (logprefs.DEFAULT_LEVEL, logprefs.DEFAULT_KEEP_ROUNDS)


def test_legacy_max_files_is_not_read_as_rounds(save_file: Path) -> None:
    """旧格式的 300 是截图张数，不能当成 300 轮；级别保留，轮数走新默认。"""
    _write(save_file, {"level": logging.INFO, "max_files": 300})
    assert logprefs.load() == (logging.INFO, logprefs.DEFAULT_KEEP_ROUNDS)

    _write(save_file, {"version": 1, "level": logging.INFO, "max_files": 300})
    loaded = logprefs.load()
    assert loaded == (logging.INFO, logprefs.DEFAULT_KEEP_ROUNDS)
    assert loaded[1] != 300


def test_unknown_level_falls_back(save_file: Path) -> None:
    """界面只有三档，存了别的级别（手改文件）当没存过；轮数不受牵连。"""
    _write(save_file, {"version": logprefs.CONFIG_VERSION, "level": logging.ERROR, "max_rounds": 7})
    assert logprefs.load() == (logprefs.DEFAULT_LEVEL, 7)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 1),
        (-5, 1),
        (99999, 100),
        ("abc", logprefs.DEFAULT_KEEP_ROUNDS),
        (7, 7),
        (100, 100),
    ],
)
def test_rounds_are_clamped(save_file: Path, value: object, expected: int) -> None:
    """越界值夹到 1—100；读不出来用默认；范围内的值原样保留。"""
    _write(save_file, {"version": logprefs.CONFIG_VERSION, "level": logging.INFO, "max_rounds": value})
    assert logprefs.load() == (logging.INFO, expected)


def test_restore_applies_saved_settings(save_file: Path) -> None:
    """关掉再开还在——持久化的全部意义。"""
    logprefs.save(logging.DEBUG, 7)
    assert logprefs.restore() == (logging.DEBUG, 7)
    assert logutil.level() == logging.DEBUG
    assert retain.get_max_rounds() == 7

    # 假装这是「新开的一次程序」：内存全刷回默认，只重新读盘
    logutil.set_level(logging.INFO)
    retain.set_max_rounds(retain.DEFAULT_MAX_ROUNDS)
    assert logprefs.load() == (logging.DEBUG, 7)


def test_restore_runs_one_retention_pass(save_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动装回设置时顺带按轮数收一次旧日志（回收只在入口触发）。"""
    calls: list[tuple] = []
    monkeypatch.setattr(retain, "retain_rounds", lambda *a, **k: calls.append((a, k)) or [])

    logprefs.save(logging.DEBUG, 7)
    logprefs.restore()

    assert len(calls) == 1


def test_dialog_applies_and_persists(qapp, save_file: Path) -> None:
    """从界面里点确定，改完得能下次启动还是它。"""
    from zzz_ddtool.logsettings import LogSettingsDialog

    dialog = LogSettingsDialog()
    dialog.level_combo.set_level(logging.DEBUG)
    dialog.rounds_edit.setText("7")
    dialog.apply_settings()

    assert logprefs.load() == (logging.DEBUG, 7)
    assert logutil.level() == logging.DEBUG
    assert retain.get_max_rounds() == 7

    # 重新打开界面：显示的是存下来的值，不是默认档
    reopened = LogSettingsDialog()
    assert reopened.rounds_edit.text() == "7"
    assert reopened.level_combo.currentData() == logging.DEBUG


def test_dialog_rounds_range_matches_prefs(qapp, save_file: Path) -> None:
    """输入框的上下限和 retain 里的数必须同一个来源，填超了也要夹回来。"""
    from zzz_ddtool.logsettings import LogSettingsDialog

    dialog = LogSettingsDialog()
    validator = dialog.rounds_edit.validator()
    assert (validator.bottom(), validator.top()) == (
        retain.MIN_KEEP_ROUNDS,
        retain.MAX_KEEP_ROUNDS,
    )

    dialog.rounds_edit.setText(str(retain.MAX_KEEP_ROUNDS + 100))
    assert dialog.chosen_rounds() == retain.MAX_KEEP_ROUNDS
    dialog.rounds_edit.setText("0")
    assert dialog.chosen_rounds() == retain.MIN_KEEP_ROUNDS
