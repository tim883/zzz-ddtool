"""独立合成的 final 正例：一件已弃置盘，时轴与引用完整；只用于离线用例。"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json

from zzz_ddtool.config import LabelPolicy, Preset


PLAN = {
    "run": {"max_pages": 3, "max_discs": 3000},
    "filters": {"skip_discarded": True}, "labels": {"save": False, "discard": False},
    "task_chain": [{"id": "j1", "slot": 1, "accepted": [], "max_discs": 3000}],
}


def json_snapshot(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def hash_json(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def minimal_events(run_id, *, short=False):
    eid = lambda n: f"e{n}" if short else f"e{n:08d}"
    preset = json_snapshot(asdict(Preset(filter_slot=1, filter_slots=(1,), accepted=(),
                                        skip_discarded=True, labels=LabelPolicy(False, False))))
    ph, ah = hash_json(preset), hash_json([])
    disc = dict(level=0, rarity="S", substats=[dict(name="攻击力", rolls=1),
                dict(name="暴击率", rolls=1), dict(name="生命值", rolls=1)],
                locked=False, equipped=False, discarded=True)
    core = {k: disc[k] for k in ("level", "rarity", "substats")}
    choice = dict(decision_id="d1", at=100.75, disc=disc, preset_sha256=ph,
                  accepted_sha256=ah, decision=dict(action="skip", reason="skip_discarded",
                  score=0.0, useful_types=0, max_useful_types=0, checkpoint=0, origin=None, pattern_id=None))
    item = dict(input_id="i1", call_id=1, api="click_client", kind="select_disc", phase="disc",
                job_index=0, disc_index=3, disc_event_id=eid(1), started_at=100.1, ended_at=100.15,
                status="completed", exception=None, hwnd=1, pid=123, client_size=[1920, 1080],
                pause_generation=0, decision_id=None, disc=disc, preset_sha256=ph, accepted_sha256=ah,
                target=dict(global_index=3, x=200, y=500), result=dict(verified=True, actual_level=0,
                to_layer="warehouse", exception=None), wait_ids=[eid(3)], x=200, y=500, vk=None, delta_units=None,
                released_at_ns=100_120_000_000,
                release_wait=dict(action_id="select_disc", released_at_ns=100_120_000_000,
                                  floor_ms=680, policy_source="default",
                                  requested_sleep_ms=580.0, active_elapsed_ms=580.0))
    job = dict(job_index=0, job_id="j1", slot=1, sets=[], mainstats=[], accepted=[], max_discs=3000,
               quota_raw=3000, quota_effective=3000, labels=dict(save=False, discard=False),
               skipped=False, processed=1, end_reason="exhausted", preset=preset,
               preset_sha256=ph, accepted_sha256=ah, started_at=100.0, prepared_at=100.05,
               ended_at=101.5, input_ids=["i1"])

    def event(n, component, duration, details, *, parent=None, scoped=False):
        return dict(schema_version=1, run_id=run_id, event_id=eid(n), parent_id=eid(parent) if parent else None,
                    job_index=0 if scoped else None, disc_index=3 if scoped else None,
                    stage_level=None, component=component, duration_ms=duration,
                    details={"trace_tag": "fixture", **details})

    samples = [dict(probe_event_id=eid(n), sample_index=index, captured_at=t,
                    probe_ended_at=t+0.02, ready=True, state_key=["same"], pause_generation=0,
                    interrupt_reason=None, evidence=dict(core=core, return_layer="warehouse"))
               for index, (n, t) in enumerate([(2, 100.66), (5, 100.71)])]
    probes = [event(n, "probe", 20, dict(
        purpose="wait:select_disc", wait_id=eid(3), sample_id=None, kind="select_disc",
        **{k: v for k, v in sample.items() if k != "probe_event_id"}), parent=3, scoped=True)
        for n, sample in zip([2, 5], samples)]
    return [
        event(1, "disc_total", 900, dict(started_at=100.1, ended_at=101.0, processed=True,
              input_sent=True, result="done", outcome="succeeded", core=core, inputs=[item],
              decisions=[choice], preset_sha256=ph, accepted_sha256=ah), scoped=True),
        probes[0], probes[1],
        event(3, "wait_total", 620, dict(kind="select_disc", outcome="succeeded", started_at=100.12,
              ended_at=100.74, active_ms=620, pause_intervals=[], samples=samples,
              t_bad=None, t_ready=100.66, t_stable=100.71, trigger_input_id="i1",
              upgrade_input_id=None, resume_from_wait_id=None, interrupt_reason=None), parent=1, scoped=True),
        event(6, "parse_total", 20, dict(profile_id="fixture", reject_reason=None,
              template_hits=0, ocr_lines=3, outcome="succeeded"), parent=2, scoped=True),
        event(7, "capture", 1, dict(method="printwindow", hwnd=1, outcome="succeeded"), parent=2, scoped=True),
        event(8, "png_save", 1, dict(source="trace", path="fixture.png", saved=True, outcome="succeeded"), parent=1, scoped=True),
        event(4, "run_total", 2000, dict(started_at=100.0, ended_at=102.0, jobs=[job], inputs=[item],
              runner_outcome="completed", termination_kind="completed", end_reason="已处理 1 个盘",
              evidence_error=None, exception=None)),
    ]


def two_job_events(run_id):
    events = minimal_events(run_id)
    second = deepcopy(events[:-1])
    root = events[-1]
    job = deepcopy(root["details"]["jobs"][0])
    job.update(job_index=1, job_id="j2", started_at=101.5, prepared_at=101.55, ended_at=103.0,
               input_ids=["i2"])
    for e in second:
        e["event_id"] += "-j2"
        if e["parent_id"]:
            e["parent_id"] += "-j2"
        e["job_index"] = 1
        details = e["details"]
        for key in ("started_at", "ended_at", "t_ready", "t_stable", "captured_at", "probe_ended_at"):
            if details.get(key) is not None:
                details[key] += 1.5
        if e["component"] == "disc_total":
            choice = details["decisions"][0]
            choice.update(decision_id="d2", at=choice["at"]+1.5)
            i = details["inputs"][0]
            i.update(input_id="i2", call_id=2, job_index=1, disc_event_id=e["event_id"],
                     started_at=i["started_at"]+1.5, ended_at=i["ended_at"]+1.5,
                     released_at_ns=i["released_at_ns"] + 1_500_000_000,
                     wait_ids=[eid+"-j2" for eid in i["wait_ids"]])
            i["release_wait"].update(released_at_ns=i["released_at_ns"])
        elif e["component"] == "wait_total":
            details["trigger_input_id"] = "i2"
            for sample in details["samples"]:
                sample["probe_event_id"] += "-j2"
                sample["captured_at"] += 1.5
                sample["probe_ended_at"] += 1.5
        elif e["component"] == "probe":
            details["wait_id"] += "-j2"
    root["details"]["jobs"].append(job)
    root["details"]["inputs"].append(next(e for e in second if e["component"] == "disc_total")["details"]["inputs"][0])
    root["details"]["ended_at"] = 103.5
    root["duration_ms"] = 3500
    return events[:-1] + second + [root]
