"""套装名图片模板（picker_names）的资产校验与判定门禁；不连游戏、不跑 OCR 引擎。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import picker_names


def _pattern(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(14, 22), dtype=np.uint8)


def _qimage_from_gray(gray: np.ndarray) -> QImage:
    gray = np.ascontiguousarray(gray, np.uint8)
    image = QImage(gray.tobytes(), gray.shape[1], gray.shape[0], gray.shape[1],
                   QImage.Format.Format_Grayscale8)
    return image.copy()


def _qimage_with_patch(gray: np.ndarray, patch: np.ndarray, x: int, y: int) -> QImage:
    canvas = np.zeros((80, 200), np.uint8)
    canvas[y:y + patch.shape[0], x:x + patch.shape[1]] = patch
    return _qimage_from_gray(canvas)


def _write_assets(tmp_path: Path, items) -> Path:
    root = tmp_path / "picker"
    (root / "templates").mkdir(parents=True)
    entries = []
    for tid, name, array in items:
        fn = f"{tid}.png"
        path = root / "templates" / fn
        assert cv2.imwrite(str(path), array)
        entries.append({
            "template_id": tid,
            "name": name,
            "image": f"templates/{fn}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_image_sha256": "0" * 64,
            "match_min_score": 0.98,
            "match_min_gap": 0.03,
            "source_frame": "synthetic",
        })
    (root / "templates.json").write_text(
        json.dumps({"schema_version": 1, "template_version": 1, "pad": 0, "entries": entries},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return root


@pytest.fixture(autouse=True)
def _fresh_cache():
    picker_names.reset_cache()
    yield
    picker_names.reset_cache()


def test_full_match_returns_name_score_and_box(tmp_path):
    pattern = _pattern(1)
    root = _write_assets(tmp_path, [("t-1", "甲套装", pattern)])
    image = _qimage_with_patch(np.zeros((80, 200), np.uint8), pattern, x=60, y=30)

    name, score, box = picker_names.classify(image, (40, 20, 160, 60), root=root)

    assert name == "甲套装"
    assert score > 0.99
    assert box == (60, 30, 60 + pattern.shape[1], 30 + pattern.shape[0])


def test_close_second_name_rejected_by_gap(tmp_path):
    pattern = _pattern(2)
    root = _write_assets(tmp_path, [("t-1", "甲套装", pattern), ("t-2", "乙套装", pattern.copy())])
    image = _qimage_with_patch(np.zeros((80, 200), np.uint8), pattern, x=60, y=30)

    assert picker_names.classify(image, (40, 20, 160, 60), root=root) is None


def test_good_match_wins_when_second_name_is_weak(tmp_path):
    pattern = _pattern(3)
    other = _pattern(4)
    root = _write_assets(tmp_path, [("t-1", "甲套装", pattern), ("t-2", "乙套装", other)])
    image = _qimage_with_patch(np.zeros((80, 200), np.uint8), pattern, x=60, y=30)

    name, _score, _box = picker_names.classify(image, (40, 20, 160, 60), root=root)

    assert name == "甲套装"


def test_unknown_band_returns_none(tmp_path):
    root = _write_assets(tmp_path, [("t-1", "甲套装", _pattern(5))])
    noise = np.random.default_rng(6).integers(0, 256, size=(80, 200), dtype=np.uint8)

    assert picker_names.classify(_qimage_from_gray(noise), (40, 20, 160, 60), root=root) is None


def test_missing_assets_raise(tmp_path):
    with pytest.raises(picker_names.PickerAssetError, match="templates.json"):
        picker_names.load_entries(tmp_path / "missing")


def test_tampered_template_png_raises(tmp_path):
    pattern = _pattern(7)
    root = _write_assets(tmp_path, [("t-1", "甲套装", pattern)])
    (root / "templates" / "t-1.png").write_bytes(b"not a png anymore")

    with pytest.raises(picker_names.PickerAssetError, match="sha256"):
        picker_names.load_entries(root)


def test_illegal_entry_shape_raises(tmp_path):
    pattern = _pattern(8)
    root = _write_assets(tmp_path, [("t-1", "甲套装", pattern)])
    doc = json.loads((root / "templates.json").read_text(encoding="utf-8"))
    doc["entries"][0]["unexpected"] = 1
    (root / "templates.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(picker_names.PickerAssetError, match="键集合"):
        picker_names.load_entries(root)
