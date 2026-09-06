"""同一张图上对比 PP-OCRv6 / v5，并验证「裁小图」会不会漏检。

用法：
    py -3.11 scripts/ocr_compare.py <图片> --v5-dir D:/WB_agent_out/zzz-ddtool/tool/models/v5

不传 --v5-dir 就只跑 v6。
每段 ROI 会打印：全图识别 vs 先裁后识别，用来看 v6 在小图上是否漏检。
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import cv2
import numpy as np

# 1920x1080 下详情面板各字段的实测 ROI（来自 scripts/ocr_probe.py 的实拍标定）
ROI = {
    "面板整块": (1400, 225, 1870, 800),
    "盘名": (1400, 260, 1620, 315),
    "等级": (1450, 390, 1620, 445),
    "主属性": (1415, 480, 1840, 535),
    "副属性区": (1415, 558, 1840, 775),
    "按钮行": (1600, 1000, 1880, 1070),
}


def load(path: Path):
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def build_engine(name: str, v5_dir: str | None):
    from rapidocr import ModelType, OCRVersion, RapidOCR

    if name == "v6":
        return RapidOCR(params={"Det.model_type": ModelType.SMALL, "Rec.model_type": ModelType.SMALL})
    if not v5_dir:
        return None
    d = Path(v5_dir)
    return RapidOCR(
        params={
            "Det.model_path": str(d / "ch_PP-OCRv5_det_mobile.onnx"),
            "Rec.model_path": str(d / "ch_PP-OCRv5_rec_mobile.onnx"),
            "Rec.rec_keys_path": str(d / "ppocrv5_dict.txt"),
            "Rec.ocr_version": OCRVersion.PPOCRV5,
        }
    )


def ocr_lines(engine, image) -> list[tuple[float, float, str]]:
    result = engine(image)
    if result is None or not result.txts:
        return []
    rows = []
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        rows.append((min(p[1] for p in box), min(p[0] for p in box), f"{text}({score:.2f})"))
    rows.sort(key=lambda r: (round(r[0] / 12), r[1]))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--v5-dir", default=os.environ.get("ZZZ_OCR_V5_DIR"))
    args = parser.parse_args()

    image = load(Path(args.image))
    if image is None:
        print(f"读不了图片：{args.image}")
        return 1

    for name in ("v6", "v5"):
        engine = build_engine(name, args.v5_dir)
        if engine is None:
            print(f"\n=== {name} === 跳过（没给 --v5-dir）")
            continue
        print(f"\n=== {name} ===")
        for label, (l, t, r, b) in ROI.items():
            crop = image[t:b, l:r]
            t0 = time.perf_counter()
            lines = ocr_lines(engine, crop)
            elapse = time.perf_counter() - t0
            texts = " | ".join(text for _y, _x, text in lines)
            print(f"  {label:<6} {crop.shape[1]}x{crop.shape[0]} {elapse:5.2f}s  {texts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
