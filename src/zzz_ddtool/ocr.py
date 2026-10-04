"""本地 OCR：只对指定 ROI 做识别，不做全图。

引擎用 rapidocr 自带的 PP-OCRv6 small（det 9.5M + rec 20M），离线、不联网。
全图识别会把左侧网格的图标也当字认，所以这里一律先裁再识别。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

import cv2
import numpy as np
from PySide6.QtGui import QImage

from zzz_ddtool.layout import (
    ACTION_ROW,
    DISC_LEVEL,
    SUB_STAT_SPLIT_X,
    SUB_STATS,
    UPGRADE_LEVEL,
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


def read_level_and_rarity(
    image: QImage, area: tuple[int, int, int, int] = DISC_LEVEL
) -> tuple[int | None, str | None]:
    """一次读出等级和稀有度，返回 (等级, 稀有度)。

    稀有度由等级上限推出（15→S、12→A、9→B）；上限读不出或不在已知档位时返回 None，
    调用方按「未知」处理、不据此跳过。等级与稀有度共用同一次识别，不额外加 OCR。
    """
    for line in read_rect(image, area):
        m = _LEVEL_RE.search(line.text)
        if m is None and area == UPGRADE_LEVEL:
            m = re.fullmatch(r"\s*(\d+)\s*", line.text)
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


def read_action_state(image: QImage) -> dict[str, bool]:
    """读取仓库页右下角的 R/T 提示文字，判断锁定/弃置状态。

    未锁定显示「T 锁定」、已锁定显示「T 解除锁定」；未弃置显示「R 弃置」、
    已弃置显示「R 取消弃置」。

    按「解除」「取消」这类**只在已生效状态出现**的词判断，不硬匹配整串：
    OCR 经常把「T」和后面的词拆成两行（实测「T」「锁定」各一行），
    整串匹配「解除锁定」会漏。未生效状态里不含这两个词，所以不会误判。
    """
    state = {"locked": False, "discarded": False}
    for line in read_rect(image, ACTION_ROW):
        text = line.text
        if "解除" in text:
            state["locked"] = True
        if "取消" in text or text == "已弃置":
            state["discarded"] = True
    return state


def parse_disc(image: QImage) -> Disc | None:
    """从驱动盘仓库页（右侧详情面板）截图解析出一个 Disc。

    失败返回 None，调用方应中断流程。
    """
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


def parse_disc_upgrade_page(image: QImage) -> Disc | None:
    """从强化页截图解析 Disc（强化页副属性/等级坐标）。

    强化页没有 R/T 按钮，弃置在详情页做；这里只读等级+副属性供 decide 判断
    升级还是弃置。稀有度同理由等级上限推出，读不出就是 None（未知，不据此跳过）。
    失败返回 None。
    """
    level, rarity = read_level_and_rarity(image, UPGRADE_LEVEL)
    if level is None:
        return None
    substats = read_substats(image, UPGRADE_SUB_STATS, UPGRADE_SUB_SPLIT_X)
    if not substats:
        return None
    return Disc(level=level, substats=substats, rarity=rarity, locked=False, discarded=False)
