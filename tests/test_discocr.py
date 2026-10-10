"""扫描提速快路径（``discocr``）的直接断言：Spec §8 的 O1~O13、C2。

用例只做两件事：
* 用真实归档图验证「快路径成功/拒绝 + 与 legacy 逐字段一致 + 实际采用的 profile」；
* 用替身把「字段缺失/漏读/低分/模板歧义/资产损坏」等分支构造成确定性输入。

所有真实图都在仓库里，缺失即失败，不静默跳过。
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import discocr
from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.ocr import (
    OcrError,
    _parse_disc_legacy,
    _parse_disc_upgrade_page_legacy,
    parse_disc,
    parse_disc_upgrade_page,
)
from zzz_ddtool.paths import assets_dir

REPO = Path(__file__).resolve().parents[1]
REF = REPO / "手册" / "参考图" / "驱动盘详情"
FIXTURES = REPO / "tests" / "fixtures"

SIDEBAR = REF / "首档复用-侧栏盘面.png"
MODAL = REF / "详情弹层.png"
UPGRADE_KEPT = REF / "首档复用-强化未升级.png"

_WAREHOUSE = "warehouse-1920x1080"
_MODAL = "modal-1920x1080"
_UPGRADE = "upgrade-1920x1080"


def load(path: Path) -> QImage:
    image = QImage(str(path))
    assert not image.isNull(), f"参考图缺失或无法解码：{path}"
    assert (image.width(), image.height()) == (1920, 1080), path
    return image


def serialized(disc: Disc | None) -> dict | None:
    if disc is None:
        return None
    return {
        "level": disc.level,
        "substats": [{"name": s.name, "rolls": s.rolls} for s in disc.substats],
        "rarity": disc.rarity,
        "locked": disc.locked,
        "equipped": disc.equipped,
        "discarded": disc.discarded,
    }


def patch_recognition(monkeypatch, texts, *, default=None) -> None:
    """按 rect 指定识别文字（None = 没认出）。"""

    def fake(_image, rects, **_kwargs):
        return [texts.get(tuple(rect), default) for rect in rects]

    monkeypatch.setattr(discocr, "_recognize_bands", fake)


def stub_assets(*entries, profile_id=_MODAL) -> discocr._Assets:
    profile = discocr._Profile(profile_id, "detail", (1920, 1080), 0.90, ())
    table: dict[tuple[str, str], list[discocr._TemplateEntry]] = {}
    for entry in entries:
        table.setdefault((entry.profile_id, entry.kind), []).append(entry)
    return discocr._Assets(
        root=Path("."),
        identity="stub",
        profiles=(profile,),
        templates={key: tuple(value) for key, value in table.items()},
    )


def tight_template(gray: np.ndarray, pad: int = 2) -> np.ndarray:
    ink = gray >= discocr.INK_THRESHOLD
    ys, xs = np.where(ink)
    assert len(ys), "构造模板的区域没有墨迹"
    y0, x0 = max(0, int(ys.min()) - pad), max(0, int(xs.min()) - pad)
    y1 = min(gray.shape[0], int(ys.max()) + 1 + pad)
    x1 = min(gray.shape[1], int(xs.max()) + 1 + pad)
    return gray[y0:y1, x0:x1].astype(np.float32)


def entry(template_id, text, array, *, kind="substat_name", profile_id=_MODAL,
          score=0.99, gap=0.03) -> discocr._TemplateEntry:
    return discocr._TemplateEntry(
        template_id=template_id,
        profile_id=profile_id,
        kind=kind,
        text=text,
        match_min_score=score,
        match_min_gap=gap,
        array=array,
    )


# ---------------------------------------------------------------------------
# O1 仓库侧栏：快路径成功且逐字段等于冻结期望
# ---------------------------------------------------------------------------


def test_o1_sidebar_fast_path_uses_warehouse_profile() -> None:
    """§8 O1：仓库侧栏单列盘面必须由 warehouse profile 的快路径直接读出。"""
    image = load(SIDEBAR)
    result = discocr.try_read_disc(image, layout="detail")
    assert result.reject_reason is None
    assert result.profile_id == _WAREHOUSE
    assert result.disc == Disc(
        level=0,
        substats=(
            Substat("异常精通", 1),
            Substat("生命值%", 1),
            Substat("暴击伤害", 1),
            Substat("穿透值", 1),
        ),
        rarity="S",
        locked=False,
        equipped=False,
        discarded=False,
    )


def test_o1_sidebar_public_entry_equals_legacy() -> None:
    """公开入口在同帧先快路径、拒绝才回退；结果必须与 legacy 逐字段一致。"""
    image = load(SIDEBAR)
    assert serialized(parse_disc(image)) == serialized(_parse_disc_legacy(image))


# ---------------------------------------------------------------------------
# O2 普通详情弹层：三词盘，第四槽必须由空槽证据确认，不许补
# ---------------------------------------------------------------------------


def test_o2_modal_fast_path_uses_modal_profile() -> None:
    image = load(MODAL)
    result = discocr.try_read_disc(image, layout="detail")
    assert result.reject_reason is None
    assert result.profile_id == _MODAL
    assert result.disc == Disc(
        level=0,
        substats=(Substat("防御力", 1), Substat("攻击力%", 1), Substat("暴击率", 1)),
        rarity=None,
        locked=None,
        equipped=False,
        discarded=None,
    )


def test_o2_modal_never_invents_fourth_substat() -> None:
    """第四槽是空槽证据（近黑占位符），不能按「OCR 成功行数」凭空补一条。"""
    image = load(MODAL)
    disc = parse_disc(image)
    assert disc is not None and len(disc.substats) == 3
    assert serialized(disc) == serialized(_parse_disc_legacy(image))


# ---------------------------------------------------------------------------
# O3 仓库侧栏（真机失败现场图）
# ---------------------------------------------------------------------------


def test_o3_warehouse_detail_fast_path_fields() -> None:
    image = load(FIXTURES / "warehouse-detail.png")
    result = discocr.try_read_disc(image, layout="detail")
    assert result.profile_id == _WAREHOUSE and result.reject_reason is None
    assert result.disc == Disc(
        level=3,
        substats=(
            Substat("暴击伤害", 1),
            Substat("暴击率", 1),
            Substat("攻击力", 1),
            Substat("异常精通", 1),
        ),
        rarity="S",
        locked=False,
        equipped=False,
        discarded=False,
    )
    assert serialized(parse_disc(image)) == serialized(_parse_disc_legacy(image))


# ---------------------------------------------------------------------------
# O4 模板：基础名称模板不接受带 +N 的整行；完整行模板可省名称 OCR
# ---------------------------------------------------------------------------


def test_o4_base_name_template_rejects_full_line_with_roll_suffix() -> None:
    """`攻击力` 基础模板命中 `攻击力+2` 整行时，尾部墨迹超出匹配框 → 必须不命中。"""
    image = load(FIXTURES / "upgrade-level-double-digit.png")
    assets, _error = discocr._current_assets()
    profile = next(p for p in assets.profiles if p.profile_id == _UPGRADE)
    field = profile.field("substat-2-name")
    # 该 ROI 实拍文字是「攻击力+2」
    assert field.rect == (1295, 419, 1425, 460)
    gray = discocr._band_gray(image, field.rect)
    base = next(e for e in assets.entries(_UPGRADE, "substat_name")
                if e.template_id == "up-name-gongjili")
    matched = discocr._match_template(gray, base.array)
    assert matched is not None
    _corr, _coverage, extra = matched
    assert extra > discocr.TEMPLATE_EXTRA_MAX
    assert discocr._template_text(assets, profile, field, gray) is None
    # 把相关度门槛放宽到 0.90，唯一还能拦住整行的就是「框外墨迹」检查
    relaxed = stub_assets(
        entry("up-base-relaxed", "攻击力", base.array, profile_id=_UPGRADE, score=0.90),
        profile_id=_UPGRADE,
    )
    assert discocr._template_text(relaxed, relaxed.profiles[0], field, gray) is None
    # 未命中就送 ROI 识别，识别到的完整文字仍能解析出 rolls=3
    text = discocr._recognize_bands(image, [field.rect])[0]
    assert text is not None and text.text == "攻击力+2"
    from zzz_ddtool.ocr import _parse_substat

    assert _parse_substat(text.text, "%6") == Substat("攻击力%", 3)


def test_o4_full_line_template_skips_name_ocr_but_value_still_read() -> None:
    """含完整 `攻击力+2` 的模板命中后可省名称 OCR；数值行仍要识别。"""
    image = load(FIXTURES / "upgrade-level-double-digit.png")
    rect = (1295, 419, 1425, 460)
    gray = discocr._band_gray(image, rect)
    assets = stub_assets(entry("up-full-name", "攻击力+2", tight_template(gray)))
    profile = assets.profiles[0]
    field = discocr._Field("substat-2-name", "substat_name", rect, 2)
    decided = discocr._template_text(assets, profile, field, gray)
    assert decided is not None and decided.text == "攻击力+2"
    value = discocr._recognize_bands(image, [(1605, 419, 1692, 460)])[0]
    assert value is not None and discocr._value_is_number(value.text)


def test_o4_template_rejects_ink_outside_the_match_box() -> None:
    """完整行覆盖检查的另一半：模板墨迹被全覆盖，但匹配框外还有别的字 → 不命中。"""
    image = load(FIXTURES / "warehouse-detail.png")
    rect = (1418, 564, 1560, 615)  # 实拍文字「暴击伤害」
    template = tight_template(discocr._band_gray(image, rect))
    th, tw = template.shape
    roi = np.zeros((41, 200), np.float32)
    roi[4 : 4 + th, 4 : 4 + tw] = template
    roi[4 : 4 + th, 4 + tw + 12 : 4 + tw + 60] = 255.0  # 匹配框外的额外墨迹

    matched = discocr._match_template(roi, template)
    assert matched is not None
    corr, coverage, extra = matched
    assert corr >= 0.99 and coverage >= discocr.TEMPLATE_COVERAGE_MIN  # 前两项都过
    assert extra > discocr.TEMPLATE_EXTRA_MAX  # 只靠框外墨迹拦住

    assets = stub_assets(
        entry("wh-name-full", "暴击伤害", template, profile_id=_WAREHOUSE),
        profile_id=_WAREHOUSE,
    )
    field = discocr._Field("substat-1-name", "substat_name", rect, 1)
    assert discocr._template_text(assets, assets.profiles[0], field, roi) is None


# ---------------------------------------------------------------------------
# O5 等级行语法（快路径专用解析）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("等级03/15", (3, "S")),
        ("等级0/12", (0, "A")),
        ("等级9/9", (9, "B")),
        ("等级5", (5, None)),
        ("等级3/7", (3, None)),
    ],
    ids=["cap15-S", "cap12-A", "cap9-B", "no-cap-5", "unknown-cap-7"],
)
def test_o5_detail_level_text_grammar(text, expected) -> None:
    assert discocr._parse_level_text(text, "detail") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0", (0, None)), ("6", (6, None)), ("9", (9, None)), ("15", (15, None))],
    ids=["lvl0", "lvl6", "lvl9", "lvl15"],
)
def test_o5_upgrade_level_text_accepts_bare_digits(text, expected) -> None:
    assert discocr._parse_level_text(text, "upgrade") == expected


def test_o5_upgrade_level_never_reads_experience_or_cap() -> None:
    """强化页窄格只可能是等级数字：经验票与越界数字都不能当等级。"""
    for text in ("0/1440", "320/2400", "320", "16", "100"):
        assert discocr._parse_level_text(text, "upgrade") is None, text
    # 详情布局必须带「等级」字样，光秃数字不算
    assert discocr._parse_level_text("0", "detail") is None


# ---------------------------------------------------------------------------
# O6 强化页：真实等级优先，经验文本不得冒充等级
# ---------------------------------------------------------------------------


def test_o6_upgrade_fast_path_reads_real_level_not_experience() -> None:
    image = load(UPGRADE_KEPT)
    result = discocr.try_read_disc(image, layout="upgrade")
    assert result.profile_id == _UPGRADE and result.reject_reason is None
    assert result.disc is not None
    assert result.disc.level == 0
    assert result.disc.rarity is None
    # 强化页接口不读 R/T，固定输出 False（§3.1）
    assert (result.disc.locked, result.disc.discarded) == (False, False)


@pytest.mark.parametrize(
    ("fixture", "level"),
    [("upgrade-level-greenfilled.png", 6), ("upgrade-level-double-digit.png", 9)],
    ids=["greenfilled-6", "double-digit-9"],
)
def test_o6_upgrade_real_levels(fixture, level) -> None:
    """绿填充帧（经验条盖住数字）与双位数帧都必须读出真实等级，且与 legacy 一致。"""
    image = load(FIXTURES / fixture)
    disc = parse_disc_upgrade_page(image)
    assert disc is not None and disc.level == level
    assert serialized(disc) == serialized(_parse_disc_upgrade_page_legacy(image))


# ---------------------------------------------------------------------------
# O7 满级展示页：强化接口拒绝并保留 None，详情回退读出 15
# ---------------------------------------------------------------------------


def test_o7_maxed_upgrade_rejects_then_detail_reads_15() -> None:
    image = load(FIXTURES / "upgrade-maxed.png")
    result = discocr.try_read_disc(image, layout="upgrade")
    assert result.disc is None and result.profile_id is None
    assert result.reject_reason == discocr.UNMATCHED_LAYOUT
    assert parse_disc_upgrade_page(image) is None  # 旧结果保持 None
    detail = parse_disc(image)
    assert detail is not None and detail.level == 15


# ---------------------------------------------------------------------------
# O8 R/T：共用整行，独立判读；只有「解除」而无提示锚点时两者都未知
# ---------------------------------------------------------------------------


def test_o8_fast_action_state_reads_both_states() -> None:
    image = load(FIXTURES / "warehouse-detail.png")
    assert discocr.read_action_state_fast(image) == {"locked": False, "discarded": False}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("R解除弃置T锁定", {"locked": False, "discarded": True}),
        ("R弃置T解除锁定", {"locked": True, "discarded": False}),
        ("R解除弃置T解除锁定", {"locked": True, "discarded": True}),
        ("R弃置T锁定", {"locked": False, "discarded": False}),
        ("解除", {"locked": None, "discarded": None}),
    ],
    ids=["discard-only", "lock-only", "both", "neither", "orphan-word"],
)
def test_o8_fast_action_state_parsing(monkeypatch, text, expected) -> None:
    from zzz_ddtool import ocr

    monkeypatch.setattr(
        ocr,
        "_get_engine",
        lambda: SimpleNamespace(
            text_rec=lambda _args: SimpleNamespace(txts=[text], scores=[0.99])
        ),
    )
    image = QImage(1920, 1080, QImage.Format.Format_RGB888)
    assert discocr.read_action_state_fast(image) == expected


# ---------------------------------------------------------------------------
# O9 拒绝分支：整帧拒绝而不是接受部分结果；非法 FastDiscRead 组合抛 ValueError
# ---------------------------------------------------------------------------


def _steer_modal(monkeypatch, overrides, *, empty_keys=()) -> None:
    """把 o2（三词盘）逐字段构造成确定性输入。

    * ``_band_gray`` 对 modal 的每个 ROI 直接返回「有字」（255）或「空槽」（0），
      让模板命中与像素空槽判据都变得可控；
    * ``overrides`` 按 rect 指定识别文字（None = 没认出）。
    """
    rects = {
        "level": (1180, 190, 1310, 250),
        "s1-name": (1166, 393, 1300, 444),
        "s1-value": (1350, 393, 1475, 444),
        "s2-name": (1488, 393, 1620, 444),
        "s2-value": (1690, 393, 1790, 444),
        "s3-name": (1166, 444, 1300, 495),
        "s3-value": (1350, 444, 1475, 495),
        "s4-name": (1488, 444, 1620, 495),
        "s4-value": (1690, 444, 1790, 495),
    }
    empty = {rects[key] for key in empty_keys}
    real_band = discocr._band_gray

    def band(image, rect):
        key = tuple(rect)
        if key in empty:
            return np.zeros((4, 4), np.float32)
        if key in set(rects.values()):
            return np.full((4, 4), 255.0, np.float32)
        return real_band(image, rect)

    texts = {
        rects["level"]: discocr._Text("等级00", 1.0),
        rects["s1-name"]: discocr._Text("防御力", 1.0),
        rects["s1-value"]: discocr._Text("15", 1.0),
        rects["s2-name"]: discocr._Text("攻击力", 1.0),
        rects["s2-value"]: discocr._Text("3%", 1.0),
        rects["s3-name"]: discocr._Text("暴击率", 1.0),
        rects["s3-value"]: discocr._Text("2.4%", 1.0),
        rects["s4-name"]: discocr._Text("暴击伤害", 1.0),
        rects["s4-value"]: discocr._Text("4.8%", 1.0),
    }
    texts.update({tuple(rect): value for rect, value in overrides.items()})
    monkeypatch.setattr(discocr, "_band_gray", band)
    patch_recognition(monkeypatch, texts)


def test_o9_fourth_slot_with_ink_but_failed_ocr_rejects_whole_frame(monkeypatch) -> None:
    """第四槽有字却漏读 → 拒绝整帧，不能当成三词盘接受。"""
    _steer_modal(monkeypatch, {(1488, 444, 1620, 495): None, (1690, 444, 1790, 495): None})
    result = discocr.try_read_disc(load(MODAL), layout="detail")
    assert result.disc is None
    assert result.reject_reason == discocr.INCOMPLETE_FIELDS
    assert result.profile_id == _MODAL


def test_o9_missing_front_slot_rejects(monkeypatch) -> None:
    """第 3 槽（有字但整槽漏读）属前部缺槽 → 拒绝，绝不接受部分结果。"""
    _steer_modal(
        monkeypatch,
        {(1166, 444, 1300, 495): None, (1350, 444, 1475, 495): None},
        empty_keys=("s3-name", "s3-value"),
    )
    result = discocr.try_read_disc(load(MODAL), layout="detail")
    assert result.disc is None
    assert result.reject_reason == discocr.INCOMPLETE_FIELDS
    assert result.profile_id == _MODAL


def test_o9_low_confidence_field_rejects(monkeypatch) -> None:
    _steer_modal(monkeypatch, {(1166, 393, 1300, 444): discocr._Text("防御力", 0.50)})
    result = discocr.try_read_disc(load(MODAL), layout="detail")
    assert result.reject_reason == discocr.LOW_CONFIDENCE
    assert result.profile_id == _MODAL


def test_o9_unparsable_name_rejects(monkeypatch) -> None:
    _steer_modal(monkeypatch, {(1166, 393, 1300, 444): discocr._Text("完全不是词条", 0.99)})
    result = discocr.try_read_disc(load(MODAL), layout="detail")
    assert result.reject_reason == discocr.INVALID_DATA
    assert result.profile_id == _MODAL


def test_o9_unparsable_name_wins_over_later_missing_value(monkeypatch) -> None:
    """字段顺序：「名称完整但不在词典」是名称字段的解析失败，先于靠后数值字段的缺失。"""
    _steer_modal(
        monkeypatch,
        {
            (1166, 393, 1300, 444): discocr._Text("完全不是词条", 0.99),
            (1350, 393, 1475, 444): None,
        },
    )
    result = discocr.try_read_disc(load(MODAL), layout="detail")
    assert result.reject_reason == discocr.INVALID_DATA
    assert result.profile_id == _MODAL


def test_o9_non_numeric_value_rejects(monkeypatch) -> None:
    _steer_modal(monkeypatch, {(1350, 393, 1475, 444): discocr._Text("内暴", 0.99)})
    result = discocr.try_read_disc(load(MODAL), layout="detail")
    assert result.reject_reason == discocr.INVALID_DATA
    assert result.profile_id == _MODAL


def test_o9_template_gap_too_small_is_rejected_then_ocr_takes_over() -> None:
    """两个同样接近的候选（分差 0 < match_min_gap）不能采用模板，改走 ROI 识别。"""
    image = load(FIXTURES / "warehouse-detail.png")
    rect = (1418, 564, 1560, 615)  # 实拍文字「暴击伤害」
    gray = discocr._band_gray(image, rect)
    template = tight_template(gray)
    assets = stub_assets(
        entry("dup-a", "暴击伤害", template, profile_id=_WAREHOUSE),
        entry("dup-b", "暴击伤害", template, profile_id=_WAREHOUSE),
        profile_id=_WAREHOUSE,
    )
    profile = assets.profiles[0]
    field = discocr._Field("substat-1-name", "substat_name", rect, 1)
    assert discocr._template_text(assets, profile, field, gray) is None
    # 识别仍能给出文字，说明是「先 OCR」而不是整帧拒绝
    recognized = discocr._recognize_bands(image, [rect])[0]
    assert recognized is not None and recognized.text == "暴击伤害"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"disc": Disc(0, (Substat("攻击力"),)), "profile_id": _MODAL, "reject_reason": "invalid_data"},
        {"disc": None, "profile_id": None, "reject_reason": None},
        {"disc": None, "profile_id": _MODAL, "reject_reason": "unmatched_layout"},
        {"disc": None, "profile_id": None, "reject_reason": "incomplete_fields"},
        {"disc": None, "profile_id": None, "reject_reason": "没这个原因"},
    ],
    ids=["disc-and-reason", "no-disc-no-reason", "id-on-pre-layout", "reason-without-id", "unknown-reason"],
)
def test_o9_fast_disc_read_rejects_invalid_combinations(kwargs) -> None:
    with pytest.raises(ValueError):
        discocr.FastDiscRead(**kwargs)


def test_o9_fast_disc_read_accepts_legal_combinations() -> None:
    disc = Disc(0, (Substat("攻击力"),))
    assert discocr.FastDiscRead(disc, _MODAL, None).disc is disc
    for reason in ("disabled", "unsupported_size", "invalid_assets", "unmatched_layout"):
        assert discocr.FastDiscRead(None, None, reason).reject_reason == reason
    for reason in ("incomplete_fields", "low_confidence", "invalid_data"):
        assert discocr.FastDiscRead(None, _WAREHOUSE, reason).profile_id == _WAREHOUSE


# ---------------------------------------------------------------------------
# O10 尺寸 / 资产 / 布局 / 模式隔离 / 内容身份
# ---------------------------------------------------------------------------


def test_o10_wrong_client_size_is_rejected() -> None:
    small = QImage(1600, 900, QImage.Format.Format_RGB888)
    result = discocr.try_read_disc(small, layout="detail")
    assert (result.disc, result.profile_id) == (None, None)
    assert result.reject_reason == discocr.UNSUPPORTED_SIZE


def test_o10_unknown_layout_argument_raises() -> None:
    image = load(FIXTURES / "warehouse-detail.png")
    for bad in ("warehouse", "DETAIL", "", "detail "):
        with pytest.raises(ValueError):
            discocr.try_read_disc(image, layout=bad)


def test_o10_frame_of_other_screen_is_unmatched() -> None:
    """布局已给但画面匹配不到（或匹配多套）→ unmatched_layout，profile_id 为 None。"""
    dark = load(FIXTURES / "upgrade-modal.png")
    detail = discocr.try_read_disc(dark, layout="detail")
    upgrade = discocr.try_read_disc(dark, layout="upgrade")
    assert detail.reject_reason == discocr.UNMATCHED_LAYOUT and detail.profile_id is None
    # upgrade-modal 本身是强化页（legacy 也这么读），这里只断言「不唯一匹配 detail」
    assert upgrade.profile_id in (_UPGRADE, None)


def test_o10_disabled_mode_falls_back_to_legacy_on_same_frame() -> None:
    image = load(FIXTURES / "warehouse-detail.png")
    with discocr._fast_read_override(enabled=False):
        result = discocr.try_read_disc(image, layout="detail")
        assert (result.disc, result.profile_id) == (None, None)
        assert result.reject_reason == discocr.DISABLED
        # 公开入口仍在同帧调用 legacy，拿到完整旧结果
        assert serialized(parse_disc(image)) == serialized(_parse_disc_legacy(image))
    # 退出开发上下文后恢复正常
    restored = discocr.try_read_disc(image, layout="detail")
    assert restored.profile_id == _WAREHOUSE and restored.disc is not None


def _copy_assets(destination: Path) -> Path:
    shutil.copytree(assets_dir() / "discocr", destination)
    return destination


def test_o10_broken_json_is_invalid_assets_and_isolated(tmp_path) -> None:
    copy = _copy_assets(tmp_path / "broken-json")
    assert discocr._load_assets(copy).identity  # 正式副本本身有效
    (copy / "profiles.json").write_bytes(b"{")
    image = load(FIXTURES / "warehouse-detail.png")
    with discocr._fast_read_override(asset_root=copy):
        result = discocr.try_read_disc(image, layout="detail")
        assert (result.disc, result.profile_id) == (None, None)
        assert result.reject_reason == discocr.INVALID_ASSETS
        # 损坏的 JSON 拒绝回到同一引擎的 legacy，不额外截图/输入
        assert serialized(parse_disc(image)) == serialized(_parse_disc_legacy(image))
    # 正式资产/缓存不受影响，退出开发上下文即恢复
    restored = discocr.try_read_disc(image, layout="detail")
    assert restored.profile_id == _WAREHOUSE and restored.disc is not None
    assert discocr._current_assets()[0] is not None


def test_o10_missing_png_is_invalid_assets(tmp_path) -> None:
    copy = _copy_assets(tmp_path / "missing-png")
    (copy / "templates" / "wh-name-baojilv.png").unlink()
    image = load(FIXTURES / "warehouse-detail.png")
    with discocr._fast_read_override(asset_root=copy):
        result = discocr.try_read_disc(image, layout="detail")
        assert result.reject_reason == discocr.INVALID_ASSETS


def test_o10_manifest_only_change_changes_content_identity(tmp_path) -> None:
    """PNG 一个字节没动，只改 templates.json 的文字 → 内容身份必须变化，且新会话重新核对。"""
    import json

    image = load(FIXTURES / "warehouse-detail.png")
    copy = _copy_assets(tmp_path / "manifest-change")
    before = discocr._load_assets(copy).identity

    document = json.loads((copy / "templates.json").read_text(encoding="utf-8"))
    document["entries"][0]["text"] = "生命值"
    (copy / "templates.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    after = discocr._load_assets(copy).identity
    assert before != after

    # 先让正式会话落地，再进入 override：必须重新核对，不沿用正式会话的旧快照
    assert discocr.try_read_disc(image, layout="detail").profile_id == _WAREHOUSE
    with discocr._fast_read_override(asset_root=copy):
        assert discocr._current_assets()[0].identity == after
    # 退出后回到正式资产（正式树与未改动的副本同内容，身份相等）
    assert discocr._current_assets()[0].identity == before


# ---------------------------------------------------------------------------
# O13 强化页普通样本：normal 必须实际采用 upgrade 快路径
# ---------------------------------------------------------------------------


def test_o13_upgrade_fast_path_uses_upgrade_profile() -> None:
    image = load(UPGRADE_KEPT)
    result = discocr.try_read_disc(image, layout="upgrade")
    assert result.profile_id == _UPGRADE and result.reject_reason is None
    disc = result.disc
    assert disc is not None
    assert disc.level == 0
    assert disc.substats == (
        Substat("异常精通", 1),
        Substat("生命值%", 1),
        Substat("暴击伤害", 1),
        Substat("穿透值", 1),
    )
    # 不沿用侧栏 rarity/标签：强化接口读不到上限，R/T 固定 False
    assert (disc.rarity, disc.locked, disc.discarded) == (None, False, False)
    assert serialized(parse_disc_upgrade_page(image)) == serialized(
        _parse_disc_upgrade_page_legacy(image)
    )


# ---------------------------------------------------------------------------
# C2 同帧批量识别：按 ROI ID 还原顺序，引擎锁串行，不跨帧凑批
# ---------------------------------------------------------------------------


def uniform_image(rects) -> QImage:
    """每个 rect 填一个互不相同的灰度；背景 0。"""
    array = np.zeros((1080, 1920, 3), np.uint8)
    for index, (left, top, right, bottom) in enumerate(rects):
        array[top:bottom, left:right] = 30 + index * 20
    image = QImage(
        array.tobytes(), 1920, 1080, 1920 * 3, QImage.Format.Format_RGB888
    )
    return image.copy()  # 拷出，脱离临时 buffer


def test_c2_batched_rois_keep_input_order_and_hold_engine_lock(monkeypatch) -> None:
    rects: list[tuple[int, int, int, int]] = []
    cursor = 50
    for width in (60, 90, 120, 150, 200, 240, 40, 70, 110):  # 9 个宽度各不同、互不重叠
        rects.append((cursor, 100, cursor + width, 140))
        cursor += width + 10
    image = uniform_image(rects)
    locks_held: list[bool] = []

    class FakeEngine:
        def text_rec(self, args):
            locks_held.append(discocr._WORD_BOX_LOCK.locked())
            crops = list(args.img)
            return SimpleNamespace(
                txts=[str(int(crop.mean())) for crop in crops],
                scores=[1.0] * len(crops),
            )

    monkeypatch.setattr(discocr, "_get_engine", lambda: FakeEngine())
    output = discocr._recognize_bands(image, rects)
    # 名称/数值不串行：每个 ROI 拿回自己那块灰度（顺序 = 输入 ROI 顺序）
    assert [int(item.text) for item in output] == [30 + i * 20 for i in range(9)]
    assert locks_held == [True]
    assert not discocr._WORD_BOX_LOCK.locked()  # 用完就还

    # 另一帧是另一次识别调用，不与上一帧凑批
    other_rects = rects[:3]
    calls = len(locks_held)
    discocr._recognize_bands(uniform_image(other_rects), other_rects)
    assert len(locks_held) == calls + 1


def test_c2_engine_lock_serializes_two_frames(monkeypatch) -> None:
    """识别进行中，别处拿不到引擎锁；识别结束立刻可拿。"""
    gate = threading.Event()
    entered = threading.Event()

    class SlowEngine:
        def text_rec(self, args):
            entered.set()
            assert gate.wait(10.0)
            crops = list(args.img)
            return SimpleNamespace(txts=["x"] * len(crops), scores=[1.0] * len(crops))

    monkeypatch.setattr(discocr, "_get_engine", lambda: SlowEngine())
    rects = [(100, 100, 200, 140)]
    result: list[object] = []
    worker = threading.Thread(
        target=lambda: result.append(discocr._recognize_bands(uniform_image(rects), rects))
    )
    worker.start()
    try:
        assert entered.wait(10.0)
        grabbed = discocr._WORD_BOX_LOCK.acquire(timeout=0.2)
        if grabbed:  # 释放掉，避免污染其他用例
            discocr._WORD_BOX_LOCK.release()
        assert not grabbed
    finally:
        gate.set()
        worker.join(10.0)
    assert not worker.is_alive()
    assert len(result) == 1
    assert discocr._WORD_BOX_LOCK.acquire(timeout=1.0)
    discocr._WORD_BOX_LOCK.release()


# ---------------------------------------------------------------------------
# 识别网络异常原样传播（§3.2 / §6.1.1）
# ---------------------------------------------------------------------------


def test_engine_exception_propagates_instead_of_becoming_reject_reason(monkeypatch) -> None:
    """识别网络异常按 §6.1.1 原样传播；只有「字段批量识别」那一批会炸。"""
    modal_level = (1180, 190, 1310, 250)  # 布局判别阶段的另一个候选 ROI

    def boom(_image, rects, **_kwargs):
        if len(rects) == 1 and tuple(rects[0]) == modal_level:
            return [None]
        raise OcrError("识别网络挂了")

    monkeypatch.setattr(discocr, "_recognize_bands", boom)
    image = load(FIXTURES / "warehouse-detail.png")
    with pytest.raises(OcrError):
        discocr.try_read_disc(image, layout="detail")
