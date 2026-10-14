"""更新器安全边界：更新包不得改写用户的 user/ 与 log/、log_test/。

打包方失误（把跑过的包拿去打 zip）会让包里带上 top-level 的 user/、log/、log_test/，
xcopy 是覆盖式复制 —— 这层剥离是"更新绝不改写用户数据与日志"的硬保证。
"""

from __future__ import annotations

import json
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


def test_strip_protected_removes_only_top_level_user_log_and_log_test(tmp_path: Path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "layout.json").write_text("x", encoding="utf-8")
    (tmp_path / "log").mkdir()
    (tmp_path / "log" / "a.log").write_text("x", encoding="utf-8")
    (tmp_path / "log_test").mkdir()
    (tmp_path / "log_test" / "a.log").write_text("x", encoding="utf-8")
    (tmp_path / "data" / "user").mkdir(parents=True)  # 嵌套同名，必须保留
    (tmp_path / "zzz-ddtool.exe").write_text("x", encoding="utf-8")

    removed = updater._strip_protected(tmp_path)

    assert sorted(removed) == ["log", "log_test", "user"]
    assert not (tmp_path / "user").exists()
    assert not (tmp_path / "log").exists()
    assert not (tmp_path / "log_test").exists()
    assert (tmp_path / "data" / "user").exists()
    assert (tmp_path / "zzz-ddtool.exe").exists()


def test_apply_program_update_cannot_touch_user_or_log(tmp_path: Path, monkeypatch) -> None:
    """更新包内含 user/、log/、log_test/ 时，用户侧仍必须逐字节不变。"""
    app = tmp_path / "app"
    (app / "user").mkdir(parents=True)
    (app / "user" / "layout.json").write_text("MINE", encoding="utf-8")
    (app / "log" / "20261012-120000-000").mkdir(parents=True)
    (app / "log" / "20261012-120000-000" / "ddtool.log").write_text("MYLOG", encoding="utf-8")
    (app / "log_test" / "20261012-120000-000").mkdir(parents=True)
    (app / "log_test" / "20261012-120000-000" / "ddtool.log").write_text("MYTESTLOG", encoding="utf-8")

    zipsrc = tmp_path / "prog.zip"
    _make_zip(
        zipsrc,
        {
            "zzz-ddtool.exe": "NEW-EXE",
            "version.txt": "9.9.9",
            "user/layout.json": "SHIPPED",
            "log/20261012-120000-000/ddtool.log": "SHIPPED",
            "log_test/20261012-120000-000/ddtool.log": "SHIPPED",
        },
    )
    staged = tmp_path / "staged"
    staged.mkdir()

    monkeypatch.setattr(updater, "_download", lambda url, dest, progress=None: Path(dest).write_bytes(zipsrc.read_bytes()))
    monkeypatch.setattr(paths, "base_dir", lambda: app)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(staged))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit):
        updater.apply_program_update("http://example.invalid/x.zip")

    new_dir = staged / "new"
    assert not (new_dir / "user").exists()
    assert not (new_dir / "log").exists()
    assert not (new_dir / "log_test").exists()

    # 模拟替身 bat 的 xcopy（覆盖式复制），证明用户文件逐字节不变
    shutil.copytree(new_dir, app, dirs_exist_ok=True)
    assert (app / "user" / "layout.json").read_text(encoding="utf-8") == "MINE"
    assert (app / "log" / "20261012-120000-000" / "ddtool.log").read_text(encoding="utf-8") == "MYLOG"
    assert (
        app / "log_test" / "20261012-120000-000" / "ddtool.log"
    ).read_text(encoding="utf-8") == "MYTESTLOG"
    assert (app / "zzz-ddtool.exe").read_text(encoding="utf-8") == "NEW-EXE"


def test_apply_data_update_strips_protected_and_merges_into_data(tmp_path: Path, monkeypatch) -> None:
    """数据更新同样不得把 zip 里的 user/、log/、log_test/ 变成 data/user/ 等。"""
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
            "log/20261012-120000-000/ddtool.log": "SHIPPED",
            "log_test/20261012-120000-000/ddtool.log": "SHIPPED",
        },
    )
    workdir = tmp_path / "work"
    workdir.mkdir()

    monkeypatch.setattr(updater, "_download", lambda url, dest, progress=None: Path(dest).write_bytes(zipsrc.read_bytes()))
    monkeypatch.setattr(paths, "data_dir", lambda: data_root)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(workdir))

    updater.apply_data_update("http://example.invalid/data.zip")

    assert (data_root / "sets.json").read_text(encoding="utf-8") == "NEW"
    assert (data_root / "stats.json").read_text(encoding="utf-8") == "NEW"
    assert (data_root / "keep.json").read_text(encoding="utf-8") == "KEEP"  # 不删除 data/ 多余文件
    assert not (data_root / "user").exists()
    assert not (data_root / "log").exists()
    assert not (data_root / "log_test").exists()


def _http_404(body: bytes):
    import io
    import urllib.error

    return urllib.error.HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(body))


def _raiser(exc):
    def _raise(*args, **kwargs):
        raise exc

    return _raise


class _FakeResp:
    """urlopen 返回值的替身：支持 ``with ... as resp: resp.read()``。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _ok_json(body: bytes):
    return lambda *args, **kwargs: _FakeResp(body)


def test_check_for_update_distinguishes_invisible_repo(monkeypatch) -> None:
    """仓库不可见（不存在/未公开）不能伪装成"没有新版本"。

    GitHub 只在"仓库不存在/未公开"时 404；仓库可见但没有发行版由列表端点
    以 200 + [] 表达，所以 404 可以无条件判为仓库不可访问。
    """
    body = b'{"message":"Not Found","documentation_url":"https://docs.github.com/"}'
    monkeypatch.setattr(updater.urllib.request, "urlopen", _raiser(_http_404(body)))

    st = updater.check_for_update()

    assert st.available is False
    assert "仓库" in st.error
    assert "暂无可用版本" not in st.error


def test_check_for_update_empty_release_list_means_no_release(monkeypatch) -> None:
    """仓库可见但还没有任何发行版 —— 维持原文案。"""
    monkeypatch.setattr(updater.urllib.request, "urlopen", _ok_json(b"[]"))

    st = updater.check_for_update()

    assert st.available is False
    assert st.error == "暂无可用版本"


def test_check_for_update_skips_prerelease_and_splits_assets(monkeypatch) -> None:
    """预览版不算更新；data*.zip 是数据包，其余 zip 是程序包。"""
    payload = json.dumps(
        [
            {"tag_name": "v9.9.9", "prerelease": True, "body": "预览版"},
            {
                "tag_name": "v0.2.0",
                "body": "正式版",
                "assets": [
                    {"name": "data.zip", "browser_download_url": "http://x/data.zip"},
                    {"name": "zzz-ddtool-v0.2.0.zip", "browser_download_url": "http://x/prog.zip"},
                ],
            },
        ],
        ensure_ascii=False,
    ).encode("utf-8")
    monkeypatch.setattr(updater, "current_version", lambda: "0.1.0")
    monkeypatch.setattr(updater.urllib.request, "urlopen", _ok_json(payload))

    st = updater.check_for_update()

    assert st.latest_version == "v0.2.0"
    assert st.body == "正式版"
    assert st.data_url == "http://x/data.zip"
    assert st.program_url == "http://x/prog.zip"
    assert st.available is True


def test_log_stripped_reaches_the_text_log(tmp_path, monkeypatch) -> None:
    """Spec §4.2 要求把被剥离项写进日志。

    必须走 logutil 的 logger —— 只用 `logging.getLogger(__name__)` 不会触发
    logutil.setup()，windowed exe 里既没有 handler 也没有 stderr，这条警告会丢。
    日志落在当前轮次的 ddtool.log（本轮目录里），所以先开一轮测试上下文。
    """
    import logging

    from zzz_ddtool import loground, logutil

    loground.use_test_context("updater", root=tmp_path / "log_test")
    monkeypatch.setattr(logutil, "_configured", False)
    root = logging.getLogger("zzz_ddtool")
    for handler in list(root.handlers):
        root.removeHandler(handler)

    updater._log_stripped(["user", "log", "log_test"])

    log_path = loground.current().log_path
    assert log_path.is_file()
    text = log_path.read_text(encoding="utf-8")
    assert "user" in text
    assert "log" in text
    assert "log_test" in text
    loground.close_active()  # 收尾释放占用：不把活动轮次留给后面的用例


def test_update_bat_survives_non_ascii_install_path(tmp_path: Path, monkeypatch) -> None:
    """安装路径含非 ASCII 时，替身脚本必须完整写出，且路径不得写进脚本正文。

    2026-10-14 线上实测踩到的两种错法：

    - ``encoding="ascii"``：``Path.write_text`` 先建文件再编码，抛
      ``UnicodeEncodeError`` 并留下 **0 字节** 的 ``_update.bat`` —— 更新静默失败，
      界面只在状态栏给一行字，用户看着就是"卡住了"。
    - ``encoding="mbcs"``：写得出来，但 cmd.exe 按自己的代码页读脚本，
      xcopy 一条都复制不到（同日在全中文路径下实测）。

    所以正文保持纯 ASCII，两个路径用 ``%~1``/``%~2`` 由命令行传入 —— 路径经
    ``CreateProcessW`` 以 Unicode 传递，与代码页无关。
    """
    app = tmp_path / "更新测试" / "zzz-ddtool"  # 中文安装路径，就是触发条件
    app.mkdir(parents=True)

    zipsrc = tmp_path / "prog.zip"
    _make_zip(zipsrc, {"zzz-ddtool.exe": "NEW-EXE", "version.txt": "1.0.1"})
    staged = tmp_path / "staged"
    staged.mkdir()

    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["args"] = list(args)
        return None

    monkeypatch.setattr(updater, "_download", lambda url, dest, progress=None: Path(dest).write_bytes(zipsrc.read_bytes()))
    monkeypatch.setattr(paths, "base_dir", lambda: app)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(staged))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(SystemExit):
        updater.apply_program_update("http://example.invalid/x.zip")

    bat = app / "_update.bat"
    assert bat.is_file()
    assert bat.stat().st_size > 0, "0 字节的替身脚本 = 更新静默失败"

    text = bat.read_text(encoding="ascii")  # 纯 ASCII：这一步本身也是断言
    assert "%~1" in text and "%~2" in text
    assert "更新测试" not in text, "路径不得写进脚本正文（cmd 会按代码页误读）"
    # 两个路径必须作为参数传出，而不是拼进脚本
    assert captured["args"][:3] == ["cmd", "/c", str(bat)]
    assert captured["args"][3] == str(staged / "new")
    assert captured["args"][4] == str(app)


class _FakeStreamResp:
    """urlopen 替身：支持分块 ``read(size)`` 与 ``headers``，用来测下载进度。"""

    def __init__(self, chunks: list[bytes], total: int | None) -> None:
        self._chunks = list(chunks)
        self.headers = {} if total is None else {"Content-Length": str(total)}

    def read(self, size: int = -1) -> bytes:  # noqa: ARG002 - 替身忽略块大小
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self) -> "_FakeStreamResp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_download_reports_progress_with_content_length(tmp_path: Path, monkeypatch) -> None:
    """分块下载要逐块回调进度，总量取 Content-Length。

    程序包约 140 MB，慢网络要十几分钟；没有这个回调，界面只能显示一句
    "正在下载…"，用户无法区分"慢"和"卡死"。
    """
    chunks = [b"a" * 1000, b"b" * 1000, b"c" * 500]
    total = sum(len(c) for c in chunks)
    seen: list[tuple[int, int | None]] = []

    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *a, **kw: _FakeStreamResp(chunks, total)
    )
    dest = tmp_path / "out.bin"
    updater._download("http://example.invalid/x", dest, progress=lambda d, t: seen.append((d, t)))

    assert dest.read_bytes() == b"".join(chunks)
    assert [d for d, _ in seen] == [1000, 2000, 2500], "进度必须单调递增到总量"
    assert all(t == total for _, t in seen)


def test_download_progress_tolerates_unknown_total(tmp_path: Path, monkeypatch) -> None:
    """服务端不给 Content-Length 时总量传 None：不能崩，也要照常落盘。"""
    chunks = [b"x" * 10, b"y" * 20]
    seen: list[tuple[int, int | None]] = []

    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *a, **kw: _FakeStreamResp(chunks, None)
    )
    dest = tmp_path / "o.bin"
    updater._download("http://example.invalid/x", dest, progress=lambda d, t: seen.append((d, t)))

    assert dest.read_bytes() == b"".join(chunks)
    assert seen == [(10, None), (30, None)]


def test_download_without_progress_callback_still_works(tmp_path: Path, monkeypatch) -> None:
    """不传 progress 时照旧可用（多数调用点不需要进度）。"""
    chunks = [b"z" * 7]
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *a, **kw: _FakeStreamResp(chunks, 7)
    )
    dest = tmp_path / "z.bin"
    updater._download("http://example.invalid/x", dest)

    assert dest.read_bytes() == b"z" * 7
