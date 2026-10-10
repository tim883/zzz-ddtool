import tempfile
from pathlib import Path

from zzz_ddtool import paths


def _app_dir():
    """系统临时目录下的独立 app 目录：祖先不含仓库根标记，复原“独立放置”前提。

    不能用 pytest 的 ``tmp_path``：本仓库环境把临时根放在仓库内，祖先会命中
    ``.zzz-ddtool-root``，与“周围无根标记”的测试前提冲突。
    """
    return tempfile.TemporaryDirectory()


def test_standalone_program_uses_its_own_root(monkeypatch):
    with _app_dir() as base:
        tmp = Path(base)
        monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
        monkeypatch.setattr(paths.sys, "executable", str(tmp / "zzz-ddtool.exe"))
        assert paths.user_dir() == tmp / "user"
        assert paths.plans_dir() == tmp / "user" / "plan"


def test_log_dir_follows_shared_root(tmp_path, monkeypatch):
    """打包态有根标记时 log/ 落在公共根 —— 换版本目录不丢日志。"""
    root = tmp_path / "root"
    app = root / "dist" / "v2" / "zzz-ddtool"
    app.mkdir(parents=True)
    (root / ".zzz-ddtool-root").write_text("zzz-ddtool", encoding="utf-8")
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(app / "zzz-ddtool.exe"))

    assert paths.log_dir() == root / "log"
    assert paths.user_dir() == root / "user"  # 与 user/ 同一条规则


def test_log_dir_falls_back_to_app_dir_without_marker(monkeypatch):
    """独立放置（周围无根标记）时 log/ 仍在 exe 同级 —— 与 user/ 同一条限制。"""
    with _app_dir() as base:
        app = Path(base) / "standalone"
        app.mkdir()
        monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
        monkeypatch.setattr(paths.sys, "executable", str(app / "zzz-ddtool.exe"))

        assert paths.log_dir() == app / "log"


def test_runtime_subdirs_hang_off_log_dir(tmp_path, monkeypatch):
    """captures / text / stats / debug / run-status 都跟随 log_dir，不各自定位。"""
    app = tmp_path / "standalone"
    app.mkdir()
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(app / "zzz-ddtool.exe"))

    log = paths.log_dir()
    assert paths.captures_dir() == log / "captures"
    assert paths.logs_dir() == log / "text"
    assert paths.stats_dir() == log / "stats"
    assert paths.debug_dir() == log / "debug"
    assert paths.status_file() == log / "run-status.json"
