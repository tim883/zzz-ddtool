"""单实例限制：同一台机器上第二个实例（含不同版本）必须被拦下。

锁放在公共根而不是版本目录，所以不同版本目录的 exe 也会互相拦住 ——
这正对"同时跑两个版本会共用同一份 log/ 与 run-status.json"的问题。
"""

from __future__ import annotations

from pathlib import Path

from zzz_ddtool import paths, singleinstance


def test_lock_path_is_in_shared_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "user_data_root", lambda: tmp_path)
    assert singleinstance.lock_path() == tmp_path / ".zzz-ddtool.lock"


def test_second_acquire_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "user_data_root", lambda: tmp_path)

    first = singleinstance.acquire()
    assert first is not None
    try:
        assert singleinstance.acquire() is None
    finally:
        singleinstance.release(first)

    second = singleinstance.acquire()
    assert second is not None
    singleinstance.release(second)


def test_acquire_creates_root_when_missing(tmp_path: Path, monkeypatch) -> None:
    """共享根尚不存在（首次运行）时不得抛异常。"""
    target = tmp_path / "not" / "yet"
    monkeypatch.setattr(paths, "user_data_root", lambda: target)

    lock = singleinstance.acquire()
    assert lock is not None
    singleinstance.release(lock)


def test_acquire_raises_when_root_unwritable(tmp_path: Path, monkeypatch) -> None:
    """共享根不可写时**不能假装"已有实例"** —— 抛 LockUnavailable 让调用方让位。

    原来返回 None 会让界面报"程序已在运行"并直接退出：明明没有第二个实例，
    程序却永远起不来，提示还是错的原因。
    """
    import pytest

    monkeypatch.setattr(paths, "user_data_root", lambda: tmp_path / "readonly")

    def boom(self, *args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "mkdir", boom)

    with pytest.raises(singleinstance.LockUnavailable):
        singleinstance.acquire()


def _stub_app(monkeypatch, events: list[str]):
    """把 gui.QApplication 换成桩，并隔离与单实例无关的 apply_light_style。

    不能只换 QApplication：apply_light_style 里的 QFont.exactMatch() 需要真实的
    QGuiApplication，纯桩对象会让进程硬崩。它也与本任务无关，直接换成空实现。
    """
    from zzz_ddtool import gui

    class _StubApp:
        @classmethod
        def instance(cls):
            return None

        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

        def exec(self):
            events.append("exec")
            return 0

    monkeypatch.setattr(gui, "QApplication", _StubApp)
    monkeypatch.setattr(gui, "apply_light_style", lambda app: None)


def test_run_gui_refuses_when_lock_held(monkeypatch) -> None:
    """接线本身也要钉住：锁被占时 run_gui 直接返回，既不建主窗口也不进事件循环。"""
    import pytest

    from zzz_ddtool import gui, singleinstance

    events: list[str] = []
    _stub_app(monkeypatch, events)
    monkeypatch.setattr(singleinstance, "acquire", lambda: None)

    notified: list[str] = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *args, **kwargs: notified.append("info"))
    monkeypatch.setattr(gui, "MainWindow", lambda *args, **kwargs: pytest.fail("不应创建主窗口"))

    assert gui.run_gui() == 0
    assert notified, "应提示已在运行"
    assert events == [], "不应进入事件循环"


def test_run_gui_proceeds_when_lock_acquired(monkeypatch) -> None:
    """互补分支：拿到锁时照常建窗口、进事件循环，退出时释放锁。

    与上一条合起来证明 run_gui 确实按 `acquire()` 的结果分流，
    而不是"碰巧返回 0"。
    """
    from zzz_ddtool import gui, singleinstance

    events: list[str] = []
    _stub_app(monkeypatch, events)
    fake_lock = object()
    monkeypatch.setattr(singleinstance, "acquire", lambda: fake_lock)
    monkeypatch.setattr(singleinstance, "release", lambda lock: events.append(f"release:{lock is fake_lock}"))

    class _StubWindow:
        def show(self):
            events.append("show")

    monkeypatch.setattr(gui, "MainWindow", lambda *args, **kwargs: events.append("window") or _StubWindow())

    assert gui.run_gui() == 0
    assert events == ["window", "show", "exec", "release:True"]


def test_run_gui_starts_without_lock_when_unavailable(monkeypatch) -> None:
    """I1/I6：锁建不出来时**照常启动**并给出提示，不能报"程序已在运行"。"""
    import types

    import pytest

    from zzz_ddtool import gui, singleinstance

    events: list[str] = []
    _stub_app(monkeypatch, events)

    def unavailable():
        raise singleinstance.LockUnavailable("read-only")

    monkeypatch.setattr(singleinstance, "acquire", unavailable)
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *args, **kwargs: pytest.fail("不该说已在运行"))

    hints: list[str] = []

    class _StubWindow:
        def __init__(self):
            self.status = types.SimpleNamespace(setText=hints.append)

        def show(self):
            events.append("show")

    monkeypatch.setattr(gui, "MainWindow", lambda *args, **kwargs: _StubWindow())

    assert gui.run_gui() == 0
    assert events == ["show", "exec"]
    assert hints and "单实例锁" in hints[0]
