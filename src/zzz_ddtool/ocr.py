"""本地 OCR：只对指定 ROI 做识别，不做全图。

引擎用 rapidocr 自带的 PP-OCRv6 small（det 9.5M + rec 20M），离线、不联网。
全图识别会把左侧网格的图标也当字认，所以这里一律先裁再识别。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher

import cv2
import numpy as np
from PySide6.QtGui import QImage

from zzz_ddtool.layout import ACTION_ROW, DISC_LEVEL, MAIN_STAT, SUB_STAT_SPLIT_X, SUB_STATS, scale_rect
from zzz_ddtool.models import Disc, Substat

# 副属性可能出现的全部名字（含 % 与 flat 区分）
_SUBSTAT_NAMES = (
    "生命值",
    "生命值%",
    "攻击力",
    "攻击力%",
    "防御力",
    "防御力%",
    "异常精通",
    "穿透值",
    "异常掌控",
    "冲击力",
    "暴击率",
    "暴击伤害",
    "能量自动回复",
)

# 主属性名。部分与副属性重叠，但主属性区用来识别套装部位/主词条。
_MAINSTAT_NAMES = (
    "生命值",
    "生命值%",
    "攻击力",
    "攻击力%",
    "防御力",
    "防御力%",
    "暴击率",
    "暴击伤害",
    "异常精通",
    "穿透率",
    "异常掌控",
    "能量自动回复",
    "冲击力",
    "物理伤害",
    "火属性伤害",
    "冰属性伤害",
    "电属性伤害",
    "以太伤害",
)

_engine = None
_engine_lock = threading.Lock()


@dataclass(frozen=True)
class OcrLine:
    text: str
    score: float
    left: int
    top: int
    right: int
    bottom: int


class OcrError(RuntimeError):
    pass


def _get_engine():
    """懒加载，模型有 30MB，没用到就别加载。"""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            from rapidocr import ModelType, OCRVersion, RapidOCR
        except ImportError as exc:  # pragma: no cover - 依赖缺失才走这里
            raise OcrError("没装 rapidocr。装：py -3.11 -m pip install rapidocr onnxruntime") from exc
        _engine = RapidOCR(
            params={
                "Det.model_type": ModelType.SMALL,
                "Rec.model_type": ModelType.SMALL,
                "Rec.ocr_version": OCRVersion.PPOCRV6,
            }
        )
        return _engine


def qimage_to_bgr(image: QImage) -> np.ndarray:
    """QImage -> OpenCV BGR。中文路径不经过文件系统，直接在内存里转。"""
    img = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = img.width(), img.height()
    bits = memoryview(img.bits())
    arr = np.frombuffer(bits.tobytes(), np.uint8).reshape(height, width, 3).copy()
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def read_rect(image: QImage, rect: tuple[int, int, int, int]) -> list[OcrLine]:
    """裁出矩形后识别，返回按位置排序的文本行。坐标是 1920x1080 设计坐标。"""
    left, top, right, bottom = scale_rect(*rect, image.width(), image.height())
    left = max(0, left)
    top = max(0, top)
    right = min(image.width(), right)
    bottom = min(image.height(), bottom)
    if right <= left or bottom <= top:
        return []

    bgr = qimage_to_bgr(image)
    crop = bgr[top:bottom, left:right]
    result = _get_engine()(crop)
    if result is None or not result.txts:
        return []

    lines: list[OcrLine] = []
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(
            OcrLine(
                text=str(text).strip(),
                score=float(score),
                left=int(min(xs)) + left,
                top=int(min(ys)) + top,
                right=int(max(xs)) + left,
                bottom=int(max(ys)) + top,
            )
        )
    lines.sort(key=lambda line: (round(line.top / 12), line.left))
    return lines


def _fix_percent(text: str) -> str:
    """OCR 常把 '6%' 读成 '%6'，做兜底纠正。"""
    if text.startswith("%") and len(text) > 1 and text[1:].replace(".", "", 1).isdigit():
        return text[1:] + "%"
    return text


def _best_match(name: str, candidates: tuple[str, ...], cutoff: float = 0.6) -> str | None:
    best: str | None = None
    best_ratio = cutoff
    for cand in candidates:
        ratio = SequenceMatcher(None, name, cand).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = cand
    return best


def _parse_value(token: str) -> tuple[float, bool]:
    """返回值与是否为百分比。'6%' -> (6.0, True)，'18' -> (18.0, False)。"""
    token = _fix_percent(token)
    is_percent = "%" in token
    num_text = token.replace("%", "").strip()
    try:
        value = float(num_text) if num_text else 0.0
    except ValueError:
        value = 0.0
    return value, is_percent


def _parse_substat(name_text: str, value_text: str) -> Substat | None:
    """把名字行与数值行解析成 Substat。

    名字里的 '+N' 表示强化次数；Substat.rolls = N + 1。
    数值只用来判断是否为百分比，从而区分 攻击力/攻击力%。
    """
    value_text = _fix_percent(value_text)
    # 拆出名字里的 +N
    roll_match = re.search(r"\+(\d+)$", name_text)
    rolls = 1
    if roll_match:
        rolls = int(roll_match.group(1)) + 1
        base_name = name_text[: roll_match.start()]
    else:
        base_name = name_text

    _, is_percent = _parse_value(value_text)
    # 百分比名字需要在 base_name 后补 %
    if is_percent and not base_name.endswith("%"):
        base_name = base_name + "%"

    # 一些常见 OCR 错字兜底
    fixes = {
        "昇常精通": "异常精通",
        "昇常掌控": "异常掌控",
        "穿透値": "穿透值",
        "攻击": "攻击",
        "防街": "防御",
        "生俞": "生命",
    }
    for wrong, right in fixes.items():
        base_name = base_name.replace(wrong, right)

    matched = _best_match(base_name, _SUBSTAT_NAMES, cutoff=0.55)
    if matched is None:
        return None
    return Substat(name=matched, rolls=rolls)


def _pair_substat_lines(lines: list[OcrLine], split_x: int) -> list[tuple[str, str]]:
    """把 SUB_STATS 区域里的名字行与数值行按垂直位置配对。"""
    names = [ln for ln in lines if (ln.left + ln.right) // 2 < split_x]
    values = [ln for ln in lines if (ln.left + ln.right) // 2 >= split_x]
    pairs: list[tuple[str, str]] = []
    used_values: set[int] = set()
    for name_line in names:
        ny = (name_line.top + name_line.bottom) // 2
        best: OcrLine | None = None
        best_dy = 1e9
        for idx, value_line in enumerate(values):
            if idx in used_values:
                continue
            vy = (value_line.top + value_line.bottom) // 2
            dy = abs(vy - ny)
            if dy < best_dy:
                best_dy = dy
                best = value_line
                best_idx = idx
        if best is not None and best_dy <= 40:
            pairs.append((name_line.text, _fix_percent(best.text)))
            used_values.add(best_idx)
    return pairs


def read_level(image: QImage) -> int | None:
    """读取详情面板等级，返回 0~15 的数字。"""
    for line in read_rect(image, DISC_LEVEL):
        m = re.search(r"(\d+)", line.text)
        if m:
            return int(m.group(1))
    return None


def read_substats(image: QImage) -> tuple[Substat, ...]:
    """读取详情面板副属性，返回 (Substat, ...)。认不出的行跳过。"""
    lines = read_rect(image, SUB_STATS)
    if not lines:
        return ()
    split_x_scaled = scale_rect(SUB_STAT_SPLIT_X, 0, SUB_STAT_SPLIT_X, 1, image.width(), image.height())[0]
    pairs = _pair_substat_lines(lines, split_x_scaled)
    substats: list[Substat] = []
    for name_text, value_text in pairs:
        sub = _parse_substat(name_text, value_text)
        if sub is not None:
            substats.append(sub)
    return tuple(substats)


def read_mainstat(image: QImage) -> str | None:
    """读取主属性名（用来复核部位/套装信息，不做决策主键）。"""
    lines = read_rect(image, MAIN_STAT)
    if not lines:
        return None
    # 取左侧那行作为名字
    name_line = min(lines, key=lambda ln: (ln.left + ln.right) // 2)
    value_line = max(lines, key=lambda ln: (ln.left + ln.right) // 2)
    _, is_percent = _parse_value(value_line.text)
    name = name_line.text
    if is_percent and not name.endswith("%"):
        name = name + "%"
    matched = _best_match(name, _MAINSTAT_NAMES, cutoff=0.55)
    return matched


def read_action_state(image: QImage) -> dict[str, bool]:
    """读取底部按钮文字，判断锁定/弃置状态。

    '解除锁定' 表示当前已锁定；'锁定' 表示当前未锁定。
    '弃置' 表示可弃置（未弃置）；已弃置时通常显示'取消弃置'。
    """
    state = {"locked": False, "discarded": False}
    for line in read_rect(image, ACTION_ROW):
        text = line.text
        if "解除锁定" in text:
            state["locked"] = True
        elif "取消弃置" in text or text == "已弃置":
            state["discarded"] = True
    return state


def parse_disc(image: QImage) -> Disc | None:
    """从驱动盘详情页截图解析出一个 Disc。

    失败返回 None，调用方应中断流程。
    """
    level = read_level(image)
    if level is None:
        return None
    substats = read_substats(image)
    if not substats:
        return None
    actions = read_action_state(image)
    return Disc(
        level=level,
        substats=substats,
        locked=actions["locked"],
        discarded=actions["discarded"],
    )
