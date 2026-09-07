"""任务链序列化——存档系统靠它存取，错了会静默丢任务。"""

from zzz_ddtool.chain import job_from_dict, new_job_id, parse_task_chain
from zzz_ddtool.gamedata import SLOTS


def _job_dict(**overrides):
    data = {
        "id": "abcd1234",
        "slot": 4,
        "sets": ["折枝剑歌"],
        "mainstats": ["暴击伤害"],
        "weights": {"暴击率": 1.0, "暴击伤害": 1.2},
        "stage_require": {"three": {0: 2.2, 3: 3.0}, "four": {0: 3.0}},
        "min_useful_types": 2,
    }
    data.update(overrides)
    return data


def test_job_roundtrip_keeps_everything() -> None:
    job = job_from_dict(_job_dict(accepted=["four:0:暴击伤害|暴击率|废词条|废词条"]))
    assert job is not None
    out = job.to_dict()
    assert out["id"] == "abcd1234"
    assert out["slot"] == 4
    assert out["sets"] == ["折枝剑歌"]
    assert out["mainstats"] == ["暴击伤害"]
    assert out["weights"]["暴击伤害"] == 1.2
    assert out["stage_require"]["three"][0] == 2.2
    assert out["stage_require"]["three"][3] == 3.0
    assert out["stage_require"]["four"][0] == 3.0
    assert out["min_useful_types"] == 2
    assert out["accepted"] == ["four:0:暴击伤害|暴击率|废词条|废词条"]


def test_invalid_slot_is_rejected() -> None:
    # 部位必须是 1–6，别的（含 0 和 7）一律丢掉，不能带进运行时
    assert job_from_dict(_job_dict(slot=0)) is None
    assert job_from_dict(_job_dict(slot=7)) is None
    assert job_from_dict(_job_dict(slot=99)) is None


def test_all_six_slots_are_valid() -> None:
    for slot in SLOTS:
        assert job_from_dict(_job_dict(slot=slot)) is not None


def test_parse_chain_skips_bad_items() -> None:
    raw = [
        _job_dict(slot=1),
        _job_dict(slot=0),  # 非法部位，应被跳过
        "not a dict",
        None,
        _job_dict(slot=6),
    ]
    jobs = parse_task_chain(raw)
    assert len(jobs) == 2
    assert [job.slot for job in jobs] == [1, 6]


def test_parse_chain_of_non_list_is_empty() -> None:
    assert parse_task_chain(None) == ()
    assert parse_task_chain("x") == ()
    assert parse_task_chain({}) == ()


def test_missing_id_gets_a_fresh_one() -> None:
    job = job_from_dict({"slot": 4})
    assert job is not None
    assert job.job_id  # 补了非空 id


def test_new_job_id_is_unique() -> None:
    ids = {new_job_id() for _ in range(50)}
    assert len(ids) == 50


def test_title_shows_slot_and_sets() -> None:
    job = job_from_dict(_job_dict(slot=4, sets=["折枝剑歌"], mainstats=["暴击伤害"]))
    title = job.title()
    assert "4" in title
    assert "折枝剑歌" in title
    assert "暴击伤害" in title


def test_title_without_mainstats_still_readable() -> None:
    job = job_from_dict(_job_dict(slot=1, sets=[], mainstats=[]))
    title = job.title()
    assert "1" in title
    assert "未选套装" in title
