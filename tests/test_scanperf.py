"""性能记录器（Spec §7.1、§7.1.1、§8 P1/P2 基础）。

覆盖：顶层字段固定、run_id/JSONL 写读、父子关联与计数回流、模拟计时、
无活动会话 no-op、记录失败不改变调用方、截断/损坏读取判定，以及依赖方向。
真实 Runner 的退出/输入关联由步骤 6 覆盖，本文件只验基础。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import discocr, scanperf


@pytest.fixture(autouse=True)
def _clean_session():
    """每个用例前后都清掉模块级会话，防止泄漏到其它测试文件里的真实调用。"""
    scanperf.end_session()
    yield
    scanperf.end_session()


def _session(tmp_path: Path, tag: str = "tag", run_id: str = "R") -> tuple[scanperf.PerfSession, Path]:
    path = tmp_path / "perf" / f"{run_id}.jsonl"
    session = scanperf.begin_session(tag, run_id=run_id, path=path)
    return session, path


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# 记不记、记什么：顶层字段固定（§7.1）
# ---------------------------------------------------------------------------


def test_no_session_record_is_noop(tmp_path, monkeypatch) -> None:
    """没有活动会话时不凭空创建残缺 run 记录。"""
    monkeypatch.setattr(scanperf, "log_dir", lambda: tmp_path / "log")
    assert scanperf.current_session() is None
    assert scanperf.record_event(scanperf.PerfEvent("capture", 1.0)) is None
    assert scanperf.new_event_id() is None
    assert not (tmp_path / "log").exists()


def test_no_session_timed_span_is_detached() -> None:
    """无会话时上下文管理器仍可用（游离句柄），只是不落盘。"""
    with scanperf.timed("parse_total", details={"profile_id": None}) as span:
        assert span.event_id is None
        span.set(template_hits=0, ocr_lines=0)
    assert scanperf.current_session() is None


def test_event_top_level_fields_are_fixed(tmp_path) -> None:
    _, path = _session(tmp_path)
    with scanperf.timed("parse_total", details={"profile_id": "w", "reject_reason": None}):
        pass
    scanperf.end_session()
    records = _lines(path)
    assert len(records) == 1
    record = records[0]
    assert list(record) == [
        "schema_version",
        "run_id",
        "event_id",
        "parent_id",
        "job_index",
        "disc_index",
        "stage_level",
        "component",
        "duration_ms",
        "details",
    ]
    assert record["schema_version"] == 1
    assert record["run_id"] == "R"
    assert record["event_id"] == "e00000001"
    assert record["parent_id"] is None
    assert record["job_index"] is None and record["disc_index"] is None
    assert record["stage_level"] is None
    assert record["details"]["trace_tag"] == "tag"


def test_trace_tag_written_on_every_event(tmp_path) -> None:
    _session(tmp_path, tag="20261010-000000", run_id="R2")
    for component in ("capture", "crop", "recognize_batch"):
        scanperf.record_event(scanperf.PerfEvent(component, 1.0))
    scanperf.end_session()
    assert {r["details"]["trace_tag"] for r in _lines(tmp_path / "perf" / "R2.jsonl")} == {
        "20261010-000000"
    }


@pytest.mark.parametrize("component", ["", "Capture", "wait", "unknown"])
def test_unknown_component_rejected(component) -> None:
    with pytest.raises(ValueError):
        scanperf.PerfEvent(component, 1.0)
    with pytest.raises(ValueError):
        with scanperf.timed(component):
            pass


@pytest.mark.parametrize("duration", [-1.0, float("nan"), float("inf"), True, "1"])
def test_bad_duration_rejected(duration) -> None:
    with pytest.raises((ValueError, TypeError)):
        scanperf.PerfEvent("capture", duration)


def test_indices_must_be_non_negative_int_or_null() -> None:
    with pytest.raises(ValueError):
        scanperf.PerfEvent("capture", 1.0, job_index=-1)
    with pytest.raises(ValueError):
        scanperf.PerfEvent("capture", 1.0, disc_index=True)
    assert scanperf.PerfEvent("capture", 1.0, job_index=0, disc_index=2).disc_index == 2


# ---------------------------------------------------------------------------
# §8 P1：解析耗时只算 parse_total，不与子区间相加
# ---------------------------------------------------------------------------


def test_p1_parse_total_not_summed_with_children(tmp_path) -> None:
    """parse_total 含 OCR 100ms、模板 10ms → 汇总解析耗时 120ms，不是 230ms。"""
    _session(tmp_path)
    parse_id = scanperf.new_event_id()
    scanperf.record_event(
        scanperf.PerfEvent(
            "recognize_batch",
            100.0,
            details={"roi_ids": ["level"], "batch_size": 4, "profile_id": "w"},
            parent_id=parse_id,
        )
    )
    scanperf.record_event(
        scanperf.PerfEvent(
            "template_match",
            10.0,
            details={
                "layout": "detail",
                "profile_id": "w",
                "roi_ids": ["level"],
                "template_hits": 1,
                "matches": [],
            },
            parent_id=parse_id,
        )
    )
    scanperf.record_event(
        scanperf.PerfEvent(
            "parse_total",
            120.0,
            details={"profile_id": "w", "reject_reason": None},
            event_id=parse_id,
        )
    )
    scanperf.end_session()
    events = _lines(tmp_path / "perf" / "R.jsonl")
    assert scanperf.total_duration_ms(events, "parse_total") == pytest.approx(120.0)
    # 直接相加会把父区间和子区间重复计入（120 + 100 + 10 = 230），这正是要避免的
    naive = sum(e["duration_ms"] for e in events)
    assert naive == pytest.approx(230.0)


def test_p1_capture_and_png_save_are_separate(tmp_path) -> None:
    _session(tmp_path)
    with scanperf.timed("capture", details={"method": "printwindow", "hwnd": 1}):
        pass
    with scanperf.timed("png_save", details={"source": "trace", "path": "x.png", "saved": True}):
        pass
    scanperf.end_session()
    totals = scanperf.component_totals(_lines(tmp_path / "perf" / "R.jsonl"))
    assert set(totals) == {"capture", "png_save"}


# ---------------------------------------------------------------------------
# 父子关联与计数回流（§7.1.1）
# ---------------------------------------------------------------------------


def test_span_nesting_sets_parent_chain(tmp_path) -> None:
    _session(tmp_path)
    with scanperf.timed("parse_total", details={"profile_id": None, "reject_reason": None}) as outer:
        with scanperf.timed("legacy_parse", details={"parser": "detail", "reject_reason": "x", "profile_id": None}) as inner:
            assert inner.parent_id == outer.event_id
    scanperf.end_session()
    records = {r["component"]: r for r in _lines(tmp_path / "perf" / "R.jsonl")}
    assert records["parse_total"]["parent_id"] is None
    assert records["legacy_parse"]["parent_id"] == records["parse_total"]["event_id"]


def test_parse_total_aggregates_child_counts(tmp_path) -> None:
    """template_hits / ocr_lines 由子事件回流到 parse_total（§7.1.1）。"""
    _session(tmp_path)
    with scanperf.timed("parse_total", details={"profile_id": "w", "reject_reason": None}) as span:
        for hits in (1, 1, 0):
            scanperf.record_event(
                scanperf.PerfEvent(
                    "template_match",
                    1.0,
                    details={
                        "layout": "detail",
                        "profile_id": "w",
                        "roi_ids": ["x"],
                        "template_hits": hits,
                        "matches": [],
                    },
                    parent_id=span.event_id,
                )
            )
        for size in (2, 3):
            scanperf.record_event(
                scanperf.PerfEvent(
                    "recognize_batch",
                    1.0,
                    details={"roi_ids": ["y"], "batch_size": size, "profile_id": "w"},
                    parent_id=span.event_id,
                )
            )
    scanperf.end_session()
    parse = next(r for r in _lines(tmp_path / "perf" / "R.jsonl") if r["component"] == "parse_total")
    assert parse["details"]["template_hits"] == 2
    assert parse["details"]["ocr_lines"] == 5


def test_child_counts_do_not_leak_into_unrelated_parent(tmp_path) -> None:
    """没有父子关系的 template_match 不会污染 parse_total 的计数。"""
    _session(tmp_path)
    with scanperf.timed(
        "parse_total",
        # 调用点会预置 0（与 ocr.parse_disc 一致），无子事件时保持 0。
        details={"profile_id": None, "reject_reason": None, "template_hits": 0, "ocr_lines": 0},
    ):
        pass
    scanperf.record_event(
        scanperf.PerfEvent(
            "template_match",
            1.0,
            details={"layout": "detail", "profile_id": None, "roi_ids": [], "template_hits": 9, "matches": []},
            parent_id=None,
        )
    )
    scanperf.end_session()
    parse = next(r for r in _lines(tmp_path / "perf" / "R.jsonl") if r["component"] == "parse_total")
    assert parse["details"]["template_hits"] == 0
    assert parse["details"]["ocr_lines"] == 0


def test_scope_inheritance_and_outer_span_wins(tmp_path) -> None:
    session, path = _session(tmp_path)
    scanperf.set_scope(job_index=3, disc_index=7)
    with scanperf.timed("disc_total", details={"input_sent": False}, disc_index=9, stage_level=2):
        with scanperf.timed("capture", details={"method": "printwindow", "hwnd": 1}):
            pass
    scanperf.end_session()
    records = {r["component"]: r for r in _lines(path)}
    assert (records["disc_total"]["job_index"], records["disc_total"]["disc_index"]) == (3, 9)
    assert records["disc_total"]["stage_level"] == 2
    # 外层区间已定 disc/stage，内层继承它而不是会话作用域
    assert records["capture"]["disc_index"] == 9
    assert records["capture"]["stage_level"] == 2
    assert records["capture"]["job_index"] == 3


def test_run_id_is_unique_per_session(tmp_path) -> None:
    assert scanperf.make_run_id("t") != scanperf.make_run_id("t")
    assert scanperf.make_run_id("t").startswith("t-")
    first, _ = _session(tmp_path, run_id="A")
    second, _ = _session(tmp_path, run_id="B")
    assert first.run_id == "A"
    assert scanperf.current_session() is second


def test_default_session_path_sits_under_log_perf(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scanperf, "log_dir", lambda: tmp_path / "log")
    assert scanperf.perf_dir() == tmp_path / "log" / "perf"
    session = scanperf.begin_session("tag-default")
    assert session.path.parent == tmp_path / "log" / "perf"
    assert session.path.name == f"{session.run_id}.jsonl"
    scanperf.end_session()


# ---------------------------------------------------------------------------
# 异常与 outcome（§7.1）
# ---------------------------------------------------------------------------


def test_failed_span_records_outcome_and_exception(tmp_path) -> None:
    _session(tmp_path)
    with pytest.raises(RuntimeError):
        with scanperf.timed("capture", details={"method": None, "hwnd": None}):
            raise RuntimeError("boom")
    scanperf.end_session()
    record = _lines(tmp_path / "perf" / "R.jsonl")[0]
    assert record["details"]["outcome"] == "failed"
    assert record["details"]["exception"]["type"] == "RuntimeError"


def test_runner_stopped_maps_to_stopped_outcome(tmp_path) -> None:
    from zzz_ddtool.hotkeys import RunnerStopped

    _session(tmp_path)
    with pytest.raises(RunnerStopped):
        with scanperf.timed("parse_total", details={"profile_id": None, "reject_reason": None}):
            raise RunnerStopped("F10")
    scanperf.end_session()
    assert _lines(tmp_path / "perf" / "R.jsonl")[0]["details"]["outcome"] == "stopped"


def test_caller_can_set_returned_none_outcome(tmp_path) -> None:
    _session(tmp_path)
    with scanperf.timed("parse_total", details={"profile_id": None, "reject_reason": None}) as span:
        span.set(outcome="returned_none")
    scanperf.end_session()
    assert _lines(tmp_path / "perf" / "R.jsonl")[0]["details"]["outcome"] == "returned_none"


def test_record_after_close_is_dropped(tmp_path) -> None:
    session, path = _session(tmp_path)
    session.close()
    session.record(scanperf.PerfEvent("capture", 1.0))
    assert path.exists()
    assert _lines(path) == []


# ---------------------------------------------------------------------------
# 记录失败不改变调用方（§7.1）
# ---------------------------------------------------------------------------


def test_write_failure_does_not_raise_and_flags_error(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    session = scanperf.begin_session("tag", run_id="R", path=blocker / "perf" / "R.jsonl")
    assert session.write_error is not None
    # 记录不抛，调用方照常
    with scanperf.timed("capture", details={"method": None, "hwnd": None}) as span:
        assert span.event_id is not None
    scanperf.record_event(scanperf.PerfEvent("png_save", 1.0, details={"source": "trace", "path": "x", "saved": True}))
    scanperf.end_session()
    assert session.write_error is not None


def test_nonserializable_detail_does_not_raise(tmp_path) -> None:
    session, path = _session(tmp_path)
    scanperf.record_event(
        scanperf.PerfEvent("capture", 1.0, details={"method": object(), "hwnd": None})
    )
    assert session.write_error is not None
    assert _lines(path) == []


# ---------------------------------------------------------------------------
# JSONL 读取：截断 vs 中间损坏（§7.1）
# ---------------------------------------------------------------------------


def _write_raw(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_reader_reports_truncated_tail(tmp_path) -> None:
    path = tmp_path / "R.jsonl"
    good = json.dumps(
        {
            "schema_version": 1,
            "run_id": "R",
            "event_id": "e1",
            "parent_id": None,
            "job_index": None,
            "disc_index": None,
            "stage_level": None,
            "component": "capture",
            "duration_ms": 1.0,
            "details": {},
        }
    )
    _write_raw(path, good + "\n" + '{"component":"png_sav')  # 末尾半行
    result = scanperf.read_perf_file(path)
    assert len(result.events) == 1
    assert result.evidence_complete is False
    assert result.discarded_bytes > 0
    assert result.error is None


def test_reader_reports_middle_corruption(tmp_path) -> None:
    path = tmp_path / "R.jsonl"
    rows = []
    for i in range(3):
        rows.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "R",
                    "event_id": f"e{i}",
                    "parent_id": None,
                    "job_index": None,
                    "disc_index": None,
                    "stage_level": None,
                    "component": "capture",
                    "duration_ms": 1.0,
                    "details": {},
                }
            )
        )
    _write_raw(path, rows[0] + "\n" + "{oops" + "\n" + rows[2] + "\n")
    result = scanperf.read_perf_file(path)
    assert result.evidence_complete is False
    assert result.error is not None
    assert len(result.events) == 1


def test_reader_flags_unknown_component(tmp_path) -> None:
    path = tmp_path / "R.jsonl"
    _write_raw(path, json.dumps({"component": "bogus"}) + "\n")
    result = scanperf.read_perf_file(path)
    assert result.evidence_complete is False
    assert result.events == ()


def test_reader_missing_file_is_incomplete(tmp_path) -> None:
    result = scanperf.read_perf_file(tmp_path / "nope.jsonl")
    assert result.evidence_complete is False
    assert result.error is not None


def test_complete_file_is_evidence_complete(tmp_path) -> None:
    _, path = _session(tmp_path)
    with scanperf.timed("capture", details={"method": "printwindow", "hwnd": 1}):
        pass
    scanperf.end_session()
    result = scanperf.read_perf_file(path)
    assert result.evidence_complete is True
    assert result.discarded_bytes == 0


# ---------------------------------------------------------------------------
# 必需 details 键（§7.1、§7.1.1）
# ---------------------------------------------------------------------------


def test_missing_detail_keys_helper() -> None:
    assert scanperf.missing_detail_keys(
        "legacy_parse", {"parser": "detail", "reject_reason": None, "profile_id": None}
    ) == []
    missing = scanperf.missing_detail_keys("template_match", {})
    assert set(missing) == set(scanperf.REQUIRED_DETAIL_KEYS["template_match"])


# ---------------------------------------------------------------------------
# 接入：ocr / discocr（§7.1、§7.1.1）
# ---------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self, texts, scores):
        self._texts = texts
        self._scores = scores

    def text_rec(self, _input):
        return SimpleNamespace(txts=self._texts, scores=self._scores)


def test_recognize_bands_emits_crop_and_batch(tmp_path, monkeypatch) -> None:
    _session(tmp_path)
    monkeypatch.setattr(discocr, "_get_engine", lambda: _FakeEngine(["等级00"], [1.0]))
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    result = discocr._recognize_bands(
        image, [(10, 20, 40, 40)], roi_ids=["level"], profile_id="warehouse-1920x1080", layout="detail"
    )
    assert [text.text for text in result] == ["等级00"]
    scanperf.end_session()
    records = {r["component"]: r for r in _lines(tmp_path / "perf" / "R.jsonl")}
    assert set(records) == {"crop", "recognize_batch"}
    assert records["recognize_batch"]["details"]["batch_size"] == 1
    assert records["recognize_batch"]["details"]["roi_ids"] == ["level"]
    assert records["crop"]["details"]["rects"] == [[10, 20, 40, 40]]
    assert records["crop"]["details"]["frame_size"] == [1920, 1080]


def test_template_text_emits_hits_and_misses(tmp_path, monkeypatch) -> None:
    import numpy as np

    _session(tmp_path)
    array = np.full((10, 20), 255.0, np.float32)
    entry = discocr._TemplateEntry(
        template_id="t-level",
        profile_id="w",
        kind="level",
        text="等级03/15",
        match_min_score=0.9,
        match_min_gap=0.1,
        array=array,
    )
    profile = discocr._Profile("w", "detail", (1920, 1080), 0.90, ())
    assets = discocr._Assets(root=Path("."), identity="stub", profiles=(profile,), templates={("w", "level"): (entry,)})
    field = discocr._Field("level", "level", (0, 0, 20, 10), None)
    roi = np.full((10, 20), 255.0, np.float32)
    text = discocr._template_text(assets, profile, field, roi)
    assert text is not None and text.text == "等级03/15"
    # 未命中的 ROI
    miss = discocr._template_text(assets, profile, field, np.zeros((10, 20), np.float32))
    assert miss is None
    scanperf.end_session()
    hits = [r for r in _lines(tmp_path / "perf" / "R.jsonl") if r["component"] == "template_match"]
    assert [h["details"]["template_hits"] for h in hits] == [1, 0]
    assert hits[0]["details"]["matches"] == [
        {"roi_id": "level", "template_id": "t-level", "accepted": True}
    ]
    assert hits[1]["details"]["matches"] == [
        {"roi_id": "level", "template_id": None, "accepted": False}
    ]


def test_template_text_record_false_emits_nothing(tmp_path) -> None:
    """布局判别用的模板尝试不进 template_match 统计（§7.1.1）。"""
    _session(tmp_path)
    import numpy as np

    array = np.full((10, 20), 255.0, np.float32)
    entry = discocr._TemplateEntry("t", "w", "level", "等级03/15", 0.9, 0.1, array)
    profile = discocr._Profile("w", "detail", (1920, 1080), 0.90, ())
    assets = discocr._Assets(root=Path("."), identity="s", profiles=(profile,), templates={("w", "level"): (entry,)})
    field = discocr._Field("level", "level", (0, 0, 20, 10), None)
    text = discocr._template_text(assets, profile, field, np.full((10, 20), 255.0, np.float32), record=False)
    assert text is not None
    scanperf.end_session()
    assert tmp_path / "perf" / "R.jsonl"  # 文件建了但无事件
    assert _lines(tmp_path / "perf" / "R.jsonl") == []


def test_capture_and_png_save_wired(tmp_path, monkeypatch) -> None:
    """capture.grab_game_image / save_image 的计时接入不改返回值。"""
    from zzz_ddtool import capture

    _session(tmp_path)
    image = QImage(64, 64, QImage.Format.Format_RGB32)
    monkeypatch.setattr(capture, "_restore_window", lambda hwnd: None)
    monkeypatch.setattr(capture, "_print_window", lambda hwnd: image)
    monkeypatch.setattr(capture, "_looks_unusable", lambda img: False)
    monkeypatch.setattr(capture, "retain_captures", lambda: [])
    got_image, method, target = capture.grab_game_image(123)
    assert (method, target) == ("printwindow", 123)
    dest = capture.save_image(image, 123, "printwindow", name="probe", dest_dir=tmp_path / "cap")
    assert dest.exists()
    scanperf.end_session()
    records = _lines(tmp_path / "perf" / "R.jsonl")
    by_component = {r["component"]: r for r in records}
    assert by_component["capture"]["details"]["method"] == "printwindow"
    assert by_component["capture"]["details"]["hwnd"] == 123
    assert by_component["png_save"]["details"]["source"] == "capture_save_image"
    assert by_component["png_save"]["details"]["saved"] is True


# ---------------------------------------------------------------------------
# 依赖方向（§2.2）：scanperf 只依赖标准库与 paths
# ---------------------------------------------------------------------------


def test_scanperf_dependencies_are_stdlib_and_paths() -> None:
    source = Path(scanperf.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed_std = {
        "__future__",
        "json",
        "math",
        "threading",
        "time",
        "uuid",
        "contextlib",
        "dataclasses",
        "pathlib",
        "typing",
    }
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in allowed_std:
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root in allowed_std:
                continue
            if module == "zzz_ddtool.paths":
                continue
            offenders.append(module)
    assert offenders == [], f"scanperf 不应依赖：{offenders}"
