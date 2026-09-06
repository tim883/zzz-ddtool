from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

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


def _gray_rect(image, left: int, top: int, right: int, bottom: int) -> tuple[int, int, list[int]]:
    width = max(1, right - left)
    height = max(1, bottom - top)
    pixels: list[int] = []
    for y in range(top, bottom):
        for x in range(left, right):
            color = image.pixelColor(x, y)
            pixels.append((color.red() * 299 + color.green() * 587 + color.blue() * 114) // 1000)
    return width, height, pixels


def _ncc_score(hay: list[int], hw: int, needle: tuple[int, ...], nw: int, nh: int, ox: int, oy: int, mean_n: float, den_n: float) -> float:
    count = nw * nh
    if den_n < 1:
        return 0.0
    total = 0
    for row in range(nh):
        start = (oy + row) * hw + ox
        for col in range(nw):
            total += hay[start + col]
    mean_h = total / count
    num = 0.0
    den_h = 0.0
    for row in range(nh):
        hay_row = (oy + row) * hw + ox
        needle_row = row * nw
        for col in range(nw):
            hv = hay[hay_row + col] - mean_h
            nv = needle[needle_row + col] - mean_n
            num += hv * nv
            den_h += hv * hv
    if den_h < 1:
        return 0.0
    return max(0.0, num / math.sqrt(den_h * den_n))


def match_icon(image, mark: IconMark) -> float:
    from PySide6.QtGui import QImage
    from PySide6.QtCore import Qt

    left, top, right, bottom = scale_rect(*mark.rect, image.width(), image.height())
    pad_x = round(mark.pad * image.width() / DESIGN_W)
    pad_y = round(mark.pad * image.height() / DESIGN_H)
    search_l = max(0, left - pad_x)
    search_t = max(0, top - pad_y)
    search_r = min(image.width(), right + pad_x)
    search_b = min(image.height(), bottom + pad_y)
    sw, sh, hay = _gray_rect(image, search_l, search_t, search_r, search_b)

    raw = QImage(str(assets_dir() / mark.path))
    if raw.isNull():
        return 0.0
    tw = max(1, round(raw.width() * image.width() / DESIGN_W))
    th = max(1, round(raw.height() * image.height() / DESIGN_H))
    if tw != raw.width() or th != raw.height():
        raw = raw.scaled(tw, th, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    nw, nh, needle_list = _gray_rect(raw, 0, 0, raw.width(), raw.height())
    needle = tuple(needle_list)
    if sw < nw or sh < nh:
        return 0.0
    mean_n = sum(needle) / len(needle)
    den_n = sum((value - mean_n) ** 2 for value in needle)
    if den_n < 1:
        return 0.0

    best = 0.0
    for oy in range(0, sh - nh + 1, 2):
        for ox in range(0, sw - nw + 1, 2):
            score = _ncc_score(hay, sw, needle, nw, nh, ox, oy, mean_n, den_n)
            if score > best:
                best = score
            if best >= 0.97:
                return best
    return best


def marks_hit(image, marks: tuple[IconMark, ...]) -> list[tuple[str, float, float]]:
    result: list[tuple[str, float, float]] = []
    for mark in marks:
        score = match_icon(image, mark)
        result.append((mark.name, score, mark.threshold))
    return result


def all_marks_hit(image, marks: tuple[IconMark, ...]) -> bool:
    return all(score >= threshold for _name, score, threshold in marks_hit(image, marks))
