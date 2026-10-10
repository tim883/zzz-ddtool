"""offline 阶段门禁（Spec §7.2、§9 步骤 5）的独立复算与 AST 检查回归。

门禁脚本不属运行包，按路径加载；这些测试只断言它的纯判据逻辑，不跑真机、
不写 验收结果/。
"""
from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dev-docs" / "功能改造" / "扫描提速" / "scripts" / "verify_spec.py"
MAPPING = ROOT / "dev-docs" / "功能改造" / "扫描提速" / "验收用例映射.json"


def _load_verify_spec():
    spec = importlib.util.spec_from_file_location("verify_spec_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vs():
    return _load_verify_spec()


def _sample_output(level: int = 0) -> dict:
    return {
        "level": level,
        "substats": [{"name": "攻击力", "rolls": 1}],
        "rarity": "S",
        "locked": False,
        "equipped": False,
        "discarded": False,
    }


_MISSING = object()


def _case(case_id: str, group: str, image: str, *, rounds: int = 10,
          legacy_ms: float = 100.0, new_ms: float = 40.0,
          hits: int = 1, fast_used: bool = True, expected=_MISSING,
          profile_id: str | None = None) -> dict:
    out = _sample_output() if expected is _MISSING else expected
    mode = {
        "samples_ms": [legacy_ms] * rounds,
        "new_samples_ms": [new_ms] * rounds,
        "legacy_outputs": [out] * rounds,
        "new_outputs": [out] * rounds,
        "new_template_hits": [hits] * rounds,
        "new_fast_used": [fast_used] * rounds,
        "new_profile_id": [profile_id] * rounds,
    }
    return {
        "case_id": case_id,
        "image": image,
        "group": group,
        "expected": out,
        "modes": {"normal": mode, "disabled": dict(mode), "corrupt": dict(mode)},
    }


def _report(cases: list[dict], *, rounds: int = 10) -> dict:
    return {"schema_version": 1, "rounds": rounds, "cases": cases}


# --------------------------------------------------------------------------
# 统计口径（§7.2）
# --------------------------------------------------------------------------
def test_median_uses_average_of_middle_pair(vs):
    assert vs._median([5, 1, 3, 2, 4]) == 3
    assert vs._median([1, 2, 3, 4]) == 2.5
    with pytest.raises(Exception):
        vs._median([])


def test_p95_is_recent_rank_ceil(vs):
    assert vs._p95(list(range(1, 21))) == 19  # ceil(0.95*20)=19
    assert vs._p95(list(range(1, 11))) == 10  # ceil(9.5)=10


# --------------------------------------------------------------------------
# 复算
# --------------------------------------------------------------------------
def test_recompute_passes_for_consistent_report(vs):
    cases = [
        _case("o1", "warehouse_sidebar", "手册/参考图/驱动盘详情/首档复用-侧栏盘面.png"),
        _case("o2", "detail_modal", "手册/参考图/驱动盘详情/详情弹层.png"),
    ]
    result = vs._recompute_from_report(_report(cases))
    assert result["cond1"] and result["cond2"] and result["cond3"]
    assert result["cond4"] and result["cond5"] and result["cond6"]
    assert result["template_hits_total"] == 20


def test_recompute_condition2_fails_when_new_slow(vs):
    cases = [_case("o1", "warehouse_sidebar", "手册/参考图/驱动盘详情/首档复用-侧栏盘面.png",
                   legacy_ms=100.0, new_ms=80.0)]
    result = vs._recompute_from_report(_report(cases))
    assert result["cond2"] is False


def test_recompute_condition2_requires_fast_path(vs):
    cases = [_case("o1", "warehouse_sidebar", "手册/参考图/驱动盘详情/首档复用-侧栏盘面.png",
                   fast_used=False)]
    result = vs._recompute_from_report(_report(cases))
    assert result["cond2"] is False


def test_recompute_condition3_checks_upgrade_samples(vs):
    ok = _case("o13", "upgrade", "手册/参考图/驱动盘详情/首档复用-强化未升级.png",
               profile_id="upgrade-1920x1080")
    assert vs._recompute_from_report(_report([ok]))["cond3"] is True
    bad = _case("o13", "upgrade", "手册/参考图/驱动盘详情/首档复用-强化未升级.png",
                profile_id="modal-1920x1080")
    assert vs._recompute_from_report(_report([bad]))["cond3"] is False
    maxed = _case("o7", "upgrade", "tests/fixtures/upgrade-maxed.png",
                  fast_used=False, expected=None)
    assert vs._recompute_from_report(_report([maxed]))["cond3"] is True
    maxed_bad = _case("o7", "upgrade", "tests/fixtures/upgrade-maxed.png", fast_used=True)
    assert vs._recompute_from_report(_report([maxed_bad]))["cond3"] is False


def test_recompute_condition4_fails_when_fallback_slow(vs):
    case = _case("o1", "warehouse_sidebar", "手册/参考图/驱动盘详情/首档复用-侧栏盘面.png",
                 legacy_ms=100.0, new_ms=40.0)
    # 让 disabled 模式的回退中位超过 1.10*legacy+10
    for mode in ("disabled", "corrupt"):
        case["modes"][mode] = dict(case["modes"]["normal"], samples_ms=[100.0] * 10,
                                   new_samples_ms=[200.0] * 10)
    result = vs._recompute_from_report(_report([case]))
    assert result["cond4"] is False
    assert result["cond1"] is True  # 输出仍一致，只有速度超限


def test_recompute_condition1_and_5_need_full_schema(vs):
    case = _case("o1", "warehouse_sidebar", "手册/参考图/驱动盘详情/首档复用-侧栏盘面.png")
    case["modes"]["normal"]["new_outputs"] = [["攻击力"]] * 10  # 文字列表冒充
    result = vs._recompute_from_report(_report([case]))
    assert result["cond1"] is False
    assert result["cond5"] is False


def test_recompute_condition6_needs_one_hit(vs):
    case = _case("o1", "warehouse_sidebar", "手册/参考图/驱动盘详情/首档复用-侧栏盘面.png", hits=0)
    result = vs._recompute_from_report(_report([case]))
    assert result["cond6"] is False
    assert result["template_hits_total"] == 0


def test_recompute_flags_missing_mode_and_short_samples(vs):
    case = _case("o1", "warehouse_sidebar", "手册/参考图/驱动盘详情/首档复用-侧栏盘面.png")
    del case["modes"]["corrupt"]
    case["modes"]["normal"]["new_samples_ms"] = [1.0, 2.0]
    result = vs._recompute_from_report(_report([case]))
    assert {"case_id": "o1", "mode": "corrupt"} in result["modes_missing"]
    assert {"case_id": "o1", "mode": "normal"} in result["samples_short"]


# --------------------------------------------------------------------------
# JUnit 读取
# --------------------------------------------------------------------------
def test_check_junit_reads_pytest_nodeid_property(vs, tmp_path):
    doc = ET.Element("testsuites")
    suite = ET.SubElement(doc, "testsuite", tests="1", failures="0", errors="0", skipped="0")
    case = ET.SubElement(suite, "testcase", classname="tests.test_x", name="test_a")
    props = ET.SubElement(case, "properties")
    ET.SubElement(props, "property", name="pytest_nodeid", value="tests/test_x.py::test_a")
    path = tmp_path / "junit.xml"
    path.write_text(ET.tostring(doc, encoding="unicode"), encoding="utf-8")
    info, ok = vs._check_junit(path)
    assert ok is True
    assert info["nodeids"] == ["tests/test_x.py::test_a"]


def test_check_junit_marks_skip_not_ok(vs, tmp_path):
    doc = ET.Element("testsuite", tests="1", failures="0", errors="0", skipped="1")
    ET.SubElement(doc, "testcase", classname="c", name="n")
    path = tmp_path / "j.xml"
    path.write_text(ET.tostring(doc, encoding="unicode"), encoding="utf-8")
    info, ok = vs._check_junit(path)
    assert info["skipped"] == 1


# --------------------------------------------------------------------------
# AST：_upgrade_once 必须显式传 before=
# --------------------------------------------------------------------------
def test_check_imports_requires_before_keyword(vs, monkeypatch, tmp_path):
    good = tmp_path / "good.py"
    good.write_text("def f(self, hwnd, disc):\n    self._upgrade_once(hwnd, 0, before=disc)\n",
                    encoding="utf-8")
    monkeypatch.setattr(vs, "_python_files", lambda: [good])
    findings, _ok = vs._check_imports()
    assert findings["upgrade_once"] == []

    bad = tmp_path / "bad.py"
    bad.write_text("def f(self, hwnd, disc):\n    self._upgrade_once(hwnd)\n", encoding="utf-8")
    monkeypatch.setattr(vs, "_python_files", lambda: [bad])
    findings, _ok = vs._check_imports()
    assert findings["upgrade_once"], "缺 before= 应被 AST 逮到"


def test_check_imports_flags_syntax_error(vs, monkeypatch, tmp_path):
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n", encoding="utf-8")
    monkeypatch.setattr(vs, "_python_files", lambda: [broken])
    findings, ok = vs._check_imports()
    assert findings["syntax"]
    assert ok is False


# --------------------------------------------------------------------------
# 映射编号集合
# --------------------------------------------------------------------------
def test_required_case_ids_match_mapping_keys(vs):
    import json

    doc = json.loads(MAPPING.read_text(encoding="utf-8"))
    assert set(doc["cases"]) == set(vs.REQUIRED_CASE_IDS)
    assert "W6" not in doc["cases"]
