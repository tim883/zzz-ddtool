"""扫描提速：固定行裁剪 + 模板快路径的驱动盘识别（Spec §3、§4）。

只从**一帧已经取得的客户区图像**读数：不截图、不点击、不读上一盘、不跨盘缓存。
识别一律先裁固定 ROI 再直送 rec 网络，不做整图检测。

公开接口
--------
``try_read_disc(image, *, layout)``
    一帧一次调用，返回 :class:`FastDiscRead`。成功给出 :class:`Disc`；拒绝给出
    Spec §3.2 拒绝原因表内的原因，不抛业务异常。

``_fast_read_override(enabled=True, asset_root=None)``
    仅开发回放/测试用的会话开关（模块级上下文），退出即恢复；不接 GUI/YAML/环境变量。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtGui import QImage

from zzz_ddtool import scanperf
from zzz_ddtool.models import MAX_LEVEL, Disc, Substat
from zzz_ddtool.ocr import (
    _LEVEL_RE,
    _WORD_BOX_LOCK,
    OcrError,
    _get_engine,
    _parse_substat,
    qimage_to_bgr,
    read_action_state_fast,
)
from zzz_ddtool.paths import assets_dir

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 快路径只启用这三套已标定的布局身份证。
PROFILE_IDS: frozenset[str] = frozenset(
    {"warehouse-1920x1080", "modal-1920x1080", "upgrade-1920x1080"}
)

#: ``try_read_disc`` 的 ``layout`` 只接受这两个值（其他字符串抛 ValueError）。
LAYOUTS: tuple[str, ...] = ("detail", "upgrade")

#: 快路径只覆盖这一个客户区尺寸，其他尺寸直接拒绝（不做未经验证的缩放）。
CLIENT_SIZE: tuple[int, int] = (1920, 1080)

#: 拒绝原因表（Spec §3.2）。前四个不选布局，profile_id 必须为 None。
DISABLED = "disabled"
UNSUPPORTED_SIZE = "unsupported_size"
INVALID_ASSETS = "invalid_assets"
UNMATCHED_LAYOUT = "unmatched_layout"
INCOMPLETE_FIELDS = "incomplete_fields"
LOW_CONFIDENCE = "low_confidence"
INVALID_DATA = "invalid_data"

_PRE_LAYOUT_REASONS = frozenset(
    {DISABLED, UNSUPPORTED_SIZE, INVALID_ASSETS, UNMATCHED_LAYOUT}
)
_FIELD_REASONS = frozenset({INCOMPLETE_FIELDS, LOW_CONFIDENCE, INVALID_DATA})
_ALL_REASONS = _PRE_LAYOUT_REASONS | _FIELD_REASONS

#: 墨迹阈值：像素灰度 >= 该值算「有字」。
#: 与步骤 2 模板标定（tmp/build_discocr_assets.py）保持一致。
INK_THRESHOLD = 60.0

#: 空槽判据（Spec §10 自由裁量，标定见 实施记录.md 步骤 2）：
#: 空槽的名称 ROI 最大灰度约 30~37（近黑占位符），占用槽为 255。
#: 60 落在两簇之间，两侧余量都很大。
EMPTY_SLOT_MAX_GRAY = 60.0

#: 模板完整行覆盖检查（Spec §4）：
#: 模板墨迹被 ROI 墨迹覆盖的比例下限、ROI 墨迹落在匹配框外的比例上限。
TEMPLATE_COVERAGE_MIN = 0.95
TEMPLATE_EXTRA_MAX = 0.05

#: 强化页窄格兜底：允许纯数字，但绝不含「/」——免得把经验票「0/1440」读成等级 0。
#: （详情布局仍只认带「等级」字样的 `_LEVEL_RE`。）
_UPGRADE_BARE_LEVEL_RE = re.compile(r"(\d{1,2})")

#: 数值行语法：数字（可带小数）可选百分号。
_VALUE_RE = re.compile(r"\d+(?:\.\d+)?%?")

#: 词条名 = 基名 + 可选 `+N` 后缀。
_ROLL_SUFFIX_RE = re.compile(r"\+(\d+)$")

_SUBSTAT_SLOTS = (1, 2, 3, 4)
_REQUIRED_FIELD_IDS = frozenset(
    {"level"} | {f"substat-{s}-{k}" for s in _SUBSTAT_SLOTS for k in ("name", "value")}
)
_STATE_FIELD_IDS = frozenset({"locked", "discarded"})
_ENTRY_KEYS = frozenset(
    {
        "template_id",
        "profile_id",
        "kind",
        "text",
        "image",
        "sha256",
        "source_image_sha256",
        "match_min_score",
        "match_min_gap",
    }
)
_KINDS = frozenset({"level", "substat_name", "substat_value", "locked", "discarded"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class _AssetError(Exception):
    """资产不完整/不合规：对外统一折算成 ``invalid_assets``。"""


# ---------------------------------------------------------------------------
# 规范化与身份（§6.4 规范化 JSON）
# ---------------------------------------------------------------------------


def canonical_json_bytes(obj: object) -> bytes:
    """Spec §6.4 规范化 JSON：键排序、无多余空白、不转义非 ASCII、禁 NaN。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 资产模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Field:
    field_id: str
    kind: str
    rect: tuple[int, int, int, int]
    slot: int | None


@dataclass(frozen=True)
class _Profile:
    profile_id: str
    layout: str
    client_size: tuple[int, int]
    rec_min_score: float
    fields: tuple[_Field, ...]

    def field(self, field_id: str) -> _Field:
        for item in self.fields:
            if item.field_id == field_id:
                return item
        raise KeyError(field_id)

    def slot_field(self, slot: int, kind: str) -> _Field:
        return self.field(f"substat-{slot}-{'name' if kind == 'substat_name' else 'value'}")

    @property
    def level_field(self) -> _Field:
        return self.field("level")

    @property
    def has_state_fields(self) -> bool:
        return all(any(f.field_id == fid for f in self.fields) for fid in _STATE_FIELD_IDS)


@dataclass(frozen=True)
class _TemplateEntry:
    template_id: str
    profile_id: str
    kind: str
    text: str
    match_min_score: float
    match_min_gap: float
    array: np.ndarray = field(repr=False, compare=False)


@dataclass(frozen=True)
class _Assets:
    root: Path
    identity: str
    profiles: tuple[_Profile, ...]
    templates: dict[tuple[str, str], tuple[_TemplateEntry, ...]]

    def entries(self, profile_id: str, kind: str) -> tuple[_TemplateEntry, ...]:
        return self.templates.get((profile_id, kind), ())


# ---------------------------------------------------------------------------
# 资产加载与校验
# ---------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _rect_of(value: object, *, what: str) -> tuple[int, int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(_is_int(v) for v in value):
        raise _AssetError(f"{what}：rect 必须是 4 个整数")
    left, top, right, bottom = (int(v) for v in value)
    if not (0 <= left < right <= CLIENT_SIZE[0] and 0 <= top < bottom <= CLIENT_SIZE[1]):
        raise _AssetError(f"{what}：rect {value} 越界或不合法")
    return left, top, right, bottom


def _load_profiles(path: Path) -> tuple[_Profile, ...]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _AssetError(f"profiles.json 读取/解析失败：{exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise _AssetError("profiles.json schema_version 不是 1")
    raw = doc.get("profiles")
    if not isinstance(raw, list) or not raw:
        raise _AssetError("profiles.json 缺少 profiles 列表")

    profiles: list[_Profile] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _AssetError("profiles.json 的 profile 必须是对象")
        pid = item.get("profile_id")
        if pid not in PROFILE_IDS:
            raise _AssetError(f"未知 profile_id：{pid!r}")
        layout = item.get("layout")
        if layout not in LAYOUTS:
            raise _AssetError(f"{pid}：layout 非法 {layout!r}")
        client = item.get("client_size")
        if list(client or []) != list(CLIENT_SIZE):
            raise _AssetError(f"{pid}：client_size 必须是 {list(CLIENT_SIZE)}")
        score = item.get("rec_min_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1:
            raise _AssetError(f"{pid}：rec_min_score 必须在 0~1")
        raw_fields = item.get("fields")
        if not isinstance(raw_fields, list):
            raise _AssetError(f"{pid}：fields 必须是列表")
        fields: list[_Field] = []
        for raw_field in raw_fields:
            if not isinstance(raw_field, dict):
                raise _AssetError(f"{pid}：field 必须是对象")
            fid = raw_field.get("field_id")
            kind = raw_field.get("kind")
            if kind not in _KINDS:
                raise _AssetError(f"{pid}：字段 {fid!r} 的 kind 非法 {kind!r}")
            rect = _rect_of(raw_field.get("rect"), what=f"{pid}/{fid}")
            slot = raw_field.get("slot")
            if kind in ("substat_name", "substat_value"):
                if not _is_int(slot) or int(slot) not in _SUBSTAT_SLOTS:
                    raise _AssetError(f"{pid}：字段 {fid!r} 的 slot 必须是 1~4")
                slot = int(slot)
                suffix = "name" if kind == "substat_name" else "value"
                if fid != f"substat-{slot}-{suffix}":
                    raise _AssetError(f"{pid}：字段 {fid!r} 与 slot/kind 不一致")
            elif slot is not None:
                raise _AssetError(f"{pid}：字段 {fid!r} 的 slot 必须为 null")
            elif fid != kind:
                raise _AssetError(f"{pid}：字段 {fid!r} 与 kind {kind!r} 不一致")
            fields.append(_Field(fid, kind, rect, slot))
        ids = [f.field_id for f in fields]
        if len(set(ids)) != len(ids):
            raise _AssetError(f"{pid}：field_id 重复")
        expected = set(_REQUIRED_FIELD_IDS)
        if layout == "detail":
            expected |= set(_STATE_FIELD_IDS)
        if set(ids) != expected:
            raise _AssetError(f"{pid}：fields 集合与布局要求不符（{sorted(set(ids) ^ expected)}）")
        if layout == "detail":
            locked = next(f for f in fields if f.field_id == "locked")
            discarded = next(f for f in fields if f.field_id == "discarded")
            if locked.rect != discarded.rect:
                raise _AssetError(f"{pid}：locked/discarded 必须共用同一个整行 rect")
        profiles.append(_Profile(pid, layout, CLIENT_SIZE, float(score), tuple(fields)))

    if len({p.profile_id for p in profiles}) != len(profiles):
        raise _AssetError("profiles.json 的 profile_id 重复")
    for layout in LAYOUTS:
        if not any(p.layout == layout for p in profiles):
            raise _AssetError(f"profiles.json 缺少 layout={layout} 的 profile")
    return tuple(profiles)


def _load_templates(
    path: Path, root: Path, profiles: tuple[_Profile, ...]
) -> dict[tuple[str, str], tuple[_TemplateEntry, ...]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _AssetError(f"templates.json 读取/解析失败：{exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise _AssetError("templates.json schema_version 不是 1")
    version = doc.get("template_version")
    if not _is_int(version) or int(version) < 1:
        raise _AssetError("templates.json template_version 必须是 >=1 的整数")
    raw = doc.get("entries")
    if not isinstance(raw, list):
        raise _AssetError("templates.json 缺少 entries 列表")

    known = {p.profile_id for p in profiles}
    table: dict[tuple[str, str], list[_TemplateEntry]] = {}
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != _ENTRY_KEYS:
            raise _AssetError("templates.json 的 entry 键集合不符合规范")
        tid = item["template_id"]
        if not isinstance(tid, str) or not tid.strip() or tid in seen:
            raise _AssetError(f"template_id 非法或重复：{tid!r}")
        seen.add(tid)
        pid = item["profile_id"]
        if pid not in known:
            raise _AssetError(f"{tid}：profile_id 未在 profiles.json 中定义")
        kind = item["kind"]
        if kind not in _KINDS:
            raise _AssetError(f"{tid}：kind 非法 {kind!r}")
        text = item["text"]
        if not isinstance(text, str) or not text.strip():
            raise _AssetError(f"{tid}：text 不能为空")
        image = item["image"]
        if not isinstance(image, str) or not image.startswith("templates/"):
            raise _AssetError(f"{tid}：image 必须是 templates/ 下的相对路径")
        image_path = root / image
        if not image_path.is_file() or image_path.is_symlink():
            raise _AssetError(f"{tid}：模板 PNG 缺失 {image}")
        if image_path.parent.resolve() != (root / "templates").resolve():
            raise _AssetError(f"{tid}：image 越出 templates/ 目录")
        digest = item["sha256"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise _AssetError(f"{tid}：sha256 格式非法")
        if _file_sha256(image_path) != digest:
            raise _AssetError(f"{tid}：模板 PNG 与清单 sha256 不一致")
        source = item["source_image_sha256"]
        if not isinstance(source, str) or not _SHA256_RE.fullmatch(source):
            raise _AssetError(f"{tid}：source_image_sha256 格式非法")
        for key in ("match_min_score", "match_min_gap"):
            value = item[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                raise _AssetError(f"{tid}：{key} 必须在 0~1")
        array = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if array is None or array.size == 0:
            raise _AssetError(f"{tid}：模板 PNG 无法解码")
        table.setdefault((pid, kind), []).append(
            _TemplateEntry(
                template_id=tid,
                profile_id=pid,
                kind=kind,
                text=text,
                match_min_score=float(item["match_min_score"]),
                match_min_gap=float(item["match_min_gap"]),
                array=array.astype(np.float32),
            )
        )
    return {key: tuple(value) for key, value in table.items()}


def _content_identity(root: Path) -> str:
    """内容身份 = 相对路径排序后的 {path,sha256} 清单的规范化 SHA256。

    覆盖 ``profiles.json``、``templates.json`` 与 ``templates/`` 下全部普通 PNG。
    只用原字节，不用 mtime、不用 schema_version 代替。
    """
    items: list[dict[str, str]] = []
    paths: list[tuple[str, Path]] = [
        ("profiles.json", root / "profiles.json"),
        ("templates.json", root / "templates.json"),
    ]
    tpl_dir = root / "templates"
    if tpl_dir.is_dir():
        for candidate in tpl_dir.iterdir():
            if candidate.is_file() and not candidate.is_symlink() and candidate.suffix.lower() == ".png":
                paths.append((f"templates/{candidate.name}", candidate))
    for rel, path in paths:
        if not path.is_file() or path.is_symlink():
            raise _AssetError(f"资产文件缺失：{rel}")
        items.append({"path": rel, "sha256": _file_sha256(path)})
    items.sort(key=lambda item: item["path"])
    return hashlib.sha256(canonical_json_bytes(items)).hexdigest()


def _load_assets(root: Path) -> _Assets:
    """完整校验一次资产树；任何不合规都折算成 :class:`_AssetError`。"""
    root = Path(root)
    if not root.is_dir():
        raise _AssetError(f"资产目录不存在：{root}")
    identity = _content_identity(root)
    profiles = _load_profiles(root / "profiles.json")
    templates = _load_templates(root / "templates.json", root, profiles)
    return _Assets(root=root, identity=identity, profiles=profiles, templates=templates)


# ---------------------------------------------------------------------------
# 会话与 override
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()

#: 当前会话是否启用快路径、用哪个资产根。只在开发 override 里改变。
_override_enabled = True
_override_root: Path | None = None


@dataclass
class _Session:
    """一次会话的已校验只读资产快照（或加载失败原因）。"""

    enabled: bool
    root: Path | None
    assets: _Assets | None = None
    error: str | None = None


_session: _Session | None = None


def _reset_session() -> None:
    global _session
    with _state_lock:
        _session = None


@contextmanager
def _fast_read_override(
    *, enabled: bool = True, asset_root: Path | None = None
) -> Iterator[None]:
    """仅开发回放/测试：切换启用状态与资产根，并强制重新核对资产。

    只作用于当前会话；退出时把状态与上一次的会话快照一起恢复。不重置
    :func:`_get_engine` 的引擎/ONNX 会话——资产损坏只关快路径，仍回到同一引擎的
    legacy 解析。
    """
    global _override_enabled, _override_root, _session
    with _state_lock:
        previous = (_override_enabled, _override_root, _session)
        _override_enabled = bool(enabled)
        _override_root = Path(asset_root) if asset_root is not None else None
        _session = None
    try:
        yield
    finally:
        with _state_lock:
            _override_enabled, _override_root, _session = previous


def _current_assets() -> tuple[_Assets | None, str | None]:
    """取得当前会话已校验的资产（首次调用时校验加载，失败记原因）。"""
    global _session
    with _state_lock:
        if _session is None:
            _session = _Session(enabled=_override_enabled, root=_override_root)
        if _session.assets is None and _session.error is None:
            root = _session.root if _session.root is not None else assets_dir() / "discocr"
            try:
                _session.assets = _load_assets(root)
            except _AssetError as exc:
                _session.error = str(exc)
            except OSError as exc:  # 读盘失败同样只关快路径
                _session.error = f"资产读取失败：{exc}"
        return _session.assets, _session.error


# ---------------------------------------------------------------------------
# 单帧识别
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Text:
    """一段识别结果：文字 + 置信度（模板命中时为模板相关度）。"""

    text: str
    score: float


@dataclass(frozen=True)
class FastDiscRead:
    """快路径单帧结果（Spec §3.2）。"""

    disc: Disc | None
    profile_id: str | None
    reject_reason: str | None

    def __post_init__(self) -> None:
        if self.disc is not None:
            if self.reject_reason is not None:
                raise ValueError("成功结果不能同时带拒绝原因")
            if self.profile_id not in PROFILE_IDS:
                raise ValueError(f"成功结果的 profile_id 非法：{self.profile_id!r}")
            return
        reason = self.reject_reason
        if reason not in _ALL_REASONS:
            raise ValueError(f"拒绝原因不在 Spec §3.2 表内：{reason!r}")
        if reason in _PRE_LAYOUT_REASONS:
            if self.profile_id is not None:
                raise ValueError(f"{reason} 阶段不得携带 profile_id")
        elif self.profile_id not in PROFILE_IDS:
            raise ValueError(f"{reason} 阶段必须携带已选择布局的 profile_id")


def _band_gray(image: QImage, rect: tuple[int, int, int, int]) -> np.ndarray:
    """裁出 ROI 并转成标定用的灰度（与步骤 2 模板构建同口径）。"""
    left, top, right, bottom = rect
    crop = qimage_to_bgr(image.copy(left, top, right - left, bottom - top))
    return crop.mean(axis=2).astype(np.float32)


def _gray_is_empty(gray: np.ndarray) -> bool:
    """空槽判据：ROI 最大灰度低于阈值即认作空（近黑占位符）。"""
    return bool(gray.size == 0 or float(gray.max()) < EMPTY_SLOT_MAX_GRAY)


def _match_template(
    roi: np.ndarray, template: np.ndarray
) -> tuple[float, float, float] | None:
    """在 ROI 内搜索模板，返回 ``(相关度, 覆盖率, 框外墨迹比例)``。"""
    th, tw = template.shape[:2]
    rh, rw = roi.shape[:2]
    if th > rh or tw > rw:
        return None
    result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
    _, peak, _, location = cv2.minMaxLoc(result)
    if not math.isfinite(peak):
        return None
    x, y = int(location[0]), int(location[1])
    patch = roi[y : y + th, x : x + tw]
    template_ink = template >= INK_THRESHOLD
    patch_ink = patch >= INK_THRESHOLD
    total_ink = int(template_ink.sum())
    coverage = float((template_ink & patch_ink).sum()) / float(total_ink) if total_ink else 0.0
    roi_ink = roi >= INK_THRESHOLD
    roi_total = int(roi_ink.sum())
    if roi_total == 0:
        return float(peak), coverage, 0.0
    covered = np.zeros_like(roi_ink)
    covered[y : y + th, x : x + tw] = True
    extra = float((roi_ink & ~covered).sum()) / float(roi_total)
    return float(peak), coverage, extra


def _template_match_entry(
    assets: _Assets, profile: _Profile, field: _Field, gray_roi: np.ndarray
) -> tuple[_Text | None, str | None]:
    """模板判定核心：返回 ``(命中文字, 命中的模板 ID)``；未命中为 ``(None, None)``。"""
    scored = []
    for entry in assets.entries(profile.profile_id, field.kind):
        matched = _match_template(gray_roi, entry.array)
        if matched is not None:
            scored.append((matched[0], entry, matched[1], matched[2]))
    if not scored:
        return None, None
    scored.sort(key=lambda item: -item[0])
    correlation, entry, coverage, extra = scored[0]
    if correlation < entry.match_min_score:
        return None, None
    if len(scored) > 1 and correlation - scored[1][0] < entry.match_min_gap:
        return None, None
    if coverage < TEMPLATE_COVERAGE_MIN or extra > TEMPLATE_EXTRA_MAX:
        return None, None
    return _Text(entry.text, correlation), entry.template_id


def _template_text(
    assets: _Assets,
    profile: _Profile,
    field: _Field,
    gray_roi: np.ndarray,
    *,
    record: bool = True,
) -> _Text | None:
    """按 Spec §4 的「一级候选 + 分差 + 完整行覆盖」决定是否采用模板文字。

    ``record=False``：只用于布局判别，结果不计入 ``template_match``/``template_hits``
    （§7.1.1 的 template_hits 是「接受并用于字段解析」的行数）。
    """
    if not record:
        text, _template_id = _template_match_entry(assets, profile, field, gray_roi)
        return text

    with scanperf.timed(
        "template_match",
        details={
            "layout": profile.layout,
            "profile_id": profile.profile_id,
            "roi_ids": [field.field_id],
            "template_hits": 0,
            "matches": [],
        },
    ) as span:
        text, template_id = _template_match_entry(assets, profile, field, gray_roi)
        if text is None:
            span.set(matches=[{"roi_id": field.field_id, "template_id": None, "accepted": False}])
        else:
            span.set(
                template_hits=1,
                matches=[
                    {"roi_id": field.field_id, "template_id": template_id, "accepted": True}
                ],
            )
        return text


def _recognize_bands(
    image: QImage,
    rects: Sequence[tuple[int, int, int, int]],
    *,
    roi_ids: Sequence[str] | None = None,
    profile_id: str | None = None,
    layout: str | None = None,
) -> list[_Text | None]:
    """固定行 ROI 一次批量识别：直接送 rec 网络，不做检测。

    结果严格按输入 ROI 顺序返回（识别器内部会按宽度重排，本函数按原下标还原；
    引擎锁保证这一批不与另一帧的识别交织）。

    性能：准备 ROI 与送网络分开计时——``crop`` 是裁剪/转格式区间，``recognize_batch``
    只含 rec 调用、不含裁剪与检测成本（§7.1）。无活动会话时两者都是 no-op。
    """
    if not rects:
        return []
    from rapidocr.ch_ppocr_rec import TextRecInput

    ids = list(roi_ids) if roi_ids is not None else [list(rect) for rect in rects]
    with scanperf.timed(
        "crop",
        details={
            "layout": layout,
            "profile_id": profile_id,
            "roi_ids": ids,
            "rects": [list(rect) for rect in rects],
            "frame_size": [image.width(), image.height()],
        },
    ):
        crops = [qimage_to_bgr(image.copy(l, t, r - l, b - t)) for l, t, r, b in rects]
    with scanperf.timed(
        "recognize_batch",
        details={"roi_ids": ids, "batch_size": len(crops), "profile_id": profile_id},
    ):
        with _WORD_BOX_LOCK:
            result = _get_engine().text_rec(TextRecInput(img=crops))
    texts, scores = result.txts, result.scores
    if texts is None or scores is None or len(texts) != len(crops) or len(scores) != len(crops):
        raise OcrError("固定行批量识别返回数量与输入 ROI 不一致")
    output: list[_Text | None] = []
    for text, score in zip(texts, scores):
        stripped = str(text).strip()
        value = float(score)
        output.append(
            _Text(stripped, value) if stripped and math.isfinite(value) else None
        )
    return output


def _parse_level_text(text: str, layout: str) -> tuple[int, str | None] | None:
    """等级文字 → ``(等级, 稀有度)``；不属于合法等级行返回 None。

    稀有度只由等级上限推出（15→S、12→A、9→B）；上限缺失或不在档位时为 None。
    """
    from zzz_ddtool.ocr import RARITY_BY_MAX_LEVEL

    compact = text.replace(" ", "")
    match = _LEVEL_RE.search(compact)
    if match is not None:
        level = int(match.group(1))
        if not 0 <= level <= MAX_LEVEL:
            return None
        cap = match.group(2)
        rarity = RARITY_BY_MAX_LEVEL.get(int(cap)) if cap else None
        return level, rarity
    if layout == "upgrade" and "/" not in compact:
        # 强化页窄格常常只剩一个裸数字；这里也绝不放行「/」，避免把经验票读成等级。
        match = _UPGRADE_BARE_LEVEL_RE.fullmatch(compact)
        if match is not None:
            level = int(match.group(1))
            if 0 <= level <= MAX_LEVEL:
                return level, None
    return None


def _value_is_number(text: str) -> bool:
    """数值行完整性：去空白与百分号兜底纠正后必须是纯数字。"""
    from zzz_ddtool.ocr import _fix_percent

    compact = _fix_percent(text.replace(" ", ""))
    return _VALUE_RE.fullmatch(compact) is not None


def _name_base_is_known(text: str) -> bool:
    """词条名合法性：剥掉 `+N` 后缀、做过 OCR 兜底纠正后必须能落到某个副词条。"""
    from zzz_ddtool.ocr import _SUBSTAT_OCR_FIXES, _best_match
    from zzz_ddtool.gamedata import SUBSTAT_NAMES

    match = _ROLL_SUFFIX_RE.search(text)
    base = text[: match.start()] if match else text
    if not base:
        return False
    for wrong, right in _SUBSTAT_OCR_FIXES.items():
        base = base.replace(wrong, right)
    return _best_match(base, SUBSTAT_NAMES, cutoff=0.55) is not None


def _reject(profile_id: str | None, reason: str) -> FastDiscRead:
    return FastDiscRead(None, profile_id, reason)


def _level_parses(text: _Text | None, layout: str) -> bool:
    """布局判别用的等级可用性：文字存在且能解析（不看置信度，留给阶段 4）。"""
    return text is not None and _parse_level_text(text.text, layout) is not None


def try_read_disc(image: QImage, *, layout: str) -> FastDiscRead:
    """一帧一次的快路径识别（Spec §3.2）。

    只读传入的这一帧；不截图、不点击、不跨盘缓存。返回 :class:`FastDiscRead`：
    成功给出 ``Disc``，拒绝给出原因表内的原因。识别网络自身抛出的异常按 §6.1.1
    原样传播，不折算成拒绝原因。
    """
    if layout not in LAYOUTS:
        raise ValueError(f"未知布局 {layout!r}：只接受 {'/'.join(LAYOUTS)}")
    if not _override_enabled:
        return _reject(None, DISABLED)
    if image.isNull() or (image.width(), image.height()) != CLIENT_SIZE:
        return _reject(None, UNSUPPORTED_SIZE)
    assets, _error = _current_assets()
    if assets is None:
        return _reject(None, INVALID_ASSETS)

    candidates = [p for p in assets.profiles if p.layout == layout]
    if not candidates:
        return _reject(None, UNMATCHED_LAYOUT)

    # 阶段 3：用 level 字段唯一判别布局。模板优先，未命中再对该 ROI 识别。
    # crop 是 parse_total 的子事件（§7.1）：只测本批 ROI 的裁剪/转格式；布局判别用的
    # 模板记录 record=False，不进 template_hits。
    level_texts: dict[str, _Text | None] = {}
    level_grays: dict[str, np.ndarray] = {}
    with scanperf.timed(
        "crop",
        details={
            "layout": layout,
            "profile_id": None,
            "roi_ids": [],
            "rects": [],
            "frame_size": [image.width(), image.height()],
        },
    ) as crop_span:
        roi_ids: list[str] = []
        rects: list[list[int]] = []
        for profile in candidates:
            level_field = profile.level_field
            level_grays[profile.profile_id] = _band_gray(image, level_field.rect)
            roi_ids.append(level_field.field_id)
            rects.append(list(level_field.rect))
        crop_span.set(roi_ids=roi_ids, rects=rects)

    pending: list[tuple[_Profile, _Field]] = []
    for profile in candidates:
        level_field = profile.level_field
        text = _template_text(
            assets, profile, level_field, level_grays[profile.profile_id], record=False
        )
        if text is None:
            pending.append((profile, level_field))
        else:
            level_texts[profile.profile_id] = text
    if pending:
        results = _recognize_bands(
            image,
            [item.rect for _p, item in pending],
            roi_ids=[item.field_id for _p, item in pending],
            profile_id=None,
            layout=layout,
        )
        for (profile, _field), text in zip(pending, results):
            level_texts[profile.profile_id] = text
    matched = [p for p in candidates if _level_parses(level_texts[p.profile_id], layout)]
    if len(matched) != 1:
        return _reject(None, UNMATCHED_LAYOUT)
    profile = matched[0]
    return _read_fields(assets, profile, image, layout, level_texts[profile.profile_id])


def _read_fields(
    assets: _Assets,
    profile: _Profile,
    image: QImage,
    layout: str,
    level_text: _Text | None,
) -> FastDiscRead:
    """阶段 4：按清单 field 顺序逐个检查 完整性 → 置信度 → 解析合法性。"""
    grays: dict[str, np.ndarray] = {}
    resolved: dict[str, _Text] = {}
    pending: list[_Field] = []

    # 批次 1：准备本布局全部字段的灰度 ROI（crop 子事件，§7.1）。
    # 先单独裁完再判定，避免把 template_match 挂到 crop 区间下、破坏 parse_total 聚合。
    with scanperf.timed(
        "crop",
        details={
            "layout": layout,
            "profile_id": profile.profile_id,
            "roi_ids": [],
            "rects": [],
            "frame_size": [image.width(), image.height()],
        },
    ) as crop_span:
        crop_ids: list[str] = []
        crop_rects: list[list[int]] = []
        for field in profile.fields:
            if field.kind not in ("level", "substat_name", "substat_value"):
                continue
            grays[field.field_id] = _band_gray(image, field.rect)
            crop_ids.append(field.field_id)
            crop_rects.append(list(field.rect))
        crop_span.set(roi_ids=crop_ids, rects=crop_rects)

    # 批次 2：模板/识别判定；template_match / recognize_batch 直接挂在 parse_total 下。
    for field in profile.fields:
        if field.kind not in ("level", "substat_name", "substat_value"):
            continue
        gray = grays[field.field_id]
        if field.kind == "level":
            text = _template_text(assets, profile, field, gray)
            if text is None:
                # 布局判别阶段已对该 ROI 识别过（或模板命中），沿用同一结果。
                text = level_text
            if text is not None:
                resolved[field.field_id] = text
            continue
        if field.kind == "substat_name":
            text = _template_text(assets, profile, field, gray)
            if text is not None:
                resolved[field.field_id] = text
            elif not _gray_is_empty(gray):
                pending.append(field)
            continue
        if not _gray_is_empty(gray):
            pending.append(field)
    if pending:
        for field, text in zip(
            pending,
            _recognize_bands(
                image,
                [f.rect for f in pending],
                roi_ids=[f.field_id for f in pending],
                profile_id=profile.profile_id,
                layout=layout,
            ),
        ):
            if text is not None:
                resolved[field.field_id] = text

    # ---- 等级：完整性 → 置信度 → 解析合法性 ----
    level_value = resolved.get("level")
    if level_value is None or not level_value.text.strip():
        return _reject(profile.profile_id, INCOMPLETE_FIELDS)
    if level_value.score < profile.rec_min_score:
        return _reject(profile.profile_id, LOW_CONFIDENCE)
    parsed_level = _parse_level_text(level_value.text, layout)
    if parsed_level is None:
        return _reject(profile.profile_id, INVALID_DATA)
    level, rarity = parsed_level

    # ---- 四个槽：1~3 必须完整；第 4 槽要么完整，要么由空槽证据确认 ----
    substats: list[Substat] = []
    for slot in _SUBSTAT_SLOTS:
        name_field = profile.slot_field(slot, "substat_name")
        value_field = profile.slot_field(slot, "substat_value")
        name_text = resolved.get(name_field.field_id)
        value_text = resolved.get(value_field.field_id)
        name_empty = name_text is None and _gray_is_empty(grays[name_field.field_id])
        value_empty = value_text is None and _gray_is_empty(grays[value_field.field_id])
        if name_empty and value_empty:
            if slot < 4:
                return _reject(profile.profile_id, INCOMPLETE_FIELDS)
            continue  # 第 4 槽：已标定空槽证据成立
        if name_text is None or not name_text.text.strip():
            return _reject(profile.profile_id, INCOMPLETE_FIELDS)
        if name_text.score < profile.rec_min_score:
            return _reject(profile.profile_id, LOW_CONFIDENCE)
        if not _name_base_is_known(name_text.text):
            return _reject(profile.profile_id, INVALID_DATA)
        if value_text is None or not value_text.text.strip():
            return _reject(profile.profile_id, INCOMPLETE_FIELDS)
        if value_text.score < profile.rec_min_score:
            return _reject(profile.profile_id, LOW_CONFIDENCE)
        if not _value_is_number(value_text.text):
            return _reject(profile.profile_id, INVALID_DATA)
        substat = _parse_substat(name_text.text, value_text.text)
        if substat is None:
            return _reject(profile.profile_id, INVALID_DATA)
        substats.append(substat)

    if not substats:
        return _reject(profile.profile_id, INCOMPLETE_FIELDS)

    # ---- R/T：详情布局共用整行只裁一次、只识别一次；强化页不读状态 ----
    if profile.has_state_fields:
        state = read_action_state_fast(image)
        locked, discarded = state["locked"], state["discarded"]
    else:
        locked, discarded = False, False

    disc = Disc(
        level=level,
        substats=tuple(substats),
        rarity=rarity,
        locked=locked,
        equipped=False,
        discarded=discarded,
    )
    return FastDiscRead(disc, profile.profile_id, None)
