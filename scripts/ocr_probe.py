"""对单张截图跑 OCR，把所有文本框按位置打印出来，用于标定 ROI。

用法：
    py -3.11 scripts/ocr_probe.py <图片路径> [--min-score 0.5]

输出每行：  y  x  置信度  文本
坐标是 1920x1080 原图像素，可直接写进 layout.py。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="截图路径")
    parser.add_argument("--min-score", type=float, default=0.5, help="低于此置信度不打印")
    parser.add_argument("--model", default="v6", choices=["v6", "v5"], help="PP-OCR 版本")
    args = parser.parse_args()

    path = Path(args.image)
    if not path.is_file():
        print(f"找不到图片：{path}", file=sys.stderr)
        return 1

    # cv2.imread 读不了中文路径，走 imdecode
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        print(f"读不了图片：{path}", file=sys.stderr)
        return 1
    print(f"# {path.name} {image.shape[1]}x{image.shape[0]} model={args.model}")

    from rapidocr import ModelType, OCRVersion, RapidOCR

    engine = RapidOCR(
        params={
            "Det.model_type": ModelType.SMALL,
            "Rec.model_type": ModelType.SMALL,
            "Rec.ocr_version": OCRVersion.PPOCRV6 if args.model == "v6" else OCRVersion.PPOCRV5,
        }
    )

    t0 = time.perf_counter()
    result = engine(image)
    elapse = time.perf_counter() - t0

    if result is None or not result.txts:
        print(f"# 没识别到任何文本（{elapse:.2f}s）")
        return 0

    rows = []
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        rows.append((min(ys), min(xs), max(xs), max(ys), score, text))
    rows.sort(key=lambda r: (round(r[0] / 12), r[1]))

    print(f"# 耗时 {elapse:.2f}s，共 {len(rows)} 个文本框")
    print(f"{'top':>6} {'left':>6} {'right':>6} {'bottom':>6}  {'score':>5}  text")
    for top, left, right, bottom, score, text in rows:
        if score < args.min_score:
            continue
        print(f"{top:6.0f} {left:6.0f} {right:6.0f} {bottom:6.0f}  {score:5.2f}  {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
