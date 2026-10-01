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

from zzz_ddtool.layout import (
    ACTION_ROW,
    DISC_LEVEL,
    MAIN_STAT,
    SUB_STAT_SPLIT_X,
    SUB_STATS,
    UPGRADE_LEVEL,
    UPGRADE_SUB_SPLIT_X,
    UPGRADE_SUB_STATS,
    WAREHOUSE_DISC_LEVEL,
    WAREHOUSE_MAIN_STAT,
    WAREHOUSE_SUB_STATS,
    scale_rect,
)
from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.gamedata import MAINSTAT_NAMES as _MAINSTAT_NAMES, SUBSTAT_NAMES as _SUBSTAT_NAMES

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
            raise OcrError("没装 rapidocr。装：py -3.13 -m pip install rapidocr（不是 rapidocr-onnxruntime）") from exc
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
    bpl = img.bytesPerLine()  # 每行可能有 4 字节对齐 padding，不能直接按 width*3 reshape
    bits = memoryview(img.bits())
    arr = np.frombuffer(bits.tobytes(), np.uint8, bpl * height).reshape(height, bpl)
    arr = arr[:, : width * 3].reshape(height, width, 3).copy()
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
    """把副属性区域里的名字行与数值行配对。

    副属性是两列布局（名字 数值 | 名字 数值），单一 split_x 切分会把第二列
    名字误当数值、第一列数值误当名字。改用：名字=含非数字字符（中文），
    数值=纯数字（可含 %）；按 y 聚成行，行内按 x 排序后「名字 → 右侧最近
    的数值」相邻配对。split_x 参数保留但不再参与切分。
    """
    def _is_value(text: str) -> bool:
        t = text.replace("%", "").replace("+", "").replace(".", "").replace("-", "").strip()
        return t.isdigit()

    rows: list[list[OcrLine]] = []
    for ln in sorted(lines, key=lambda l: (l.top, l.left)):
        cy = (ln.top + ln.bottom) // 2
        if rows and abs(cy - ((rows[-1][-1].top + rows[-1][-1].bottom) // 2)) <= 30:
            rows[-1].append(ln)
        else:
            rows.append([ln])

    pairs: list[tuple[str, str]] = []
    for row in rows:
        row.sort(key=lambda l: l.left)
        names = [ln for ln in row if not _is_value(ln.text)]
        values = [ln for ln in row if _is_value(ln.text)]
        for name in names:
            nx = (name.left + name.right) // 2
            best: OcrLine | None = None
            best_dx = 1e9
            for val in values:
                vx = (val.left + val.right) // 2
                if vx > nx and vx - nx < best_dx:
                    best_dx = vx - nx
                    best = val
            if best is not None:
                pairs.append((name.text, _fix_percent(best.text)))
                values.remove(best)
    return pairs


def read_level(image: QImage, area: tuple[int, int, int, int] = DISC_LEVEL) -> int | None:
    """读取等级数字（默认详情面板，可传强化页区域），返回 0~15 的数字。"""
    for line in read_rect(image, area):
        m = re.search(r"等级\s*(\d+)", line.text)
        if m is None and area == UPGRADE_LEVEL:
            m = re.fullmatch(r"\s*(\d+)\s*", line.text)
        if m and 0 <= int(m.group(1)) <= 15:
            return int(m.group(1))
    return None


def read_substats(
    image: QImage,
    area: tuple[int, int, int, int] = SUB_STATS,
    split_x: int = SUB_STAT_SPLIT_X,
) -> tuple[Substat, ...]:
    """读取副属性，返回 (Substat, ...)。认不出的行跳过。area/split_x 可传强化页区域。"""
    lines = read_rect(image, area)
    if not lines:
        return ()
    split_x_scaled = scale_rect(split_x, 0, split_x, 1, image.width(), image.height())[0]
    pairs = _pair_substat_lines(lines, split_x_scaled)
    substats: list[Substat] = []
    for name_text, value_text in pairs:
        sub = _parse_substat(name_text, value_text)
        if sub is not None:
            substats.append(sub)
    return tuple(substats)


def read_mainstat(image: QImage) -> str | None:
    """读取主属性名（用来复核部位/套装信息，不做决策主键）。"""
    area = WAREHOUSE_MAIN_STAT if read_level(image, WAREHOUSE_DISC_LEVEL) is not None else MAIN_STAT
    lines = read_rect(image, area)
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
    # 只有在等级标签成功匹配时才选择布局，避免把套装说明里的数字当等级。
    level = read_level(image, WAREHOUSE_DISC_LEVEL)
    area = WAREHOUSE_SUB_STATS
    if level is None:
        level = read_level(image)
        area = SUB_STATS
    if level is None:
        return None
    substats = read_substats(image, area)
    if not substats:
        return None
    actions = read_action_state(image)
    return Disc(
        level=level,
        substats=substats,
        locked=actions["locked"],
        discarded=actions["discarded"],
    )


def parse_disc_upgrade_page(image: QImage) -> Disc | None:
    """从强化页截图解析 Disc（强化页副属性/等级坐标）。

    强化页没有 R/T 按钮，弃置在详情页做；这里只读等级+副属性供 decide 判断
    升级还是弃置。失败返回 None。
    """
    level = read_level(image, UPGRADE_LEVEL)
    if level is None:
        return None
    substats = read_substats(image, UPGRADE_SUB_STATS, UPGRADE_SUB_SPLIT_X)
    if not substats:
        return None
    return Disc(level=level, substats=substats, locked=False, discarded=False)
