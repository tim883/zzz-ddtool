from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from zzz_ddtool.models import UPGRADE_STEP
from zzz_ddtool.origin import Origin


@dataclass(frozen=True)
class Preset:
    name: str = "default"
    weights: Mapping[str, float] = field(default_factory=dict)
    stage_require: Mapping[str, Mapping[int, float]] = field(default_factory=dict)
    min_useful_types: int = 0
    require_s_rank: bool = True
    skip_locked: bool = True
    skip_equipped: bool = True
    skip_discarded: bool = True
    ocr_retry: int = 3
    accepted: tuple[str, ...] | None = None

    def useful_names(self) -> frozenset[str]:
        return frozenset(name for name, weight in self.weights.items() if weight > 0)

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
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
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
    weights = {str(k): float(v) for k, v in (data.get("weights") or {}).items()}
    stage_require = _parse_stage_require(data)
    raw_accepted = data.get("accepted", None)
    accepted = None if raw_accepted is None else tuple(str(item) for item in raw_accepted)
    return Preset(
        name=str(data.get("name") or "default"),
        weights=weights,
        stage_require=stage_require,
        min_useful_types=int(data.get("min_useful_types") or 0),
        require_s_rank=bool(filters.get("require_s_rank", True)),
        skip_locked=bool(filters.get("skip_locked", True)),
        skip_equipped=bool(filters.get("skip_equipped", True)),
        skip_discarded=bool(filters.get("skip_discarded", True)),
        ocr_retry=int(data.get("ocr_retry") or 3),
        accepted=accepted,
    )


def preset_to_dict(preset: Preset) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": preset.name,
        "min_useful_types": preset.min_useful_types,
        "ocr_retry": preset.ocr_retry,
        "filters": {
            "require_s_rank": preset.require_s_rank,
            "skip_locked": preset.skip_locked,
            "skip_equipped": preset.skip_equipped,
            "skip_discarded": preset.skip_discarded,
        },
        "weights": {str(name): float(weight) for name, weight in preset.weights.items()},
        "stage_require": {
            str(origin): {int(level): float(value) for level, value in sorted(table.items())}
            for origin, table in preset.stage_require.items()
        },
    }
    if preset.accepted is not None:
        data["accepted"] = list(preset.accepted)
    return data


def save_preset(path: str | Path, preset: Preset) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        yaml.safe_dump(preset_to_dict(preset), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return dest
