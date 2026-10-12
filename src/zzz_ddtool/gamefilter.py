from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from difflib import SequenceMatcher
from types import SimpleNamespace

from PySide6.QtGui import QImage

from zzz_ddtool.capture import client_size, find_game_hwnd, grab_game_image, window_pid
from zzz_ddtool.control import checkpoint_control, validated_inputs
from zzz_ddtool.filter_replay import (
    FilterEvidence,
    FilterReplaySession,
    SetPickerClick,
    SetPickerIdentity,
    SetPickerKey,
    SetPickerLayout,
    SetPickerRoute,
    SetPickerStage,
    identity_matches,
    make_identity,
    normalize_set_names,
    validate_route,
)
from zzz_ddtool.gamedata import SUBSTAT_NAMES
from zzz_ddtool.layout import (
    DISMANTLE_FORBID,
    FILTER_BTN,
    scale_point,
    scale_rect,
)
from zzz_ddtool import loground, picker_names
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.ocr import OcrLine, read_name_bands, read_name_span, read_rect
from zzz_ddtool.actionwait import InputRelease, wait_release
from zzz_ddtool.wininput import (
    VK_ESCAPE,
    click_client,
    client_to_screen,
    focus_window,
    input_context,
    press_key,
    send_wheel_event,
    user32,
    wheel_client,
)

# 筛选面板里「套装 → 点击选择」条的搜索区（面板右半屏）
FILTER_SETS_BAR = (1300, 260, 1860, 380)
# 「分区[N]」按钮搜索区。未滚动时在 y≈766-876，滚动后在 y≈429-574，两态都盖住
FILTER_SLOT_AREA = (1300, 380, 1860, 950)
# 面板底部「重置」按钮搜索区（避开右边的批量操作）
FILTER_RESET_AREA = (1250, 990, 1650, 1080)
# 弹层 / 词条按钮的全窗口搜索区。认不上的话宁可中断。
FULL_WINDOW = (0, 0, 1920, 1080)
# 滚动找词条时的尝试上限
_MAX_SCROLL_ROUNDS = 8
# 筛选面板中部滚轮落点。面板内容比视口高，「主属性词条」区默认在视口外。
FILTER_PANEL_SCROLL = (1560, 500)
# 主属性 / 副属性词条区的标题文字，用来框定主属性选项的纵向范围。
MAINSTAT_TITLE = "主属性词条"
SUBSTAT_TITLE = "副属性词条"
# 把面板滚到主属性区的尝试上限（真机实测 3~4 次滚轮即到底）
_MAX_PANEL_SCROLLS = 8
# 面板最下方有一条固定高度的「点击无效带」：落在其中的点击游戏完全不响应（真机实测，
# 非本工具的坐标 bug）。实测边界（2026-10-04）：按钮中心 y=942 点不动、y=901 能点，
# 副属性「展开」按钮中心 y=916 点不动——即中心 y≳910 一律无效。
# 所以面板内任何要点的东西，其底边低于 height-160 时，必须**先滚动把它顶上来再点**，
# 而不是点了没反应再补救（那样必然失败）。
_PANEL_CLICK_BOTTOM_GAP = 160
# 面板一次滑到底连发的滚轮事件条数。真机实测一格约 41px、内容比视口高出约 300px，
# 8 条足够到底（到顶后会停住，多发无害）。宁可一次发到位，也不要「滚一格→抓图→再滚」。
_PANEL_SCROLL_NOTCHES = 8

#: 2026-10-12 作者裁定（**勿重复叠加**）：最终等待 = 真机实测值（见《等待时间表》第 14 轮）
#: + 本保底。调用点一律写成「实测值 + _MEASURED_SAFETY_MS」；日后调保底只改这里，
#: 不准再往任何数值里手写 +200。
_MEASURED_SAFETY_MS = 200
# 套装滚动采用用户已修正的学习预算；学习/回放/回顶/legacy共用，避免坐标基准不同。
_SET_SCROLL_WAIT_MS = 784  # 最终预算，不再叠加200ms
#: 连续点击：组内零等待且不检测，最后一个实际点击按其动作的实测值等待。
#: 四项沿用组动作不叠加200ms的约定；来源《等待时间表》第14轮。
_SET_OPTION_TAIL_WAIT_MS = 87
_MAINSTAT_OPTION_TAIL_WAIT_MS = 160
_SUBSTAT_OPTION_TAIL_WAIT_MS = 93
_SUBSTAT_COUNT_TAIL_WAIT_MS = 86
# 主属性名与词条名的相似度下限，低于它宁可中断也不猜（手册 01 底线）
_MATCH_CUTOFF = 0.75


class FilterClickError(RuntimeError):
    pass


# ---------------- 套装路径：分派、学习、指标（spec §5） ----------------

# 每轮一次 wheel_client（delta=-8，一条 -960 原始事件）。
# 本机实测约 447px、行距约 150px，即三行；底部由游戏截断，保留重叠行。
_PICKER_SCROLL_EVENTS = 1
_PICKER_SCROLL_DELTA = -8
_PICKER_MATCH_CUTOFF = 0.75
_PICKER_MAX_ROUNDS = 8
_PICKER_OBSERVE_FRAMES = 3
# 2026-10-11 用户裁定：判读/观察延迟一律 50 ms（与原先取低值；原先 0.1 s）。
_PICKER_OBSERVE_INTERVAL_S = 0.05
_PICKER_CONFIRM_TEXT = "确认筛选"
# 名称带近似命中但未能精确隔离时的重拍重读上限（用户决定 2026-10-11）：
# 识别错误不点击，重拍新帧再认，认对一次即用；上限内仍无法精确隔离才按 R21 停止。
_PICKER_NAME_READ_ATTEMPTS = 5
# 重拍前的短暂等待：给游戏一帧新的呈现，不改变任何点击/滚动时序。
# 2026-10-11 用户裁定：判读/观察延迟一律 50 ms（与原先取低值；原先 0.1 s）。
_PICKER_NAME_RETRY_DELAY_S = 0.05


class _PickerMetrics:
    """套装阶段指标（spec §5.4）。字段与日志标记固定，文本说明可变。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.search_ocr_calls = 0
        self.verification_frames = 0
        self.wheel_events = 0
        self.selection_clicks = 0
        self.success = False
        self._started = time.monotonic()

    def payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "success": bool(self.success),
            "search_ocr_calls": self.search_ocr_calls,
            "verification_frames": self.verification_frames,
            "wheel_events": self.wheel_events,
            "selection_clicks": self.selection_clicks,
            "elapsed_ms": int((time.monotonic() - self._started) * 1000),
        }


def _log_picker_metrics(metrics: _PickerMetrics) -> None:
    payload = metrics.payload()
    get_logger("zzz_ddtool.gamefilter").info(
        "set_picker_metrics=%s", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _window_dpi(hwnd: int) -> int:
    try:
        return int(user32.GetDpiForWindow(hwnd))
    except Exception:
        return 0


def _calibrated_layout(width: int, height: int) -> SetPickerLayout | None:
    """生产标定布局（模块内数据；与静态清单的 1920x1080-v8 同值）。"""
    if (width, height) != (1920, 1080):
        return None
    return SetPickerLayout(
        layout_id="1920x1080-v8",
        list_area=(800, 340, 1490, 840),
        confirm_area=(1040, 838, 1260, 900),
        origin_mode="reopen_top",
        top_wheel_events=0,
        top_delta_units=0,
        scroll_x=960,
        scroll_y=540,
        scroll_wait_ms=600,
        click_wait_ms=400,
        confirm_wait_ms=500,
    )


def _picker_key(
    hwnd: int, width: int, height: int, names: Sequence[str], layout: SetPickerLayout
) -> SetPickerKey | None:
    dpi = _window_dpi(hwnd)
    if dpi <= 0:
        return None
    try:
        names_key = normalize_set_names(names)
    except ValueError as exc:
        raise FilterClickError(str(exc)) from exc
    return SetPickerKey(
        hwnd=int(hwnd),
        pid=int(window_pid(hwnd)),
        width=width,
        height=height,
        dpi=dpi,
        layout_id=layout.layout_id,
        names=names_key,
    )


def _invalidate_session_routes(session: FilterReplaySession, hwnd: int, width: int, height: int, dpi: int) -> None:
    """窗口键变化时清空会话路径（spec §5.3）：hwnd/pid/尺寸/DPI 任一变化都不复用旧坐标。"""
    if not session.routes:
        return
    pid = int(window_pid(hwnd))
    for key in list(session.routes):
        if (key.hwnd, key.pid, key.width, key.height, key.dpi) != (hwnd, pid, width, height, dpi):
            session.routes.pop(key, None)


def _normalize_match_text(text: str) -> str:
    return text.replace(" ", "").strip()


def _match_targets(lines: list, pending: Sequence[str]) -> dict[str, object] | None:
    """§5.1.2 的匹配：完全相等 (3,1.0)＞包含 (2,占比)＞相似 (1,ratio≥0.75)；

    按元组原值取唯一最大值；两个不同框最大元组相等（冲突）返回 None；
    两个目标命中同一文字框也返回 None。
    """
    result: dict[str, object] = {}
    for name in pending:
        target = _normalize_match_text(name)
        winner = None
        conflict = False
        for line in lines:
            text = _normalize_match_text(line.text)
            if not text:
                continue
            if text == target:
                score = (3, 1.0)
            elif target in text:
                score = (2, len(target) / len(text))
            else:
                ratio = SequenceMatcher(None, target, text).ratio()
                if ratio < _PICKER_MATCH_CUTOFF:
                    continue
                score = (1, ratio)
            if winner is None or score > winner[0]:
                winner = (score, line)
                conflict = False
            elif score == winner[0]:
                conflict = True
        if winner is None:
            continue
        if conflict:
            return None
        result[name] = winner[1]
    boxes = {
        (line.left, line.top, line.right, line.bottom): name
        for name, line in result.items()
    }
    if len(boxes) != len(result):
        return None
    return result


def _observe(metrics: _PickerMetrics, predicate, *, key: SetPickerKey | None = None) -> bool | None:
    """三态观察（§4.2）：明确结果立即返回；未知最多共拍 3 张，追加帧间隔 100ms，期间不补发输入。"""
    for attempt in range(_PICKER_OBSERVE_FRAMES):
        image, hwnd = _grab()
        metrics.verification_frames += 1
        if key is not None and (
            hwnd != key.hwnd or (image.width(), image.height()) != (key.width, key.height)
            or window_pid(hwnd) != key.pid or _window_dpi(hwnd) != key.dpi
        ):
            return False
        result = predicate(image)
        if result is not None:
            return result
        if attempt < _PICKER_OBSERVE_FRAMES - 1:
            time.sleep(_PICKER_OBSERVE_INTERVAL_S)
    return None


def _boxes_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _click_ref(box: tuple[int, int, int, int]):
    left, top, right, bottom = box
    return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)


# ---------------- 关键帧取证（spec §8.4；仅供开发验收消费，生产未启用时不加帧） ----------------

_evidence_sink: ContextVar = ContextVar("filter_evidence_sink", default=None)


@contextmanager
def _capture_filter_evidence(sink):
    """进入时装上关键帧 sink（执行上下文隔离）；退出（含异常与 F10）按 token 恢复，不串上下文。"""
    token = _evidence_sink.set(sink)
    try:
        yield
    finally:
        _evidence_sink.reset(token)


def _evidence_enabled() -> bool:
    """当前执行上下文是否启用了取证（其他线程/独立上下文不互相影响）。"""
    return _evidence_sink.get() is not None


def _emit_evidence(event: str, stage_index: int | None, targets, boxes, image: QImage) -> None:
    sink = _evidence_sink.get()
    if sink is None:
        return
    sink(
        FilterEvidence(
            event=event,
            stage_index=stage_index,
            targets=tuple(targets),
            boxes=tuple(boxes),
            image=image.copy(),
        )
    )


def _pre_click_state(image: QImage, click, identity: SetPickerIdentity) -> bool | None:
    """§13：套装包括灰色零件数选项均可点，仅确认名称身份，不判断勾选状态。"""
    identity_ok = identity_matches(image, identity)
    if identity_ok is not True:
        get_logger("zzz_ddtool.gamefilter").debug(
            "套装点击前检查 identity=%s box=%s",
            identity_ok, (click.left, click.top, click.right, click.bottom),
        )
    if identity_ok is False:
        _save_picker_pre_click_failure(image, click, identity_ok)
    return identity_ok


def _read_picker_name_span(
    image: QImage, line: OcrLine, *, name: str,
    layout: SetPickerLayout, metrics: _PickerMetrics,
) -> OcrLine | None:
    """§14：原框成功直接复用；残缺行仅在同一格位名称带重识别一次。"""
    metrics.search_ocr_calls += 1
    span = read_name_span(image, line, name=name)
    if span is not None or (image.width(), image.height()) != (1920, 1080):
        return span
    center = (line.left + line.right) / 2
    column = min(_PICKER_COLUMN_CENTERS, key=lambda x: abs(x - center))
    if abs(center - column) > 74:
        return None
    left = max(0, line.left - 32, column - 74, layout.list_area[0])
    top = max(0, line.top - 4, layout.list_area[1])
    right = min(image.width(), line.right + 32, column + 74, layout.list_area[2])
    bottom = min(image.height(), line.bottom + 4, layout.list_area[3])
    box = (left, top, right, bottom)
    original = (line.left, line.top, line.right, line.bottom)
    if (
        right <= left or bottom <= top or box == original
        or left > line.left or top > line.top or right < line.right or bottom < line.bottom
    ):
        return None
    search_line = OcrLine(text=line.text, score=line.score, left=left, top=top, right=right, bottom=bottom)
    metrics.search_ocr_calls += 1
    return read_name_span(image, search_line, name=name)


def _picker_name_bands(
    image: QImage, lines: Sequence[OcrLine], layout: SetPickerLayout,
) -> tuple[tuple[int, int, int, int], ...]:
    """粗 OCR 只定位行高；四列均读整条名称带，最多三行十二格。"""
    if (image.width(), image.height()) != (1920, 1080):
        return ()
    area = (max(layout.list_area[0], 800), max(layout.list_area[1], 354),
            min(layout.list_area[2], 1490), min(layout.list_area[3], 827))
    centers = (900, 1064, 1228, 1392)
    seeds = []
    for line in lines:
        if len(re.findall(r"[\u3400-\u9fff]", line.text)) < 2 or not 0 < line.bottom - line.top <= 40:
            continue
        column = min(centers, key=lambda x: abs(x - (line.left + line.right) / 2))
        if not (column - 74 <= line.left < line.right <= column + 74):
            continue
        if area[1] <= line.top and line.bottom <= area[3]:
            seeds.append(line)
    rows: list[list[OcrLine]] = []
    for line in sorted(seeds, key=lambda item: (item.top + item.bottom) / 2):
        if rows and abs((line.top + line.bottom) / 2 -
                        (rows[-1][0].top + rows[-1][0].bottom) / 2) <= 12:
            rows[-1].append(line)
        else:
            rows.append([line])
    rows = [row for row in rows
            if area[1] <= min(x.top for x in row) - 4
            and max(x.bottom for x in row) + 4 <= area[3]]
    if len(rows) > 3:
        raise FilterClickError("套装名称行几何存在歧义，停止")
    bands = []
    for row in rows:
        top, bottom = min(x.top for x in row) - 4, max(x.bottom for x in row) + 4
        if not (area[1] <= top < bottom <= area[3]) or bottom - top > 48:
            continue
        bands.extend((x - 74, top, x + 74, bottom) for x in centers)
    return tuple(bands)


#: 套装名模板资产不可用时置真：本轮回落名称带 OCR（不阻断点选流程）。
_picker_templates_disabled = False


def _read_picker_targets(
    image: QImage, pending: Sequence[str], layout: SetPickerLayout, metrics: _PickerMetrics,
    *, cached_clicks: Sequence[SetPickerClick] = (),
) -> dict | None:
    """一帧的名称读取与匹配（§5.1.2）：列表区粗读 → 完整名称带逐带认名 → 匹配。

    名称带先走本地图片模板（`picker_names`，用户决定 2026-10-11）：命中即可靠认定，
    未命中/歧义的带才送批量 OCR；模板资产损坏时整轮回落 OCR。冲突返回 None。
    """
    global _picker_templates_disabled
    lines = read_rect(image, layout.list_area)
    metrics.search_ocr_calls += 1
    bands = _picker_name_bands(image, lines, layout)
    # 只读未缓存的名称带；粗读仍用于本屏行几何，不匹配缓存名称。
    bands = tuple(band for band in bands if not any(
        band[0] <= click.x <= band[2] and band[1] <= click.y <= band[3]
        for click in cached_clicks
    ))
    if not bands:
        return _match_targets([], pending)
    decided: dict[int, OcrLine] = {}
    undecided: list[int] = []
    for index, band in enumerate(bands):
        hit = None
        if not _picker_templates_disabled:
            try:
                hit = picker_names.classify(image, band)
            except picker_names.PickerAssetError as exc:
                _picker_templates_disabled = True
                get_logger("zzz_ddtool.gamefilter").warning(
                    "套装名模板资产不可用，本轮回落名称带 OCR：%s", exc
                )
        if hit is None:
            undecided.append(index)
            continue
        name, score, box = hit
        decided[index] = OcrLine(
            text=name, score=score, left=box[0], top=box[1], right=box[2], bottom=box[3]
        )
    if undecided:
        read = read_name_bands(image, [bands[i] for i in undecided])
        metrics.search_ocr_calls += 1
        for index, line in zip(undecided, read):
            if line is not None:
                decided[index] = line
    return _match_targets([decided[i] for i in sorted(decided)], pending)


def _prepare_picker_clicks(
    image: QImage,
    names: Sequence[str],
    matched: dict,
    prepared: list[tuple[str, SetPickerClick]],
    prepared_boxes: list[tuple[str, SetPickerClick, tuple[int, int, int, int]]],
) -> str | None:
    """准备本帧全部候选的点击框与指纹（§5.1.3 / R21）。

    返回第一个近似命中但未能精确隔离的名称（调用方可重拍重读）；框重叠或指纹生成失败
    立即抛错。任何点击之前必须全部就绪。
    """
    prepared.clear()
    prepared_boxes.clear()
    for name in [n for n in names if n in matched]:
        line = matched[name]
        if _normalize_match_text(line.text) != _normalize_match_text(name):
            return name
        box = (line.left, line.top, line.right, line.bottom)
        if any(_boxes_overlap(box, other) for _n, _c, other in prepared_boxes):
            raise FilterClickError("两个目标的名称框重叠，停止。")
        identity = make_identity(image, box)
        if identity is None:
            raise FilterClickError(f"套装名 {name!r} 的文字指纹生成失败，停止。")
        prepared.append(
            (
                name,
                SetPickerClick(
                    name=name,
                    left=box[0],
                    top=box[1],
                    right=box[2],
                    bottom=box[3],
                    x=(box[0] + box[2]) // 2,
                    y=(box[1] + box[3]) // 2,
                    identity=identity,
                ),
            )
        )
        prepared_boxes.append((name, prepared[-1][1], box))
    return None


def _cached_picker_targets(
    session: FilterReplaySession, key: SetPickerKey, layout: SetPickerLayout,
) -> tuple[dict[str, tuple[int, SetPickerClick]], SetPickerRoute | None]:
    targets: dict[str, tuple[int, SetPickerClick]] = {}
    confirm = None
    for route in session.routes.values():
        if route.layout != layout or (route.key.hwnd, route.key.pid, route.key.width,
                route.key.height, route.key.dpi, route.key.layout_id) != (
                key.hwnd, key.pid, key.width, key.height, key.dpi, key.layout_id):
            continue
        if any(stage.wheel_events and (stage.delta_units != _PICKER_SCROLL_DELTA
                or stage.wheel_events % _PICKER_SCROLL_EVENTS) for stage in route.stages):
            continue
        confirm = route
        round_index = 0
        for stage in route.stages:
            round_index += stage.wheel_events // _PICKER_SCROLL_EVENTS
            for click in stage.clicks:
                targets.setdefault(click.name, (round_index, click))
    return targets, confirm


def _select_sets_learn(
    hwnd: int,
    names: Sequence[str],
    *,
    width: int,
    height: int,
    layout: SetPickerLayout,
    key: SetPickerKey,
    metrics: _PickerMetrics,
    cached_targets: dict[str, tuple[int, SetPickerClick]] | None = None,
    cached_confirm: SetPickerRoute | None = None,
) -> SetPickerRoute:
    """§5.1 学习：定位、执行并记录滚动与点击；成功后才返回完整路径（失败不发布半成品）。

    名称带近似命中但未精确隔离时，不点击并重拍新帧重读（上限见
    `_PICKER_NAME_READ_ATTEMPTS`），认对一次即用；上限内仍不精确才按 R21 停止。
    """
    cached_targets = cached_targets or {}
    stages: list[SetPickerStage] = []
    pending = list(names)
    stage_index = 0
    carried_events = 0

    # 1) 起始位置（reopen_top 只检查一次；wheel_top 先按记录回顶）
    _establish_origin(hwnd, layout, width=width, height=height, metrics=metrics, key=key, cached=bool(cached_targets))
    image = None

    for round_index in range(_PICKER_MAX_ROUNDS):
        uncached = [name for name in pending if name not in cached_targets]
        if uncached:
            image, _ = _grab()
            if round_index:
                metrics.verification_frames += 1
        cached_on_screen = [click for row, click in cached_targets.values() if row == round_index]
        matched = (_read_picker_targets(image, uncached, layout, metrics, cached_clicks=cached_on_screen)
                   if uncached else {})
        if matched is None:
            raise FilterClickError("套装名称匹配到冲突框（同分或同框），停止。")
        clicks: list[SetPickerClick] = []
        boxes: list[tuple[int, int, int, int]] = []
        frame_boxes: list[tuple[int, int, int, int]] = []
        prepared: list[tuple[str, SetPickerClick]] = []
        prepared_boxes: list[tuple[str, SetPickerClick, tuple[int, int, int, int]]] = []
        # §5.1.3 / R21：本帧**所有**候选的名称框与指纹先全部就绪，任何一项失败都在任何点击之前停止
        failing = _prepare_picker_clicks(image, uncached, matched, prepared, prepared_boxes)
        # 近似命中但未精确隔离：识错不点击，重拍新帧再认，认对一次即用（用户决定 2026-10-11）。
        for attempt in range(2, _PICKER_NAME_READ_ATTEMPTS + 1):
            if failing is None:
                break
            get_logger("zzz_ddtool.gamefilter").info(
                "套装名 %r 未精确隔离，重拍重读第 %d/%d 次", failing, attempt, _PICKER_NAME_READ_ATTEMPTS
            )
            time.sleep(_PICKER_NAME_RETRY_DELAY_S)
            image, _ = _grab()
            metrics.verification_frames += 1
            matched = _read_picker_targets(image, uncached, layout, metrics, cached_clicks=cached_on_screen)
            if matched is None:
                raise FilterClickError("套装名称匹配到冲突框（同分或同框），停止。")
            failing = _prepare_picker_clicks(image, uncached, matched, prepared, prepared_boxes)
        if failing is not None:
            raise FilterClickError(f"套装名 {failing!r} 无法隔离精确文字框，停止。")
        by_name = dict(prepared)
        by_name.update({name: cached_targets[name][1] for name in pending
                        if name in cached_targets and cached_targets[name][0] == round_index})
        prepared = [(name, by_name[name]) for name in names if name in by_name]
        _click_picker_group(
            hwnd, [click for _name, click in prepared], metrics=metrics,
            layout=layout, key=key, stage_index=stage_index + 1, cached=bool(cached_targets),
        )
        for name, click in prepared:
            box = (click.left, click.top, click.right, click.bottom)
            clicks.append(click)
            boxes.append(box)
            frame_boxes.append(box)
            pending.remove(name)

        if clicks:
            stage_index += 1
            stages.append(
                SetPickerStage(
                    wheel_events=carried_events,
                    delta_units=(_PICKER_SCROLL_DELTA if carried_events else 0),
                    clicks=tuple(clicks),
                )
            )
            if _evidence_enabled():
                evidence_image, _ = _grab()
                metrics.verification_frames += 1
                # 保留历史事件名，表示本段点击已发送；不证明实际勾选。
                _emit_evidence("set_stage_selected", stage_index, [c.name for c in clicks], boxes, evidence_image)
            carried_events = 0
        if not pending:
            break
        if round_index == _PICKER_MAX_ROUNDS - 1:
            break
        # 4) 本屏未找全：滚动后重读（滚动事件归属下一段）
        for _ in range(_PICKER_SCROLL_EVENTS):
            guard = (_bound_picker_input(key) if cached_targets else _checked_picker_input(
                metrics, layout, key, None, "套装滚动前弹窗布局不符",
            ))
            with guard, input_context("set_scroll", defer_post_release=True):
                release = wheel_client(
                    hwnd,
                    *scale_point(layout.scroll_x, layout.scroll_y, width, height),
                    delta=_PICKER_SCROLL_DELTA,
                )
            metrics.wheel_events += 1
            carried_events += 1
            wait_release("set_scroll", release, fallback_ms=_SET_SCROLL_WAIT_MS, allow_measured=False)

    if pending:
        raise FilterClickError(f"滚动 {_PICKER_MAX_ROUNDS} 轮后仍未找到全部目标：{pending}")

    if cached_confirm is not None:
        confirm_identity = cached_confirm.confirm_identity
        confirm_x, confirm_y = cached_confirm.confirm_x, cached_confirm.confirm_y
        with _bound_picker_input(key), input_context("confirm_sets", defer_post_release=True):
            release = click_client(hwnd, confirm_x, confirm_y)
    else:
        # 5) 确认按钮：只读确认区、精确匹配、隔离文字框、点中心
        image, _ = _grab()
        metrics.verification_frames += 1
        lines = read_rect(image, layout.confirm_area)
        metrics.search_ocr_calls += 1
        hits = [ln for ln in lines if _normalize_match_text(ln.text) == _PICKER_CONFIRM_TEXT]
        if len(hits) != 1:
            raise FilterClickError(f"确认按钮精确命中 {len(hits)} 个框，停止。")
        confirm_span = read_name_span(image, hits[0], name=_PICKER_CONFIRM_TEXT)
        metrics.search_ocr_calls += 1
        if confirm_span is None:
            raise FilterClickError("确认按钮文字框无法隔离，停止。")
        confirm_box = (confirm_span.left, confirm_span.top, confirm_span.right, confirm_span.bottom)
        confirm_identity = make_identity(image, confirm_box)
        if confirm_identity is None:
            raise FilterClickError("确认按钮的文字指纹生成失败，停止。")
        confirm_ref = _click_ref(confirm_box)
        confirm_x = (confirm_box[0] + confirm_box[2]) // 2
        confirm_y = (confirm_box[1] + confirm_box[3]) // 2
        with _checked_picker_input(
            metrics, layout, key,
            lambda img: _confirm_ready(img, confirm_identity, confirm_ref),
            "确认按钮输入前状态不符",
        ), input_context("confirm_sets", defer_post_release=True):
            release = click_client(hwnd, confirm_x, confirm_y)
    wait_release("confirm_sets", release, fallback_ms=78 + _MEASURED_SAFETY_MS)
    if _observe(metrics, _picker_popup_closed) is not True:
        raise FilterClickError("点击确认后弹窗未关闭，停止。")
    if not _panel_visible():
        raise FilterClickError("确认后右侧筛选面板不见了，停止。")

    route = SetPickerRoute(
        key=key,
        layout=layout,
        stages=tuple(stages),
        confirm_x=confirm_x,
        confirm_y=confirm_y,
        confirm_identity=confirm_identity,
    )
    validate_route(route)
    return route


def _confirm_ready(image: QImage, identity: SetPickerIdentity, click) -> bool | None:
    """确认按钮点击前状态（§4.2/§5.2）：身份与可用都必须是 True；明确不符优先，未知返回 None。"""
    identity_ok = identity_matches(image, identity)
    enabled = _picker_enabled(image, click)
    if identity_ok is False or enabled is False:
        return False
    if identity_ok is True and enabled is True:
        return True
    return None


@contextmanager
def _checked_picker_input(
    metrics: _PickerMetrics, layout: SetPickerLayout, key: SetPickerKey,
    predicate: Callable[[QImage], bool | None] | None, message: str,
) -> Iterator[None]:
    """§5.3：在每个底层检查点返回后复验，覆盖输入内部聚焦；不拆鼠标原子区。"""
    def state(image: QImage) -> bool | None:
        layout_ok = _picker_stage_matches(image, layout)
        target_ok = True if predicate is None else predicate(image)
        if layout_ok is False or target_ok is False:
            return False
        if layout_ok is True and target_ok is True:
            return True
        return None

    def validate() -> None:
        verdict = _observe(metrics, state, key=key)
        if verdict is not True:
            raise FilterClickError(f"{message}（{verdict}），停止。")

    with validated_inputs(validate, reuse_unchanged=True):
        yield


@contextmanager
def _bound_picker_input(key: SetPickerKey) -> Iterator[None]:
    """缓存坐标输入只核对窗口绑定，不截图、不识别游戏页面。"""
    def validate() -> None:
        hwnd = find_game_hwnd()
        if (hwnd != key.hwnd or window_pid(hwnd) != key.pid
                or client_size(hwnd) != (key.width, key.height)
                or _window_dpi(hwnd) != key.dpi):
            raise FilterClickError("缓存坐标输入前窗口绑定变化，停止。")

    with validated_inputs(validate):
        yield


def _click_picker_group(
    hwnd: int, clicks: Sequence[SetPickerClick], *, metrics: _PickerMetrics,
    layout: SetPickerLayout, key: SetPickerKey, stage_index: int, cached: bool = False,
) -> None:
    """连续发送整批；学习复验剩余身份，缓存坐标只绑定窗口。"""
    completed = 0

    def remaining_state(image: QImage) -> bool | None:
        states = [
            _pre_click_state(image, c, c.identity)
            for c in clicks[completed:]
        ]
        if any(state is False for state in states):
            return False
        return True if all(state is True for state in states) else None

    # 验证作用域跨越整批，正常检查点复用同一暂停代次的整批结果。
    # 控制检查仍逐次执行；恢复时重验剩余目标，已完成点击不重发。
    guard = (_bound_picker_input(key) if cached else _checked_picker_input(
        metrics, layout, key, remaining_state, "套装批量点击输入前状态不符",
    ))
    with guard:
        for click in clicks:
            with input_context(
                "set_option", target=click.name, stage_index=stage_index,
                selected=True, defer_post_release=True,
            ):
                release = click_client(hwnd, click.x, click.y)
            completed += 1
            metrics.selection_clicks += 1
            wait_release(
                "set_option", release,
                fallback_ms=_SET_OPTION_TAIL_WAIT_MS if completed == len(clicks) else 0,
                allow_measured=completed == len(clicks),
            )


def _establish_origin(
    hwnd: int, layout: SetPickerLayout, *, width: int, height: int, metrics: _PickerMetrics, key: SetPickerKey,
    cached: bool = False,
) -> None:
    """学习检查顶部；缓存仅按起始模式执行回顶事件，不判读页面。"""
    if layout.origin_mode == "wheel_top":
        for _ in range(layout.top_wheel_events):
            guard = (_bound_picker_input(key) if cached else _checked_picker_input(
                metrics, layout, key, None, "套装回顶前弹窗布局不符",
            ))
            with guard, input_context("set_scroll", defer_post_release=True):
                release = wheel_client(
                    hwnd,
                    *scale_point(layout.scroll_x, layout.scroll_y, width, height),
                    delta=layout.top_delta_units,
                )
            metrics.wheel_events += 1
            wait_release("set_scroll", release, fallback_ms=_SET_SCROLL_WAIT_MS, allow_measured=False)
    if not cached and _observe(metrics, lambda img: _picker_at_top(img, layout)) is not True:
        raise FilterClickError("套装弹窗起始位置检查失败（顶部锚点不符），停止。")


def _panel_present(image: QImage) -> bool:
    """右侧筛选面板是否仍在（§4.2 图像判据，不做 OCR）：底部「重置」区亮尾(5%)阈值。"""
    values = []
    for color in _region(image, FILTER_RESET_AREA, step=2):
        values.append(0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue())
    if not values:
        return False
    values.sort()
    tail = values[int(len(values) * 0.95):]
    return (sum(tail) / len(tail)) >= _PANEL_RESET_MIN_TAIL


def _panel_visible() -> bool:
    """抓一帧后按像素判据判断面板是否仍在（回放范围内零 OCR）。"""
    image, _ = _grab()
    return _panel_present(image)


def _select_sets_replay(
    hwnd: int, route: SetPickerRoute, *, width: int, height: int, metrics: _PickerMetrics
) -> None:
    """§5.2 回放：按记录的滚动与点击执行，零名称查找 OCR；失败由调用方移除路径。"""
    layout = route.layout
    _establish_origin(hwnd, layout, width=width, height=height, metrics=metrics, key=route.key, cached=True)
    for index, stage in enumerate(route.stages, start=1):
        for _ in range(stage.wheel_events):
            with _bound_picker_input(route.key), input_context("set_scroll", defer_post_release=True):
                release = wheel_client(
                    hwnd,
                    *scale_point(layout.scroll_x, layout.scroll_y, width, height),
                    delta=stage.delta_units,
                )
            metrics.wheel_events += 1
            wait_release("set_scroll", release, fallback_ms=_SET_SCROLL_WAIT_MS, allow_measured=False)
        _click_picker_group(
            hwnd, stage.clicks, metrics=metrics, layout=layout,
            key=route.key, stage_index=index, cached=True,
        )
        if _evidence_enabled():
            evidence_image, _ = _grab()
            metrics.verification_frames += 1
            # 保留历史事件名，表示本段点击已发送；不证明实际勾选。
            _emit_evidence(
                "set_stage_selected", index, [c.name for c in stage.clicks],
                [(c.left, c.top, c.right, c.bottom) for c in stage.clicks], evidence_image,
            )

    with _bound_picker_input(route.key), input_context("confirm_sets", defer_post_release=True):
        release = click_client(hwnd, route.confirm_x, route.confirm_y)
    wait_release("confirm_sets", release, fallback_ms=78 + _MEASURED_SAFETY_MS)
    if _observe(metrics, _picker_popup_closed) is not True:
        raise FilterClickError("回放点击确认后弹窗未关闭，停止。")
    if not _panel_visible():
        raise FilterClickError("回放确认后右侧筛选面板不见了，停止。")


def _select_sets_with_replay(
    hwnd: int,
    names: Sequence[str],
    *,
    width: int,
    height: int,
    replay_session: FilterReplaySession | None,
) -> None:
    """§5 分派：legacy / learn / replay。一次调用只输出一条指标，模式在分派时定一次。"""
    log = get_logger("zzz_ddtool.gamefilter")
    if not names:
        return
    layout = _calibrated_layout(width, height)
    session = replay_session
    metrics: _PickerMetrics | None = None
    try:
        if session is not None:
            dpi = _window_dpi(hwnd)
            # §5.3：hwnd/pid/尺寸/DPI 任一变化先清空会话路径（不受是否已标定影响）
            _invalidate_session_routes(session, hwnd, width, height, dpi)
        if session is not None and layout is not None and _window_dpi(hwnd) > 0:
            key = _picker_key(hwnd, width, height, names, layout)
            route = session.routes.get(key)
            if route is None:
                cached_targets, cached_confirm = _cached_picker_targets(session, key, layout)
                cached_targets = {name: cached_targets[name] for name in names if name in cached_targets}
                metrics = _PickerMetrics("replay" if len(cached_targets) == len(names) else "learn")
                try:
                    route = _select_sets_learn(
                        hwnd, names, width=width, height=height, layout=layout, key=key, metrics=metrics,
                        cached_targets=cached_targets, cached_confirm=cached_confirm,
                    )
                except Exception:
                    if cached_targets:
                        session.routes.clear()  # 混合执行失败后不再沿用所消费的坐标缓存。
                    raise
                session.routes[key] = route
                metrics.success = True
            else:
                metrics = _PickerMetrics("replay")
                try:
                    _select_sets_replay(hwnd, route, width=width, height=height, metrics=metrics)
                except Exception:
                    session.routes.pop(key, None)  # §5.3：失败路径立即移除
                    raise
                metrics.success = True
        else:
            metrics = _PickerMetrics("legacy")
            _select_names(
                hwnd, names, width=width, height=height, scroll_center=(960, 540), metrics=metrics
            )
            _confirm_popup(hwnd, metrics)
            metrics.success = True
        log.info("sets picked: %s", list(names))
    finally:
        if metrics is not None:
            _log_picker_metrics(metrics)
            if session is not None:
                session.last_metrics = metrics.payload()
# ---------------- 套装弹窗判读（spec §4.2 / §4.3） ----------------
# 区域为设计坐标（1920x1080）；阈值取自 2026-10-06 静态样本实测
# （手册/参考图/驱动盘筛选/，见该页说明.md）：
#   提示行均值：弹窗存在 124.4—124.6 / 关闭 42.9；首行上沿条带亮尾：在顶部 34.2—65.5 / 已滚动 242.2；
_PICKER_HINT_BAND = (764, 299, 1156, 328)
_PICKER_TOP_BAND = (830, 366, 1470, 384)
_PICKER_POPUP_OPEN_MIN = 100.0
_PICKER_POPUP_CLOSED_MAX = 60.0
_PICKER_TOP_MAX = 120.0
_PICKER_SCROLLED_MIN = 180.0
_PICKER_ENABLED_MIN_LUMA = 80.0
# 可用判据用「亮尾」（区域最亮 5% 像素的均值）：可用格 216—255、灰格 128—133、确认按钮 255
_PICKER_ENABLED_MIN_TAIL = 180.0
# 面板仍在判据（§4.2 图像判据，不做 OCR）：底部「重置」区亮尾(5%) 实测 面板在 191.3 / 面板关 57.3（2026-10-07）
_PANEL_RESET_MIN_TAIL = 120.0
_PICKER_COLUMN_CENTERS = (900, 1064, 1228, 1392)
# 副属性就位观察（§6.5）：一次滑到底后未知最多共 3 张（含滚后第一张），期间不补发输入
_SUBSTAT_OBSERVE_FRAMES = 3
# 2026-10-11 用户裁定：判读/观察延迟一律 50 ms（与原先取低值；原先 0.1 s）。
_SUBSTAT_OBSERVE_INTERVAL_S = 0.05


def _region(image: QImage, area: tuple[int, int, int, int], step: int = 2):
    """按设计坐标取区域内的像素（1920x1080 设计坐标 → 实际客户区像素）。"""
    left, top, right, bottom = scale_rect(*area, image.width(), image.height())
    for y in range(top, bottom, step):
        for x in range(left, right, step):
            yield image.pixelColor(x, y)


def _mean_luma(image: QImage, area: tuple[int, int, int, int]) -> float:
    total = count = 0
    for color in _region(image, area):
        total += 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        count += 1
    return total / max(1, count)


def _bright_tail_px(image: QImage, box: tuple[int, int, int, int], fraction: float = 0.05) -> float:
    """客户区像素矩形内，最亮的一小部分的平均亮度（对稀疏亮元素敏感）。"""
    left, top, right, bottom = (int(v) for v in box)
    values = []
    for y in range(max(0, top), min(image.height(), bottom)):
        for x in range(max(0, left), min(image.width(), right)):
            color = image.pixelColor(x, y)
            values.append(0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue())
    if not values:
        return 0.0
    values.sort(reverse=True)
    take = max(1, int(len(values) * fraction))
    return sum(values[:take]) / take


def _bright_tail(image: QImage, area: tuple[int, int, int, int], fraction: float = 0.05) -> float:
    """设计坐标版亮尾（供固定条带用）。"""
    return _bright_tail_px(image, scale_rect(*area, image.width(), image.height()), fraction)


def _picker_popup_open(image: QImage) -> bool | None:
    """套装弹窗是否存在：看提示行的亮文字（三态，未知不当作关闭）。"""
    hint = _mean_luma(image, _PICKER_HINT_BAND)
    if hint >= _PICKER_POPUP_OPEN_MIN:
        return True
    if hint <= _PICKER_POPUP_CLOSED_MAX:
        return False
    return None


def _picker_popup_closed(image: QImage) -> bool | None:
    opened = _picker_popup_open(image)
    return None if opened is None else (not opened)


def _picker_at_top(image: QImage, layout) -> bool | None:
    """列表是否停在顶部：首行瓦片上沿条带没有亮内容（§4.3，不用滑块）。"""
    band = _bright_tail(image, _PICKER_TOP_BAND)
    if band <= _PICKER_TOP_MAX:
        return True
    if band >= _PICKER_SCROLLED_MIN:
        return False
    return None


def _picker_stage_matches(image: QImage, layout) -> bool | None:
    """当前段布局是否匹配：弹窗在，且列表区里能看到成片的格子内容。"""
    opened = _picker_popup_open(image)
    if opened is False:
        return False
    left, top, right, bottom = scale_rect(*layout.list_area, image.width(), image.height())
    bright = total = 0
    for y in range(top, bottom, 3):
        for x in range(left, right, 3):
            color = image.pixelColor(x, y)
            bright += 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue() > 90
            total += 1
    if bright / max(1, total) < 0.02:
        return False
    return True if opened is True else None


def _save_picker_pre_click_failure(image, click, identity_ok) -> None:
    """DEBUG 下把导致拒绝的原帧存进本轮 ``diagnostics/``，避免事后截图丢失瞬时误判证据。"""
    log = get_logger("zzz_ddtool.gamefilter")
    if not log.isEnabledFor(logging.DEBUG) or (image.width(), image.height()) != (1920, 1080):
        return
    try:
        folder = loground.diagnostics_dir() / "picker-failures"
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"pre-click-{time.time_ns()}"
        path = folder / f"{stem}.png"
        if not image.save(str(path), "PNG"):
            raise OSError(f"无法保存 {path}")
        details = {
            "target": getattr(click, "name", getattr(click, "text", None)),
            "name_box": [click.left, click.top, click.right, click.bottom],
            "identity": identity_ok,
        }
        (folder / f"{stem}.json").write_text(
            json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        log.debug("套装拒绝原帧=%s 判据=%s", path, json.dumps(details, ensure_ascii=False))
    except Exception as exc:
        log.warning("保存套装拒绝原帧失败：%s", exc)


def _picker_enabled(image: QImage, click) -> bool | None:
    """确认按钮是否可用：亮尾确认 True，灰暗保持未知。"""
    left, top, right, bottom = click.left, click.top, click.right, click.bottom
    tile_zone = (left - 40, top - 110, right + 35, bottom + 10)
    tight_zone = (left - 8, top - 8, right + 8, bottom + 8)
    tail = max(_bright_tail_px(image, tile_zone), _bright_tail_px(image, tight_zone))
    return True if tail >= _PICKER_ENABLED_MIN_TAIL else None


def _refuse_dismantle(x: int, y: int, width: int, height: int) -> None:
    left, top, right, bottom = scale_rect(*DISMANTLE_FORBID, width, height)
    if left <= x <= right and top <= y <= bottom:
        raise FilterClickError(f"坐标 ({x},{y}) 落在拆解按钮上，已拒绝点击。")


def _grab() -> tuple[QImage, int]:
    image, _method, hwnd = grab_game_image()
    return image, hwnd


def _find_and_click(
    hwnd: int,
    needle: str,
    region: tuple[int, int, int, int],
    *,
    width: int,
    height: int,
    cutoff: float = 0.75,
    refuse_dismantle: bool = False,
) -> InputRelease | None:
    """截屏 OCR，在 region 里找含 needle 的文本行并点中心：命中返回释放回执，落空返回 None。"""
    image, _ = _grab()
    for line in read_rect(image, region):
        ratio = SequenceMatcher(None, needle, line.text).ratio()
        if needle not in line.text and ratio < cutoff:
            continue
        cx = (line.left + line.right) // 2
        cy = (line.top + line.bottom) // 2
        if refuse_dismantle:
            _refuse_dismantle(cx, cy, width, height)
        return click_client(hwnd, cx, cy)
    return None


def _select_names(
    hwnd: int,
    names: Sequence[str],
    *,
    width: int,
    height: int,
    scroll_center: tuple[int, int],
    metrics: _PickerMetrics | None = None,
) -> None:
    """在当前弹层里逐个点名。找不到就滚动重找，超过上限抛 FilterClickError。

    证据上下文启用时，每段点完抓一帧原帧（不额外 OCR、不增发输入），
    与 set_option 记录共用同一个目标字符串（spec §8.4）。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    pending = list(names)
    if not pending:
        return
    stage_index = 0
    for _round in range(_MAX_SCROLL_ROUNDS):
        image, _ = _grab()
        lines = read_rect(image, FULL_WINDOW)
        if metrics is not None:
            metrics.search_ocr_calls += 1
        still: list[str] = []
        clicks: list[tuple[int, int, tuple[int, int, int, int]]] = []
        targets: list[str] = []
        last_release = None
        for name in pending:
            hit = None
            for line in lines:
                if name in line.text or SequenceMatcher(None, name, line.text).ratio() >= _MATCH_CUTOFF:
                    hit = line
                    break
            if hit is None:
                still.append(name)
                continue
            target = _legacy_option_target(hit.text, name)
            cx = (hit.left + hit.right) // 2
            cy = (hit.top + hit.bottom) // 2
            with input_context("set_option", target=target, stage_index=stage_index + 1,
                               selected=True, defer_post_release=True):
                release = click_client(hwnd, cx, cy)
            if metrics is not None:
                metrics.selection_clicks += 1
            log.info("picker clicked %r at (%d,%d)", name, hit.left, hit.top)
            # legacy 分支沿用相同动作ID但不消费实测（§4.2）。
            # 组内零等待；不在相邻点击之间抓图判读。
            wait_release("set_option", release, fallback_ms=0, allow_measured=False)
            last_release = release
            clicks.append((cx, cy, (hit.left, hit.top, hit.right, hit.bottom)))
            targets.append(target)
        pending = still
        if last_release is not None:
            wait_release("set_option", last_release, fallback_ms=_SET_OPTION_TAIL_WAIT_MS, allow_measured=False)
        if clicks:
            stage_index += 1
            if _evidence_enabled():  # 只在启用证据时抓这一帧
                frame, _ = _grab()
                if metrics is not None:
                    metrics.verification_frames += 1
                _emit_evidence(
                    "set_stage_selected",
                    stage_index,
                    targets,
                    [box for _cx, _cy, box in clicks],
                    frame,
                )
        if not pending:
            return
        with input_context("set_scroll", defer_post_release=True):
            release = wheel_client(hwnd, *scale_point(*scroll_center, width, height))
        if metrics is not None:
            metrics.wheel_events += 1
        # legacy滚动共用套装预算，不消费实测档案。
        wait_release("set_scroll", release, fallback_ms=_SET_SCROLL_WAIT_MS, allow_measured=False)
    raise FilterClickError(f"弹层里找不到目标，滚动 {_MAX_SCROLL_ROUNDS} 轮未果：{pending}")


def _legacy_option_target(raw_text: str, requested: str) -> str:
    """legacy set_option 的目标字符串（spec §8.4）：与证据 targets 复用同一值。

    归一化原文与 requested 相等 → 记归一化原文；归一化原文恰为 requested 加
    件数后缀 `(?:×)?[0-9]+` → 去后缀（即 requested）；其他情况保留原文。
    只做字符串处理，不改点击框、点击中心，也不调用 OCR。
    """
    wanted = _normalize_match_text(requested)
    text = _normalize_match_text(raw_text)
    if text == wanted:
        return text
    match = re.fullmatch(rf"{re.escape(wanted)}((?:×)?[0-9]+)", text)
    if match:
        return text[: match.start(1)]
    return raw_text


def _confirm_popup(hwnd: int, metrics: _PickerMetrics | None = None) -> None:
    """关弹层必须且只能点「确认筛选」，绝不按 Esc（按了会取消勾选）。

    找不到按钮就中断，保持现状等人处理。查找按钮恰做一次整窗 OCR，
    与 optimized 确认区读取同口径计入 search_ocr_calls（spec §5.4）。
    """
    with input_context("confirm_sets", defer_post_release=True):
        release = _find_and_click(hwnd, "确认筛选", FULL_WINDOW, width=1920, height=1080, cutoff=0.8)
    if metrics is not None:
        metrics.search_ocr_calls += 1
    if release is None:
        raise FilterClickError("没找到「确认筛选」按钮，弹层未关闭。按手册 01 中断。")
    # legacy 分支：580 = 180 + 400，不消费实测。
    wait_release("confirm_sets", release, fallback_ms=78 + _MEASURED_SAFETY_MS, allow_measured=False)


def _text_lines() -> list:
    """主属性定位只识别右侧面板，避免扫描仓库中的无关文字。"""
    image, _ = _grab()
    return _panel_lines(image)


def _title_y(lines, title: str) -> int | None:
    """找标题行的中心 y（取最靠上的那条），找不到返回 None。"""
    found = None
    for line in lines:
        if title not in line.text.replace(" ", ""):
            continue
        y = (line.top + line.bottom) // 2
        if found is None or y < found:
            found = y
    return found


def _toggle_below(lines, title_y: int):
    """标题正下方的「点击展开 / 点击收起」开关，取 y 最小的一条。"""
    best = None
    for line in lines:
        y = (line.top + line.bottom) // 2
        if y <= title_y:
            continue
        text = line.text.replace(" ", "")
        if "点击" not in text or ("展开" not in text and "收起" not in text):
            continue
        if best is None or y < best[0]:
            best = (y, line)
    return best[1] if best else None


def _open_mainstat_section(hwnd: int, width: int, height: int) -> list:
    """把筛选面板滚到「主属性词条」区，并确保该区已展开。

    真机标定（2026-10-02，log/captures/probe-filter-open.png）：
    面板内容比视口高，滚到顶时「主属性词条」区在视口外；
    该区默认**收起**，只有一条「点击展开」，点开后才是
    「生命值 / 攻击力百分比 / …」这些复选框。
    此前直接点名，OCR 永远找不到主属性名，滚 8 轮后中断——B5 的根因。

    **返回**「已确认展开」那一帧的 OCR 文本行。紧接着调用的 `_pick_mainstats` 就在同一
    状态下点名，直接把这一帧交给它，省掉一次面板 OCR（中间无点击，帧不会变）。

    2026-10-11 真机（S5 轮次 2）：标题露出但开关被裁在视口底部之外时，旧实现直接中断；
    按 §4.2「目标未露出或落在底部点击无效区时向下滚动后重读」，改为继续滚动直到开关
    进入可点区，或滚满轮数再中断。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    click_bottom = scale_point(0, 1080 - _PANEL_CLICK_BOTTOM_GAP, width, height)[1]
    for _round in range(_MAX_PANEL_SCROLLS + 1):
        lines = _text_lines()
        title_y = _title_y(lines, MAINSTAT_TITLE)
        toggle = _toggle_below(lines, title_y) if title_y is not None else None
        if toggle is None or toggle.bottom > click_bottom:
            if _round == _MAX_PANEL_SCROLLS:
                break
            with input_context("mainstat_scroll", defer_post_release=True):
                release = wheel_client(hwnd, *scale_point(*FILTER_PANEL_SCROLL, width, height))
            wait_release("mainstat_scroll", release, fallback_ms=94 + _MEASURED_SAFETY_MS)
            continue
        if "收起" in toggle.text.replace(" ", ""):
            return lines
        with input_context("open_mainstats", defer_post_release=True):
            release = click_client(hwnd, (toggle.left + toggle.right) // 2, (toggle.top + toggle.bottom) // 2)
        wait_release("open_mainstats", release, fallback_ms=0)
        after = _text_lines()
        # 作者裁定（2026-10-12）：关闭「展开成功」回读检测（原实现保留备查）。
        # after_y = _title_y(after, MAINSTAT_TITLE)
        # after_toggle = _toggle_below(after, after_y) if after_y is not None else None
        # if after_toggle is not None and "收起" in after_toggle.text.replace(" ", ""):
        #     log.info("已点开主属性词条区")
        #     return after
        # raise FilterClickError("点了主属性「点击展开」但该区没有展开，中断。")
        return after
    raise FilterClickError(
        "滚动面板 %d 轮后「主属性词条」区仍未就位或无法展开，中断。" % _MAX_PANEL_SCROLLS
    )


def _normalize_stat(text: str) -> str:
    """把预设里的写法对齐到游戏 UI。

    data/stats.json 的 slot_mainstats 写「攻击力%」，游戏面板显示「攻击力百分比」，
    不归一化就永远认不上（B5 的第二半）。
    """
    return text.replace("％", "%").replace("%", "百分比")


def _best_match(lines, name: str, top: int, bottom: int):
    """在 [top, bottom] 纵向区间里挑最贴 name 的一条：全等 > 包含 > 相似度。

    包含匹配按「名字占整条文本的比例」打分，避免「生命值」抢走
    「生命值百分比」这种更长更具体的条目。
    """
    wanted = _normalize_stat(name)
    best = None
    best_score = 0.0
    for line in lines:
        y = (line.top + line.bottom) // 2
        if not top <= y <= bottom:
            continue
        text = _normalize_stat(line.text.replace(" ", ""))
        if text == wanted:
            score = 3.0
        elif wanted in text:
            score = 2.0 + len(wanted) / max(1, len(text))
        else:
            score = SequenceMatcher(None, wanted, text).ratio()
            if score < _MATCH_CUTOFF:
                continue
        if score > best_score:
            best, best_score = line, score
    return best


def _pick_mainstats(
    hwnd: int, names: Sequence[str], *, width: int, height: int, lines: list | None = None
) -> None:
    """在已展开的主属性词条区里点名勾选。

    主属性是复选框，勾上即生效，没有弹层、也没有「确认筛选」；
    逐条点名；目标未露出或落在底部点击无效区时，向下滚动后重新 OCR。
    已选词条从待选列表移除，滚动后不重复点击；只匹配主属性区。

    `lines` 传入 `_open_mainstat_section` 返回的那一帧即可（两者状态相同、中间无点击），
    不传则自己抓一帧。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    pending = list({_normalize_stat(name): name for name in names}.values())
    if not pending:
        return
    if lines is None:
        lines = _text_lines()
    if _title_y(lines, MAINSTAT_TITLE) is None:
        raise FilterClickError("主属性词条区不在视口里，无法点名，中断。")
    left, panel_top, right, panel_bottom = scale_rect(1300, 100, 1880, 980, width, height)
    click_bottom = scale_point(0, 1080 - _PANEL_CLICK_BOTTOM_GAP, width, height)[1]
    margin = max(1, round(20 * height / 1080))
    for scrolls in range(_MAX_PANEL_SCROLLS + 1):
        title_y = _title_y(lines, MAINSTAT_TITLE)
        sub_y = _title_y(lines, SUBSTAT_TITLE)
        # 向下滚动后，主标题可能移到视口上方；副标题仍可界定主属性区的下沿。
        # 两个标题都认不上时不点，继续滚动重找，避免误选副属性同名项。
        if title_y is not None or sub_y is not None:
            top = title_y + margin if title_y is not None else panel_top
            bottom = sub_y - margin if sub_y is not None else min(
                panel_bottom, title_y + round(320 * height / 1080)
            )
            candidates = [line for line in lines if left <= (line.left + line.right) // 2 <= right]
            still = []
            last_release = None
            for name in pending:
                hit = _best_match(candidates, name, top, bottom)
                if hit is None or hit.bottom > click_bottom:
                    still.append(name)
                    continue
                release = _click_line(hwnd, hit, role="mainstat_option", target=hit.text)
                log.info("mainstat clicked %r at (%d,%d)", name, hit.left, hit.top)
                # 组内零等待，不消费会把零等待抬高的实测策略。
                wait_release("mainstat_option", release, fallback_ms=0, allow_measured=False)
                last_release = release
            pending = still
            if last_release is not None:
                # 每个视口已发送的点击是一组；滚动或返回前只等待最后一次释放。
                wait_release("mainstat_option", last_release, fallback_ms=_MAINSTAT_OPTION_TAIL_WAIT_MS)
            if not pending:
                return
        if scrolls == _MAX_PANEL_SCROLLS:
            break
        log.info("主属性选项未全部就位，向下滚动重找（第%d次）：%s", scrolls + 1, pending)
        with input_context("mainstat_scroll", defer_post_release=True):
            release = wheel_client(hwnd, *scale_point(*FILTER_PANEL_SCROLL, width, height))
        wait_release("mainstat_scroll", release, fallback_ms=94 + _MEASURED_SAFETY_MS)
        lines = _text_lines()
    raise FilterClickError(f"主属性词条区滚动 {_MAX_PANEL_SCROLLS} 轮后仍找不到或无法点击：{pending}")


def apply_sets_mainstats(
    hwnd: int,
    width: int,
    height: int,
    *,
    sets: Sequence[str],
    mainstats: Sequence[str],
    replay_session: FilterReplaySession | None = None,
) -> None:
    """在筛选面板里点开套装 / 主属性词条并勾选。

    套装走 `_select_sets_with_replay` 分派（legacy / learn / replay），主属性走原来的展开+点名。
    全部靠 OCR 或标定判读；认不上就中断（手册 01 底线）。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    if sets:
        _validate_picker_names(sets)  # 第一条游戏输入之前拒绝非法名称（§3.1/§4.1）
        with input_context("open_sets", defer_post_release=True):
            release = _find_and_click(hwnd, "点击选择", FILTER_SETS_BAR, width=width, height=height)
        if release is None:
            raise FilterClickError("筛选面板里没找到「点击选择」条。")
        wait_release("open_sets", release, fallback_ms=166 + _MEASURED_SAFETY_MS)
        _select_sets_with_replay(hwnd, sets, width=width, height=height, replay_session=replay_session)
    if mainstats:
        # 展开最后一帧直接交给点名用，省一次面板 OCR（两者之间不点击，帧不变）。
        lines = _open_mainstat_section(hwnd, width, height)
        _pick_mainstats(hwnd, mainstats, width=width, height=height, lines=lines)
        log.info("mainstats picked: %s", list(mainstats))


def _click_slot(hwnd: int, slot: int, width: int, height: int) -> InputRelease:
    """OCR 找「分区[N]」按钮，点文字中心并返回释放回执。面板滚动状态无关。认不上就中断。"""
    image, _ = _grab()
    for line in read_rect(image, FILTER_SLOT_AREA):
        text = line.text.replace(" ", "")
        if "分区" not in text:
            continue
        m = re.search(r"(\d+)", text)
        if m and int(m.group(1)) == slot:
            with input_context("slot", target=slot, defer_post_release=True):
                return click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)
    raise FilterClickError(f"筛选面板里没找到「分区[{slot}]」按钮，中断。")


def _try_click_reset(hwnd: int, width: int, height: int) -> InputRelease | None:
    """OCR 找面板底部的「重置」按钮点中心；落空返回 None（由调用方决定要不要再点漏斗）。"""
    image, _ = _grab()
    for line in read_rect(image, FILTER_RESET_AREA):
        text = line.text.replace(" ", "")
        if "重置" in text and "批量" not in text:
            with input_context("reset", defer_post_release=True):
                return click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)
    return None


def _click_reset(hwnd: int, width: int, height: int) -> InputRelease:
    """OCR 找面板底部的「重置」按钮点中心并返回释放回执（避开批量操作）。"""
    release = _try_click_reset(hwnd, width, height)
    if release is None:
        raise FilterClickError("筛选面板里没找到「重置」按钮，中断。")
    return release


def _click_funnel(hwnd: int, fx: int, fy: int) -> None:
    """点漏斗按钮并等 ``open_panel`` 下限。漏斗是开/关切换，不管方向。"""
    with input_context("open_panel", defer_post_release=True):
        release = click_client(hwnd, fx, fy)
    wait_release("open_panel", release, fallback_ms=75 + _MEASURED_SAFETY_MS)


def _open_panel_and_reset(
    hwnd: int, width: int, height: int, *, fx: int, fy: int, allow_reopen: bool
) -> InputRelease:
    """点漏斗打开筛选面板并点「重置」；**读不到「重置」就再点一次漏斗**开回来。

    2026-10-12 真机因果：漏斗是**开/关切换**。面板本来就开着时（上一轮没关就点了开始），
    开头那一下会把它**关掉**，接着找「重置」必然读空、整轮中断。日志证据：失败轮
    ``0002-导航到仓库.png`` 左侧网格已被压暗（p95 119~129），正常轮是 218~242，
    说明任务开始时面板就是开着的。

    做法：点一次漏斗 → 找「重置」；找不到就**再点一次漏斗**。开关按两次回到原状态，
    面板原本开着会被恢复，原本关着则第一次就已经打开。所以：

    - 面板本来关着（正常路径）：点 1 次漏斗 + 读 1 次区域 OCR，与旧实现完全相同，不多抓帧；
    - 面板本来开着：多 1 次点击 + 1 次区域 OCR，但把"必然失败"救成"正常继续"。

    不读亮度、不判"面板开没开"，只用"读没读到重置"——不依赖跨机器会变的像素阈值。
    ``allow_reopen=False`` 供 ``reset_game_filter`` 用：那条路径读不到时紧接着就 Esc 关面板，
    没有"再点重置"的下一步，补点没有意义。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    _click_funnel(hwnd, fx, fy)
    release = _try_click_reset(hwnd, width, height)
    if release is not None:
        return release
    if not allow_reopen:
        raise FilterClickError("筛选面板里没找到「重置」按钮，中断。")
    log.info("没读到「重置」，再点一次漏斗把它开回来")
    _click_funnel(hwnd, fx, fy)
    release = _try_click_reset(hwnd, width, height)
    if release is None:
        raise FilterClickError("筛选面板里没找到「重置」按钮，中断。")
    return release


def reset_game_filter(hwnd: int, width: int, height: int) -> None:
    """打开筛选面板 → 点「重置」→ Esc 关闭。

    给滚轮标定用：标定要选第 3 行的盘做锚点，残留的筛选会把列表裁短（实测只剩
    16/3000），锚点落在空格子上就选不中。标定要求列表满屏，所以先清一次。
    """
    fx, fy = scale_point(*FILTER_BTN, width, height)
    _refuse_dismantle(fx, fy, width, height)
    reset_release = _open_panel_and_reset(
        hwnd, width, height, fx=fx, fy=fy, allow_reopen=False
    )
    wait_release("reset", reset_release, fallback_ms=203 + _MEASURED_SAFETY_MS)
    with input_context("close_panel_reset", defer_post_release=True):
        esc_release = press_key(hwnd, VK_ESCAPE)
    wait_release("close_panel_reset", esc_release, fallback_ms=80 + _MEASURED_SAFETY_MS)


def _validate_picker_names(names: Sequence[str]) -> tuple[str, ...]:
    """套装名合法性（§3.1/§4.1）：非空、去首尾空白、拒绝重复；非法抛 FilterClickError。"""
    try:
        return normalize_set_names(names)
    except ValueError as exc:
        raise FilterClickError(str(exc)) from exc


def apply_game_filter(
    hwnd: int,
    width: int,
    height: int,
    *,
    slots: Sequence[int],
    sets: Sequence[str],
    mainstats: Sequence[str],
    substats: Sequence[str] = (),
    substat_counts: Sequence[int] = (),
    replay_session: FilterReplaySession | None = None,
) -> None:
    """打开并重置筛选，套用部位/套装/主副属性条件，最后 Esc 关闭。

    能按键就按键：关筛选用 Esc，不扫关闭按钮。
    分区/重置按钮靠 OCR 点文字中心，不用硬坐标（旧表偏到错误按钮上）。
    套装名的合法性在第一条游戏输入之前校验（§4.1）。
    """
    _validate_substat_filter(substats, substat_counts)
    if sets:
        _validate_picker_names(sets)
    log = get_logger("zzz_ddtool.gamefilter")
    fx, fy = scale_point(*FILTER_BTN, width, height)
    _refuse_dismantle(fx, fy, width, height)
    # 面板若本来就开着，第一下会把它关掉；读不到「重置」时补点一次开回来。
    # 这里允许补点：紧接着还要在同一面板里点号位/套装/词条，不是马上关面板。
    reset_release = _open_panel_and_reset(
        hwnd, width, height, fx=fx, fy=fy, allow_reopen=True
    )
    wait_release("reset", reset_release, fallback_ms=203 + _MEASURED_SAFETY_MS)
    for slot in slots:
        slot_release = _click_slot(hwnd, slot, width, height)
        wait_release("slot", slot_release, fallback_ms=85 + _MEASURED_SAFETY_MS)
    if sets or mainstats:
        apply_sets_mainstats(
            hwnd, width, height, sets=sets, mainstats=mainstats, replay_session=replay_session
        )
    if substats:
        apply_substat_filter(hwnd, width, height, names=substats, counts=substat_counts)
    with input_context("close_panel", defer_post_release=True):
        esc_release = press_key(hwnd, VK_ESCAPE)
    wait_release("close_panel", esc_release, fallback_ms=89 + _MEASURED_SAFETY_MS)
    log.info("game filter done slots=%s sets=%s mains=%s", list(slots), list(sets), list(mainstats))


def _validate_substat_filter(names: Sequence[str], counts: Sequence[int]) -> None:
    if not names and not counts:
        return
    if not names or len(set(names)) != len(names) or set(names) - set(SUBSTAT_NAMES):
        raise FilterClickError("副属性筛选必须选择合法且不重复的副词条")
    # 2026-10-11 真机：游戏只允许数量 ≤ 已选词条数（上限 4），按钮连续从最低数量选到上限。
    upper = min(4, len(names))
    if (not counts or tuple(counts) != tuple(range(min(counts), upper + 1))
            or min(counts) not in range(1, upper + 1)):
        raise FilterClickError("副属性命中数量必须从最低数量连续选到已选词条数（不超过 4）")


def _panel_lines(image: QImage) -> list:
    """仅右侧面板内容；排除详情、关闭按钮和底部重置/批量操作区。"""
    left, top, right, bottom = scale_rect(1300, 100, 1880, 980, image.width(), image.height())
    return [
        line for line in read_rect(image, (1300, 100, 1880, 980))
        if left <= (line.left + line.right) // 2 <= right
        and top <= (line.top + line.bottom) // 2 <= bottom
    ]


def _read_panel() -> tuple[QImage, list]:
    """抓一帧并解析出面板行，返回 (image, lines)。"""
    image, _ = _grab()
    return image, _panel_lines(image)


def _click_line(hwnd: int, line, *, role: str | None = None, target: str | int | None = None,
                selected: bool | None = None) -> InputRelease:
    """点一条 OCR 行的中心并返回释放回执；带 role 时同时声明业务接管收尾（§3.2）。"""
    if role is None:
        return click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)
    with input_context(role, target=target, selected=selected, defer_post_release=True):
        return click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)


def _scroll_panel_to_bottom(
    hwnd: int, width: int, height: int, *, role: str = "substat_scroll", budget_ms: int = 173 + _MEASURED_SAFETY_MS
) -> None:
    """一次把筛选面板滑到底：连发滚轮事件，中途不抓图、不重复定位光标。

    面板内容比视口高，底部还有一条「点击无效带」。与其「滚一格 → 抓图 → 还不行 →
    再滚一格」（每格约 41px，要两三格才够，而每次都要一次全屏 OCR ≈1s），不如一次
    连发到底：到底后布局固定，按钮与后续选项都落在可点区，下一次 OCR 就能直接定位点击。
    §4.2：事件间 60 ms 不变，最后一条后的原 60 ms 计入总预算（60+250=310），不另加。
    """
    sx, sy = client_to_screen(hwnd, *scale_point(*FILTER_PANEL_SCROLL, width, height))
    focus_window(hwnd)
    user32.SetCursorPos(sx, sy)
    time.sleep(0.05)
    release: InputRelease | None = None
    with input_context(role, defer_post_release=True):
        for _ in range(_PANEL_SCROLL_NOTCHES):
            release = send_wheel_event(-3)
            time.sleep(0.06)
    assert release is not None  # _PANEL_SCROLL_NOTCHES > 0 时必已发送
    wait_release(role, release, fallback_ms=budget_ms)


def _option_selected(image: QImage, line) -> bool:
    """游戏已选按钮为蓝色。采样文字周围背景，避免把白色文字当选中态。"""
    pad = max(4, round(14 * image.width() / 1920))
    left, right = max(0, line.left - pad), min(image.width() - 1, line.right + pad)
    top, bottom = max(0, line.top), min(image.height() - 1, line.bottom)
    blue = samples = 0
    for y in range(top, bottom + 1, max(1, (bottom - top) // 6)):
        for x in range(left, right + 1, max(1, (right - left) // 20)):
            color = image.pixelColor(x, y)
            blue += color.blue() > 110 and color.blue() > color.red() + 45 and color.green() > color.red() + 25
            samples += 1
    return blue / max(1, samples) > 0.15


def _open_substat_section(hwnd: int, width: int, height: int) -> tuple[QImage, list]:
    """收起主属性区并展开副属性区，返回「已确认展开」那一帧的 (image, lines)。

    面板内容比视口高，底部还有一条「点击无效带」，副属性「点击展开」按钮默认正好压在
    带上，此时点击**必然**不生效（坐标没错，是游戏不响应）。所以这里**不逐步滚**：
    需要展开时，先把面板一次滑到底（`_scroll_panel_to_bottom`），再定位点击；到底后
    布局固定，一次 OCR 就够。返回的帧与 `apply_substat_filter` 的起始状态相同、中间
    无点击，直接复用可省一次全屏 OCR（约 1s）。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    image, lines = _read_panel()

    # 1) 主属性区若展开，先收起，腾出纵向空间（否则副属性区被挤到视口外）。
    main_y = _title_y(lines, MAINSTAT_TITLE)
    main_toggle = _toggle_below(lines, main_y) if main_y is not None else None
    if main_toggle is not None and "收起" in main_toggle.text and (main_toggle.top - main_y) < height / 8:
        release = _click_line(hwnd, main_toggle, role="collapse_mainstats")
        wait_release("collapse_mainstats", release, fallback_ms=84 + _MEASURED_SAFETY_MS)
        image, lines = _read_panel()

    # 2) 副属性区已展开就直接复用这一帧，不必再动。
    title_y = _title_y(lines, SUBSTAT_TITLE)
    toggle = _toggle_below(lines, title_y) if title_y is not None else None
    near = toggle is not None and title_y is not None and (toggle.top - title_y) < height / 8
    if near and "收起" in toggle.text:
        return image, lines

    # 3) 未展开：按钮若压在底部「点击无效带」上（或还没露出来），先一次滑到底，让按钮与
    #    后续所有选项一起进入可点区，再定位点击；已经可点就别多滚。
    if not near or toggle.bottom > height - _PANEL_CLICK_BOTTOM_GAP:
        log.info("副属性「展开」未就位或压在底部无效带，一次滑到底后定位")
        _scroll_panel_to_bottom(hwnd, width, height)
        image, lines = _read_panel()
        title_y = _title_y(lines, SUBSTAT_TITLE)
        toggle = _toggle_below(lines, title_y) if title_y is not None else None
        if toggle is None:
            raise FilterClickError("滑到底后仍找不到副属性词条区的「点击展开」开关，停止筛选")
        if "收起" in toggle.text:
            return image, lines

    release = _click_line(hwnd, toggle, role="open_substats")
    wait_release("open_substats", release, fallback_ms=0)

    after, after_lines = _read_panel()
    # 作者裁定（2026-10-12）：关闭「展开成功」回读检测（原实现保留备查）。
    # after_y = _title_y(after_lines, SUBSTAT_TITLE)
    # after_toggle = _toggle_below(after_lines, after_y) if after_y is not None else None
    # if after_toggle is None or "收起" not in after_toggle.text:
    #     raise FilterClickError("点击副属性「展开」后未确认展开，停止筛选")
    return after, after_lines


def _count_value(text: str) -> int | None:
    match = re.fullmatch(r"[\[【]?([1-4])[\]】]?(?:个|条|项)?", text.replace(" ", ""))
    return int(match[1]) if match else None


def _inspect_substat_targets(
    image: QImage,
    lines: list,
    *,
    names: Sequence[str],
    counts: Sequence[int],
) -> tuple[bool | None, dict[str | int, object]]:
    """副属性目标就位判读（spec §6）。生产执行与静态样本测试共用这一个函数。

    输出当前帧识别到的目标映射：字符串键为输入副词条名，整数键为 1—4 的数量按钮；
    缺失目标不填假框。命中语义复用 `_normalize_stat`、`_count_value`，范围限定在
    「副属性词条」标题下方、右侧面板之内；同名或同数量的重复命中按歧义处理，不取第一个。

    就位值：任一已确认目标 bottom > height-160（面板底部点击无效带）→ False；
    否则缺目标或有歧义 → None；全部目标唯一可见且都在可点击区 → True。
    颜色是否选中不参与就位判读。
    """
    height = image.height()
    click_bottom = height - _PANEL_CLICK_BOTTOM_GAP
    title_y = _title_y(lines, SUBSTAT_TITLE)
    if title_y is None:
        return None, {}
    left, top, right, bottom = scale_rect(1300, 100, 1880, 980, image.width(), height)
    wanted_counts = tuple(range(1, 5))
    found: dict[str | int, object] = {}
    ambiguous: set[str | int] = set()
    for line in lines:
        cx = (line.left + line.right) // 2
        cy = (line.top + line.bottom) // 2
        if line.top <= title_y or not (left <= cx <= right and top <= cy <= bottom):
            continue
        text = _normalize_stat(line.text.replace(" ", ""))
        key: str | int | None = next((n for n in names if _normalize_stat(n) == text), None)
        if key is None:
            key = _count_value(text)
        if key is None:
            continue
        if key in found:
            ambiguous.add(key)
            continue
        found[key] = line
    for key in ambiguous:
        found.pop(key, None)

    if any(line.bottom > click_bottom for line in found.values()):
        return False, found
    missing_names = [n for n in names if n not in found]
    # 数量按钮按 §6.2 的全量清单（1—4）判就位：低于下限的数量也要能取消，少了就没法完成设置
    missing_counts = [c for c in wanted_counts if c not in found]
    if missing_names or missing_counts or ambiguous:
        return None, found
    return True, found


def apply_substat_filter(
    hwnd: int, width: int, height: int, *, names: Sequence[str], counts: Sequence[int]
) -> None:
    """副属性与数量共享一组条件（spec §6 就位契约）。

    先按共享判读函数 `_inspect_substat_targets` 确认全部目标就位，
    **就位之前不选择任何目标**；未就位时只滑到底一次，之后只观察、不再重复滚动；
    就位后用同一帧准备属性和数量，连续点击；最后一次实际点击等待后统一复核。
    """
    _validate_substat_filter(names, counts)
    if not names:
        return
    log = get_logger("zzz_ddtool.gamefilter")
    # _open_substat_section 返回「副属性区已展开」那一帧，状态与这里相同、中间无点击，
    # 直接复用，省掉一次全屏 OCR（约 1s）。
    frame = _open_substat_section(hwnd, width, height)
    # 2026-10-11 真机：数量按钮只在 ≤ 已选词条数时可用；只处理实际可选的数量键。
    count_keys = list(range(1, min(4, len(names)) + 1))
    targets: list[str | int] = [*names, *count_keys]
    expect: dict[str | int, bool] = {name: True for name in names}
    # 重置态可能预选数量：低于下限的数量也要明确取消，不假定其状态。
    expect.update({count: count in counts for count in count_keys})

    ready, found = _inspect_substat_targets(frame[0], frame[1], names=names, counts=counts)
    if ready is not True:
        # 任何目标缺失或压到底部禁区：先一次滑到底，再观察就位；此前不选择部分属性。
        log.info("副属性目标未就位（%s），先一次滑到底再定位", ready)
        _scroll_panel_to_bottom(hwnd, width, height)
        frame = _read_panel()
        ready, found = _inspect_substat_targets(frame[0], frame[1], names=names, counts=counts)
        observed = 1
        while ready is None and observed < _SUBSTAT_OBSERVE_FRAMES:
            time.sleep(_SUBSTAT_OBSERVE_INTERVAL_S)
            frame = _read_panel()
            observed += 1
            ready, found = _inspect_substat_targets(frame[0], frame[1], names=names, counts=counts)
        if ready is not True:
            missing = [key for key in targets if key not in found]
            raise FilterClickError(
                f"副属性目标未就位（{ready}）：缺少 {missing} 或压在点击禁区，停止筛选"
            )

    image, _lines = frame
    done_boxes: dict[str | int, tuple[int, int, int, int]] = {}
    planned = []
    # 用已就位的同一帧决定全部点击，发送过程中不读切换动画。
    for key in targets:
        line = found.get(key)
        if line is None:
            raise FilterClickError(f"副属性目标 {key!r} 没有文字框，停止筛选")
        done_boxes[key] = (line.left, line.top, line.right, line.bottom)
        if _option_selected(image, line) != expect[key]:
            x, y = (line.left + line.right) // 2, (line.top + line.bottom) // 2
            _refuse_dismantle(x, y, width, height)
            planned.append((key, x, y))

    for index, (key, x, y) in enumerate(planned):
        role = "substat_count" if isinstance(key, int) else "substat_option"
        with input_context(role, target=key, selected=expect[key], defer_post_release=True):
            release = click_client(hwnd, x, y)
        last = index == len(planned) - 1
        wait_release(
            role, release,
            fallback_ms=(
                (_SUBSTAT_COUNT_TAIL_WAIT_MS if role == "substat_count" else _SUBSTAT_OPTION_TAIL_WAIT_MS)
                if last else 0
            ),
            allow_measured=last,
        )

    if planned:
        # 不补发点击；动画尚未完成时最多观察三帧，避免首帧仍为旧态就立即判失败。
        mismatched = []
        for observed in range(_SUBSTAT_OBSERVE_FRAMES):
            if observed:
                time.sleep(_SUBSTAT_OBSERVE_INTERVAL_S)
            checkpoint_control()
            image, _ = _grab()
            mismatched = [key for key in targets if _option_selected(image, found[key]) != expect[key]]
            if not mismatched:
                break
        if mismatched:
            key = mismatched[0]
            raise FilterClickError(f"副属性筛选选项 {key!r} 点击后未确认{'选中' if expect[key] else '取消'}")
    for key in targets:
        log.info("substat filter %r selected=%s", key, expect[key])
    # §8.4：全部属性和数量完成、返回之前发一次，复用最后检查帧，不等 Esc 后取证。
    if _evidence_enabled():
        _emit_evidence(
            "substats_selected",
            None,
            targets,
            [done_boxes[key] for key in targets],
            image,
        )
