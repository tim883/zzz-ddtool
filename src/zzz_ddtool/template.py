from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from zzz_ddtool.layout import DESIGN_H, DESIGN_W, scale_rect


def assets_dir() -> Path:
    return Path(__file__).resolve().parent / "assets" / "templates"


@dataclass(frozen=True)
class IconMark:
    name: str
    path: str
    rect: tuple[int, int, int, int]
    threshold: float = 0.82
    pad: int = 16


# 同一帧图会被 7 个模板轮流匹配：灰度和色素只转一次，按 cacheKey 认帧。
# 只留最近一帧，避免长跑时越攒越多。
_frame_key: tuple[int, int, int] | None = None
_frame_rgb: np.ndarray | None = None
_frame_gray: np.ndarray | None = None
# 模板灰度：key = (模板名, 目标宽, 目标高)。模板文件不变，缩放结果可长留。
_needle_cache: dict[tuple[str, int, int], np.ndarray] = {}
# 模板原始尺寸：省掉每次 match 都读一遍 PNG。
_mark_size_cache: dict[str, tuple[int, int]] = {}


def _image_to_rgb(image) -> np.ndarray:
    """QImage -> HxWx3 uint8 RGB。内存里直接转，不经过文件系统。"""
    from PySide6.QtGui import QImage

    img = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = img.width(), img.height()
    if width <= 0 or height <= 0:
        return np.zeros((0, 0, 3), dtype=np.uint8)
    bpl = img.bytesPerLine()  # 每行可能有 4 字节对齐 padding，不能直接按 width*3 reshape
    arr = np.frombuffer(memoryview(img.bits()).tobytes(), np.uint8, bpl * height).reshape(height, bpl)
    return arr[:, : width * 3].reshape(height, width, 3).copy()


def _gray_from_rgb(rgb: np.ndarray) -> np.ndarray:
    """RGB -> 灰度。权重与旧实现逐位一致（BT.601 整数式，// 向下取整）。"""
    if rgb.size == 0:
        return np.zeros((0, 0), dtype=np.uint8)
    a = rgb.astype(np.int32)
    return ((a[:, :, 0] * 299 + a[:, :, 1] * 587 + a[:, :, 2] * 114) // 1000).astype(np.uint8)


def to_rgb(image) -> np.ndarray:
    """整图 RGB（按帧缓存）。"""
    global _frame_key, _frame_rgb, _frame_gray
    key = (image.cacheKey(), image.width(), image.height())
    if key != _frame_key or _frame_rgb is None:
        _frame_key, _frame_rgb, _frame_gray = key, _image_to_rgb(image), None
    return _frame_rgb


def to_gray(image) -> np.ndarray:
    """整图灰度（按帧缓存，同一帧只算一次）。"""
    global _frame_gray
    rgb = to_rgb(image)
    if _frame_gray is None:
        _frame_gray = _gray_from_rgb(rgb)
    return _frame_gray


def _mark_raw_size(path: str) -> tuple[int, int]:
    hit = _mark_size_cache.get(path)
    if hit is not None:
        return hit
    from PySide6.QtGui import QImage

    raw = QImage(str(assets_dir() / path))
    size = (0, 0) if raw.isNull() else (raw.width(), raw.height())
    _mark_size_cache[path] = size
    return size


def _needle_gray(path: str, tw: int, th: int) -> np.ndarray:
    key = (path, tw, th)
    hit = _needle_cache.get(key)
    if hit is not None:
        return hit
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    raw = QImage(str(assets_dir() / path))
    if raw.isNull():
        arr = np.zeros((0, 0), dtype=np.uint8)
    else:
        if tw != raw.width() or th != raw.height():
            raw = raw.scaled(
                tw, th, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        arr = _gray_from_rgb(_image_to_rgb(raw))
    _needle_cache[key] = arr
    return arr


def match_icon(image, mark: IconMark) -> float:
    """在 mark.rect（外扩 pad）里滑窗找模板，返回最大相关系数。

    归一化互相关用 cv2.TM_CCOEFF_NORMED；负相关钳到 0（与旧实现一致）。
    旧实现是纯 Python 三重循环（一个模板上百万次迭代），同一帧 7 个模板
    要 1~5 秒；换 numpy + OpenCV 后降到几十毫秒。
    """
    rw, rh = _mark_raw_size(mark.path)
    if rw == 0 or rh == 0:
        return 0.0
    tw = max(1, round(rw * image.width() / DESIGN_W))
    th = max(1, round(rh * image.height() / DESIGN_H))
    needle = _needle_gray(mark.path, tw, th)
    if needle.size == 0:
        return 0.0
    nh, nw = needle.shape

    left, top, right, bottom = scale_rect(*mark.rect, image.width(), image.height())
    pad_x = round(mark.pad * image.width() / DESIGN_W)
    pad_y = round(mark.pad * image.height() / DESIGN_H)
    search_l = max(0, left - pad_x)
    search_t = max(0, top - pad_y)
    search_r = min(image.width(), right + pad_x)
    search_b = min(image.height(), bottom + pad_y)

    hay = to_gray(image)[search_t:search_b, search_l:search_r]
    if hay.shape[0] < nh or hay.shape[1] < nw:
        return 0.0
    result = cv2.matchTemplate(hay.astype(np.float32), needle.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    if result.size == 0:
        return 0.0
    # 旧实现滑窗步长是 2，OpenCV 默认步长 1——步长 1 会挑到更高的分，等于把
    # threshold 的标定语义换掉（可能出现「本来不命中、现在刚好过阈值」的翻转）。
    # 这里按 2 取回同一步长集合，分数与旧实现对齐。
    result = result[::2, ::2]
    # 局部方差为 0 的窗口会算出 NaN，先归零再取最大。
    result = np.nan_to_num(result, nan=0.0, posinf=1.0, neginf=0.0)
    return max(0.0, float(result.max()))


def marks_hit(image, marks: tuple[IconMark, ...]) -> list[tuple[str, float, float]]:
    result: list[tuple[str, float, float]] = []
    for mark in marks:
        score = match_icon(image, mark)
        result.append((mark.name, score, mark.threshold))
    return result


def all_marks_hit(image, marks: tuple[IconMark, ...]) -> bool:
    return all(score >= threshold for _name, score, threshold in marks_hit(image, marks))
