"""守住「测试绝不弹真实窗口」这条线。

2026-10-04 跑 pytest 时真的建了桌面窗口，抢了正在操作的用户屏幕。
根因：防弹窗只靠各测试文件在模块顶层写 ``os.environ.setdefault(...)``，
``setdefault`` 只在变量不存在时才设，环境或 import 顺序一变就静默失效。

现在改由 ``tests/conftest.py`` 在收集任何测试之前用**赋值**强制钉成 offscreen。
这条守卫确保那个设置不会被误删、或改回会失效的写法。
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

CONFTEST = Path(__file__).with_name("conftest.py")


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
