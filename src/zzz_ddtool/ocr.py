"""本地 OCR：只对指定 ROI 做识别，不做全图。

引擎用 rapidocr 自带的 PP-OCRv6 small（det 9.5M + rec 20M），离线、不联网。
全图识别会把左侧网格的图标也当字认，所以这里一律先裁再识别。
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from collections.abc import Sequence
from types import SimpleNamespace

import cv2
import numpy as np
from PySide6.QtGui import QImage

from zzz_ddtool import scanperf
from zzz_ddtool.layout import (
    ACTION_ROW,
    ACTION_TEXT_LINE,
    DISC_LEVEL,
    SUB_STAT_SPLIT_X,
    SUB_STATS,
    UPGRADE_LEVEL,
    UPGRADE_LEVEL_BAR,
    UPGRADE_SUB_SPLIT_X,
    UPGRADE_SUB_STATS,
    WAREHOUSE_DISC_LEVEL,
    WAREHOUSE_SUB_STATS,
    scale_rect,
)
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.models import MAX_LEVEL, Disc, Substat
from zzz_ddtool.gamedata import SUBSTAT_NAMES as _SUBSTAT_NAMES

_engine = None
_engine_lock = threading.Lock()

# rapidocr 的 return_word_box / return_single_char_box 是**引擎实例上的属性**
# （rapidocr/main.py 的 update_params 直接 setattr(self, ...)），调用一次就会留
# 在实例上，下一次不传参也照样算字符框。名称字符范围识别必须临时打开这两个开关，
# 于是所有走同一引擎的识别都用这把锁串行化：普通 read_rect 不会在别人开着字符框
# 模式的窗口里跑，读到的引擎状态也不会带着临时开关。
_WORD_BOX_LOCK = threading.Lock()
#: 会被 __call__ 写进引擎实例的两个开关。
_WORD_BOX_ATTRS = ("return_word_box", "return_single_char_box")
#: 进入时读不到该属性（属性本来不存在）——退出时同样把属性删掉。
_ATTR_MISSING = object()

#: 件数后缀语法（第 3.3 节）：可选的「×」加阿拉伯数字，别的写法一律不接受。
_COUNT_SUFFIX_RE = re.compile(r"(?:×)?[0-9]+")


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
                # 检测前的缩放下限。默认 736 配 limit_type=min，会把「最短边不足 736」
                # 的裁剪图等比放大：详情页等级区只有 150x47，最短边 47 → 撑成 736x2350
                # 才进检测网络，一次识别要 1~2 秒。调到 160 后小区域快 4~9 倍；
                # 最短边本来就 >= 160 的大区域（如扫屏 1270x940）原图不动，不受影响。
                "Det.limit_side_len": 160,
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


def _log_rect(image: QImage, rect: tuple[int, int, int, int], count: int, started: float) -> None:
    """每次识别留一条 DEBUG，供事后审计「有没有多余 / 重复识别」。

    记区域、读出几行、图尺寸、耗时，以及图像对象标识——同一次截图上的多次识别
    共享同一个标识，据此可数出「一张图被识别了几次」。默认级别（信息）下不输出。
    """
    get_logger("zzz_ddtool.ocr").debug(
        "识别 %s → %d 行（%dx%d，%.0fms，图#%x）",
        rect,
        count,
        image.width(),
        image.height(),
        (time.perf_counter() - started) * 1000,
        image.cacheKey(),
    )


def read_rect(image: QImage, rect: tuple[int, int, int, int]) -> list[OcrLine]:
    """裁出矩形后识别，返回按位置排序的文本行。坐标是 1920x1080 设计坐标。"""
    started = time.perf_counter()
    left, top, right, bottom = scale_rect(*rect, image.width(), image.height())
    left = max(0, left)
    top = max(0, top)
    right = min(image.width(), right)
    bottom = min(image.height(), bottom)
    if right <= left or bottom <= top:
        _log_rect(image, rect, 0, started)
        return []

    # 先按矩形裁出子图再转 BGR：整图 1920x1080 转换约 12ms，裁完再转只要 0.02ms。
    # 下面的 box 是 crop 局部坐标，仍按 + (left, top) 还原到全图。
    crop = image.copy(left, top, right - left, bottom - top)
    # 与 read_name_span 互斥：字符框开关是引擎实例属性，不能并发观察到临时状态。
    with _WORD_BOX_LOCK:
        result = _get_engine()(qimage_to_bgr(crop))
    if result is None or not result.txts:
        _log_rect(image, rect, 0, started)
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
    _log_rect(image, rect, len(lines), started)
    return lines


def _normalize_ocr_name(text: str) -> str:
    """第 5.1 节同一规则：去首尾空白、删除 U+0020 空格。"""
    return text.strip().replace(" ", "")


@dataclass(frozen=True)
class _CharBox:
    """识别出的单个字符：内容、置信度、客户区像素矩形。"""

    text: str
    score: float
    left: int
    top: int
    right: int
    bottom: int


def _char_boxes(result, offset_x: int, offset_y: int) -> list[_CharBox]:
    """按引擎的单行阅读顺序取字符框（坐标加裁剪偏移），不跨行拼接。

    rapidocr 的 `word_results` 是「每检测行一个元组，行内每个字符一个
    (内容, 置信度, 四点框)」；`return_single_char_box=True` 时行内元素就是逐字符
    （英文数字也按单字符拆），内容顺序与文本顺序一致。框是传入图（即裁剪图）的
    像素坐标，所以要加裁剪偏移才能还原到客户区。

    多条非空行或结构不符合预期（元素不是三元组、框缺失）时返回空列表——调用方按「无字符框」
    返回 None，不做任何宽度推算。
    """
    word_results = getattr(result, "word_results", None)
    if not word_results:
        return []
    # 单行隔离不跨行拼接；字符框 top 可以因检测倾斜相差 1px，
    # 不能按 top 重排引擎已经给出的行内文字顺序（§14.1）。
    if sum(bool(line_words) for line_words in word_results) != 1:
        return []
    chars: list[_CharBox] = []
    for line_words in word_results:
        if not line_words:
            continue
        for item in line_words:
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                return []
            text, score, box = item
            if box is None or len(box) == 0:
                return []
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            chars.append(
                _CharBox(
                    text=str(text),
                    score=float(score),
                    left=int(math.floor(min(xs))) + offset_x,
                    top=int(math.floor(min(ys))) + offset_y,
                    right=int(math.ceil(max(xs))) + offset_x,
                    bottom=int(math.ceil(max(ys))) + offset_y,
                )
            )
    return chars


def read_name_span(image: QImage, line: OcrLine, *, name: str) -> OcrLine | None:
    """在候选整行框里隔离出「目标套装名」的字符范围（第 3.3 节）。

    行框可能含件数后缀或其他文字，所以不能按行宽 / 字符数 / 中文等宽推算分割点：
    这里只裁剪 `line` 与图像的交集，对裁剪图用本机 RapidOCR 的
    `return_word_box=True, return_single_char_box=True` 读**逐字符**内容与四点框，
    加裁剪偏移还原到客户区像素，再按精确语法取目标名的字符范围。

    接受的完整文本只有两种：归一化后等于 `name`，或等于 `name` + 件数后缀
    `(?:×)?[0-9]+`。目标名之前的文字、其他中文后缀、相似名称（不做
    SequenceMatcher 近似）一律不接受。

    目标字符必须唯一、连续、与框一一对应；对目标字符框并集取外接矩形，左/上向下
    取整、右/下向上取整，裁剪到图像内，且与非目标字符框无正面积交叠。

    返回 `OcrLine(text=name, score=目标字符最小置信度, 坐标为上述矩形)`；
    无字符框、文本不满足精确语法、范围不唯一或边界无法隔离时返回 None。
    """
    normalized_name = _normalize_ocr_name(name)
    if not normalized_name:
        return None

    # 只裁 line 与图像的交集；交集为空就没有可识别的区域。
    left = max(0, int(line.left))
    top = max(0, int(line.top))
    right = min(image.width(), int(line.right))
    bottom = min(image.height(), int(line.bottom))
    if right <= left or bottom <= top:
        return None

    crop = image.copy(left, top, right - left, bottom - top)
    engine = _get_engine()

    # 引擎状态隔离（R25）：进入前记下两个开关的原值，正常/异常退出都恢复；
    # 原异常照原样往外抛，不吞。
    with _WORD_BOX_LOCK:
        saved = {}
        for attr in _WORD_BOX_ATTRS:
            try:
                saved[attr] = getattr(engine, attr)
            except AttributeError:
                saved[attr] = _ATTR_MISSING
        try:
            result = engine(
                qimage_to_bgr(crop),
                return_word_box=True,
                return_single_char_box=True,
            )
        finally:
            for attr, value in saved.items():
                if value is _ATTR_MISSING:
                    if hasattr(engine, attr):
                        delattr(engine, attr)
                else:
                    setattr(engine, attr, value)

    # 归一化：删除空白字符，同时删掉它对应的框，保持字符↔框一一对应。
    chars = [char for char in _char_boxes(result, left, top) if char.text.strip()]
    text = "".join(char.text for char in chars)
    if not text:
        return None

    if text == normalized_name:
        target_count = len(chars)
    elif text.startswith(normalized_name) and _COUNT_SUFFIX_RE.fullmatch(
        text[len(normalized_name) :]
    ):
        target_count = len(normalized_name)
    else:
        return None

    targets = chars[:target_count]
    others = chars[target_count:]
    if not targets or any(len(char.text) != 1 for char in targets):
        # 字符与框必须一一对应：一个框里塞了多个字符就不算隔离成功。
        return None

    span_left = max(0, min(min(char.left for char in targets), image.width()))
    span_top = max(0, min(min(char.top for char in targets), image.height()))
    span_right = max(0, min(max(char.right for char in targets), image.width()))
    span_bottom = max(0, min(max(char.bottom for char in targets), image.height()))
    if span_right <= span_left or span_bottom <= span_top:
        return None

    for char in others:
        overlap_x = min(span_right, char.right) - max(span_left, char.left)
        overlap_y = min(span_bottom, char.bottom) - max(span_top, char.top)
        if overlap_x > 0 and overlap_y > 0:
            return None

    return OcrLine(
        text=name,
        score=min(char.score for char in targets),
        left=span_left,
        top=span_top,
        right=span_right,
        bottom=span_bottom,
    )


def read_name_bands(
    image: QImage, bands: Sequence[tuple[int, int, int, int]]
) -> list[OcrLine | None]:
    """整页完整名称带一次批识别；字符框来自识别引擎，不使用粗检测的残缺 x 框。"""
    from rapidocr.ch_ppocr_rec import TextRecInput

    if not bands:
        return []
    crops = []
    quadrilaterals = []
    for left, top, right, bottom in bands:
        if not (0 <= left < right <= image.width() and 0 <= top < bottom <= image.height()):
            raise OcrError("完整名称带超出图像边界")
        crop = qimage_to_bgr(image.copy(left, top, right - left, bottom - top))
        # 名称灰字的彩色抗锯齿会干扰偏旁识别；保持尺寸和坐标，仅去掉色度。
        crops.append(cv2.cvtColor(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR))
        quadrilaterals.append([[left, top], [right, top], [right, bottom], [left, bottom]])
    engine = _get_engine()
    with _WORD_BOX_LOCK:
        result = engine.text_rec(TextRecInput(img=crops, return_word_box=True))
        if (result.txts is None or result.word_results is None or
                len(result.txts) != len(bands) or len(result.word_results) != len(bands)):
            raise OcrError("整页名称识别返回数量与输入名称带不一致")
        # 直接传单字参数，不修改 RapidOCR 的全局 return_* 开关。
        result = engine.cal_rec_boxes(crops, np.asarray(quadrilaterals, dtype=np.float32), result, True)
    if len(result.word_results) != len(bands):
        raise OcrError("整页名称字符框数量与输入名称带不一致")
    output: list[OcrLine | None] = []
    for band, words in zip(bands, result.word_results):
        chars = [c for c in _char_boxes(SimpleNamespace(word_results=(words,)), 0, 0) if c.text.strip()]
        text = "".join(c.text for c in chars)
        match = re.fullmatch(r"([\u3400-\u9fff]+)(?:×?[0-9]+)?", text)
        if match is None or any(len(c.text) != 1 for c in chars):
            output.append(None)
            continue
        name = match[1]
        targets, others = chars[:len(name)], chars[len(name):]
        left, top = min(c.left for c in targets), min(c.top for c in targets)
        right, bottom = max(c.right for c in targets), max(c.bottom for c in targets)
        if not (band[0] <= left < right <= band[2] and band[1] <= top < bottom <= band[3]):
            output.append(None)
            continue
        if any(min(right, c.right) > max(left, c.left) and min(bottom, c.bottom) > max(top, c.top)
               for c in others):
            output.append(None)
            continue
        output.append(OcrLine(name, min(c.score for c in targets), left, top, right, bottom))
    return output


def _fix_percent(text: str) -> str:
    """OCR 常把 '6%' 读成 '%6'，做兜底纠正。"""
    if text.startswith("%") and len(text) > 1 and text[1:].replace(".", "", 1).isdigit():
        return text[1:] + "%"
    return text


# 常见 OCR 错字的兜底纠正表（错字 -> 正确名）。
#
# 两条硬要求：
#   1. 只放**副词条**的错字。曾有一条「昇常掌控 → 异常掌控」：异常掌控是 6 号位主属性，
#      不在副词条池里，纠正了也会在 _best_match 里被丢掉，纯空转，已删。
#   2. 每条必须是**真替换**（错字 != 正确名）。曾有一条「攻击 → 攻击」：左右同字，
#      replace 之后原样返回，永远不生效，已删 —— 它大概是想纠某个认错的「攻击」，
#      但没写出具体错字，等于没写。
# 守卫见 tests/test_ocr.py（会拦住自我替换与主属性名）。
_SUBSTAT_OCR_FIXES = {
    "昇常精通": "异常精通",
    "穿透値": "穿透值",
    "防街": "防御",
    "生俞": "生命",
}


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

    # 常见 OCR 错字兜底（表在模块级，见 _SUBSTAT_OCR_FIXES）
    for wrong, right in _SUBSTAT_OCR_FIXES.items():
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


# 驱动盘稀有度按**等级上限**区分：S=15、A=12、B=9（游戏固定值）。
# 详情面板那一行是「等级03 / 15」，分母就是上限——不用额外 OCR，也没有颜色阈值要标定。
RARITY_BY_MAX_LEVEL: dict[int, str] = {15: "S", 12: "A", 9: "B"}

# 「等级 03」后面可以跟「/ 15」；上限缺失时 rarity 为 None。
_LEVEL_RE = re.compile(r"等级\s*(\d+)\s*(?:/\s*(\d+))?")
# 强化页有时只识别到纯数字（没有「等级」字样）。必须和 _LEVEL_RE 一样带第 2 个
# （可选上限）分组：否则下面 m.group(2) 会 IndexError: no such group（真崩过）。
_UPGRADE_BARE_LEVEL_RE = re.compile(r"\s*(\d+)\s*(?:/\s*(\d+))?\s*")


def _level_areas(area: tuple[int, int, int, int]):
    """等级区候选，逐项给 (矩形, 该矩形要不要排除带「/」的经验票)。

    强化页等级条从左到右是「当前等级 ┃ 经验 320/2400 ┃ 上限」，三者同一条带子：

    * 主用贴住数字的窄格 `UPGRADE_LEVEL`——便宜，且不受经验条绿填充干扰；
    * 窄格读不出（绿填充涨上来盖住数字，检测整格 0 行；真机 0127/0128 实拍，
      2026-10-04 全链路第 13 件就断在这里）时退回整条 `UPGRADE_LEVEL_BAR`。
      整条带上经验文字反而更稳，但要按 skip_slash 把经验票挑掉——它带「/」，
      否则「0/1440」会被 `_UPGRADE_BARE_LEVEL_RE` 当成等级 0，把 3 级读成 0 级。

    正常帧窄格一次命中，只有少数帧才多花一次识别。
    """
    if area == UPGRADE_LEVEL:
        return ((UPGRADE_LEVEL, False), (UPGRADE_LEVEL_BAR, True))
    return ((area, False),)


def read_level_and_rarity(
    image: QImage, area: tuple[int, int, int, int] = DISC_LEVEL
) -> tuple[int | None, str | None]:
    """一次读出等级和稀有度，返回 (等级, 稀有度)。

    稀有度由等级上限推出（15→S、12→A、9→B）；上限读不出或不在已知档位时返回 None，
    调用方按「未知」处理、不据此跳过。等级与稀有度共用同一次识别，不额外加 OCR。

    传 UPGRADE_LEVEL 时按 `_level_areas` 依次试窄格与整条（见该函数）。
    """
    for rect, skip_slash in _level_areas(area):
        for line in read_rect(image, rect):
            if skip_slash and "/" in line.text:
                continue
            m = _LEVEL_RE.search(line.text)
            if m is None and area == UPGRADE_LEVEL:
                m = _UPGRADE_BARE_LEVEL_RE.fullmatch(line.text)
            if m is None:
                continue
            level = int(m.group(1))
            if not 0 <= level <= MAX_LEVEL:
                continue
            cap = m.group(2)
            rarity = RARITY_BY_MAX_LEVEL.get(int(cap)) if cap else None
            return level, rarity
    return None, None


def read_level(image: QImage, area: tuple[int, int, int, int] = DISC_LEVEL) -> int | None:
    """读取等级数字（默认详情面板，可传强化页区域），返回 0~15 的数字。"""
    return read_level_and_rarity(image, area)[0]


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


#: R/T 提示的两个状态：提示关键词 + 「已生效」词。
#: 真机实测（86 帧仓库页截图）：
#: 未锁定 =「T 锁定」、已锁定 =「T 解除锁定」；未弃置 =「R 弃置」、已弃置 =「R 解除弃置」。
#: **「解除」是两种状态共同的已生效词**，必须按它出现在哪个提示里归属——旧实现
#: 「整行出现解除就判锁定」，把 26/26 帧已弃置盘判成了「已锁定、未弃置」。
#: 「取消弃置」是旧文档记的写法，真机未见过，保留为兼容写法。
_ACTION_PROMPTS = (
    ("locked", "锁定", ("解除",)),
    ("discarded", "弃置", ("解除", "取消")),
)


def _nearest_prompt(line, lines, anchors) -> str | None:
    """把单独成行的「解除 / 取消」归到横向最近的提示；没有任何提示行时返回 None。

    OCR 偶尔把「T」「解除锁定」拆成两行，更极端时会拆成「解除」「锁定」两行；
    这时靠「解除」与哪组提示关键词在同一列附近来归属，而不是整行乱认。
    """
    best_key = None
    best_dx = None
    for key, indices in anchors.items():
        for index in indices:
            dx = abs(((line.left + line.right) - (lines[index].left + lines[index].right)) / 2.0)
            if best_dx is None or dx < best_dx:
                best_dx, best_key = dx, key
    return best_key


def read_action_state(image: QImage) -> dict[str, bool | None]:
    """读取仓库页右下角的 R/T 提示文字，判断锁定/弃置状态。

    未锁定显示「T 锁定」、已锁定显示「T 解除锁定」；未弃置显示「R 弃置」、
    已弃置显示「R 解除弃置」（真机实测写法，不是「取消弃置」）。

    判读规则：
    - 含「锁定」的行属于 T 提示、含「弃置」的行属于 R 提示，各自判断「解除 /
      取消」是否出现在自己的提示里，互不串判；
    - 认不出的提示返回 **None（未知）**，不默认成「未生效」。调用方据此重读，
      仍未知时停止或拒绝盲按标签按键（未知不能当作未标记）。

    真机 86 帧对账（面板文字 vs 格位标记）在本规则下 86/86 一致。
    """
    lines = read_rect(image, ACTION_ROW)
    texts = [line.text.replace(" ", "") for line in lines]
    anchors = {
        key: [index for index, text in enumerate(texts) if keyword in text]
        for key, keyword, _words in _ACTION_PROMPTS
    }
    state: dict[str, bool | None] = {}
    for key, keyword, active_words in _ACTION_PROMPTS:
        group = anchors[key]
        active = any(any(word in texts[index] for word in active_words) for index in group)
        if not active:
            # 「解除 / 取消」单独成行：按与哪组提示最近归属；自带提示词的行已按组处理。
            for index, text in enumerate(texts):
                if not any(word in text for word in active_words):
                    continue
                if any(prompt in text for _k, prompt, _w in _ACTION_PROMPTS):
                    continue
                if _nearest_prompt(lines[index], lines, anchors) == key:
                    active = True
                    break
        state[key] = True if active else (False if group else None)
    return state


def read_action_state_fast(image: QImage) -> dict[str, bool | None]:
    """固定提示行直接识别；正常标签复核不做检测、不读取盘面。"""
    from rapidocr.ch_ppocr_rec import TextRecInput

    unknown = {"locked": None, "discarded": None}
    if image.isNull() or image.width() * 9 != image.height() * 16:
        return unknown
    left, top, right, bottom = scale_rect(*ACTION_TEXT_LINE, image.width(), image.height())
    crop = qimage_to_bgr(image.copy(left, top, right - left, bottom - top))
    with _WORD_BOX_LOCK:
        # 性能记录：这一行是快路径实际送识别网络的一次识别（§7.1、§7.1.1）。
        # 共用 R/T 整行只裁一次、只识别一次，因此 batch_size 固定为 1。
        with scanperf.timed(
            "recognize_batch",
            details={"roi_ids": ["action_line"], "batch_size": 1, "profile_id": None},
        ) as span:
            result = _get_engine().text_rec(TextRecInput(img=[crop]))
            span.set(batch_size=1)
    if result.txts is None or result.scores is None or len(result.txts) != 1 or len(result.scores) != 1:
        raise OcrError("标签行识别返回数量不正确")
    score = float(result.scores[0])
    if not math.isfinite(score) or score < 0.90:
        return unknown
    text = re.sub(r"\s+", "", result.txts[0])
    # 完整提示语法；残缺「解除」或杂字不能退化成未标记。
    match = re.fullmatch(r"R?(解除弃置|取消弃置|弃置)T?(解除锁定|锁定)", text)
    if match is not None:
        return {"discarded": match[1] != "弃置", "locked": match[2] != "锁定"}
    # 某些页面只显示一个提示，另一个保持未知，调用方不能盲按。
    match = re.fullmatch(r"R?(解除弃置|取消弃置|弃置)", text)
    if match is not None:
        return {"discarded": match[1] != "弃置", "locked": None}
    match = re.fullmatch(r"T?(解除锁定|锁定)", text)
    if match is not None:
        return {"discarded": None, "locked": match[1] != "锁定"}
    return unknown


def _parse_disc_legacy(image: QImage, *, reject_reason: str | None = None, profile_id: str | None = None) -> Disc | None:
    """旧整帧解析器：从驱动盘仓库页（右侧详情面板）截图解析出一个 Disc。

    §2.2 的 legacy：完整保存「启用盘面快路径前」的算法，不改动。
    失败返回 None，调用方应中断流程。

    ``reject_reason`` / ``profile_id``：公开入口拒绝后调用时带上 §3.2 的原因与布局；
    基准直接调用时都为 None（§7.1.1）。它们只用于性能记录的 ``legacy_parse`` 事件，
    不参与解析。
    """
    with scanperf.timed(
        "legacy_parse",
        details={"parser": "detail", "reject_reason": reject_reason, "profile_id": profile_id},
    ) as span:
        result = _parse_disc_legacy_impl(image)
        if result is None:
            span.set(outcome="returned_none")
        return result


def _parse_disc_legacy_impl(image: QImage) -> Disc | None:
    # 只有在等级标签成功匹配时才选择布局，避免把套装说明里的数字当等级。
    level, rarity = read_level_and_rarity(image, WAREHOUSE_DISC_LEVEL)
    area = WAREHOUSE_SUB_STATS
    if level is None:
        level, rarity = read_level_and_rarity(image)
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
        rarity=rarity,
        locked=actions["locked"],
        discarded=actions["discarded"],
    )


def _parse_disc_upgrade_page_legacy(
    image: QImage, *, reject_reason: str | None = None, profile_id: str | None = None
) -> Disc | None:
    """旧整帧解析器：从强化页截图解析 Disc（强化页副属性/等级坐标）。

    §2.2 的 legacy：完整保存「启用盘面快路径前」的算法，不改动。
    强化页没有 R/T 按钮，弃置在详情页做；这里只读等级+副属性供 decide 判断
    升级还是弃置。稀有度同理由等级上限推出，读不出就是 None（未知，不据此跳过）。
    失败返回 None。``reject_reason`` / ``profile_id`` 同上，只用于 ``legacy_parse``。
    """
    with scanperf.timed(
        "legacy_parse",
        details={"parser": "upgrade", "reject_reason": reject_reason, "profile_id": profile_id},
    ) as span:
        result = _parse_disc_upgrade_page_legacy_impl(image)
        if result is None:
            span.set(outcome="returned_none")
        return result


def _parse_disc_upgrade_page_legacy_impl(image: QImage) -> Disc | None:
    level, rarity = read_level_and_rarity(image, UPGRADE_LEVEL)
    if level is None:
        return None
    substats = read_substats(image, UPGRADE_SUB_STATS, UPGRADE_SUB_SPLIT_X)
    if not substats:
        return None
    return Disc(level=level, substats=substats, rarity=rarity, locked=False, discarded=False)


# ---------------------------------------------------------------------------
# 公开解析入口（§2.2 / §3.2）
#
# 步骤 1 只完成 legacy 提取；步骤 3 起改为「先 try_read_disc 成功则返回其 disc；
# 拒绝则同帧调用对应 _legacy」。快路径只省识别开销，不改旧解析结果：提取后的
# legacy 逐字段不变，识别网络自身抛出的异常按 §6.1.1 原样传播，不折算成拒绝原因。
# ---------------------------------------------------------------------------


def parse_disc(image: QImage) -> Disc | None:
    """从驱动盘仓库页（右侧详情面板）截图解析出一个 Disc。

    公开入口：先在**同一帧**上走快路径（`discocr.try_read_disc`），成功就直接返回
    它的 Disc；拒绝或异常以外的任何拒绝原因都回退到旧整帧解析器 `_parse_disc_legacy`。
    失败返回 None，调用方应中断流程。

    性能：整段包一个 ``parse_total``（§7.1）；模板/批量识别/legacy 是它的子事件。
    """
    from zzz_ddtool import discocr

    with scanperf.timed(
        "parse_total",
        details={"profile_id": None, "reject_reason": None, "template_hits": 0, "ocr_lines": 0},
    ) as span:
        fast = discocr.try_read_disc(image, layout="detail")
        span.set(profile_id=fast.profile_id, reject_reason=fast.reject_reason)
        if fast.disc is not None:
            return fast.disc
        result = _parse_disc_legacy(
            image, reject_reason=fast.reject_reason, profile_id=fast.profile_id
        )
        if result is None:
            span.set(outcome="returned_none")
        return result


def parse_disc_upgrade_page(image: QImage) -> Disc | None:
    """从强化页截图解析 Disc（强化页副属性/等级坐标）。

    公开入口：先在同帧上走 `layout="upgrade"` 的快路径，拒绝则回退强化页 legacy。
    本入口**不调用** detail 入口（§3.2）；满级后回退详情解析由 Runner 的
    `_parse_upgrade_page` 负责。失败返回 None。性能同 `parse_disc`。
    """
    from zzz_ddtool import discocr

    with scanperf.timed(
        "parse_total",
        details={"profile_id": None, "reject_reason": None, "template_hits": 0, "ocr_lines": 0},
    ) as span:
        fast = discocr.try_read_disc(image, layout="upgrade")
        span.set(profile_id=fast.profile_id, reject_reason=fast.reject_reason)
        if fast.disc is not None:
            return fast.disc
        result = _parse_disc_upgrade_page_legacy(
            image, reject_reason=fast.reject_reason, profile_id=fast.profile_id
        )
        if result is None:
            span.set(outcome="returned_none")
        return result
