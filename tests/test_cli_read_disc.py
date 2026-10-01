import json

from zzz_ddtool.cli import main
from zzz_ddtool.models import Disc, Substat


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
