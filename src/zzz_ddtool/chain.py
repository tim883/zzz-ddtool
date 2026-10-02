from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from zzz_ddtool.gamedata import SLOTS


def new_job_id() -> str:
    return uuid4().hex[:8]


@dataclass
class TaskJob:
    slot: int
    job_id: str = field(default_factory=new_job_id)
    name: str = ""
    sets: tuple[str, ...] = ()
    mainstats: tuple[str, ...] = ()
    weights: dict[str, float] = field(default_factory=dict)
    stage_require: dict[str, dict[int, float]] = field(default_factory=dict)
    accepted: tuple[str, ...] | None = None
    min_useful_types: int = 0

    def title(self) -> str:
        """内容摘要。不是名字——名字在 name 字段，这里只描述"这条是什么"。"""
        sets = "、".join(self.sets) if self.sets else "未选套装"
        if self.mainstats:
            mains = "、".join(self.mainstats)
            return f"{self.slot}号 · {sets} · {mains}"
        return f"{self.slot}号 · {sets}"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.job_id,
            "slot": self.slot,
            "sets": list(self.sets),
            "mainstats": list(self.mainstats),
            "weights": {str(name): float(weight) for name, weight in self.weights.items()},
            "stage_require": {
                str(origin): {int(level): float(value) for level, value in sorted(table.items())}
                for origin, table in (self.stage_require or {}).items()
            },
            "min_useful_types": self.min_useful_types,
        }
        if self.accepted is not None:
            data["accepted"] = list(self.accepted)
        if self.name:
            data["name"] = self.name
        return data


def job_from_dict(data: dict[str, Any]) -> TaskJob | None:
    slot = int(data.get("slot") or 0)
    if slot not in SLOTS:
        return None
    accepted = data.get("accepted", None)
    if accepted is not None:
        accepted = tuple(str(item) for item in accepted)
    require_raw = data.get("stage_require") or {}
    stage_require = {
        str(origin): {int(level): float(value) for level, value in (table or {}).items()}
        for origin, table in require_raw.items()
    }
    return TaskJob(
        job_id=str(data.get("id") or new_job_id()),
        slot=slot,
        name=str(data.get("name") or "").strip(),
        sets=tuple(str(item).strip() for item in (data.get("sets") or ()) if str(item).strip()),
        mainstats=tuple(str(item).strip() for item in (data.get("mainstats") or ()) if str(item).strip()),
        weights={str(name): float(weight) for name, weight in (data.get("weights") or {}).items()},
        stage_require=stage_require,
        accepted=accepted,
        min_useful_types=int(data.get("min_useful_types") or 0),
    )


def parse_task_chain(raw: Any) -> tuple[TaskJob, ...]:
    if not isinstance(raw, list):
        return ()
    jobs: list[TaskJob] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        job = job_from_dict(item)
        if job is not None:
            jobs.append(job)
    return tuple(jobs)
