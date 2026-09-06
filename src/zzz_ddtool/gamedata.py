from __future__ import annotations

from collections.abc import Sequence

# 游戏仓库筛选会用到的主词条名。5 号盘还有属性伤害。
MAINSTATS = (
    "生命值",
    "生命值%",
    "攻击力",
    "攻击力%",
    "防御力",
    "防御力%",
    "暴击率",
    "暴击伤害",
    "异常精通",
    "异常掌控",
    "穿透率",
    "能量自动回复",
    "冲击力",
    "物理伤害",
    "火属性伤害",
    "冰属性伤害",
    "电属性伤害",
    "以太伤害",
)

SLOTS = (1, 2, 3, 4, 5, 6)

# 1–3 号位主词条固定，筛选不用选。4–6 和游戏仓库一致。
SLOT_MAINSTATS: dict[int, tuple[str, ...]] = {
    1: (),
    2: (),
    3: (),
    4: ("生命值%", "攻击力%", "防御力%", "异常精通", "暴击率", "暴击伤害"),
    5: (
        "生命值%",
        "攻击力%",
        "防御力%",
        "穿透率",
        "物理伤害",
        "火属性伤害",
        "冰属性伤害",
        "电属性伤害",
        "以太伤害",
    ),
    6: ("生命值%", "攻击力%", "防御力%", "能量自动回复", "冲击力", "异常掌控"),
}


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
SETS = (
    "沧浪行歌",
    "谶羽之誓",
    "独角兽电音",
    "法厄同之歌",
    "拂晓生花",
    "拂晓行纪",
    "河豚电音",
    "呼啸沙龙",
    "混沌爵士",
    "混沌重金属",
    "极地重金属",
    "季风放克",
    "激素朋克",
    "荆棘玫瑰",
    "静听嘉音",
    "狂欢朋克",
    "雷暴重金属",
    "獠牙重金属",
    "灵魂摇滚",
    "流光咏叹",
    "流浪民谣",
    "猛犸电音",
    "末日碾核",
    "囚徒手记",
    "如影相随",
    "杀手民谣",
    "山大王",
    "歪曲碾核",
    "喧闹波普",
    "雪兔梦游仙境",
    "炎狱重金属",
    "摇摆爵士",
    "原始朋克",
    "月光骑士颂",
    "云岿如我",
    "折枝剑歌",
    "震星迪斯科",
    "啄木鸟电音",
    "自由蓝调",
)
