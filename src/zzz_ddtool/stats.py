"""运行统计：把「这一轮都干了什么、花了多少时间」记下来。

要回答的问题（老大 2026-10-04 提的）：
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

产物两处（收尾时一次性写出）：
    log/text/ddtool.log          一段中文摘要（给人看的，可 grep）
    log/stats/<tag>.json         结构化数据（给脚本/后续报表用）

**为什么放 log/stats 而不是 captures/<tag>/**：截图目录按张数整轮回收，
统计文件虽然不匹配 ``*.png`` 删不掉，但跟着截图走容易被误清理。
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
    level_end: int = 0
    outcome: str = ""
    score: float = 0.0
    seconds: float = 0.0
    stages: list[StageSpan] = field(default_factory=list)


@dataclass
class JobStat:
    """一个任务的用量与格位计数。

    slots 取「见过的最大全局序号 + 1」，也就是这个任务的筛选结果有多少个盘。
    跳过去重（同一格位在相邻批次里会重复出现，用 set 收）。
    """

    index: int
    name: str
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


class RunStats:
    """一轮运行的统计收集器。只收集，聚合放到 ``summary()``。"""

    def __init__(self, tag: str = "") -> None:
        self.tag = tag or time.strftime("%Y%m%d-%H%M%S")
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.seconds = 0.0
        self.jobs: list[JobStat] = []
        self.discs: list[DiscStat] = []
        self.actions: list[StepSpan] = []
        self._open: dict[str, float] = {}

    # ------------------------------------------------------------ 打点接口

    def job_begin(self, index: int, name: str, slot: int, sets, mainstats) -> None:
        self.jobs.append(
            JobStat(
                index=index,
                name=name or f"任务{index}",
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

    def scan(self, cells, skips, deferred: int, max_index: int | None = None) -> None:
        """记一批扫描结果。

        cells：本批可见格位数（跨批会重复，只用来做参考量）。
        skips：本批跳过的 {序号: 原因}，按序号去重后才是真正的「跳过多少」。
        deferred：本批挂起的边界行格位数。
        max_index：本批见过的最小/最大全局序号里最大的那个，用来推这个任务有多少格位。
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

    def disc_begin(self, job, index: int, level: int, name: str = "") -> int:
        """开始处理一件盘，返回它在 ``discs`` 里的下标。"""
        self.discs.append(
            DiscStat(job=str(job), job_name=name, index=int(index), level_start=int(level))
        )
        self._open[f"disc{index}"] = time.monotonic()
        return len(self.discs) - 1

    def disc_stage(self, slot: int, from_level: int, to_level: int, seconds: float) -> None:
        if 0 <= slot < len(self.discs):
            self.discs[slot].stages.append(
                StageSpan(from_level=int(from_level), to_level=int(to_level), seconds=float(seconds))
            )

    def disc_end(self, slot: int, outcome: str, level: int, score: float = 0.0) -> None:
        """收尾一件盘。重复调用以第一次为准（一件盘有多个出口，别重复记账）。"""
        if not (0 <= slot < len(self.discs)):
            return
        disc = self.discs[slot]
        if disc.outcome:
            return
        disc.outcome = outcome
        disc.level_end = int(level)
        disc.score = float(score)
        t0 = self._open.pop(f"disc{disc.index}", None)
        if t0 is not None:
            disc.seconds = time.monotonic() - t0

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
                    "name": j.name,
                    "slot": j.slot,
                    "sets": list(j.sets),
                    "mainstats": list(j.mainstats),
                    "seconds": round(j.seconds, 3),
                    "batches": j.batches,
                    "slots": j.slots,
                    "skipped": j.skipped,
                    "deferred": j.deferred,
                    "processed": j.processed,
                }
                for j in self.jobs
            ],
            "totals": {
                "jobs": len(self.jobs),
                "slots": sum(j.slots for j in self.jobs),
                "skipped": sum(j.skipped for j in self.jobs),
                "processed": len(self.discs),
            },
            "outcomes": counts,
            "max_level_kept": max_level_kept,
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
        log.info(
            "任务 %d 个 · 用时 %.1fs · 筛选后共 %d 个盘（跳过 %d，实际处理 %d）",
            totals["jobs"],
            data["seconds"],
            totals["slots"],
            totals["skipped"],
            totals["processed"],
        )
        for job in data["jobs"]:
            log.info(
                "    任务%d(%s号位%s%s) · 筛选后 %d 个 · 跳过 %d · 处理 %d · 用时 %.1fs",
                job["index"],
                job["slot"],
                " 套装" + "、".join(job["sets"]) if job["sets"] else "",
                " 主词条" + "、".join(job["mainstats"]) if job["mainstats"] else "",
                job["slots"],
                job["skipped"],
                job["processed"],
                job["seconds"],
            )
        log.info(
            "结果：强化 %d 件（其中满级保留 %d 件）· 弃置 %d 件 · 跳过 %d 件 · 中断 %d 件",
            counts[OUTCOME_UPGRADED],
            data["max_level_kept"],
            counts[OUTCOME_DISCARDED],
            counts[OUTCOME_SKIPPED],
            counts[OUTCOME_FAILED],
        )
        dist = data["level_distribution"]
        if dist:
            log.info(
                "最终等级分布：%s",
                "、".join(f"{lvl}级 {n}件" for lvl, n in dist.items()),
            )
        if data["unfinished"]:
            log.info("未走完流程：%d 件（中途中断，不计入等级分布）", data["unfinished"])
        for level, stat in data["stage_seconds"].items():
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
