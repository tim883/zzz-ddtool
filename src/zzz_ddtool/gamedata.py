from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from zzz_ddtool.paths import data_dir


def _load_json(name: str):
    return json.loads((data_dir() / name).read_text(encoding="utf-8"))


_stats = _load_json("stats.json")

# 副属性名（OCR 识别词典）。副词条只有 10 种，别往里加主属性：
#   * 副词条是「穿透值」，主属性才是「穿透率」（5 号位）；
#   * 「冲击力」「异常掌控」「能量自动回复」是 6 号位主属性，不是副词条。
# 曾经把这三个混进词典，界面下拉框就多出三条选不中的词条，选进去的组合永远命不中。
SUBSTAT_NAMES = tuple(_stats["substat_names"])

# 主属性名（OCR 识别词典 + 游戏仓库筛选用，共用一份）。
MAINSTAT_NAMES = tuple(_stats["mainstat_names"])

# 兼容旧名：tests 里 import MAINSTATS。
MAINSTATS = MAINSTAT_NAMES

SLOTS = (1, 2, 3, 4, 5, 6)
FIXED_MAINSTATS = {1: "生命值", 2: "攻击力", 3: "防御力"}

# 1–3 号位主词条固定，筛选不用选。4–6 和游戏仓库一致。
SLOT_MAINSTATS: dict[int, tuple[str, ...]] = {
    int(k): tuple(v) for k, v in _stats["slot_mainstats"].items()
}


def normalize_required_substats(names: Iterable[str], weights: Iterable[str]) -> tuple[str, ...]:
    """必带条件只属于单价表中存在的合法副词条，保留顺序并去重。"""
    available = set(weights) & set(SUBSTAT_NAMES)
    return tuple(dict.fromkeys(name for item in names if (name := str(item).strip()) in available))


def blocked_substats(slot: int, mainstats: Iterable[str] = ()) -> set[str]:
    fixed = FIXED_MAINSTATS.get(slot)
    if fixed is not None:
        return {fixed}
    choices = set(mainstats) & set(SLOT_MAINSTATS.get(slot, ()))
    # 多个主属性是任选其一：某主属性在其他盘上仍能成为副属性。
    return choices & set(SUBSTAT_NAMES) if len(choices) == 1 else set()


def possible_substats(slot: int, mainstats: Iterable[str], names: Iterable[str]) -> bool:
    """副词条组合至少能出现在一个所选主属性的盘上。"""
    present = set(names)
    fixed = FIXED_MAINSTATS.get(slot)
    if fixed is not None:
        return fixed not in present
    choices = set(mainstats) & set(SLOT_MAINSTATS.get(slot, ()))
    return not choices or any(main not in present for main in choices)


def default_stage_require(slot: int) -> dict[str, dict[int, float]]:
    """1–3 门槛更高，4–6 更松。各号独立，不互相拷贝。"""
    if int(slot) <= 3:
        return {
            "three": {0: 2.2, 3: 3.0, 6: 4.0, 9: 5.0, 12: 6.0, 15: 7.0},
            "four": {0: 3.0, 3: 5.0, 6: 6.0, 9: 7.0, 12: 8.0, 15: 9.0},
        }
    return {
        "three": {0: 1.6, 3: 2.2, 6: 2.8, 9: 3.4, 12: 4.0, 15: 4.6},
        "four": {0: 2.2, 3: 3.0, 6: 3.6, 9: 4.2, 12: 4.8, 15: 5.4},
    }


def empty_slot_plan(slot: int) -> dict:
    return {
        "weights": {},
        "stage_require": default_stage_require(slot),
        "accepted": None,
        "mainstats": (),
        "sets": (),
        "required_substats": (),
        "min_useful_types": 0,
    }


def selectable_mainstats(slots: Sequence[int]) -> tuple[str, ...]:
    """没勾部位 = 按 4/5/6 可选主词条。只勾 1–3 则没有可选项。"""
    chosen = tuple(int(slot) for slot in slots if int(slot) in SLOTS)
    if not chosen:
        chosen = (4, 5, 6)
    names: list[str] = []
    seen: set[str] = set()
    for slot in chosen:
        for name in SLOT_MAINSTATS[slot]:
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return tuple(names)

# 简中套装名，按 biligame「分类:驱动盘」2026-09。新套装可往下加。
SETS = tuple(_load_json("sets.json"))
