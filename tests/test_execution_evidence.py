"""真实 Runner/input/wait 控制流产出证据；游戏、原生输入、时钟是边界替身。"""
from unittest.mock import Mock

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import loground, runevidence, runner as rm, scanperf, wininput
from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import LabelPolicy, Preset
from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.traversal import GridCell, TraversalState


@pytest.fixture(autouse=True)
def close_round_after_test():
    """用例收尾释放轮次占用：run() 真的开了轮次，别把它留给后面的用例。"""
    yield
    loground.close_active()


@pytest.mark.parametrize("mode", ["already_discarded", "label_success", "label_failure"])
def test_real_runner_records_selection_and_failed_label_without_fabricated_ids(
    monkeypatch, sim_time, tmp_path, mode,
):
    fail_label = mode == "label_failure"
    needs_label = mode != "already_discarded"
    frame = QImage(1920, 1080, QImage.Format.Format_RGB32)
    frame.fill(0)
    observed = Disc(0, (Substat("攻击力"), Substat("暴击率"), Substat("生命值")),
                    rarity="S", discarded=not needs_label)
    job = TaskJob(slot=1, job_id="j1", accepted=(), max_discs=1)
    skipped = TaskJob(slot=2, job_id="j2", accepted=None)
    preset = Preset(task_chain=(job, skipped), skip_discarded=not needs_label, ocr_retry=1,
                    labels=LabelPolicy(discard=True, save=False))
    # run() 自己开轮次并建 trace/stats：把日志根钉在临时目录，性能文件即本轮 perf.jsonl。
    loground.use_test_context("execution-evidence", root=tmp_path / "log_test")
    runner = rm.Runner(preset, hotkeys=False)
    runner._hwnd, runner._pid = 1, 123
    monkeypatch.setattr(runner, "_set_status", Mock())
    monkeypatch.setattr(runner, "_finish_stats", lambda *_a: "")
    begin_session = scanperf.begin_session
    monkeypatch.setattr(scanperf, "begin_session", lambda *a, **k: begin_session(*a, clock=sim_time.now, **k))
    monkeypatch.setattr(rm, "client_size", lambda _h: (1920, 1080))
    monkeypatch.setattr(rm, "window_pid", lambda _h: 123)
    monkeypatch.setattr(wininput.time, "monotonic_ns", lambda: int(sim_time.now()*1e9))
    native = Mock()
    native.MapVirtualKeyW.return_value = 1
    native.SendInput.side_effect = lambda count, *_a: count
    monkeypatch.setattr(wininput, "user32", native)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    monkeypatch.setattr(wininput, "client_to_screen", lambda _h, x, y: (x, y))
    monkeypatch.setattr(wininput, "is_foreground", lambda _h: True)
    monkeypatch.setattr(wininput, "uniform", lambda a, b: (a+b)/2)
    monkeypatch.setattr(wininput, "randint", lambda *_a: 0)

    def grab(*_a, **_k):
        with scanperf.timed("capture", details=dict(method="printwindow", hwnd=1)):
            sim_time.advance(0.001)
        return frame, "drive_disc", "printwindow", 1

    def parse(_frame):
        with scanperf.timed("parse_total", details=dict(profile_id="fixture", reject_reason=None,
                                                        template_hits=0, ocr_lines=3)):
            sim_time.advance(0.005)
        return observed

    monkeypatch.setattr(runner, "_grab", grab)
    monkeypatch.setattr(rm, "parse_disc", parse)
    monkeypatch.setattr(runner, "_tile_selected_for_cell", lambda *_a, **_k: True)
    monkeypatch.setattr(rm, "is_page", lambda *_a: True)
    reads = {"count": 0}

    def label(_frame):
        reads["count"] += 1
        # 无热键控制器时代次为 None：两次底层检查均须复验。
        return {"locked": False, "discarded": False if reads["count"] <= 3 else (None if fail_label else True)}

    monkeypatch.setattr(rm, "read_action_state_fast", label)

    def job_body(actual_job, _replay):
        runner._job_started_at = runner._processed
        runner._job_max_discs = runner._job_limit(actual_job)
        sim_time.advance(0.02)
        runner._job_records[-1]["prepared_at"] = sim_time.now()
        state = TraversalState(job_id=actual_job.job_id, filter_signature="fixture")
        runner._state = state
        cell = GridCell(global_index=3, global_row0=0, global_col0=3, screen_row0=1,
                        screen_col0=3, center_x=400, center_y=450, level=0)
        runner._process_cell(1, actual_job, state, cell, runner._sub_preset(actual_job), set(), {})

    monkeypatch.setattr(runner, "_run_job", job_body)
    runner.run()
    path = loground.current().perf_path
    assert path.parent.parent == tmp_path / "log_test"  # 性能文件落在本轮目录里
    assert path.is_file()
    read = scanperf.read_perf_file(path)
    assert read.evidence_complete
    events = list(read.events)
    root = next(e for e in events if e["component"] == "run_total")
    disc = next(e for e in events if e["component"] == "disc_total")
    inputs = root["details"]["inputs"]
    assert len({i["input_id"] for i in inputs}) == len(inputs)
    assert [i["kind"] for i in inputs] == (["select_disc", "label_discard"] if needs_label else ["select_disc"])
    assert disc["details"]["inputs"] == inputs
    assert len(disc["details"]["decisions"][0]["decision"]) == 8
    coverage = runevidence.coverage_record(events, root["run_id"])
    if fail_label:
        assert runner.outcome == "failed"
        label_input = inputs[-1]
        assert label_input["status"] == "completed"  # 发键完成后，复核失败仍有输入证据。
        assert label_input["result"]["verified"] is False
        assert label_input["result"]["exception"]["type"] == "RunnerError"
        assert coverage["label_discard"]["count"] == 1
        assert coverage["label_discard"]["evidence_refs"][0]["input_ids"] == [label_input["input_id"]]
    else:
        assert runner.outcome == "completed"
        assert len(root["details"]["jobs"]) == 2 and root["details"]["jobs"][1]["skipped"]
        assert coverage["label_discard"]["count"] == int(needs_label)
        assert all(c["status"] == "not_observed" for k, c in coverage.items() if k != "label_discard")
        errors = runevidence.validate_execution(events, run_id=root["run_id"],
                    jobs=root["details"]["jobs"], discs=runevidence.disc_records(events),
                    timing=runevidence.timing_record(events, root), coverage=coverage,
                    processed=runevidence.processed_count(events))
        assert errors == []
    wait = next(e for e in events if e["component"] == "wait_total")
    assert wait["job_index"] == 0 and wait["disc_index"] == 3
    assert wait["details"]["trigger_input_id"] == inputs[0]["input_id"]
    assert wait["details"]["t_ready"] is not None and wait["details"]["t_stable"] is not None
