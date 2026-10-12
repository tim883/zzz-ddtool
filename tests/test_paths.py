"""公共根定位：开发态 = 仓库根，打包态 = 有根标记的公共根。

``log/``（正式运行）与 ``log_test/``（测试、探测、回放、标定）走同一条规则，
彼此分开：换版本目录不丢日志，测试也不进生产计数。

注意：tests/conftest.py 为了隔离会把 ``paths.log_root`` / ``log_settings_file``
临时换成沙箱替身，所以这里取一份未被替换的 ``paths`` 模块来验定位规则本身。
"""

import importlib.util
import tempfile
from pathlib import Path

import pytest

from zzz_ddtool import paths


@pytest.fixture(scope="module")
def real_paths():
    spec = importlib.util.spec_from_file_location("paths_pristine", paths.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app_dir():
    """系统临时目录下的独立 app 目录：祖先不含仓库根标记，复原“独立放置”前提。

    不能用 pytest 的 ``tmp_path``：本仓库环境把临时根放在仓库内，祖先会命中
    ``.zzz-ddtool-root``，与“周围无根标记”的测试前提冲突。
    """
    return tempfile.TemporaryDirectory()


def _fake_exe(monkeypatch, module, exe: Path) -> None:
    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.sys, "executable", str(exe))


def test_standalone_program_uses_its_own_root(real_paths, monkeypatch):
    with _app_dir() as base:
        tmp = Path(base)
        _fake_exe(monkeypatch, real_paths, tmp / "zzz-ddtool.exe")
        assert real_paths.user_dir() == tmp / "user"
        assert real_paths.plans_dir() == tmp / "user" / "plan"


def test_log_roots_follow_shared_root(real_paths, tmp_path, monkeypatch):
    """打包态有根标记时两个日志根都落在公共根 —— 换版本目录不丢日志。"""
    root = tmp_path / "root"
    app = root / "dist" / "v2" / "zzz-ddtool"
    app.mkdir(parents=True)
    (root / ".zzz-ddtool-root").write_text("zzz-ddtool", encoding="utf-8")
    _fake_exe(monkeypatch, real_paths, app / "zzz-ddtool.exe")

    assert real_paths.log_root() == root / "log"
    assert real_paths.test_log_root() == root / "log_test"
    assert real_paths.user_dir() == root / "user"  # 与 user/ 同一条规则


def test_log_roots_fall_back_to_app_dir_without_marker(real_paths, monkeypatch):
    """独立放置（周围无根标记）时两个根仍在 exe 同级，且彼此分开。"""
    with _app_dir() as base:
        app = Path(base) / "standalone"
        app.mkdir()
        _fake_exe(monkeypatch, real_paths, app / "zzz-ddtool.exe")

        assert real_paths.log_root() == app / "log"
        assert real_paths.test_log_root() == app / "log_test"


def test_dev_tree_puts_both_log_roots_under_repo(real_paths):
    """开发态（非打包）两个根都落在仓库根，和 user/ 同一层，互不重名。"""
    assert not getattr(real_paths.sys, "frozen", False)
    assert real_paths.log_root() == real_paths.repo_root() / "log"
    assert real_paths.test_log_root() == real_paths.repo_root() / "log_test"
    assert real_paths.log_settings_file() == real_paths.user_dir() / "log-settings.json"
