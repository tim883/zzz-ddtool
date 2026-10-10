"""更新器安全边界：更新包不得改写用户的 user/ 与 log/。

打包方失误（把跑过的包拿去打 zip）会让包里带上 top-level 的 user/、log/，
xcopy 是覆盖式复制 —— 这层剥离是"更新绝不改写用户数据与日志"的硬保证。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from zzz_ddtool import paths, updater


def _make_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)


def test_strip_protected_removes_only_top_level_user_and_log(tmp_path: Path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "layout.json").write_text("x", encoding="utf-8")
    (tmp_path / "log").mkdir()
    (tmp_path / "log" / "a.log").write_text("x", encoding="utf-8")
    (tmp_path / "data" / "user").mkdir(parents=True)  # 嵌套同名，必须保留
    (tmp_path / "zzz-ddtool.exe").write_text("x", encoding="utf-8")

    removed = updater._strip_protected(tmp_path)

    assert sorted(removed) == ["log", "user"]
    assert not (tmp_path / "user").exists()
    assert not (tmp_path / "log").exists()
    assert (tmp_path / "data" / "user").exists()
    assert (tmp_path / "zzz-ddtool.exe").exists()


def test_apply_program_update_cannot_touch_user_or_log(tmp_path: Path, monkeypatch) -> None:
    """更新包内含 user/、log/ 时，用户侧仍必须逐字节不变。"""
    app = tmp_path / "app"
    (app / "user").mkdir(parents=True)
    (app / "user" / "layout.json").write_text("MINE", encoding="utf-8")
    (app / "log" / "text").mkdir(parents=True)
    (app / "log" / "text" / "ddtool.log").write_text("MYLOG", encoding="utf-8")

    zipsrc = tmp_path / "prog.zip"
    _make_zip(
        zipsrc,
        {
            "zzz-ddtool.exe": "NEW-EXE",
            "version.txt": "9.9.9",
            "user/layout.json": "SHIPPED",
            "log/text/ddtool.log": "SHIPPED",
        },
    )
    staged = tmp_path / "staged"
    staged.mkdir()

    monkeypatch.setattr(updater, "_download", lambda url, dest: Path(dest).write_bytes(zipsrc.read_bytes()))
    monkeypatch.setattr(paths, "base_dir", lambda: app)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(staged))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit):
        updater.apply_program_update("http://example.invalid/x.zip")

    new_dir = staged / "new"
    assert not (new_dir / "user").exists()
    assert not (new_dir / "log").exists()

    # 模拟替身 bat 的 xcopy（覆盖式复制），证明用户文件逐字节不变
    shutil.copytree(new_dir, app, dirs_exist_ok=True)
    assert (app / "user" / "layout.json").read_text(encoding="utf-8") == "MINE"
    assert (app / "log" / "text" / "ddtool.log").read_text(encoding="utf-8") == "MYLOG"
    assert (app / "zzz-ddtool.exe").read_text(encoding="utf-8") == "NEW-EXE"


def test_apply_data_update_strips_protected_and_merges_into_data(tmp_path: Path, monkeypatch) -> None:
    """数据更新同样不得把 zip 里的 user/、log/ 变成 data/user/、data/log/。"""
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "stats.json").write_text("OLD", encoding="utf-8")
    (data_root / "keep.json").write_text("KEEP", encoding="utf-8")

    zipsrc = tmp_path / "data.zip"
    _make_zip(
        zipsrc,
        {
            "sets.json": "NEW",
            "stats.json": "NEW",
            "user/layout.json": "SHIPPED",
            "log/text/ddtool.log": "SHIPPED",
        },
    )
    workdir = tmp_path / "work"
    workdir.mkdir()

    monkeypatch.setattr(updater, "_download", lambda url, dest: Path(dest).write_bytes(zipsrc.read_bytes()))
    monkeypatch.setattr(paths, "data_dir", lambda: data_root)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(workdir))

    updater.apply_data_update("http://example.invalid/data.zip")

    assert (data_root / "sets.json").read_text(encoding="utf-8") == "NEW"
    assert (data_root / "stats.json").read_text(encoding="utf-8") == "NEW"
    assert (data_root / "keep.json").read_text(encoding="utf-8") == "KEEP"  # 不删除 data/ 多余文件
    assert not (data_root / "user").exists()
    assert not (data_root / "log").exists()


def _http_404(body: bytes):
    import io
    import urllib.error

    return urllib.error.HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(body))


def _raiser(exc):
    def _raise(*args, **kwargs):
        raise exc

    return _raise


def test_check_for_update_distinguishes_invisible_repo(monkeypatch) -> None:
    """仓库不可见（不存在/未公开）不能伪装成"没有新版本"。"""
    body = b'{"message":"Not Found Project"}'
    monkeypatch.setattr(updater.urllib.request, "urlopen", _raiser(_http_404(body)))

    st = updater.check_for_update()

    assert st.available is False
    assert "仓库" in st.error
    assert "暂无可用版本" not in st.error


def test_check_for_update_plain_404_means_no_release(monkeypatch) -> None:
    """仓库可见但还没有发行版 —— 维持原文案。"""
    body = b'{"message":"404 Not Found"}'
    monkeypatch.setattr(updater.urllib.request, "urlopen", _raiser(_http_404(body)))

    st = updater.check_for_update()

    assert st.available is False
    assert st.error == "暂无可用版本"
