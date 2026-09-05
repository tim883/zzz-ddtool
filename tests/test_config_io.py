from pathlib import Path

from zzz_ddtool.config import load_preset, preset_from_dict, preset_to_dict, save_preset


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
