"""CLI 入口的日志上下文：正式 run 走生产根 log/，诊断/测试入口走 log_test/。

一轮 = 一个时间戳目录；一次 run 只该产生一个轮次目录（入口先开、Runner 认领）。
"""
import json

import pytest

from zzz_ddtool import loground, paths
from zzz_ddtool.cli import main
from zzz_ddtool.models import Disc, Substat

_MINIMAL_YAML = """
name: fixture
game_filter:
  slot: 4
  slots: [4]
accepted: []
"""


@pytest.fixture(autouse=True)
def isolated_log_roots(tmp_path, monkeypatch):
    """把两个日志根都指到 tmp_path：CLI 按入口自己开轮次，别写进仓库的 log/、log_test/。

    收尾关掉本轮占用：一条 CLI 命令就是一个进程，测试也不该把活动轮次留给后面的用例。
    """
    monkeypatch.setattr(paths, "log_root", lambda: tmp_path / "log")
    monkeypatch.setattr(paths, "test_log_root", lambda: tmp_path / "log_test")
    yield tmp_path
    loground.close_active()


class _StubRunner:
    """替身执行器：按入口给的参数认领或新开轮次，不碰游戏、不发送输入。"""

    instances: list = []

    def __init__(self, preset, **kwargs) -> None:
        self.preset = preset
        self.kwargs = kwargs
        self.outcome = "completed"
        self.end_reason = "已处理 0 个盘"
        self.processed_disc_count = 0
        self.round = None
        _StubRunner.instances.append(self)

    def run(self):
        config = self.kwargs.get("config_path", "")
        if self.kwargs.get("claim_round"):
            self.round = loground.claim_task(config=config, task=self.preset.name)
        else:
            self.round = loground.begin_task(config=config, task=self.preset.name)
        return []


@pytest.fixture
def stub_runner(monkeypatch):
    from zzz_ddtool import runner as runner_module

    _StubRunner.instances = []
    monkeypatch.setattr(runner_module, "Runner", _StubRunner)
    return _StubRunner


def test_saved_image_command_exports_reading(tmp_path, monkeypatch):
    monkeypatch.setattr("zzz_ddtool.ocr.parse_disc", lambda image: Disc(3, (Substat("暴击率"),)))
    out = tmp_path / "reading.json"
    assert main(["read-disc", "tests/fixtures/warehouse-detail.png", "--out", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["ok"]
    assert result["disc"]["level"] == 3


def test_saved_image_command_exports_failure(tmp_path):
    out = tmp_path / "reading.json"
    assert main(["read-disc", str(tmp_path / "missing.png"), "--out", str(out)]) == 2
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert "无法读取截图" in result["error"]


def test_read_disc_default_output_lands_in_the_test_round(isolated_log_roots, monkeypatch):
    """read-disc 是诊断入口：轮次开在 log_test/，默认输出就是本轮目录里的 JSON。"""
    monkeypatch.setattr("zzz_ddtool.ocr.parse_disc", lambda image: Disc(3, (Substat("暴击率"),)))

    assert main(["read-disc", "tests/fixtures/warehouse-detail.png"]) == 0

    round_ = loground.current()
    assert round_ is not None
    assert round_.root.parent == isolated_log_roots / "log_test"
    assert (round_.root / "disc-reading.json").is_file()
    assert not (isolated_log_roots / "log").exists()


def test_run_defaults_to_production_context_and_claims_one_round(
    isolated_log_roots, stub_runner, tmp_path
):
    """run 默认是正式运行：开在生产根 log/ 下；入口开好的那轮被认领，只出现一个轮次目录。"""
    loground.close_active()  # CLI 一次进程只跑一条命令：等价于新进程
    config = tmp_path / "preset.yaml"
    config.write_text(_MINIMAL_YAML, encoding="utf-8")

    assert main(["run", "--config", str(config)]) == 0

    runner = stub_runner.instances[-1]
    assert runner.kwargs["claim_round"] is True
    assert runner.kwargs["config_path"] == str(config)
    assert runner.round.root.parent == isolated_log_roots / "log"
    assert [path.name for path in loground.round_dirs(isolated_log_roots / "log")] == [
        runner.round.id
    ]
    assert not (isolated_log_roots / "log_test").exists()
    assert loground.current() is None  # 收尾后已释放占用


@pytest.mark.parametrize("flag", ["--start-index", "--material-slot"])
def test_run_test_entries_use_test_context_and_one_round(
    isolated_log_roots, stub_runner, tmp_path, flag
):
    """非 0 的 --start-index / --material-slot 是测试入口：轮次改开在 log_test/，仍只一个。"""
    loground.close_active()
    config = tmp_path / "preset.yaml"
    config.write_text(_MINIMAL_YAML, encoding="utf-8")

    assert main(["run", "--config", str(config), flag, "1"]) == 0

    runner = stub_runner.instances[-1]
    assert runner.kwargs[flag.lstrip("-").replace("-", "_")] == 1
    assert runner.round.root.parent == isolated_log_roots / "log_test"
    assert [path.name for path in loground.round_dirs(isolated_log_roots / "log_test")] == [
        runner.round.id
    ]
    assert not (isolated_log_roots / "log").exists()
