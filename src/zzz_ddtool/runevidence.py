"""执行点快照及 final 证据核对。只读事件，不截图、不发输入、不重演业务。"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, is_dataclass

COVERAGE_KEYS = (
    "upgrade", "label_save", "label_discard", "label_unlock_before_discard",
    "maxed_refund", "material_shortfall",
)


def snapshot(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(k): snapshot(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot(v) for v in value]
    return value


def digest(value) -> str:
    raw = json.dumps(snapshot(value), sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def accepted_digest(accepted) -> str:
    return digest(None if accepted is None else sorted(set(accepted)))


def validate_plan(raw, jobs) -> list[str]:
    """独立读实际 YAML 的原值和有效配置，拒绝加载时 clamp 掩盖的非法配额。"""
    from zzz_ddtool.chain import TaskJob
    from zzz_ddtool.config import Preset, preset_from_dict

    errors = []
    if not isinstance(raw, dict):
        return ["YAML 根不是对象"]
    try:
        run = raw.get("run") or {}
        for key in ("max_discs", "max_pages"):
            if key in run and (type(run[key]) is not int or run[key] < 1):
                errors.append(f"run.{key} 原值非法")
        chain = raw.get("task_chain") or []
        if not isinstance(chain, list):
            return errors + ["task_chain 不是数组"]
        for row in chain:
            if not isinstance(row, dict):
                errors.append("任务不是对象")
                continue
            if type(row.get("slot")) is not int or row["slot"] not in range(1, 7):
                errors.append("任务号位非法")
            quota = row.get("max_discs")
            if quota is not None and (type(quota) is not int or quota < 1):
                errors.append("任务配额原值非法")
            accepted = row.get("accepted")
            if accepted is not None and (not isinstance(accepted, list) or not all(isinstance(i, str) for i in accepted)):
                errors.append("accepted 不是字符串数组或 null")
        preset = preset_from_dict(raw)
        planned = preset.task_chain or (TaskJob(
            slot=preset.filter_slot, sets=preset.filter_sets, mainstats=preset.filter_mainstats,
            weights=dict(preset.weights), stage_require=dict(preset.stage_require),
            accepted=preset.accepted, min_useful_types=preset.min_useful_types,
            max_discs=preset.max_discs, required_substats=preset.required_substats,
        ),)
        if not any(j.accepted is not None for j in planned):
            errors.append("无可执行任务")
        if jobs is None:
            return errors
        if not isinstance(jobs, list) or len(jobs) != len(planned):
            return errors + ["任务数量与实际计划不符"]
        for index, (j, record) in enumerate(zip(planned, jobs)):
            sub = Preset(
                weights=j.weights, filter_slot=j.slot, filter_slots=(j.slot,), filter_mainstats=j.mainstats,
                stage_require=j.stage_require, min_useful_types=j.min_useful_types,
                skip_locked=preset.skip_locked,
                skip_equipped=preset.skip_equipped, skip_discarded=preset.skip_discarded,
                accepted=j.accepted, labels=preset.labels, required_substats=j.required_substats,
            )
            quota = j.max_discs if j.max_discs is not None else preset.max_discs
            expected = dict(
                job_index=index, slot=j.slot, sets=list(j.sets), mainstats=list(j.mainstats),
                accepted=None if j.accepted is None else list(j.accepted), max_discs=j.max_discs,
                quota_raw=quota, quota_effective=0 if j.accepted is None else max(1, quota),
                skipped=j.accepted is None, labels=snapshot(preset.labels), preset=snapshot(sub),
                preset_sha256=digest(sub), accepted_sha256=accepted_digest(j.accepted),
            )
            if chain and chain[index].get("id"):
                expected["job_id"] = str(chain[index]["id"])
            if any(record.get(k) != v for k, v in expected.items()):
                errors.append(f"任务 {index} 配置/顺序/配额与 YAML 不符")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        errors.append(f"计划结构错误：{type(exc).__name__}: {exc}")
    return errors


def disc_records(events) -> list[dict]:
    keys = ("started_at", "ended_at", "processed", "input_sent", "result", "outcome",
            "core", "inputs", "decisions", "preset_sha256", "accepted_sha256")
    return [dict(job_index=e.get("job_index"), disc_index=e.get("disc_index"),
                 event_id=e.get("event_id"), duration_ms=e.get("duration_ms"),
                 **{k: (e.get("details") or {}).get(k) for k in keys})
            for e in events if e.get("component") == "disc_total"]


def processed_count(events) -> int:
    return len({(e.get("job_index"), e.get("disc_index")) for e in events
                if e.get("component") == "disc_total"
                and (e.get("details") or {}).get("processed") is True})


def timing_record(events, root) -> dict:
    counts = Counter(e.get("component") for e in events)
    discs = [e for e in events if e.get("component") == "disc_total"]
    waits = []
    for e in events:
        if e.get("component") == "wait_total":
            d = e.get("details") or {}
            waits.append(dict(event_id=e.get("event_id"),
                              **{k: e.get(k) for k in ("job_index", "disc_index", "stage_level", "parent_id")},
                              **{k: d.get(k) for k in ("kind", "outcome", "trigger_input_id",
                                                     "upgrade_input_id", "resume_from_wait_id")}))
    parses = [e for e in events if e.get("component") == "parse_total"]
    upgrades = []
    for w in events:
        if w.get("component") != "wait_total" or (w.get("details") or {}).get("kind") != "upgrade_result":
            continue
        probes = {s.get("probe_event_id") for s in w["details"].get("samples", [])}
        upgrades.append({"wait_id": w["event_id"], "parse_event_ids": [
            e["event_id"] for e in parses if e.get("parent_id") in probes]})
    jobs = (root.get("details") or {}).get("jobs", []) if root else []
    return {
        "run_total_event_id": root.get("event_id") if root else None,
        "run_duration_ms": root.get("duration_ms") if root else None,
        "first_parse_event_id": parses[0]["event_id"] if parses else None,
        "disc_event_ids": [e["event_id"] for e in discs],
        "capture_count": counts["capture"], "png_save_count": counts["png_save"],
        "parse_total_count": counts["parse_total"], "waits": waits,
        "jobs": [{k: j.get(k) for k in ("job_index", "started_at", "prepared_at", "ended_at", "input_ids")}
                 for j in jobs],
        "initial_reads": [{"disc_event_id": d["event_id"], "parse_event_id": next(
            (p["event_id"] for p in parses
             if (p.get("job_index"), p.get("disc_index")) == (d.get("job_index"), d.get("disc_index"))), None)}
            for d in discs],
        "upgrade_results": upgrades,
        "capture_event_ids": [e["event_id"] for e in events if e.get("component") == "capture"],
        "png_save_event_ids": [e["event_id"] for e in events if e.get("component") == "png_save"],
    }


def coverage_record(events, run_id) -> dict:
    roots = [e for e in events if e.get("component") == "run_total"]
    root = roots[0] if len(roots) == 1 else {}
    inputs = (root.get("details") or {}).get("inputs", [])
    waits = [e for e in events if e.get("component") == "wait_total"]
    refs = {k: [] for k in COVERAGE_KEYS}

    def ref(*, job_index=None, disc_index=None, input_ids=(), event_ids=(), exception=None):
        ids = [eid for eid in event_ids if eid]
        return dict(run_id=run_id, job_index=job_index, disc_index=disc_index,
                    input_ids=list(input_ids), event_ids=ids,
                    log_refs=[f"{run_id}/{eid}" for eid in ids], exception=exception)

    for i in inputs:
        kind = i.get("kind")
        key = "upgrade" if kind == "upgrade_result" else kind
        if key not in refs or key == "maxed_refund":
            continue
        if i.get("status") != "completed":
            continue  # begin/未完成调用不能冒充已经发出的逻辑输入。
        # 逻辑调用已完成；游戏是否完成另看 result，不丢发出后的复核失败。
        matched = [w for w in waits if (w.get("details") or {}).get("trigger_input_id") == i.get("input_id")
                   or (key == "upgrade" and (w.get("details") or {}).get("upgrade_input_id") == i.get("input_id"))]
        refs[key].append(ref(job_index=i.get("job_index"), disc_index=i.get("disc_index"),
                             input_ids=(i.get("input_id"),),
                             event_ids=(i.get("disc_event_id"), *(w.get("event_id") for w in matched)),
                             exception=(i.get("result") or {}).get("exception") or i.get("exception")))
    first_ids = {w["event_id"] for w in waits if (w.get("details") or {}).get("outcome") == "interrupted"}
    first_ids.update((w.get("details") or {}).get("resume_from_wait_id") for w in waits
                     if (w.get("details") or {}).get("resume_from_wait_id"))
    for w in waits:
        d = w.get("details") or {}
        if w.get("event_id") not in first_ids:
            continue
        resumed = [e for e in waits if (e.get("details") or {}).get("resume_from_wait_id") == w.get("event_id")]
        refs["maxed_refund"].append(ref(
            job_index=w.get("job_index"), disc_index=w.get("disc_index"),
            input_ids=[e["details"].get("trigger_input_id") for e in resumed],
            event_ids=(w.get("event_id"), *(e.get("event_id") for e in resumed))))
    rd = root.get("details") or {}
    if rd.get("termination_kind") == "material_shortfall":
        refs["material_shortfall"].append(ref(event_ids=(root.get("event_id"),),
                                               exception=rd.get("exception")))
    return {k: dict(status="measured" if rows else "not_observed", count=len(rows), evidence_refs=rows)
            for k, rows in refs.items()}


def wait_times(samples) -> tuple:
    """最后同代次候选序列的实际时刻；生产没有观察尾段，不声称测到动画最低时间。"""
    if not samples:
        return None, None, None
    last = samples[-1]
    gen, key, reason = last.get("pause_generation"), last.get("state_key"), last.get("interrupt_reason")
    candidate = bool(last.get("ready")) or reason is not None
    if not candidate:
        return last.get("captured_at"), None, None
    start = len(samples) - 1
    while start > 0:
        prev = samples[start - 1]
        if (prev.get("pause_generation") != gen or prev.get("state_key") != key
                or prev.get("interrupt_reason") != reason
                or not (prev.get("ready") or prev.get("interrupt_reason"))):
            break
        start -= 1
    bad = samples[start - 1].get("captured_at") if start and samples[start - 1].get("pause_generation") == gen else None
    stable = samples[start + 1].get("captured_at") if len(samples) - start >= 2 else None
    return bad, samples[start].get("captured_at"), stable


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


#: §7.1 release_wait 对象的固定字段与策略来源取值。
_RELEASE_WAIT_FIELDS = (
    "action_id", "released_at_ns", "floor_ms", "policy_source",
    "requested_sleep_ms", "active_elapsed_ms",
)
_POLICY_SOURCES = ("default", "measured", "measurement")


def _release_wait_valid(body, released_at_ns: int) -> bool:
    """§7.1：执行输入的释放等待明细结构校验（六字段、与释放点一致、不越过剩余下限）。"""
    from zzz_ddtool.actionwait import ACTION_IDS

    if not isinstance(body, dict) or set(body) != set(_RELEASE_WAIT_FIELDS):
        return False
    action_id = body.get("action_id")
    if action_id is not None and action_id not in ACTION_IDS:
        return False
    if body.get("released_at_ns") != released_at_ns:
        return False
    floor = body.get("floor_ms")
    if type(floor) is not int or floor < 0:
        return False
    if body.get("policy_source") not in _POLICY_SOURCES:
        return False
    requested = body.get("requested_sleep_ms")
    if not _number(requested) or requested < 0 or requested > floor + 1:
        return False
    active = body.get("active_elapsed_ms")
    return _number(active) and active >= 0


def _interval(start, end, duration=None):
    if not (_number(start) and _number(end) and start <= end):
        return False
    return duration is None or (_number(duration) and abs((end - start) * 1000 - duration) < 0.1)


def validate_execution(events, *, run_id, jobs, discs, timing, coverage, processed) -> list[str]:
    """从原始 JSONL 核对报告；缺失/损坏按错误返回，绝不接受空 all() 的真值。"""
    errors = []

    def require(ok, message):
        if not ok:
            errors.append(message)

    try:
        roots = [e for e in events if e.get("component") == "run_total"]
        require(len(roots) == 1, "必须恰有一个 run_total")
        if len(roots) != 1:
            return errors
        root = roots[0]
        rd = root["details"]
        require(bool(events) and all(e.get("run_id") == run_id for e in events), "run_id 不一致")
        by_id = {e.get("event_id"): e for e in events}
        require(None not in by_id and len(by_id) == len(events), "事件 ID 丢失或重复")
        require(_interval(rd.get("started_at"), rd.get("ended_at"), root.get("duration_ms")), "运行区间缺失/不符")
        raw_jobs, raw_inputs = rd.get("jobs"), rd.get("inputs")
        require(isinstance(raw_jobs, list) and bool(raw_jobs) and jobs == raw_jobs, "任务记录缺失/与原事件不符")
        require(isinstance(raw_inputs, list), "实际输入记录缺失")
        if not isinstance(raw_jobs, list) or not isinstance(raw_inputs, list):
            return errors
        require([j.get("job_index") for j in raw_jobs] == list(range(len(raw_jobs))), "任务顺序不符")
        inputs = {i.get("input_id"): i for i in raw_inputs}
        require(None not in inputs and len(inputs) == len(raw_inputs), "输入 ID 丢失/重复")
        require(all(isinstance(i, str) and bool(i) for i in inputs), "输入 ID 非空字符串契约不符")
        calls = [i.get("call_id") for i in raw_inputs]
        require(all(type(c) is int and c > 0 for c in calls) and len(calls) == len(set(calls)), "真实调用 ID 丢失/重复")
        decisions = {}
        raw_discs = disc_records(events)
        require(isinstance(discs, list) and bool(discs) and discs == raw_discs, "逐盘记录缺失/与原事件不符")
        require(type(processed) is int and processed == processed_count(events) and processed > 0, "实际盘数不符")
        counted = [d for d in raw_discs if d.get("processed") is True]
        require(len(counted) == len({(d["job_index"], d["disc_index"]) for d in counted}), "同任务重复处理盘")
        for d in raw_discs:
            require(_interval(d.get("started_at"), d.get("ended_at"), d.get("duration_ms")), "盘区间缺失/不符")
            require(rd.get("started_at", math.inf) <= d.get("started_at", -math.inf)
                    <= d.get("ended_at", math.inf) <= rd.get("ended_at", -math.inf), "盘区间超出运行")
            require(type(d.get("processed")) is bool and isinstance(d.get("inputs"), list), "盘输入/处理状态缺失")
            own = [i for i in raw_inputs if i.get("disc_event_id") == d["event_id"]]
            require(d.get("inputs") == own, "盘与根输入不一致")
            if d.get("processed"):
                require(isinstance(d.get("core"), dict) and bool(d.get("decisions")), "已处理盘缺少盘面/决策")
            for choice in d.get("decisions") or []:
                cid, data = choice.get("decision_id"), choice.get("decision")
                require(isinstance(cid, str) and cid not in decisions, "决策 ID 丢失/重复")
                decisions[cid] = (d, choice)
                require(isinstance(data, dict) and {
                    "action", "reason", "score", "useful_types", "max_useful_types", "checkpoint", "origin", "pattern_id"
                } <= data.keys(), "Decision 字段不完整")
                require(isinstance(choice.get("disc"), dict) and {
                    "level", "substats", "rarity", "locked", "equipped", "discarded"
                } <= choice["disc"].keys(), "决策 Disc 字段不完整")
                require(d["started_at"] <= choice.get("at", -math.inf) <= d["ended_at"], "决策不在盘区间")
                require(choice.get("preset_sha256") == d.get("preset_sha256")
                        and choice.get("accepted_sha256") == d.get("accepted_sha256"), "决策配置身份不符")
        require(timing == timing_record(events, root), "计时引用缺失/与原事件不符")
        require(coverage == coverage_record(events, run_id), "实际覆盖缺失/改写/引用不符")
        for j in raw_jobs:
            ji = j.get("job_index")
            own = [d for d in counted if d["job_index"] == ji]
            require(type(j.get("processed")) is int and j["processed"] == len(own), "任务已处理数不符")
            require(j.get("skipped") is (j.get("accepted") is None), "未生成状态不符")
            require(j.get("preset_sha256") == digest(j.get("preset"))
                    and j.get("accepted_sha256") == accepted_digest(j.get("accepted")), "任务配置身份不符")
            quota = j.get("quota_effective")
            require(type(quota) is int and quota >= 0 and j["processed"] <= quota, "配额非法/越额")
            require(j.get("input_ids") == [i["input_id"] for i in raw_inputs if i.get("job_index") == ji], "任务输入丢失")
            require(_interval(j.get("started_at"), j.get("ended_at")), "任务区间缺失")
            require(rd["started_at"] <= j["started_at"] <= j["ended_at"] <= rd["ended_at"], "任务超出运行区间")
            if j.get("skipped"):
                require(not own and quota == 0 and not j.get("input_ids") and j.get("end_reason") == "skipped", "跳过任务发输入/计盘")
            else:
                require(_number(j.get("prepared_at")) and j["started_at"] <= j["prepared_at"] <= j["ended_at"], "任务准备区间缺失")
                if rd.get("runner_outcome") == "completed":
                    require(j.get("end_reason") in ("quota_reached", "exhausted"), "正常任务结束原因不符")
                    if j.get("end_reason") == "quota_reached":
                        require(j["processed"] == quota, "达到配额记录不符")
            for d in own:
                require(j["prepared_at"] <= d["started_at"] <= d["ended_at"] <= j["ended_at"], "盘不在任务处理区间")
                require(d.get("preset_sha256") == j.get("preset_sha256")
                        and d.get("accepted_sha256") == j.get("accepted_sha256"), "盘配置身份不符")
        required_input = {
            "input_id", "call_id", "api", "kind", "phase", "job_index", "disc_index", "disc_event_id",
            "started_at", "ended_at", "status", "exception", "hwnd", "pid", "client_size", "pause_generation",
            "decision_id", "disc", "preset_sha256", "accepted_sha256", "target", "result", "wait_ids",
            "released_at_ns", "release_wait",
        }
        for i in raw_inputs:
            require(required_input <= i.keys(), "输入字段缺失")
            require(_interval(i.get("started_at"), i.get("ended_at")), "输入区间缺失")
            require(type(i.get("hwnd")) is int and type(i.get("pid")) is int
                    and isinstance(i.get("client_size"), list) and len(i["client_size"]) == 2, "输入绑定缺失")
            require(i.get("status") in ("completed", "failed", "stopped"), "输入无真实收尾")
            require(i.get("api") in ("click_client", "wheel_client", "send_wheel_event", "press_key", "press_key_once"), "未知实际输入 API")
            released = i.get("released_at_ns")
            if released is None:
                require(i.get("release_wait") is None, "未释放输入却有释放等待明细")
            else:
                require(type(released) is int and released >= 0, "释放点非整数")
                require(i.get("started_at", math.inf) <= released / 1e9 <= i.get("ended_at", -math.inf),
                        "释放点不在输入区间")
                require(_release_wait_valid(i.get("release_wait"), released), "释放等待明细不符")
            ji = i.get("job_index")
            require(type(ji) is int and 0 <= ji < len(raw_jobs), "输入没有有效任务")
            if type(ji) is not int or not 0 <= ji < len(raw_jobs):
                continue
            j = raw_jobs[ji]
            require(j["started_at"] <= i["started_at"] <= i["ended_at"] <= j["ended_at"], "输入超出任务区间")
            if i.get("phase") == "disc":
                d = by_id.get(i.get("disc_event_id"), {})
                require(d.get("component") == "disc_total" and d.get("job_index") == ji
                        and d.get("disc_index") == i.get("disc_index"), "输入盘关联不符")
                dd = d.get("details") or {}
                require(dd.get("started_at", math.inf) <= i["started_at"] <= i["ended_at"]
                        <= dd.get("ended_at", -math.inf), "输入超出本盘区间")
                if i.get("kind") == "select_disc":
                    require(isinstance(i.get("target"), dict), "选择目标缺失")
                    target = i.get("target") or {}
                    require(i.get("api") == "click_client" and target.get("global_index") == i.get("disc_index")
                            and (target.get("x"), target.get("y")) == (i.get("x"), i.get("y")), "选择实际调用与目标不符")
                    if (d.get("details") or {}).get("processed"):
                        require(isinstance(i.get("disc"), dict), "选择完成盘面缺失")
                else:
                    pair = decisions.get(i.get("decision_id"))
                    require(pair is not None, "业务输入缺少决策")
                    if pair:
                        require(pair[0]["event_id"] == i.get("disc_event_id")
                                and pair[1]["at"] <= i["started_at"] and pair[1]["disc"] == i.get("disc"), "输入决策/盘面不符")
                        require(i.get("preset_sha256") == j.get("preset_sha256")
                                and i.get("accepted_sha256") == j.get("accepted_sha256"), "输入配置身份不符")
                        preceding = [c for c in dd.get("decisions", []) if c.get("at", math.inf) <= i["started_at"]]
                        require(bool(preceding) and preceding[-1].get("decision_id") == i.get("decision_id"), "输入未关联最新决策")
                if i.get("kind") in ("label_save", "label_discard", "label_unlock_before_discard", "clear_discard"):
                    require(i.get("api") == "press_key_once"
                            and i.get("vk") == (0x54 if i["kind"] in ("label_save", "label_unlock_before_discard") else 0x52), "标签实际按键不符")
                    result = i.get("result") or {}
                    require(type(result.get("verified")) is bool, "标签后置复核丢失")
                    if rd.get("runner_outcome") == "completed":
                        require(result.get("verified") is True and result.get("actual") == (i.get("target") or {}).get("value"), "标签结果不符")
                if i.get("kind") == "upgrade_result":
                    target, result = i.get("target") or {}, i.get("result") or {}
                    require(target.get("before_level") == (i.get("disc") or {}).get("level")
                            and type(target.get("expected_level")) is int, "升级前实读/期望等级缺失")
                    if rd.get("runner_outcome") == "completed":
                        require(result.get("verified") is True and type(result.get("actual_level")) is int
                                and result["actual_level"] >= target["expected_level"], "升级实读结果不符")
            else:
                require(i.get("phase") in ("preparation", "recovery") and i.get("disc_index") is None
                        and i.get("decision_id") is None, "准备/恢复伪造盘关联")
            require(isinstance(i.get("wait_ids"), list) and all(w in by_id for w in i["wait_ids"]), "输入等待引用缺失")
        for w in events:
            if w.get("component") != "wait_total":
                continue
            wd = w.get("details") or {}
            i = inputs.get(wd.get("trigger_input_id"))
            require(i is not None and i.get("status") == "completed", "等待未关联实际完成输入")
            if not i:
                continue
            require((w.get("job_index"), w.get("disc_index"), w.get("parent_id"))
                    == (i.get("job_index"), i.get("disc_index"), i.get("disc_event_id")), "等待作用域不符")
            require(i.get("kind") == ("refund_confirm" if wd.get("resume_from_wait_id") else wd.get("kind")), "等待与实际输入角色不符")
            require(
                w["event_id"] in i.get("wait_ids", [])
                and i.get("released_at_ns") is not None
                and abs(wd.get("started_at", math.inf) - i["released_at_ns"] / 1e9) <= 0.000001,
                "等待起点/输入关联不符",
            )
            require(_interval(wd.get("started_at"), wd.get("ended_at"), w.get("duration_ms")), "等待区间不符")
            require(wd["ended_at"] <= rd["ended_at"], "等待超出运行区间")
            samples = wd.get("samples")
            require(isinstance(samples, list), "等待样本丢失")
            for sample in samples or []:
                p = by_id.get(sample.get("probe_event_id"), {})
                require(_interval(sample.get("captured_at"), sample.get("probe_ended_at"))
                        and wd["started_at"] <= sample["captured_at"] <= sample["probe_ended_at"] <= wd["ended_at"], "样本超出等待区间")
                require(p.get("component") == "probe" and p.get("parent_id") == w["event_id"]
                        and p.get("job_index") == w.get("job_index") and p.get("disc_index") == w.get("disc_index")
                        and p.get("stage_level") == w.get("stage_level"), "样本 probe 关联不符")
                require(all((p.get("details") or {}).get(k) == v for k, v in sample.items() if k != "probe_event_id"), "样本与 probe 不符")
            require(tuple(wd.get(k) for k in ("t_bad", "t_ready", "t_stable")) == wait_times(samples), "就绪时刻不符")
            if wd.get("outcome") == "succeeded":
                require(len(samples or []) >= 2 and wd.get("t_stable") is not None, "成功等待缺双帧证据")
            if wd.get("kind") == "upgrade_result":
                upgrade = inputs.get(wd.get("upgrade_input_id"))
                require(upgrade is not None and upgrade.get("kind") == "upgrade_result", "升级输入关联伪造")
                if wd.get("resume_from_wait_id"):
                    first = by_id.get(wd["resume_from_wait_id"], {})
                    require(i.get("kind") == "refund_confirm" and first.get("component") == "wait_total"
                            and first.get("parent_id") == w.get("parent_id")
                            and (first.get("details") or {}).get("upgrade_input_id") == wd.get("upgrade_input_id"), "返还双等待关联不符")
        require(sum(j.get("processed", 0) for j in raw_jobs) == processed, "任务盘数总和不符")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        errors.append(f"执行证据结构错误：{type(exc).__name__}: {exc}")
    return errors
