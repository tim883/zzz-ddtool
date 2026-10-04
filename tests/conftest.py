"""pytest 全局配置：钉死 Qt 平台，防止测试把窗口弹到桌面上。

**必须在任何测试模块 import 之前**把 ``QT_QPA_PLATFORM`` 设成 ``offscreen``。
否则 PySide6 会退回真实 Windows 平台，测试里 new 出来的 ``QWidget`` 就会真的
建桌面窗口、抢用户焦点。

2026-10-04 踩过：跑 ``pytest`` 时真的弹了窗口，把正在动鼠标的用户屏幕抢了。
根因是防弹窗只靠各测试文件在模块顶层写
``os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")``——
``setdefault`` 只在变量不存在时才设，一旦环境里已有该变量、或 import 顺序让
``QApplication`` 先建出来，它就**静默失效**，Qt 退回真实平台。

这里在收集测试前用**赋值**强制设置，不依赖 import 顺序。
要故意跑真窗口（例如验浮层）时，设 ``ZZZ_DDTOOL_TEST_WINDOW=1`` 放行。
"""

from __future__ import annotations

import os

if os.environ.get("ZZZ_DDTOOL_TEST_WINDOW") != "1":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
