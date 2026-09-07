"""步骤跟踪：让日志说人话，并在需要时自动配图。

一句话说明白它干什么：
    每一条日志都是「在哪个页面 · 干了什么 → 结果」。
    截图由日志级别决定：DEBUG=一步一截，WARNING/ERROR=出问题时截。

日志示例：
    15:42:01 [信息] 仓库页   · 点开 第2行第3列 → 成功
    15:42:02 [信息] 详情页   · 识别盘面 → 等级3 副词条4条
    15:42:02 [信息] 详情页   · 判断 → 继续强化（分数4.0≥要求3.5）
    15:42:03 [错误] 详情页   · 进强化页 → 失败：没找到「查看」按钮  截图=0007-进强化页.png
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from zzz_ddtool.capture import grab_game_image
from zzz_ddtool.logutil import get_logger, level as current_level
from zzz_ddtool.paths import captures_dir

# 页面：程序内部标识 → 日志里给人看的中文
PAGE_NAMES = {
    "menu": "功能页",
    "overworld": "大地图",
    "storage": "音擎仓库",
    "drive_disc": "驱动盘仓库",
    "detail": "详情页",
    "zoom": "缩放页",
    "upgrade": "强化页",
    "unknown": "未知页",
}

# 动作：关键字 → 中文。日志里统一用这些词，避免中英混杂。
ACTION_NAMES = {
    "run": "开始任务",
    "navigate": "导航到仓库",
    "filter": "设置游戏筛选",
    "scan": "扫描列表",
    "click_cell": "点开格子",
    "parse": "识别盘面",
    "decide": "判断",
    "upgrade": "强化",
    "discard": "打弃置标记",
    "skip": "跳过",
    "esc": "返回",
    "enter_upgrade": "进强化页",
    "scroll": "翻页",
}


def page_name(page: str) -> str:
    """把页面标识翻成人话；认不出来就原样返回，别编。"""
    return PAGE_NAMES.get(page, page)


def action_name(action: str) -> str:
    return ACTION_NAMES.get(action, action)


class Trace:
    """一轮运行的步骤跟踪。"""

    def __init__(self, tag: str = "") -> None:
        self.tag = tag or time.strftime("%Y%m%d-%H%M%S")
        self._step = 0
        self._dir: Path | None = None
        self.failed = False

    @property
    def dir(self) -> Path:
        if self._dir is None:
            self._dir = captures_dir() / self.tag
            self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    @property
    def step(self) -> int:
        return self._step

    def step_start(self, page: str, action: str) -> int:
        """开始一步，返回它的步骤号（用于把后续日志挂到同一步）。"""
        self._step += 1
        get_logger("zzz_ddtool.trace").debug(
            "[%04d] %s · %s", self._step, page_name(page), action_name(action)
        )
        return self._step

    def ok(self, page: str, action: str, detail: str = "") -> None:
        """一步成功。DEBUG 级别下会截图。"""
        self.step_start(page, action)
        # 判断类动作说「结论」比说「成功」通顺
        result = "结论" if action == "decide" else "成功"
        message = f"{page_name(page)} · {action_name(action)} → {result}"
        if detail:
            message += f"（{detail}）"
        self._shoot_if_debug(action)
        get_logger("zzz_ddtool.trace").info(message)

    def fail(self, page: str, action: str, reason: str = "") -> None:
        """一步失败。必定截图。"""
        self.step_start(page, action)
        self.failed = True
        message = f"{page_name(page)} · {action_name(action)} → 失败"
        if reason:
            message += f"：{reason}"
        name = self._shoot(action)
        if name:
            message += f"  截图={name}"
        else:
            message += "  （截图没成功，看上一条警告）"
        get_logger("zzz_ddtool.trace").error(message)

    def warn(self, page: str, action: str, reason: str = "", shoot: bool = True) -> None:
        """可疑但没中断。WARNING 及以上才出现，默认截图。"""
        self.step_start(page, action)
        message = f"{page_name(page)} · {action_name(action)} → 注意"
        if reason:
            message += f"：{reason}"
        if shoot:
            name = self._shoot(action)
            if name:
                message += f"  截图={name}"
        get_logger("zzz_ddtool.trace").warning(message)

    def note(self, message: str) -> None:
        """补充说明，不推进步骤号。"""
        get_logger("zzz_ddtool.trace").info("    · %s", message)

    def _shoot_if_debug(self, action: str) -> str | None:
        """DEBUG 级别 = 一步一截。"""
        if current_level() > logging.DEBUG:
            return None
        return self._shoot(action)

    def _shoot(self, action: str) -> str | None:
        try:
            image, _method, _hwnd = grab_game_image()
        except Exception as exc:
            get_logger("zzz_ddtool.trace").warning("截图失败：%s", exc)
            return None
        safe = "".join(c if c not in '\\/:*?"<>| ' else "_" for c in action_name(action))
        name = f"{self._step:04d}-{safe}.png"
        dest = self.dir / name
        try:
            if not image.save(str(dest), "PNG"):
                return None
        except Exception as exc:
            get_logger("zzz_ddtool.trace").warning("存图失败：%s", exc)
            return None
        return f"{self.tag}/{name}"

    def finish(self) -> None:
        """收尾：DEBUG 保留全部，否则只留失败那次的图。"""
        log = get_logger("zzz_ddtool.trace")
        if self.failed:
            log.error("本轮结束：有失败，截图保留在 captures/%s（共 %d 步）", self.tag, self._step)
            return
        if current_level() <= logging.DEBUG:
            log.info("本轮结束：调试模式，截图全部保留在 captures/%s（共 %d 步）", self.tag, self._step)
            return
        if self._dir is not None and self._dir.exists():
            try:
                for path in self._dir.glob("*.png"):
                    path.unlink()
                self._dir.rmdir()
            except OSError as exc:
                log.warning("清理本轮截图失败：%s", exc)
        log.info("本轮结束：无失败，共 %d 步", self._step)
