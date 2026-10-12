"""守住「测试绝不弹真实窗口」这条线。

2026-10-04 跑 pytest 时真的建了桌面窗口，抢了正在操作的用户屏幕。
根因：防弹窗只靠各测试文件在模块顶层写 ``os.environ.setdefault(...)``，
``setdefault`` 只在变量不存在时才设，环境或 import 顺序一变就静默失效。

现在改由 ``tests/conftest.py`` 在收集任何测试之前用**赋值**强制钉成 offscreen。
这条守卫确保那个设置不会被误删、或改回会失效的写法。

同一份 conftest 还负责日志隔离：测试的日志、截图、统计、性能、故障取证
只落 ``log_test/<时间戳>/``，不碰真实 ``log/`` 和 ``user/log-settings.json``。
下面用哨兵比对证明那条隔离没失效——不能靠「没报错」通过。
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

CONFTEST = Path(__file__).with_name("conftest.py")

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_LOG = REPO_ROOT / "log"
REAL_SETTINGS = REPO_ROOT / "user" / "log-settings.json"


def _load_conftest(platform: str | None, allow_window: bool = False) -> str | None:
    env = os.environ.copy()
    env.pop("QT_QPA_PLATFORM", None)
    env.pop("ZZZ_DDTOOL_TEST_WINDOW", None)
    if platform is not None:
        env["QT_QPA_PLATFORM"] = platform
    if allow_window:
        env["ZZZ_DDTOOL_TEST_WINDOW"] = "1"
    # 子进程只执行环境守卫，不导入 Qt、不创建窗口。
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import json, os, runpy, sys; runpy.run_path(sys.argv[1]); print(json.dumps(os.environ.get('QT_QPA_PLATFORM')))",
         str(CONFTEST)],
        env=env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("platform", [None, "windows", "other"])
def test_qt_platform_is_forced_offscreen(platform) -> None:
    assert _load_conftest(platform) == "offscreen"


def test_explicit_window_override_preserves_requested_platform() -> None:
    assert _load_conftest("windows", allow_window=True) == "windows"


# --------------------------------------------------------------------------
# 日志隔离守卫
# --------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, int]:
    """目录下每个文件的相对路径与大小；目录不存在就当空清单。"""
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _read_settings() -> bytes | None:
    return REAL_SETTINGS.read_bytes() if REAL_SETTINGS.is_file() else None


def test_sandbox_keeps_real_logs_and_settings_untouched() -> None:
    """在「生产模式」下写日志/截图/统计/性能/取证并跑一次回收，真实产出一律不变。"""
    from zzz_ddtool import loground, logutil, paths, retain, scanperf
    from zzz_ddtool.stats import RunStats

    before_log = _snapshot(REAL_LOG)
    before_settings = _read_settings()

    round_ = loground.use_production_context("sentinel")
    written: list[Path] = [round_.root, round_.log_path]
    try:
        logutil.get_logger("zzz_ddtool.isolation").info("隔离守卫：这条不能进真实 log/")

        round_.captures_dir.mkdir(parents=True, exist_ok=True)
        shot = round_.captures_dir / "0001-守.png"
        shot.write_bytes(b"png")
        written.append(shot)

        assert RunStats(tag=round_.id).write(round_.stats_path) is not None
        written.append(round_.stats_path)

        scanperf.begin_session(round_.id, path=round_.perf_path)
        with scanperf.timed("capture", details={"method": None, "hwnd": None}):
            pass
        scanperf.end_session()
        written.append(round_.perf_path)

        round_.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        evidence = round_.diagnostics_dir / "故障取证.txt"
        evidence.write_text("x", encoding="utf-8")
        written.append(evidence)

        assert retain.retain_rounds() == []  # 沙箱里还没有历史轮次可收
        assert retain.clear_history() == 0
        assert round_.root.is_dir()  # 回收没碰正在写入的这一轮
    finally:
        loground.close_active()

    # 1) 写操作真的发生了，而且只落在沙箱里（别被「隔离把写吞掉」骗过）
    sandbox = paths.test_log_root()
    assert REAL_LOG != sandbox and sandbox not in REAL_LOG.parents
    assert all(path == sandbox or sandbox in path.parents for path in written)
    assert round_.registry_path.is_file()
    assert round_.log_path.is_file()
    assert "隔离守卫" in round_.log_path.read_text(encoding="utf-8")
    for path in (shot, round_.stats_path, round_.perf_path, evidence):
        assert path.is_file(), path

    # 2) 真实生产日志与用户日志设置一个字节都没变
    assert _snapshot(REAL_LOG) == before_log
    assert _read_settings() == before_settings
