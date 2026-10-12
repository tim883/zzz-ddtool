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
        按**轮数**整轮回收——一轮的日志、报告、统计、性能、截图一起删。
        不做「失败永远保留」——排错靠 debug 重跑复现，不靠翻旧图回溯，
        留着失败图只会把正常的挤掉。见 retain.py 与 loground.py。

耗时怎么量（给运行统计用）：
    Trace 的接口是「干完了再记一笔」，没有显式的 begin，所以单步耗时用
    **相邻两步的时间差**：这一步的日志刚写完，上一步的工作 + 这一步的工作都在
    这段里。步骤之间没有空闲等待，这个差值就是这一步的开销（误差只来自上一步
    存盘 PNG 的几十毫秒，会算到下一步头上）。
    收集到的 StepSpan 由 Runner 在收尾时并进 RunStats。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from zzz_ddtool import loground, scanperf
from zzz_ddtool.capture import grab_game_image
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.stats import StepSpan

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
    # 注意：「布局复核」不是滚轮标定。标定（navigation.calibrate）只在开局跑一次，
    # 而且不写 trace；每个任务写的是「拿本轮实测布局复核原点」。以前这个词写成
    # calibrate，日志里看着像每个任务都在标定，误导过审计。
    "layout_check": "布局复核",
    "layout": "网格布局",
    # 整屏可见的快速路径：确认整屏（筛选后）、原地复核（强化后）、回退置顶恢复。
    "single_screen": "整屏确认",
    "single_screen_recheck": "强化后原地复核",
    "recovery_cache": "复用未处理候选",
    "scan_cache": "复用列表候选",
    "restore_fallback": "回退置顶恢复",
}

def page_name(page: str) -> str:
    """把页面标识翻成人话；认不出来就原样返回，别编。"""
    return PAGE_NAMES.get(page, page)


def action_name(action: str) -> str:
    return ACTION_NAMES.get(action, action)


class Trace:
    """一轮运行的步骤跟踪：每步一条日志 + 一张图，图存本轮 ``captures/``。"""

    def __init__(self, tag: str = "", *, dir: Path | None = None) -> None:
        if tag:
            self.tag = tag
        else:
            round_ = loground.current()
            # 轮次编号就是一轮的 tag：日志、统计、性能用的是同一个身份。
            self.tag = round_.id if round_ is not None else time.strftime("%Y%m%d-%H%M%S")
        self._step = 0
        self._dir: Path | None = Path(dir) if dir is not None else None
        self.failed = False
        self._disc_no = 0  # 当前在处理第几个盘（0=还没进盘）
        self._shots: list[Path] = []  # 本轮存过的图，收尾时用来算数
        # 步骤耗时：见模块头「耗时怎么量」。
        self.spans: list[StepSpan] = []
        self._last_step_at = time.monotonic()

    @property
    def dir(self) -> Path:
        if self._dir is None:
            self._dir = loground.captures_dir()
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
        now = time.monotonic()
        self.spans.append(
            StepSpan(
                page=page_name(page),
                action=action_name(action),
                detail="",
                seconds=now - self._last_step_at,
            )
        )
        self._last_step_at = now
        get_logger("zzz_ddtool.trace").debug(
            "[%04d] %s · %s", self._step, page_name(page), action_name(action)
        )
        return self._step

    def _close_span(self, detail: str = "") -> None:
        """补上这一步的说明（耗时在 step_start 时已记）。"""
        if self.spans:
            self.spans[-1].detail = detail

    def ok(self, page: str, action: str, detail: str = "", image=None) -> None:
        """一步成功。

        image：这一步刚 OCR 用的那张图。传了就存盘（开销已付）；
        没传说明这步没 OCR，现截一张——坐标偏移就靠这类图核对。
        """
        self.step_start(page, action)
        self._close_span(detail)
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
        self._close_span(reason)
        self.failed = True
        message = f"{page_name(page)} · {action_name(action)} → 失败"
        if reason:
            message += f"：{reason}"
        name = self._persist(action, image)
        if name:
            message += f"  截图={name}"
        else:
            message += "  （截图没成功，看上一条警告）"
        get_logger("zzz_ddtool.trace").error(message)

    def warn(self, page: str, action: str, reason: str = "", image=None) -> None:
        """可疑但没中断。图必留。"""
        self.step_start(page, action)
        self._close_span(reason)
        message = f"{page_name(page)} · {action_name(action)} → 注意"
        if reason:
            message += f"：{reason}"
        name = self._persist(action, image)
        if name:
            message += f"  截图={name}"
        get_logger("zzz_ddtool.trace").warning(message)

    def note(self, message: str) -> None:
        """补充说明，不推进步骤号、不截图。"""
        get_logger("zzz_ddtool.trace").info("    · %s", message)

    def _persist(self, action: str, image) -> str | None:
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
            # 性能记录：只包裹这一次 image.save（§7.1）。image 为空时上面已另截一张，
            # 那次 capture 由 grab_game_image 单独记，不并入这次 png_save。
            with scanperf.timed(
                "png_save",
                details={"source": "trace", "path": str(target), "saved": False},
            ) as span:
                saved = image.save(str(target), "PNG")
                span.set(saved=bool(saved))
            if not saved:
                return None
        except Exception as exc:
            get_logger("zzz_ddtool.trace").warning("存图失败：%s", exc)
            return None
        self._shots.append(target)
        return name

    def finish(self) -> None:
        """收尾：把本轮收成一条摘要。

        回收不在这里做——按轮数清理只由正式入口触发（见 retain.py），
        免得测试或开发脚本反过来清掉生产日志。
        """
        log = get_logger("zzz_ddtool.trace")
        round_ = loground.current()
        where = str(round_.root) if round_ is not None else str(self.dir)
        log.info(
            "本轮结束：共 %d 步，处理 %d 个盘，%s，本轮 %d 张图，日志在 %s",
            self._step,
            self._disc_no,
            ("有失败" if self.failed else "无失败"),
            len(self._shots),
            where,
        )
