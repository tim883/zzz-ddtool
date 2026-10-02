from pathlib import Path

from zzz_ddtool.config import load_preset, preset_from_dict, preset_to_dict, save_preset
from zzz_ddtool.gamedata import MAINSTATS, SETS, selectable_mainstats


def test_roundtrip_keeps_stage_require_and_accepted(tmp_path: Path) -> None:
    preset = preset_from_dict(
        {
            "name": "试",
            "weights": {"暴击率": 1.0, "暴击伤害": 1.2},
            "stage_require": {"three": {0: 2.2, 3: 3.0}, "four": {0: 3.0}},
            "accepted": ["three:0:暴击伤害|暴击率|废词条|未出"],
            "filters": {"require_s_rank": False},
        }
    )
    path = tmp_path / "p.yaml"
    save_preset(path, preset)
    loaded = load_preset(path)
    assert loaded.name == "试"
    assert loaded.weights["暴击伤害"] == 1.2
    assert loaded.require_for("three", 0) == 2.2
    assert loaded.accepted == ("three:0:暴击伤害|暴击率|废词条|未出",)
    assert loaded.require_s_rank is False
    assert "accepted" in preset_to_dict(loaded)


def test_max_discs_and_game_filter_roundtrip(tmp_path: Path) -> None:
    preset = preset_from_dict(
        {
            "name": "限100",
            "weights": {"暴击率": 1.0},
            "run": {"max_discs": 20},
            "game_filter": {"sets": ["折枝剑歌"], "slot": 4, "mainstats": ["暴击伤害"]},
        }
    )
    path = tmp_path / "limit.yaml"
    save_preset(path, preset)
    loaded = load_preset(path)
    assert loaded.max_discs == 20
    assert loaded.filter_sets == ("折枝剑歌",)
    assert loaded.filter_slot == 4
    assert loaded.filter_slots == (4,)
    assert loaded.filter_mainstats == ("暴击伤害",)


def test_set_and_mainstat_lists_include_common_names() -> None:
    assert "折枝剑歌" in SETS
    assert "啄木鸟电音" in SETS
    assert "暴击伤害" in MAINSTATS
    assert "攻击力%" in MAINSTATS


def test_selectable_mainstats_follow_game_slots() -> None:
    assert selectable_mainstats([1, 2, 3]) == ()
    assert "暴击伤害" in selectable_mainstats([4])
    assert "生命值" not in selectable_mainstats([4])
    assert "穿透率" in selectable_mainstats([5])
    assert "异常掌控" in selectable_mainstats([6])
    assert "暴击伤害" not in selectable_mainstats([5])
    assert "暴击伤害" in selectable_mainstats([])


def test_task_limits_roundtrip_and_legacy_fallback(tmp_path: Path) -> None:
    preset = preset_from_dict({
        "run": {"max_discs": 29},
        "task_chain": [
            {"slot": 1, "max_discs": 5},
            {"slot": 4, "max_discs": 12},
            {"slot": 6},
        ],
    })
    path = tmp_path / "chain.yaml"
    save_preset(path, preset)
    loaded = load_preset(path)
    assert [job.max_discs for job in loaded.task_chain] == [5, 12, None]
    from zzz_ddtool.runner import Runner

    from types import SimpleNamespace

    runner = SimpleNamespace(preset=loaded)
    assert [Runner._job_limit(runner, job) for job in loaded.task_chain] == [5, 12, 29]
