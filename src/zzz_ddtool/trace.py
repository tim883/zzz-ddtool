"""步骤跟踪：让日志说人话，并给每一步配一张图。

一句话说明白它干什么：
    每一条日志都是「在哪个页面 · 干了什么 → 结果」，并且每一步都留一张图。

为什么一步一扫：
    这是 OCR + 模拟点击工具。不 OCR 就不知道现在在哪个页面、按钮在不在、
    坐标有没有偏。截图是唯一能事后核对"当时屏幕长什么样"的东西。
    一步一扫是基础，不是可选的高级功能。

日志示例：
    15:42:01 [信息] 驱动盘仓库 · 扫描列表 → 成功（第 1 页 9 个盘）  截图=0003-扫描列表.png
    15:42:02 [信息] 详情页 · 识别盘面 → 成功（第 3 个/共 9 个 · 第2行第3列 · 等级3）  截图=0008-识别盘面.png
    15:42:02 [信息] 详情页 · 判断     → 结论（继续强化：分数4.0≥要求3.5）
    15:42:03 [错误] 详情页 · 进强化页 → 失败：没找到「查看」按钮  截图=0011-进强化页.png

截图从哪来（不重复抓、不重复 OCR）：
    - 这一步做了 OCR：调用方把那张图传进来（image=xxx），直接存盘。
      抓图和推理的开销已经付了，写盘只多几毫秒。
    - 这一步没做 OCR（点格子、按 R、Esc、滚轮）：现截一张。
      这类步骤正是坐标可能偏的地方，图是核对坐标的唯一依据。

容量怎么控（截图 1.5MB 一张，不控会撑爆磁盘）：
    - 路径类步骤（导航/筛选/扫描/翻页）：全存，数量少且最常出问题
    - 盘的处理：只留最初 KEEP_FIRST_DISCS 个 + 最近 KEEP_LAST_DISCS 个
    - 失败/告警：永远保留，不被任何清理删掉
    - 一轮一个目录，只留最近 KEEP_ROUNDS 轮
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from zzz_ddtool.capture import grab_game_image
from zzz_ddtool.logutil import get_logger
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

# 路径类步骤：数量少、最容易出岔子，一律全存
PATH_ACTIONS = frozenset({"navigate", "filter", "scan", "scroll"})

KEEP_FIRST_DISCS = 5  # 前几个盘全留：刚启动时的异常最多
KEEP_LAST_DISCS = 5  # 最近几个盘全留：跑了一阵才跑偏的情况
KEEP_ROUNDS = 3  # 只留最近几轮


def page_name(page: str) -> str:
    """把页面标识翻成人话；认不出来就原样返回，别编。"""
    return PAGE_NAMES.get(page, page)


def action_name(action: str) -> str:
    return ACTION_NAMES.get(action, action)


class Trace:
    """一轮运行的步骤跟踪：每步一条日志 + 一张图，容量按策略回收。"""

    def __init__(self, tag: str = "") -> None:
        self.tag = tag or time.strftime("%Y%m%d-%H%M%S")
        self._step = 0
        self._dir: Path | None = None
        self.failed = False
        self._disc_no = 0  # 当前在处理第几个盘（0=还没进盘）
        self._shots: list[tuple[Path, int, bool]] = []  # (路径, 盘号, 是否必留)

    @property
    def dir(self) -> Path:
        if self._dir is None:
            self._dir = captures_dir() / self.tag
            self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    @property
    def step(self) -> int:
        return self._step

    def new_disc(self) -> None:
        """开始处理一个新盘。盘号决定这张图最后留不留。"""
        self._disc_no += 1

    def _make_name(self, action: str) -> str:
        safe = "".join(c if c not in '\\/:*?"<>| ' else "_" for c in action_name(action))
        return f"{self._step:04d}-{safe}.png"

    def step_start(self, page: str, action: str) -> int:
        """开始一步，返回它的步骤号（用于把后续日志挂到同一步）。"""
        self._step += 1
        get_logger("zzz_ddtool.trace").debug(
            "[%04d] %s · %s", self._step, page_name(page), action_name(action)
        )
        return self._step

    def ok(self, page: str, action: str, detail: str = "", image=None) -> None:
        """一步成功。

        image：这一步刚 OCR 用的那张图。传了就存盘（开销已付）；
        没传说明这步没 OCR，现截一张——坐标偏移就靠这类图核对。
        """
        self.step_start(page, action)
        # 判断是纯计算，不碰屏幕，截图跟上一张一模一样，不截
        if action == "decide":
            message = f"{page_name(page)} · {action_name(action)} → 结论"
            if detail:
                message += f"（{detail}）"
            get_logger("zzz_ddtool.trace").info(message)
            return
        message = f"{page_name(page)} · {action_name(action)} → 成功"
        if detail:
            message += f"（{detail}）"
        name = self._persist(action, image)
        if name:
            message += f"  截图={name}"
        get_logger("zzz_ddtool.trace").info(message)

    def fail(self, page: str, action: str, reason: str = "", image=None) -> None:
        """一步失败。图必留，永远不被容量清理删掉。"""
        self.step_start(page, action)
        self.failed = True
        message = f"{page_name(page)} · {action_name(action)} → 失败"
        if reason:
            message += f"：{reason}"
        name = self._persist(action, image, keep=True)
        if name:
            message += f"  截图={name}"
        else:
            message += "  （截图没成功，看上一条警告）"
        get_logger("zzz_ddtool.trace").error(message)

    def warn(self, page: str, action: str, reason: str = "", image=None) -> None:
        """可疑但没中断。图必留。"""
        self.step_start(page, action)
        message = f"{page_name(page)} · {action_name(action)} → 注意"
        if reason:
            message += f"：{reason}"
        name = self._persist(action, image, keep=True)
        if name:
            message += f"  截图={name}"
        get_logger("zzz_ddtool.trace").warning(message)

    def note(self, message: str) -> None:
        """补充说明，不推进步骤号、不截图。"""
        get_logger("zzz_ddtool.trace").info("    · %s", message)

    def _persist(self, action: str, image, keep: bool = False) -> str | None:
        """存一步的图。image 为空则现截一张（这步没做 OCR）。"""
        if image is None:
            try:
                image, _method, _hwnd = grab_game_image()
            except Exception as exc:
                get_logger("zzz_ddtool.trace").warning("截图失败：%s", exc)
                return None
        name = self._make_name(action)
        target = self.dir / name
        try:
            if not image.save(str(target), "PNG"):
                return None
        except Exception as exc:
            get_logger("zzz_ddtool.trace").warning("存图失败：%s", exc)
            return None
        # 路径类步骤和失败/告警：必留
        self._shots.append((target, self._disc_no, keep or action in PATH_ACTIONS))
        return f"{self.tag}/{name}"

    def finish(self) -> None:
        """收尾：按容量策略清理，再回收旧轮次。"""
        log = get_logger("zzz_ddtool.trace")
        removed = self._prune()
        log.info(
            "本轮结束：共 %d 步，处理 %d 个盘，%s，截图在 captures/%s",
            self._step,
            self._disc_no,
            ("有失败" if self.failed else "无失败"),
            self.tag,
        )
        if removed:
            log.info(
                "按容量策略清理了 %d 张（只留最初 %d 个 + 最近 %d 个盘）",
                removed,
                KEEP_FIRST_DISCS,
                KEEP_LAST_DISCS,
            )
        self._prune_rounds()

    def _prune(self) -> int:
        """删掉中间那些盘的图：只留最初 N 个 + 最近 N 个，必留的不动。"""
        if not self._shots:
            return 0
        disc_nos = sorted({no for _p, no, _k in self._shots if no > 0})
        keep_nos = set(disc_nos[:KEEP_FIRST_DISCS]) | set(disc_nos[-KEEP_LAST_DISCS:])
        removed = 0
        for path, disc_no, keep in self._shots:
            if keep or disc_no == 0 or disc_no in keep_nos:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def _prune_rounds(self) -> None:
        """只留最近 KEEP_ROUNDS 轮，更早的整目录删掉。"""
        root = captures_dir()
        if not root.exists():
            return
        rounds = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
        for old in rounds[:-KEEP_ROUNDS]:
            try:
                for f in old.glob("*.png"):
                    f.unlink()
                old.rmdir()
            except OSError as exc:
                get_logger("zzz_ddtool.trace").warning("清理旧轮次失败：%s", exc)
