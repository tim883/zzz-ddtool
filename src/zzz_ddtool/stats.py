"""运行统计：把「这一轮都干了什么、花了多少时间」记下来。

要回答的问题：
    - 筛选后有多少格位？总计 / 跳过 / 实际处理各多少？
    - 每件盘升到几级？有几件满级保留？几件弃置？
    - 每个动作耗时多少（合计 / 平均 / 最快 / 最慢）？
    - 切页置顶（切换窗口）花了多少？
    - 升到 3 / 6 / 9 / 15 各档分别花了多少？
    - 耗时浮动大不大、稳不稳？

两类耗时，**分开统计，不混在一张表**：

- **步骤耗时**（``kind="step"``）：由 ``trace.Trace`` 每步记一条。口径是「距上一步
  的间隔」，所以一个没打步骤的长动作会把时间算到它后面那一步头上。粒度细，用来看
  「哪一步慢」。
- **长动作耗时**（``kind="span"``）：由 ``Runner`` 自己用 ``timed()`` 包住整段，
  口径是真实墙钟。用来看「切页、筛选、恢复视口这些大块到底吞了多少」。

两者会重叠（筛选那段既是 1 个长动作，也会被记进相邻的步骤），所以**别相加**。

格位计数按**任务**分开算：每个任务有自己的筛选条件和独立序号空间，
跨任务求和只会得到没有意义的数字。

产物三处，都在本轮目录里（收尾时一次性写出）：
    <轮次>/ddtool.log            一段中文摘要（给人看的，可 grep）
    <轮次>/stats.json            结构化数据（给脚本/后续报表用）
    <轮次>/report.md             执行结果总结；客户端的结果统计界面展示的就是它

轮次目录由 ``loground`` 管理，按轮数整轮回收：日志、报告、统计、性能、截图
一起删，统计文件不会落单。
"""

from __future__ import annotations

import json
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 结果分类

OUTCOME_UPGRADED = "upgraded"  # 升过级（含升满 / 到升阶上限）
OUTCOME_DISCARDED = "discarded"  # 打上弃置标记
OUTCOME_SKIPPED = "skipped"  # 不满足条件，跳过
OUTCOME_FAILED = "failed"  # 处理中断（素材不足等）

OUTCOME_NAMES = {
    OUTCOME_UPGRADED: "强化",
    OUTCOME_DISCARDED: "弃置",
    OUTCOME_SKIPPED: "跳过",
    OUTCOME_FAILED: "中断",
}

# Action.value → 结果分类。decision 的动作词就这四个（models.Action）。
OUTCOME_BY_ACTION = {
    "upgrade": OUTCOME_UPGRADED,
    "mark_discard": OUTCOME_DISCARDED,
    "skip": OUTCOME_SKIPPED,
}


def outcome_for_action(action_value: str) -> str:
    """把 decision.action.value 换成统计用的结果词。认不出来按「跳过」记（最保守）。"""
    return OUTCOME_BY_ACTION.get(action_value, OUTCOME_SKIPPED)


def outcome_name(outcome: str) -> str:
    return OUTCOME_NAMES.get(outcome, outcome)


# 驱动盘的升级档位就是这 5 个（models.next_stage_level / UPGRADE_STEP=3 / MAX_LEVEL=15）。
# 报告里**固定列出全部 5 档**，某档没数据也留一行写 0 —— 否则读的人会以为漏了一档。
STAGE_LEVELS = (3, 6, 9, 12, 15)

# 结果口径的固定说法，摘要行和详细表都引用它，避免两处写岔。
KEPT_NOTE = "升满 15 级且判定保留"
DISCARD_NOTE = "判定为弃置的盘；不等于实际打过弃置标记的数量"


def format_duration(seconds: float) -> str:
    """秒 → 人读的时长。"""
    total = int(round(max(0.0, float(seconds))))
    if total < 60:
        return f"{total} 秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


def _cell(value: float) -> str:
    """表格里的秒数：保留 1 位小数。"""
    return f"{float(value):.1f}"


# ---------------------------------------------------------------- 明细行

STEP = "step"
SPAN = "span"


@dataclass
class StepSpan:
    """一段耗时。page/action 是中文，和日志里一模一样。

    kind: ``step`` = Trace 的单步（距上一步的间隔）；``span`` = Runner 包住的整段。
    """

    page: str
    action: str
    detail: str
    seconds: float
    kind: str = STEP


@dataclass
class StageSpan:
    """一次「+3」的耗时：从某等级升到下一档。

    from_level 是升级前的等级，to_level 是实际到达的等级（可能不足一档）。
    """

    from_level: int
    to_level: int
    seconds: float


@dataclass
class DiscStat:
    """一件盘的处理记录。"""

    job: str
    job_name: str
    index: int
    level_start: int
    # 套装名 / 部位号从所属任务（job）拿：每个任务按固定号位 + 套装过滤，
    # 成功盘子归属到某个任务，所以其套装/部位就是任务过滤值。
    slot: int = 0
    sets: tuple[str, ...] = ()
    line_count: int = 0  # 副词条条数（词条数）
    # 升满成功盘的截图相对路径（相对本轮的 report.md，如 "success/disc12.png"）。
    # 由 runner 在判定成功时存好；截不到就留空，报告里不贴图。
    image: str = ""
    level_end: int = 0
    outcome: str = ""
    score: float = 0.0
    seconds: float = 0.0
    stages: list[StageSpan] = field(default_factory=list)


@dataclass
class JobStat:
    """一个任务的用量与格位计数。

    slots 取游戏标题栏读到的**筛选后总盘数**（含满级、锁定、弃置）；读不到时退回
    「见过的最大全局序号 + 1」这个下界——它只说明扫到哪，不等于筛选结果有多少个盘。
    跳过去重（同一格位在相邻批次里会重复出现，用 set 收）。
    """

    index: int
    slot: int
    sets: tuple[str, ...]
    mainstats: tuple[str, ...]
    seconds: float = 0.0
    processed: int = 0
    batches: int = 0
    slots: int = 0
    deferred: int = 0
    skip_indices: set[int] = field(default_factory=set)

    @property
    def skipped(self) -> int:
        return len(self.skip_indices)


def unprocessed_of(job: JobStat) -> int:
    """没被点开的盘数：筛选后总数 − 扫到并跳过的 − 已处理。负数按 0 收。

    额度用满、页面到顶或用户中途停止都会留下这个差值；它包含没滚到的尾部，
    不是「确定没被处理」的精确清单。
    """
    return max(0, int(job.slots) - int(job.skipped) - int(job.processed))


class RunStats:
    """一轮运行的统计收集器。只收集，聚合放到 ``summary()``。"""

    def __init__(self, tag: str = "") -> None:
        self.tag = tag or time.strftime("%Y%m%d-%H%M%S")
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.seconds = 0.0
        self.jobs: list[JobStat] = []
        self.discs: list[DiscStat] = []
        self.actions: list[StepSpan] = []
        # 实际打上弃置标记的次数。和「判定为弃置」不是一个数：盘上已有标记、或标签
        # 设置关闭时都不会真打，所以它 ≤ 判定弃置数。报告里两个都报，差异才解释得清。
        self.discard_labels = 0
        self._open: dict[str, float] = {}

    # ------------------------------------------------------------ 打点接口

    def job_begin(self, index: int, slot: int, sets, mainstats) -> None:
        self.jobs.append(
            JobStat(
                index=index,
                slot=int(slot),
                sets=tuple(str(s) for s in (sets or ())),
                mainstats=tuple(str(s) for s in (mainstats or ())),
            )
        )

    def job_end(self, seconds: float, processed: int = 0) -> None:
        if self.jobs:
            self.jobs[-1].seconds = float(seconds)
            self.jobs[-1].processed = int(processed)

    @property
    def _job(self) -> JobStat | None:
        return self.jobs[-1] if self.jobs else None

    def scan(self, cells, skips, deferred: int, max_index: int | None = None,
             total: int | None = None) -> None:
        """记一批扫描结果。

        cells：本批可见格位数（跨批会重复，只用来做参考量）。
        skips：本批跳过的 {序号: 原因}，按序号去重后才是真正的「跳过多少」。
        deferred：本批挂起的边界行格位数。
        max_index：本批见过的最小/最大全局序号里最大的那个，用来兜底格位数。
        total：同一帧读到的游戏标题数量（筛选后总盘数）。读不到传 None。
        """
        job = self._job
        if job is None:
            return
        job.batches += 1
        if isinstance(skips, dict):
            job.skip_indices.update(int(k) for k in skips)
        elif skips:
            job.skip_indices.update(int(k) for k in skips)
        job.deferred = max(job.deferred, int(deferred))
        if max_index is not None:
            job.slots = max(job.slots, int(max_index) + 1)
        if total is not None:
            # 读到的总数才是筛选结果有多少个盘；取大值是为了读花时不吃亏，
            # 也保住任务开始时的口径（中途数量变化不把筛选后总数改小）。
            job.slots = max(job.slots, int(total))

    def disc_begin(self, job, index: int, level: int, name: str = "", *,
                   slot: int = 0, sets: tuple[str, ...] = (), line_count: int = 0) -> int:
        """开始处理一件盘，返回它在 ``discs`` 里的下标。

        slot / sets / line_count 用于「升满成功明细」：套装名与部位号取所属任务的
        过滤值，词条数取副词条条数。这几个都不影响统计口径，缺省也行。
        """
        self.discs.append(
            DiscStat(
                job=str(job), job_name=name, index=int(index), level_start=int(level),
                slot=int(slot), sets=tuple(str(s) for s in (sets or ())), line_count=int(line_count),
            )
        )
        self._open[f"disc{index}"] = time.monotonic()
        return len(self.discs) - 1

    def disc_stage(self, slot: int, from_level: int, to_level: int, seconds: float) -> None:
        if 0 <= slot < len(self.discs):
            self.discs[slot].stages.append(
                StageSpan(from_level=int(from_level), to_level=int(to_level), seconds=float(seconds))
            )

    def disc_end(self, slot: int, outcome: str, level: int, score: float = 0.0, *,
                  image: str = "") -> None:
        """收尾一件盘。重复调用以第一次为准（一件盘有多个出口，别重复记账）。

        image：成功盘截图相对路径（相对本轮的 report.md），由 runner 在判定成功时传入。
        """
        if not (0 <= slot < len(self.discs)):
            return
        disc = self.discs[slot]
        if disc.outcome:
            return
        disc.outcome = outcome
        disc.level_end = int(level)
        disc.score = float(score)
        disc.image = image
        t0 = self._open.pop(f"disc{disc.index}", None)
        if t0 is not None:
            disc.seconds = time.monotonic() - t0

    def note_discard_label(self) -> None:
        """记一次「真的把弃置标记打上去了」。判定弃置但没打标记的不算。"""
        self.discard_labels += 1

    def span(self, page: str, action: str, seconds: float, detail: str = "") -> None:
        """记一个长动作的真实耗时（切页、筛选、恢复视口…）。"""
        self.actions.append(
            StepSpan(page=page, action=action, detail=detail, seconds=float(seconds), kind=SPAN)
        )

    def absorb_steps(self, spans) -> None:
        """把 Trace 的步骤耗时并进来（统一标成 step）。拿不到就当没有。"""
        try:
            items = list(spans)
        except TypeError:
            return
        for item in items:
            item.kind = STEP
        self.actions.extend(items)

    @contextmanager
    def timed(self, page: str, action: str, detail: str = ""):
        """``with stats.timed("导航", "切页置顶"): ...``"""
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.span(page, action, time.monotonic() - t0, detail)

    # ------------------------------------------------------------ 聚合

    @staticmethod
    def _describe(values: list[float]) -> dict:
        if not values:
            return {"n": 0}
        return {
            "n": len(values),
            "total": round(sum(values), 3),
            "mean": round(statistics.fmean(values), 3),
            "min": round(min(values), 3),
            "p50": round(statistics.median(values), 3),
            "max": round(max(values), 3),
            # 波动幅度：极差 / 平均。越小越稳，用来回答「耗时浮动大吗」。
            "spread": round(
                (max(values) - min(values)) / statistics.fmean(values), 3
            )
            if statistics.fmean(values) > 0
            else 0.0,
        }

    def _group(self, kind: str) -> dict:
        grouped: dict[str, list[float]] = {}
        for item in self.actions:
            if item.kind != kind:
                continue
            grouped.setdefault(f"{item.page} · {item.action}", []).append(item.seconds)
        return {key: self._describe(values) for key, values in sorted(grouped.items())}

    def _stage_stats(self) -> dict:
        """按「到达档位」聚合：升到 3 级花多久、升到 6 级花多久……"""
        buckets: dict[int, list[float]] = {}
        for disc in self.discs:
            for stage in disc.stages:
                buckets.setdefault(stage.to_level, []).append(stage.seconds)
        return {str(level): self._describe(values) for level, values in sorted(buckets.items())}

    def _disc_stats_by_final_level(self) -> dict:
        """按「最终等级」聚合整件盘的耗时：升到 15 级的盘一共花了多少。"""
        buckets: dict[int, list[float]] = {}
        for disc in self.discs:
            if disc.stages:
                buckets.setdefault(disc.level_end, []).append(disc.seconds)
        return {str(level): self._describe(values) for level, values in sorted(buckets.items())}

    def outcome_counts(self) -> dict:
        counts = {key: 0 for key in OUTCOME_NAMES}
        for disc in self.discs:
            if disc.outcome in counts:
                counts[disc.outcome] += 1
        return counts

    def level_distribution(self) -> dict:
        """最终等级分布。只算走完流程的盘（中断在半路的没有可信的最终等级）。"""
        dist: dict[str, int] = {}
        for disc in self.discs:
            if not disc.outcome:
                continue
            dist[str(disc.level_end)] = dist.get(str(disc.level_end), 0) + 1
        return dict(sorted(dist.items(), key=lambda kv: int(kv[0])))

    def unfinished(self) -> int:
        return sum(1 for disc in self.discs if not disc.outcome)

    def summary(self) -> dict:
        counts = self.outcome_counts()
        max_level_kept = sum(
            1 for d in self.discs if d.outcome == OUTCOME_UPGRADED and d.level_end >= 15
        )
        return {
            "tag": self.tag,
            "started_at": self.started_at,
            "seconds": round(self.seconds, 3),
            "jobs": [
                {
                    "index": j.index,
                    "slot": j.slot,
                    "sets": list(j.sets),
                    "mainstats": list(j.mainstats),
                    "seconds": round(j.seconds, 3),
                    "batches": j.batches,
                    "slots": j.slots,
                    "skipped": j.skipped,
                    "deferred": j.deferred,
                    "processed": j.processed,
                    "unprocessed": unprocessed_of(j),
                }
                for j in self.jobs
            ],
            "totals": {
                "jobs": len(self.jobs),
                "slots": sum(j.slots for j in self.jobs),
                "skipped": sum(j.skipped for j in self.jobs),
                "processed": len(self.discs),
                "unprocessed": sum(unprocessed_of(j) for j in self.jobs),
            },
            "outcomes": counts,
            "max_level_kept": max_level_kept,
            "kept_discs": [
                {
                    "index": d.index,
                    "slot": d.slot,
                    "sets": list(d.sets),
                    "line_count": d.line_count,
                    "level_end": d.level_end,
                    "score": round(d.score, 3),
                    "image": d.image,
                }
                for d in self.discs
                if d.outcome == OUTCOME_UPGRADED and d.level_end >= 15
            ],
            "discard_labels": self.discard_labels,
            "unfinished": self.unfinished(),
            "level_distribution": self.level_distribution(),
            # 长动作：真实墙钟，大块时间都在这儿。
            "long_actions": self._group(SPAN),
            # 步骤：距上一步的间隔，粒度细。
            "steps": self._group(STEP),
            "stage_seconds": self._stage_stats(),
            "disc_seconds_by_final_level": self._disc_stats_by_final_level(),
            "discs": [
                {
                    "job": d.job,
                    "index": d.index,
                    "level_start": d.level_start,
                    "level_end": d.level_end,
                    "outcome": d.outcome,
                    "score": d.score,
                    "seconds": round(d.seconds, 3),
                    "stages": [
                        {"from": s.from_level, "to": s.to_level, "seconds": round(s.seconds, 3)}
                        for s in d.stages
                    ],
                }
                for d in self.discs
            ],
        }

    # ------------------------------------------------------------ 输出

    @staticmethod
    def _print_group(log, title: str, grouped: dict, limit: int | None = None) -> None:
        if not grouped:
            return
        log.info("%s（名称 · 次数 · 合计 · 平均 · 最快~最慢 · 波动）：", title)
        ordered = sorted(grouped.items(), key=lambda kv: kv[1].get("total", 0.0), reverse=True)
        for name, stat in ordered[:limit] if limit else ordered:
            log.info(
                "    %s · %d 次 · 合计 %.1fs · 平均 %.2fs · %.2f~%.2fs · 波动 ±%.0f%%",
                name,
                stat["n"],
                stat["total"],
                stat["mean"],
                stat["min"],
                stat["max"],
                stat["spread"] * 100,
            )

    def log_summary(self, log) -> None:
        """把摘要写进文本日志（grep 得到，别只躺在 JSON 里）。"""
        data = self.summary()
        counts = data["outcomes"]
        totals = data["totals"]
        log.info("─" * 28 + " 本轮统计 " + "─" * 28)
        log.info("%s", self.headline())
        log.info(
            "    （任务 %d 个 · 筛选后合计 %d 个盘 · 跳过 %d · 未处理 %d）",
            totals["jobs"],
            totals["slots"],
            totals["skipped"],
            totals["unprocessed"],
        )
        for job in data["jobs"]:
            log.info(
                "    任务%d(%s号位%s%s) · 筛选后 %d 个 · 跳过 %d · 处理 %d · 未处理 %d · 用时 %.1fs",
                job["index"],
                job["slot"],
                " 套装" + "、".join(job["sets"]) if job["sets"] else "",
                " 主词条" + "、".join(job["mainstats"]) if job["mainstats"] else "",
                job["slots"],
                job["skipped"],
                job["processed"],
                job["unprocessed"],
                job["seconds"],
            )
        log.info(
            "结果：升满成功 %d 件（%s）· 判定弃置 %d 件（%s）· 跳过 %d 件 · 中断 %d 件",
            data["max_level_kept"],
            KEPT_NOTE,
            counts[OUTCOME_DISCARDED],
            DISCARD_NOTE,
            counts[OUTCOME_SKIPPED],
            counts[OUTCOME_FAILED],
        )
        if data["kept_discs"]:
            kept_desc = "；".join(
                f"{d['slot']}号位"
                f"{'/'.join(d['sets']) if d['sets'] else '未知'}"
                f"{d['line_count']}词条"
                + (f" 图={d['image']}" if d.get("image") else "")
                for d in data["kept_discs"]
            )
            log.info("    升满成功明细：%s", kept_desc)
        log.info(
            "    实际打上弃置标记 %d 次（判定弃置 %d 件，两者本来就不等：盘上已有标记、"
            "或标记开关关闭时不会真打）",
            data["discard_labels"],
            counts[OUTCOME_DISCARDED],
        )
        dist = data["level_distribution"]
        if dist:
            log.info(
                "最终等级分布：%s",
                "、".join(f"{lvl}级 {n}件" for lvl, n in dist.items()),
            )
        if data["unfinished"]:
            log.info("未走完流程：%d 件（中途中断，不计入等级分布）", data["unfinished"])
        for level in sorted(set(STAGE_LEVELS) | {int(k) for k in data["stage_seconds"]}):
            stat = data["stage_seconds"].get(str(level))
            if not stat or not stat.get("n"):
                continue
            log.info(
                "升到 %s 级：%d 次，平均 %.1fs（%.1f~%.1f，中位 %.1f，波动 ±%.0f%%）",
                level,
                stat["n"],
                stat["mean"],
                stat["min"],
                stat["max"],
                stat["p50"],
                stat["spread"] * 100,
            )
        for level, stat in data["disc_seconds_by_final_level"].items():
            log.info(
                "升到 %s 级的整件耗时：%d 件，平均 %.1fs（%.1f~%.1f，波动 ±%.0f%%）",
                level,
                stat["n"],
                stat["mean"],
                stat["min"],
                stat["max"],
                stat["spread"] * 100,
            )
        self._print_group(log, "长动作（真实耗时，大块时间都在这）", data["long_actions"])
        self._print_group(log, "步骤耗时（距上一步的间隔，看细节；会和上面重叠，别相加）", data["steps"])
        log.info("─" * 68)

    # ------------------------------------------------------------ 执行结果总结

    def headline(self) -> str:
        """一行摘要：共计耗时、处理了几个盘、升满成功几件、判定弃置几件。

        客户端的结果统计栏、报告文件的开头、文本日志都用这一句，三处口径不会写岔。
        """
        data = self.summary()
        counts = data["outcomes"]
        totals = data["totals"]
        return (
            f"共计耗时 {format_duration(data['seconds'])}，"
            f"处理了 {totals['processed']} 个盘，"
            f"升满成功 {data['max_level_kept']} 件，"
            f"判定弃置 {counts[OUTCOME_DISCARDED]} 件。"
        )

    @staticmethod
    def _markdown_group(title: str, grouped: dict) -> list[str]:
        if not grouped:
            return []
        out = [
            f"## {title}",
            "",
            "| 动作 | 次数 | 合计 | 平均 | 最快 | 最慢 | 波动 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        ordered = sorted(grouped.items(), key=lambda kv: kv[1].get("total", 0.0), reverse=True)
        for name, stat in ordered:
            out.append(
                f"| {name} | {stat['n']} | {_cell(stat['total'])}s | {_cell(stat['mean'])}s | "
                f"{_cell(stat['min'])}s | {_cell(stat['max'])}s | ±{stat['spread'] * 100:.0f}% |"
            )
        out.append("")
        return out

    def report_text(self) -> str:
        """Markdown 报告：写进本轮 ``report.md``，客户端结果统计栏展示的也是它。"""
        data = self.summary()
        counts = data["outcomes"]
        totals = data["totals"]
        lines: list[str] = [
            f"# 运行结果统计 · {data['tag']}",
            "",
            f"开始时间：{data['started_at']}",
            "",
            f"> **{self.headline()}**",
            "",
            "## 总览",
            "",
            "| 指标 | 值 |",
            "| --- | --- |",
            f"| 任务数 | {totals['jobs']} |",
            f"| 筛选后合计 | {totals['slots']} 个盘 |",
            f"| 扫描跳过 | {totals['skipped']} 个 |",
            f"| 处理 | {totals['processed']} 个 |",
            f"| 未处理 | {totals['unprocessed']} 个 |",
            f"| 升满成功 | {data['max_level_kept']} 件 |",
            f"| 判定弃置 | {counts[OUTCOME_DISCARDED]} 件 |",
            f"| 总耗时 | {format_duration(data['seconds'])} |",
            "",
            "## 逐任务",
            "",
            "| # | 号位 | 套装 | 主词条 | 筛选后 | 跳过 | 处理 | 未处理 | 用时 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for job in data["jobs"]:
            lines.append(
                f"| {job['index']} | {job['slot']} 号位 | "
                f"{'、'.join(job['sets']) or '不限'} | "
                f"{'、'.join(job['mainstats']) or '不限'} | "
                f"{job['slots']} | {job['skipped']} | {job['processed']} | "
                f"{job['unprocessed']} | "
                f"{format_duration(job['seconds'])} |"
            )
        lines += [
            "",
            "## 结果明细",
            "",
            "| 结果 | 件数 | 口径 |",
            "| --- | --- | --- |",
            f"| 升满成功 | {data['max_level_kept']} | {KEPT_NOTE} |",
            f"| 判定弃置 | {counts[OUTCOME_DISCARDED]} | {DISCARD_NOTE} |",
            f"| 进入处理后跳过 | {counts[OUTCOME_SKIPPED]} | 已读取盘面，未改动 |",
            f"| 中断 | {counts[OUTCOME_FAILED]} | 素材不足等中途停止 |",
            f"| 未完成 | {data['unfinished']} | 未走完流程，不计入等级分布 |",
            "",
            f"- 本轮实际打上弃置标记：**{data['discard_labels']}** 次"
            f"（和上面的「判定弃置」不是一个数）",
            "",
            "## 升满成功明细（15 级且判定保留）",
            "",
        ]
        kept = data["kept_discs"]
        if kept:
            lines += [
                "| 任务号位 | 套装（取任务过滤值） | 词条数 | 满级 | 评分 |",
                "| --- | --- | --- | --- | --- |",
            ]
            for d in kept:
                set_name = "、".join(d["sets"]) if d["sets"] else "未知"
                lines.append(
                    f"| {d['slot']} 号位 | {set_name} | {d['line_count']} 条 | "
                    f"{d['level_end']} 级 | {d['score']:.2f} |"
                )
            lines.append("")
            for d in kept:
                if d.get("image"):
                    set_name = "、".join(d["sets"]) if d["sets"] else "未知"
                    lines.append(f"![{d['slot']} 号位 {set_name}]({d['image']})")
            lines.append("")
            lines.append(
                "- 套装名取所属任务的过滤套装（任务未限定套装时显示「未知」）；部位号 = 任务号位；"
                "词条数 = 副词条条数。这几项帮你看清「升满成功的到底是哪几件盘」。"
                "每张图是对应盘升满成功时的界面截图（存于与本报告同目录的 success/，"
                "链接即 success/discN.png）。"
            )
        else:
            lines.append("（无：本轮没有升满 15 级且判定保留的盘）")
        lines += [
            "",
            "## 最终等级分布",
            "",
        ]
        dist = data["level_distribution"]
        if dist:
            lines += ["| 等级 | 件数 |", "| --- | --- |"]
            lines += [f"| {level} 级 | {n} |" for level, n in dist.items()]
        else:
            lines.append("（无：本轮没有走完流程的盘）")
        lines += [
            "",
            "## 单件升级耗时（按到达档位）",
            "",
            "| 档位 | 次数 | 平均 | 最快 | 最慢 | 波动 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        stage = data["stage_seconds"]
        for level in sorted(set(STAGE_LEVELS) | {int(k) for k in stage}):
            stat = stage.get(str(level))
            if stat and stat.get("n"):
                lines.append(
                    f"| 升到 {level} 级 | {stat['n']} | {_cell(stat['mean'])}s | "
                    f"{_cell(stat['min'])}s | {_cell(stat['max'])}s | ±{stat['spread'] * 100:.0f}% |"
                )
            else:
                lines.append(f"| 升到 {level} 级 | 0 | — | — | — | — |")
        lines.append("")
        by_final = data["disc_seconds_by_final_level"]
        if by_final:
            lines += [
                "## 整件盘耗时（按最终等级）",
                "",
                "| 最终等级 | 件数 | 平均 | 最快 | 最慢 | 波动 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for level, stat in by_final.items():
                lines.append(
                    f"| {level} 级 | {stat['n']} | {_cell(stat['mean'])}s | "
                    f"{_cell(stat['min'])}s | {_cell(stat['max'])}s | ±{stat['spread'] * 100:.0f}% |"
                )
            lines.append("")
        lines += self._markdown_group("长动作耗时（真实墙钟，大块时间都在这）", data["long_actions"])
        lines += self._markdown_group("步骤耗时（距上一步的间隔，粒度细）", data["steps"])
        lines += [
            "## 说明",
            "",
            "- 「筛选后」是游戏标题栏读到的筛选后总盘数（含满级、锁定、弃置），"
            "不是本轮扫到多深；读不到时退回「扫过的最深格位序号 + 1」。",
            "- 「未处理」= 筛选后 − 扫描跳过 − 处理，即本轮没被点开的盘"
            "（含额度用满后没滚到的尾部）。",
            "- 长动作与步骤耗时口径不同，**会重叠，别相加**。",
            f"- 「升满成功」= {KEPT_NOTE}；「判定弃置」= {DISCARD_NOTE}。",
            "",
        ]
        return "\n".join(lines)

    def write_report(self, path: Path) -> Path | None:
        """写执行结果总结（Markdown）。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(self.report_text(), encoding="utf-8")
            temp.replace(path)
            return path
        except Exception:  # noqa: BLE001 - 统计写不出去不该影响任务
            return None

    def write(self, path: Path) -> Path | None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp.replace(path)
            return path
        except Exception:  # noqa: BLE001 - 统计写不出去不该影响任务
            return None
