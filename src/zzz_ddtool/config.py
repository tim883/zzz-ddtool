from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from zzz_ddtool.chain import TaskJob, parse_task_chain
from zzz_ddtool.gamedata import SLOTS, blocked_substats, normalize_required_substats
from zzz_ddtool.models import UPGRADE_STEP
from zzz_ddtool.origin import Origin

_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_SAFE_DUMPER = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


@dataclass(frozen=True)
class LabelPolicy:
    """全局标签执行策略，预留高级设置入口；不影响判断和强化。"""
    discard: bool = True
    save: bool = True

    def allows(self, label: str) -> bool:
        if label not in ("discard", "save"):
            raise ValueError(f"未知标签：{label}")
        return getattr(self, label)


@dataclass(frozen=True)
class Preset:
    name: str = "default"
    weights: Mapping[str, float] = field(default_factory=dict)
    stage_require: Mapping[str, Mapping[int, float]] = field(default_factory=dict)
    min_useful_types: int = 0
    skip_locked: bool = True
    skip_equipped: bool = True
    skip_discarded: bool = False  # 新任务默认不勾，等用户自己开
    skip_max_level: bool = True  # 等级15=满级，扫描阶段直接跳过不点击
    max_discs: int = 100
    filter_sets: tuple[str, ...] = ()
    filter_slot: int = 4
    filter_slots: tuple[int, ...] = (4,)
    filter_mainstats: tuple[str, ...] = ()
    slot_plans: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    task_chain: tuple[TaskJob, ...] = ()
    ocr_retry: int = 3
    accepted: tuple[str, ...] | None = None
    max_pages: int = 20
    labels: LabelPolicy = field(default_factory=LabelPolicy)
    required_substats: tuple[str, ...] = ()

    def useful_names(self) -> frozenset[str]:
        return frozenset(name for name, weight in self.effective_weights().items() if weight > 0)

    def effective_weights(self) -> dict[str, float]:
        blocked = blocked_substats(self.filter_slot, self.filter_mainstats)
        return {name: weight for name, weight in self.weights.items() if name not in blocked}

    def require_for(self, origin: Origin | str, checkpoint: int) -> float | None:
        key = origin.value if isinstance(origin, Origin) else origin
        table = self.stage_require.get(key) or {}
        if checkpoint in table:
            return float(table[checkpoint])
        return None


def checkpoint_level(level: int) -> int:
    """OCR 到 1、2 这类中间值时，归到上一档检查点。+15 保持 15。"""
    if level < 0:
        return 0
    snapped = (level // UPGRADE_STEP) * UPGRADE_STEP
    return min(snapped, 15)


def load_preset(path: str | Path) -> Preset:
    data = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_SAFE_LOADER) or {}
    return preset_from_dict(data)


def _parse_stage_require(data: dict[str, Any]) -> dict[str, dict[int, float]]:
    raw = data.get("stage_require")
    if isinstance(raw, dict) and raw:
        out: dict[str, dict[int, float]] = {}
        for origin, table in raw.items():
            out[str(origin)] = {int(k): float(v) for k, v in (table or {}).items()}
        return out
    # 旧字段：一套门槛套所有阶段、3/4 词共用（已废弃）
    legacy = data.get("thresholds") or {}
    shared = {int(k): float(v) for k, v in legacy.items()}
    if not shared:
        return {origin: {} for origin in ("three", "four")}
    return {"three": dict(shared), "four": dict(shared)}


def preset_from_dict(data: dict[str, Any]) -> Preset:
    filters = data.get("filters") or {}
    labels = data.get("labels") or {}
    weights = {str(k): float(v) for k, v in (data.get("weights") or {}).items()}
    stage_require = _parse_stage_require(data)
    raw_accepted = data.get("accepted", None)
    accepted = None if raw_accepted is None else tuple(str(item) for item in raw_accepted)
    run = data.get("run") or {}
    game_filter = data.get("game_filter") or {}
    raw_sets = game_filter.get("sets") or ()
    raw_mains = game_filter.get("mainstats") or ()
    mains = tuple(str(item).strip() for item in raw_mains if str(item).strip())
    filter_slot = _parse_filter_slot(game_filter)
    min_useful = int(data.get("min_useful_types") or 0)
    slot_plans = _parse_slot_plans(data.get("slot_plans") or {})
    active = dict(slot_plans.get(filter_slot) or {})
    if weights or accepted is not None or mains or stage_require:
        active.setdefault("weights", weights)
        active.setdefault("stage_require", stage_require)
        active.setdefault("accepted", accepted)
        active.setdefault("mainstats", mains)
        active.setdefault("min_useful_types", min_useful)
        active.setdefault("required_substats", normalize_required_substats(data.get("required_substats") or (), weights))
        slot_plans[filter_slot] = active
    active_weights = dict(active.get("weights") or weights)
    active_require = dict(active.get("stage_require") or stage_require)
    active_accepted = active.get("accepted", accepted)
    if isinstance(active_accepted, list):
        active_accepted = tuple(str(item) for item in active_accepted)
    active_mains = tuple(active.get("mainstats") or mains)
    return Preset(
        name=str(data.get("name") or "default"),
        weights=active_weights,
        stage_require=active_require,
        min_useful_types=int(active.get("min_useful_types") or min_useful),
        skip_locked=bool(filters.get("skip_locked", True)),
        skip_equipped=bool(filters.get("skip_equipped", True)),
        skip_discarded=bool(filters.get("skip_discarded", False)),
        skip_max_level=bool(filters.get("skip_max_level", True)),
        max_discs=max(1, int(run.get("max_discs") or 100)),
        filter_sets=tuple(str(item).strip() for item in raw_sets if str(item).strip()),
        filter_slot=filter_slot,
        filter_slots=(filter_slot,),
        filter_mainstats=active_mains,
        slot_plans=slot_plans,
        task_chain=parse_task_chain(data.get("task_chain") or ()),
        ocr_retry=int(data.get("ocr_retry") or 3),
        accepted=active_accepted,
        labels=LabelPolicy(discard=bool(labels.get("discard", True)), save=bool(labels.get("save", True))),
        required_substats=normalize_required_substats(active.get("required_substats", data.get("required_substats") or ()), active_weights),
    )


def _parse_filter_slot(game_filter: Mapping[str, Any]) -> int:
    raw = game_filter.get("slot")
    if raw is None:
        raw_slots = game_filter.get("slots") or ()
        slots = [int(item) for item in raw_slots if int(item) in SLOTS]
        raw = slots[0] if slots else 4
    slot = int(raw)
    return slot if slot in SLOTS else 4


def _parse_slot_plans(raw: Mapping[Any, Any]) -> dict[int, dict[str, Any]]:
    plans: dict[int, dict[str, Any]] = {}
    for key, value in (raw or {}).items():
        slot = int(key)
        if slot not in SLOTS or not isinstance(value, dict):
            continue
        weights = {str(name): float(weight) for name, weight in (value.get("weights") or {}).items()}
        accepted = value.get("accepted", None)
        if accepted is not None:
            accepted = tuple(str(item) for item in accepted)
        mains = tuple(str(item).strip() for item in (value.get("mainstats") or ()) if str(item).strip())
        sets = tuple(str(item).strip() for item in (value.get("sets") or ()) if str(item).strip())
        plans[slot] = {
            "weights": weights,
            "stage_require": _parse_stage_require(value) if value.get("stage_require") or value.get("thresholds") else {"three": {}, "four": {}},
            "accepted": accepted,
            "mainstats": mains,
            "sets": sets,
            "min_useful_types": int(value.get("min_useful_types") or 0),
            "required_substats": normalize_required_substats(value.get("required_substats") or (), weights),
        }
    return plans


def preset_to_dict(preset: Preset) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": preset.name,
        "labels": {"discard": preset.labels.discard, "save": preset.labels.save},
        "min_useful_types": preset.min_useful_types,
        "ocr_retry": preset.ocr_retry,
        "run": {"max_discs": preset.max_discs},
        "game_filter": {
            "sets": list(preset.filter_sets),
            "slot": preset.filter_slot,
            "slots": [preset.filter_slot],
            "mainstats": list(preset.filter_mainstats),
        },
        "slot_plans": {
            str(slot): {
                "weights": {str(name): float(weight) for name, weight in (plan.get("weights") or {}).items()},
                "stage_require": {
                    str(origin): {int(level): float(value) for level, value in sorted((table or {}).items())}
                    for origin, table in (plan.get("stage_require") or {}).items()
                },
                "mainstats": list(plan.get("mainstats") or ()),
                "sets": list(plan.get("sets") or ()),
                "min_useful_types": int(plan.get("min_useful_types") or 0),
                **({"accepted": list(plan["accepted"])} if plan.get("accepted") is not None else {}),
                **({"required_substats": list(plan["required_substats"])} if plan.get("required_substats") else {}),
            }
            for slot, plan in sorted((preset.slot_plans or {}).items())
            if int(slot) in SLOTS
        },
        "filters": {
            "skip_locked": preset.skip_locked,
            "skip_equipped": preset.skip_equipped,
            "skip_discarded": preset.skip_discarded,
        },
        "task_chain": [job.to_dict() for job in preset.task_chain],
        "weights": {str(name): float(weight) for name, weight in preset.weights.items()},
        "stage_require": {
            str(origin): {int(level): float(value) for level, value in sorted(table.items())}
            for origin, table in preset.stage_require.items()
        },
    }
    if preset.accepted is not None:
        data["accepted"] = list(preset.accepted)
    if preset.required_substats:
        data["required_substats"] = list(preset.required_substats)
    return data


def save_preset(path: str | Path, preset: Preset) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        yaml.dump(preset_to_dict(preset), Dumper=_SAFE_DUMPER, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return dest
