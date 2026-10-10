"""套装名图片模板比对：模板优先、名称带 OCR 兜底（用户决定 2026-10-11）。

资产：``assets/picker/templates.json`` + ``templates/*.png``（从读对帧裁出的
灰度名称框）。判定与 ``discocr`` 同一套路：``cv2.matchTemplate``（TM_CCOEFF_NORMED）
取最像的模板，相关度达到该模板 ``match_min_score`` 且与次优名称的分差达到
``match_min_gap`` 才认定；未命中/歧义返回 ``None``，由调用方回落名称带 OCR。

只做纯读取：不发送输入、不碰 OCR 引擎、不写文件。资产损坏时抛
``PickerAssetError``，由调用方决定回落策略（生产回落纯 OCR，不点击）。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from zzz_ddtool.ocr import qimage_to_bgr
from zzz_ddtool.paths import assets_dir

if TYPE_CHECKING:  # 运行期只用作类型标注
    from PySide6.QtGui import QImage

SCHEMA_VERSION = 1
#: 名称带四周外扩的搜索区（像素）。名称框紧贴文字，靠外扩容忍跨帧 1～3px 的错位。
ROI_EXPAND = 8

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY_KEYS = {
    "template_id", "name", "image", "sha256", "source_image_sha256",
    "match_min_score", "match_min_gap", "source_frame",
}


class PickerAssetError(RuntimeError):
    """套装名模板资产缺失/损坏/不合法。"""


@dataclass(frozen=True)
class _Entry:
    template_id: str
    name: str
    match_min_score: float
    match_min_gap: float
    array: np.ndarray


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _load_entries(root: Path) -> tuple[_Entry, ...]:
    try:
        doc = json.loads((root / "templates.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PickerAssetError(f"templates.json 读取/解析失败：{exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        raise PickerAssetError("templates.json schema_version 不是 1")
    raw = doc.get("entries")
    if not isinstance(raw, list) or not raw:
        raise PickerAssetError("templates.json 缺少 entries 列表")
    entries: list[_Entry] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != _ENTRY_KEYS:
            raise PickerAssetError("templates.json 的 entry 键集合不符合规范")
        tid = item["template_id"]
        name = item["name"]
        if not isinstance(tid, str) or not tid.strip() or tid in seen:
            raise PickerAssetError(f"template_id 非法或重复：{tid!r}")
        seen.add(tid)
        if not isinstance(name, str) or not name.strip():
            raise PickerAssetError(f"{tid}：name 不能为空")
        image = item["image"]
        if not isinstance(image, str) or not image.startswith("templates/"):
            raise PickerAssetError(f"{tid}：image 必须是 templates/ 下的相对路径")
        path = root / image
        if not path.is_file() or path.is_symlink():
            raise PickerAssetError(f"{tid}：模板 PNG 缺失 {image}")
        if path.parent.resolve() != (root / "templates").resolve():
            raise PickerAssetError(f"{tid}：image 越出 templates/ 目录")
        digest = item["sha256"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise PickerAssetError(f"{tid}：sha256 格式非法")
        if _file_sha256(path) != digest:
            raise PickerAssetError(f"{tid}：模板 PNG 与清单 sha256 不一致")
        source = item["source_image_sha256"]
        if not isinstance(source, str) or not _SHA256_RE.fullmatch(source):
            raise PickerAssetError(f"{tid}：source_image_sha256 格式非法")
        for key in ("match_min_score", "match_min_gap"):
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise PickerAssetError(f"{tid}：{key} 必须在 0~1")
        array = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if array is None or array.size == 0:
            raise PickerAssetError(f"{tid}：模板 PNG 无法解码")
        entries.append(
            _Entry(
                template_id=tid,
                name=name,
                match_min_score=float(item["match_min_score"]),
                match_min_gap=float(item["match_min_gap"]),
                array=array,
            )
        )
    return tuple(entries)


_cache: dict[str, tuple[_Entry, ...]] = {}


def load_entries(root: Path | None = None) -> tuple[_Entry, ...]:
    """加载并校验模板资产；同一根目录只解析一次。失败抛 ``PickerAssetError``。"""
    target = Path(root) if root is not None else assets_dir() / "picker"
    key = str(target)
    cached = _cache.get(key)
    if cached is None:
        cached = _load_entries(target)
        _cache[key] = cached
    return cached


def reset_cache() -> None:
    """测试用：清空加载缓存。"""
    _cache.clear()


def classify(
    image: "QImage", band: tuple[int, int, int, int], *, root: Path | None = None
) -> tuple[str, float, tuple[int, int, int, int]] | None:
    """在名称带 ``band`` 内按模板认套装名。

    返回 ``(name, score, box)``；box 为模板在客户区像素中的实际位置（点击与指纹用）。
    未命中/歧义返回 ``None``。资产问题抛 ``PickerAssetError``。
    """
    entries = load_entries(root)
    left, top, right, bottom = band
    roi_l = max(0, left - ROI_EXPAND)
    roi_t = max(0, top - ROI_EXPAND)
    roi_r = min(image.width(), right + ROI_EXPAND)
    roi_b = min(image.height(), bottom + ROI_EXPAND)
    if roi_r <= roi_l or roi_b <= roi_t:
        return None
    crop = image.copy(roi_l, roi_t, roi_r - roi_l, roi_b - roi_t)
    gray = cv2.cvtColor(qimage_to_bgr(crop), cv2.COLOR_BGR2GRAY)

    best: dict[str, tuple[float, tuple[int, int], _Entry]] = {}
    for entry in entries:
        th, tw = entry.array.shape
        if th > gray.shape[0] or tw > gray.shape[1]:
            continue
        result = cv2.matchTemplate(gray, entry.array, cv2.TM_CCOEFF_NORMED)
        _, peak, _, location = cv2.minMaxLoc(result)
        if not math.isfinite(peak):
            continue
        current = best.get(entry.name)
        if current is None or peak > current[0]:
            best[entry.name] = (float(peak), (int(location[0]), int(location[1])), entry)
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda pair: -pair[1][0])
    name, (score, location, entry) = ranked[0]
    second = ranked[1][1][0] if len(ranked) > 1 else None
    if score < entry.match_min_score:
        return None
    if second is not None and score - second < entry.match_min_gap:
        return None
    th, tw = entry.array.shape
    box = (roi_l + location[0], roi_t + location[1], roi_l + location[0] + tw, roi_t + location[1] + th)
    return name, score, box
