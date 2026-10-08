"""套装弹窗路径学习与回放的数据结构（spec §3）。

本模块只放不可变记录与校验，不放算法、不放进程级共享可变缓存：

* `SetPickerKey` / `SetPickerLayout` / `SetPickerIdentity` / `SetPickerClick` /
  `SetPickerStage` / `SetPickerRoute`：创建后不再变化；
* `FilterReplaySession`：每次 Runner._run 新建、同一任务链共享、收尾清空；
* `FilterEvidence`：仅供开发验收消费的关键帧记录（spec §8.4）。

坐标语义：`SetPickerLayout.list_area` / `confirm_area` 是 1920x1080 设计坐标
（与 ocr.read_rect 同一约定）；其余坐标都是**实际客户区像素**，不得从另一台机器
或另一种尺寸缩放后当作已验证缓存使用（spec §3 开头）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from PySide6.QtGui import QImage


@dataclass(frozen=True)
class SetPickerKey:
    """套装弹窗缓存键（spec §3.1）。缓存键不含号位、主副词条、任务 ID。"""

    hwnd: int
    pid: int
    width: int
    height: int
    dpi: int
    layout_id: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class SetPickerLayout:
    """已标定布局（spec §3.2）。数值以静态图片与实测标定为输入。"""

    layout_id: str
    list_area: tuple[int, int, int, int]
    confirm_area: tuple[int, int, int, int]
    origin_mode: Literal["reopen_top", "wheel_top"]
    top_wheel_events: int
    top_delta_units: int
    scroll_x: int
    scroll_y: int
    scroll_wait_ms: int
    click_wait_ms: int
    confirm_wait_ms: int


@dataclass(frozen=True)
class SetPickerIdentity:
    """名称首次精确识别的未选状态文字指纹（spec §3.3/§13）。只在内存里，不落盘。"""

    left: int
    top: int
    right: int
    bottom: int
    width: int
    height: int
    glyph_mask: bytes


@dataclass(frozen=True)
class SetPickerClick:
    """一次套装选项点击：文字框、点击中心与身份指纹（spec §3.3）。"""

    name: str
    left: int
    top: int
    right: int
    bottom: int
    x: int
    y: int
    identity: SetPickerIdentity


@dataclass(frozen=True)
class SetPickerStage:
    """一个可见选择段的滚动与点击（spec §3.4）。回顶事件不属于 stage。"""

    wheel_events: int
    delta_units: int
    clicks: tuple[SetPickerClick, ...]


@dataclass(frozen=True)
class SetPickerRoute:
    """一条完整的套装弹窗路径（spec §3.5）。"""

    key: SetPickerKey
    layout: SetPickerLayout
    stages: tuple[SetPickerStage, ...]
    confirm_x: int
    confirm_y: int
    confirm_identity: SetPickerIdentity


@dataclass
class FilterReplaySession:
    """本次任务链的已验证路径（spec §3.6）。Runner._run 新建、收尾清空。"""

    routes: dict[SetPickerKey, SetPickerRoute] = field(default_factory=dict)
    last_metrics: dict[str, object] | None = None


@dataclass(frozen=True)
class FilterEvidence:
    """开发验收用的关键帧记录（spec §8.4）。image 是原帧副本，sink 不得修改。"""

    event: Literal["set_stage_selected", "substats_selected"]
    stage_index: int | None
    targets: tuple[str | int, ...]
    boxes: tuple[tuple[int, int, int, int], ...]
    image: QImage


def normalize_set_names(names: Sequence[str]) -> tuple[str, ...]:
    """按 spec §3.1/§4.1 归一化套装名：去首尾空白 → 非空 → 去重 → 排序。

    非法输入抛 ValueError，由调用方（gamefilter）在第一条游戏输入之前转成
    FilterClickError。排序只用于缓存键；首次实际执行的点击顺序保存在路径里。
    """
    stripped = tuple(name.strip() for name in names)
    if any(not name for name in stripped):
        raise ValueError("套装名不能为空")
    if len(set(stripped)) != len(stripped):
        raise ValueError(f"套装名重复：{list(stripped)}")
    return tuple(sorted(stripped))


# ---------------------------------------------------------------- 字形指纹
# 判据按 Spec §10 第 2 项由实测标定（样本见 手册/参考图/驱动盘筛选/裁剪/ 与说明）；§3.3 锁定的是
# 「精确名称范围、数据含义、三态与样本结果」，阈值本身可标定。
#
# 2026-10-07 真机核查（套装名指纹-真机对比度问题-核查结论.md）：
#   名称文字画在与字形等大的浅灰底板上（底板 ≈95、字亮部 ≈128）。旧判据「中位数 + 38」把阈值顶到
#   字形之上 → 空掩码；而按暗部估背景又会把底板/外围一并吞进前景 → 换名 IoU 0.72—0.79 被误判为 True；
#   只取「中段—高位」的中点仍偏松（换名样本 0.51，落进不确定带）。
#   最终：只保留最亮的文字核心（阈值 = p95 − 偏移），并先要求 p95 与 p50 有足够对比度，
#   否则视为「没有可判文字」（空掩码 → 未知）。
_IDENTITY_FOREGROUND_HIGH = 0.95   # 文字亮部所在分位
_IDENTITY_HIGH_OFFSET = 6          # 亮部再往下的偏移
_IDENTITY_MIN_CONTRAST = 12        # 亮部与中段的对比度下限；低于此值没有可判文字
_IDENTITY_MIN_COVERAGE = 0.02
_IDENTITY_MAX_COVERAGE = 0.60
# 匹配带 ±1px 平移容差（同一格子在两次绘制间可能有亚像素偏移）；
# 同一名称实测 IoU=1.00，换名用新判据实测 ≤0.43（核查结论 §4），阈值取在中间。
_IDENTITY_MATCH_IOU = 0.60
_IDENTITY_REJECT_IOU = 0.45
_IDENTITY_SHIFT = 1


def _glyph_mask(crop: QImage) -> tuple[bytes, float]:
    """把裁剪图转成 0/1 字形掩码，并返回前景占比（§3.3 前景提取，阈值见上）。

    只保留明显亮于底板的文字核心；对比度不足或没有亮部的裁剪返回空掩码，由调用方判为未知。
    """
    width, height = crop.width(), crop.height()
    if width <= 0 or height <= 0:
        return b"", 0.0
    luma = []
    for y in range(height):
        for x in range(width):
            color = crop.pixelColor(x, y)
            luma.append(0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue())
    ordered = sorted(luma)
    mid = ordered[len(ordered) // 2]
    high = ordered[min(len(ordered) - 1, int(len(ordered) * _IDENTITY_FOREGROUND_HIGH))]
    if high - mid < _IDENTITY_MIN_CONTRAST:
        return b"", 0.0
    threshold = high - _IDENTITY_HIGH_OFFSET
    mask = bytes(1 if value > threshold else 0 for value in luma)
    coverage = sum(mask) / len(mask)
    return mask, coverage


def make_identity(image: QImage, box: tuple[int, int, int, int]) -> SetPickerIdentity | None:
    """从「首次精确识别的未选状态」名称框生成内存文字指纹（spec §3.3/§13）。

    裁剪范围就是 `read_name_span` 返回的精确名称范围，不外扩（§3.3：排除非目标内容；
    真机核查后已去掉 2026-10-07 试过的外扩回退——它会把底板与外围像素带进身份前景，
    导致换名被接受）。前景提取见 `_glyph_mask`。
    """
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    mask, coverage = _glyph_mask(image.copy(left, top, width, height))
    if not mask or coverage < _IDENTITY_MIN_COVERAGE or coverage > _IDENTITY_MAX_COVERAGE:
        return None
    return SetPickerIdentity(
        left=left, top=top, right=right, bottom=bottom, width=width, height=height, glyph_mask=mask
    )


def identity_score(image: QImage, identity: SetPickerIdentity) -> float | None:
    """当前帧在身份框处的字形与指纹的最好 IoU（±1px 平移容差）。

    无法判读（空白/遮挡/满框）返回 None。
    """
    width, height = identity.width, identity.height
    mask, coverage = _glyph_mask(image.copy(identity.left, identity.top, width, height))
    if not mask or coverage < _IDENTITY_MIN_COVERAGE or coverage > _IDENTITY_MAX_COVERAGE:
        return None
    reference = identity.glyph_mask
    best: float | None = None
    span = _IDENTITY_SHIFT
    for dy in range(-span, span + 1):
        for dx in range(-span, span + 1):
            intersection = union = 0
            for y in range(height):
                sy = y + dy
                if not (0 <= sy < height):
                    continue
                base_ref = y * width
                base_src = sy * width
                for x in range(width):
                    sx = x + dx
                    if not (0 <= sx < width):
                        continue
                    a = reference[base_ref + x]
                    b = mask[base_src + sx]
                    if a or b:
                        union += 1
                        if a and b:
                            intersection += 1
            if union:
                score = intersection / union
                if best is None or score > best:
                    best = score
    return best


def identity_matches(image: QImage, identity: SetPickerIdentity) -> bool | None:
    """三态身份判读：True 匹配、False 明确不符、None 未知（spec §3.3/§4.2）。"""
    score = identity_score(image, identity)
    if score is None:
        return None
    if score >= _IDENTITY_MATCH_IOU:
        return True
    if score <= _IDENTITY_REJECT_IOU:
        return False
    return None


def validate_layout(layout: SetPickerLayout, *, width: int, height: int) -> None:
    """校验布局自身不自相矛盾（spec §3.2）。不合法抛 ValueError。"""
    if layout.origin_mode not in ("reopen_top", "wheel_top"):
        raise ValueError(f"未知起始位置模式：{layout.origin_mode!r}")
    for name, area in (("list_area", layout.list_area), ("confirm_area", layout.confirm_area)):
        left, top, right, bottom = area
        if right <= left or bottom <= top:
            raise ValueError(f"{name} 不是有效矩形：{area}")
        if not (0 <= left and 0 <= top and right <= width and bottom <= height):
            raise ValueError(f"{name} 超出图像范围：{area}")
        if (right - left) >= width and (bottom - top) >= height:
            raise ValueError(f"{name} 必须是整张图的一部分：{area}")
    if layout.origin_mode == "reopen_top":
        if layout.top_wheel_events != 0 or layout.top_delta_units != 0:
            raise ValueError("reopen_top 的 top_wheel_events/top_delta_units 必须为 0")
    else:
        if layout.top_wheel_events <= 0 or layout.top_delta_units <= 0:
            raise ValueError("wheel_top 的 top_wheel_events/top_delta_units 必须大于 0")
    for name, value in (
        ("scroll_wait_ms", layout.scroll_wait_ms),
        ("click_wait_ms", layout.click_wait_ms),
        ("confirm_wait_ms", layout.confirm_wait_ms),
    ):
        if value < 0:
            raise ValueError(f"{name} 不能为负：{value}")


def validate_route(route: SetPickerRoute) -> None:
    """校验路径自洽（spec §3.5）：clicks 名称集合恰等于 key.names，各出现一次。"""
    clicked: list[str] = []
    for stage in route.stages:
        if stage.wheel_events < 0 or (stage.wheel_events == 0 and stage.delta_units != 0):
            raise ValueError(f"stage 滚动字段不自洽：{stage}")
        for click in stage.clicks:
            clicked.append(click.name)
    if sorted(clicked) != sorted(route.key.names) or len(set(clicked)) != len(clicked):
        raise ValueError(f"路径点击集合与目标不符：{clicked} != {list(route.key.names)}")
