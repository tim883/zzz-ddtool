"""守住「测试绝不弹真实窗口」这条线。

2026-10-04 跑 pytest 时真的建了桌面窗口，抢了正在操作的用户屏幕。
根因：防弹窗只靠各测试文件在模块顶层写 ``os.environ.setdefault(...)``，
``setdefault`` 只在变量不存在时才设，环境或 import 顺序一变就静默失效。

现在改由 ``tests/conftest.py`` 在收集任何测试之前用**赋值**强制钉成 offscreen。
这条守卫确保那个设置不会被误删、或改回会失效的写法。
"""

from __future__ import annotations

import os


def test_qt_platform_is_forced_offscreen() -> None:
    # 只有显式放行（要故意看真窗口）时才允许不是 offscreen
    if os.environ.get("ZZZ_DDTOOL_TEST_WINDOW") == "1":
        return
    assert os.environ.get("QT_QPA_PLATFORM") == "offscreen", (
        "测试进程的 Qt 平台必须是 offscreen，否则建出来的窗口会弹到桌面上抢用户屏幕；"
        "检查 tests/conftest.py 是否还在用赋值强制设置。"
    )
