from __future__ import annotations

import pytest

from PySide6.QtGui import QImage

from zzz_ddtool.gamedata import SUBSTAT_NAMES
from zzz_ddtool.models import Substat
from zzz_ddtool.ocr import (
    _SUBSTAT_OCR_FIXES,
    _best_match,
    _fix_percent,
    _parse_substat,
    _parse_value,
    parse_disc,
    parse_disc_upgrade_page,
    read_action_state,
    read_level,
    read_level_and_rarity,
    read_rect,
)

SAMPLE = "手册/样张/详情页.png"


@pytest.fixture
def sample_image() -> QImage:
    return QImage(SAMPLE)


def test_fix_percent() -> None:
    assert _fix_percent("%6") == "6%"
    assert _fix_percent("6%") == "6%"
    assert _fix_percent("18") == "18"


def test_parse_value() -> None:
    assert _parse_value("6%") == (6.0, True)
    assert _parse_value("9.6%") == (9.6, True)
    assert _parse_value("18") == (18.0, False)
    assert _parse_value("%6") == (6.0, True)


def test_best_match() -> None:
    assert _best_match("昇常精通", ("异常精通", "攻击力"), cutoff=0.5) == "异常精通"
    assert _best_match("foo", ("异常精通",), cutoff=0.9) is None


def test_parse_substat() -> None:
    assert _parse_substat("异常精通+1", "18") == Substat("异常精通", rolls=2)
    assert _parse_substat("攻击力+2", "%6") == Substat("攻击力%", rolls=3)
    assert _parse_substat("防御力+1", "9.6%") == Substat("防御力%", rolls=2)
    assert _parse_substat("昇常精通+1", "18") == Substat("异常精通", rolls=2)
    assert _parse_substat("生命值", "128") == Substat("生命值", rolls=1)


def test_ocr_fixes_are_real_replacements() -> None:
    """兜底表里每条都必须真的「改写」点什么：错字不能等于正确名。

    踩过坑：表里有一条「攻击 → 攻击」，左右同字，replace 之后原样返回，永远不生效
    ——看着像在纠错，实际是死代码。
    """
    noop = [wrong for wrong, right in _SUBSTAT_OCR_FIXES.items() if wrong == right]
    assert noop == [], f"这些条目是自我替换，永远不会生效：{noop}"

    # 逐条确认替换确实改变了含错字的输入
    for wrong, right in _SUBSTAT_OCR_FIXES.items():
        assert f"{wrong}xx".replace(wrong, right) != f"{wrong}xx"


def test_ocr_fixes_only_target_substats() -> None:
    """纠正目标必须能**落到某个副词条上**（是某个副词条名的子串）。

    判据不能写成「目标本身在词典里」：表里「防街 → 防御」「生俞 → 生命」纠正出的
    是**词根**，词典里没有「防御」「生命」，靠 _best_match 的相似度补全成「防御力」
    「生命值」。所以只要目标能落在某个副词条名上就算数。

    这条挡的是曾犯的错：有条「昇常掌控 → 异常掌控」，异常掌控是 6 号位主属性，
    纠正了也匹配不到任何副词条，纯空转。
    """
    bad = [
        right
        for right in _SUBSTAT_OCR_FIXES.values()
        if not any(right in name for name in SUBSTAT_NAMES)
    ]
    assert bad == [], f"这些纠正目标落不到任何副词条上，纠正了也没用：{bad}"


def test_ocr_fixes_resolve_to_the_right_substat() -> None:
    """每条兜底都要真的救得回来：错字经纠正后认得出**正确**的副词条。

    端到端走 _parse_substat，把「错字 → 解析结果」逐条钉死。数值那列用来区分
    是否百分比（决定补不补 %），所以百分号分支也要各测一条。
    """
    expected = {
        ("昇常精通", "18"): "异常精通",
        ("穿透値", "9"): "穿透值",
        ("防街", "18"): "防御力",
        ("防街", "9.6%"): "防御力%",
        ("生俞", "128"): "生命值",
        ("生俞", "6%"): "生命值%",
    }
    for (wrong, value), right in expected.items():
        assert _parse_substat(wrong, value) == Substat(right, rolls=1), (
            f"{wrong!r} + {value!r} 应解析成 {right!r}"
        )

    # 表里的错字必须都被上面覆盖，新增条目时逼着同步补用例
    covered = {wrong for wrong, _value in expected}
    assert covered == set(_SUBSTAT_OCR_FIXES), (
        f"兜底表有的错字没被用例覆盖：{set(_SUBSTAT_OCR_FIXES) - covered}"
    )


def test_read_level(sample_image: QImage) -> None:
    # 详情页等级文字 OCR 有误差（03 可能读成 00），只验证能读到数字
    assert read_level(sample_image) is not None


def test_level_cap_decides_rarity(monkeypatch) -> None:
    """稀有度按等级上限判定：15→S、12→A、9→B；上限缺失或不在档位就是未知。

    仓库页实拍的等级行是「等级03/15」（score 1.0），分母就是上限，所以不用额外
    OCR、也不用猜颜色。读不出上限时返回 None——**不假定 S**（B2 的根因就是恒默认 S）。
    """
    from zzz_ddtool import ocr

    cases = {
        "等级03/15": (3, "S"),
        "等级0/12": (0, "A"),
        "等级9/9": (9, "B"),
        "等级5": (5, None),
        "等级3/7": (3, None),
    }
    for text, expected in cases.items():
        monkeypatch.setattr(
            ocr, "read_rect", lambda *args, t=text: [ocr.OcrLine(t, 1.0, 0, 0, 100, 20)]
        )
        assert read_level_and_rarity(QImage()) == expected, text


def test_upgrade_page_bare_level_number_does_not_crash(monkeypatch) -> None:
    """强化页只读到纯数字（没有「等级」字样）时不能崩。

    曾经这条兜底用的是 `re.fullmatch(r"\\s*(\\d+)\\s*")`——只有一个分组，走到
    `m.group(2)` 直接 IndexError: no such group，全链路在强化页中断。
    """
    from zzz_ddtool import ocr

    for text, expected in {"15": (15, None), "15/15": (15, "S"), "03/12": (3, "A")}.items():
        monkeypatch.setattr(
            ocr, "read_rect", lambda *args, t=text: [ocr.OcrLine(t, 1.0, 0, 0, 100, 20)]
        )
        assert read_level_and_rarity(QImage(), ocr.UPGRADE_LEVEL) == expected, text


def test_upgrade_level_tries_narrow_area_before_full_bar(monkeypatch) -> None:
    """强化页等级先读窄格；窄格命中就不再读整条（省一次识别）。"""
    from zzz_ddtool import ocr

    calls: list[tuple[int, int, int, int]] = []

    def fake(image, rect):
        calls.append(rect)
        if rect == ocr.UPGRADE_LEVEL:
            return [ocr.OcrLine("7", 1.0, 0, 0, 10, 10)]
        return [ocr.OcrLine("0/1440", 1.0, 0, 0, 10, 10), ocr.OcrLine("3", 1.0, 0, 0, 10, 10)]

    monkeypatch.setattr(ocr, "read_rect", fake)
    assert read_level_and_rarity(QImage(), ocr.UPGRADE_LEVEL) == (7, None)
    assert calls == [ocr.UPGRADE_LEVEL]


def test_upgrade_level_falls_back_to_full_bar(monkeypatch) -> None:
    """窄格读 0 行时退回整条等级条，仍要读出等级。

    真机 2026-10-04 全链路第 13 件：升到 6 级后经验条绿填充盖住左侧等级格，
    窄格连读三帧全 0 行 →「未识别出盘面」打断整条任务链。
    """
    from zzz_ddtool import ocr

    def fake(image, rect):
        if rect == ocr.UPGRADE_LEVEL:
            return []
        return [
            ocr.OcrLine("320/2400", 1.0, 1220, 545, 1350, 580),
            ocr.OcrLine("6", 1.0, 985, 546, 1010, 579),
            ocr.OcrLine("6", 1.0, 1553, 544, 1584, 580),
        ]

    monkeypatch.setattr(ocr, "read_rect", fake)
    assert read_level_and_rarity(QImage(), ocr.UPGRADE_LEVEL) == (6, None)


def test_upgrade_full_bar_never_reads_exp_as_level(monkeypatch) -> None:
    """整条等级条里的经验票「0/1440」不能被当成等级 0。

    否则 3 级的盘被读成 0 级 → 该弃置的没弃、该停的继续强化，静默出错。
    """
    from zzz_ddtool import ocr

    def fake(image, rect):
        if rect == ocr.UPGRADE_LEVEL:
            return []
        return [
            ocr.OcrLine("0/1440", 1.0, 1220, 545, 1350, 580),
            ocr.OcrLine("3", 1.0, 984, 543, 1013, 582),
        ]

    monkeypatch.setattr(ocr, "read_rect", fake)
    assert read_level_and_rarity(QImage(), ocr.UPGRADE_LEVEL) == (3, None)


def test_upgrade_full_bar_without_digits_returns_none(monkeypatch) -> None:
    """整条只读到「0/480」这种经验票（没有独立数字）时返回 None，不能硬凑一个 0。"""
    from zzz_ddtool import ocr

    def fake(image, rect):
        if rect == ocr.UPGRADE_LEVEL:
            return []
        return [ocr.OcrLine("0/480", 1.0, 1220, 545, 1350, 580)]

    monkeypatch.setattr(ocr, "read_rect", fake)
    assert read_level_and_rarity(QImage(), ocr.UPGRADE_LEVEL) == (None, None)


def test_upgrade_level_green_fill_replay() -> None:
    """真机实拍：经验条绿填充盖住等级数字那一帧也要读出等级 6。

    2026-10-04 全链路第 13 件失败现场。旧等级格 (940,525,1100,580) 在这张图上
    直接读 0 行（检测不到），是那次 run 中断的直接原因。
    """
    from zzz_ddtool import ocr

    image = QImage("tests/fixtures/upgrade-level-greenfilled.png")
    assert not image.isNull()
    level, _rarity = read_level_and_rarity(image, ocr.UPGRADE_LEVEL)
    assert level == 6
    disc = parse_disc_upgrade_page(image)
    assert disc is not None and disc.level == 6


def test_upgrade_level_digit_bottom_not_clipped() -> None:
    """真机实拍：等级「9」的尾巴不能被裁掉读成「6」。

    旧等级格底边 580 比数字底边（实拍 582）高 2px，把 9 裁成 6——**静默读错**，
    比读不出更危险：会按错误的等级做弃置/强化判断。
    """
    from zzz_ddtool import ocr

    image = QImage("tests/fixtures/upgrade-level-double-digit.png")
    assert not image.isNull()
    level, _rarity = read_level_and_rarity(image, ocr.UPGRADE_LEVEL)
    assert level == 9


def test_read_action_state_uses_words_only_present_when_active(monkeypatch) -> None:
    """锁定/弃置按「解除」「取消」判断，不硬匹配整串。

    实拍仓库页 OCR 会把「T」和后面的词拆成两行（「T」「锁定」各一行），只匹配
    「解除锁定」会漏掉已锁定的盘——这正是 B1 挂着的直接原因。未生效状态里不含
    这两个词，所以放宽不会误判。
    """
    from zzz_ddtool import ocr

    def patch(texts):
        monkeypatch.setattr(
            ocr, "read_rect", lambda *args: [ocr.OcrLine(t, 1.0, 0, 0, 100, 20) for t in texts]
        )

    patch(["R", "弃置", "T", "锁定", "UID: 32810691"])
    assert read_action_state(QImage()) == {"locked": False, "discarded": False}

    patch(["R", "取消弃置", "T", "解除锁定"])
    assert read_action_state(QImage()) == {"locked": True, "discarded": True}

    # 四个字被 OCR 拆开也要认出来
    patch(["T", "解除", "R", "取消"])
    assert read_action_state(QImage()) == {"locked": True, "discarded": True}


def test_warehouse_action_row_replay() -> None:
    """仓库页真图（未锁定、未弃置）重放：不能被判成已锁定/已弃置。"""
    image = QImage("tests/fixtures/warehouse-detail.png")
    assert not image.isNull()
    assert read_action_state(image) == {"locked": False, "discarded": False}


def test_parse_disc(sample_image: QImage) -> None:
    disc = parse_disc(sample_image)
    assert disc is not None
    names = [sub.name for sub in disc.substats]
    assert "防御力" in names
    assert "攻击力%" in names
    assert "暴击率" in names


def test_warehouse_sidebar_regression() -> None:
    # 用户 2026-10-01 的失败现场；右侧单列面板不能套用放大页的两列区域。
    image = QImage("tests/fixtures/warehouse-detail.png")
    assert not image.isNull()
    disc = parse_disc(image)
    assert disc is not None
    assert disc.level == 3
    # 该图等级行是「等级03/15」，上限 15 → S；不再靠 models 里的默认值蒙。
    assert disc.rarity == "S"
    assert disc.substats == tuple(Substat(name) for name in ("暴击伤害", "暴击率", "攻击力", "异常精通"))


def test_upgrade_page_max_level_regression() -> None:
    """满级后强化界面切成展示态：强化页坐标读空，详情页坐标仍能读出 +15。

    2026-10-02 真机第 5 轮失败现场：12→15 满级确认后，强化页 UPGRADE_LEVEL 区域
    无文字（等级挪到右上角），parse_disc_upgrade_page 返回 None 直接打断任务链。
    """
    image = QImage("tests/fixtures/upgrade-maxed.png")
    assert not image.isNull()
    assert parse_disc_upgrade_page(image) is None  # 强化页坐标随布局变化已失效
    disc = parse_disc(image)  # 回退详情页坐标
    assert disc is not None
    assert disc.level == 15
    assert [sub.name for sub in disc.substats] == ["攻击力", "攻击力%", "暴击伤害", "异常精通"]


def test_zoom_level_area_regression() -> None:
    """缩放页「等级00」识别区必须只套住等级那一行。

    2026-10-02 真机第 6 轮 R5C5 现场：旧区 (1100,150,1550,300) 把「等级00/15」整行
    读成「00碳美」，_enter_upgrade_page 找不到「等级」而崩链。收紧后同一张图读出「等级00」。
    """
    from zzz_ddtool.runner import Runner

    image = QImage("tests/fixtures/zoom-preview-level00.png")
    assert not image.isNull()
    texts = [line.text.replace(" ", "") for line in read_rect(image, Runner.ZOOM_LEVEL_AREA)]
    assert any("等级" in text for text in texts), texts
    # 2026-10-02：原先这里还有一条反面对照——断言旧区 (1100,150,1550,300) 读不出「等级」，
    # 用「旧区确实坏」来反证收紧有效。c 优化把 Det.limit_side_len 由 736 降到 160 后，
    # 旧区在同一张图上也能读出「等级00」（实得 ['15','等级00','主属性']），对照前提不再成立，故删除。
    # 本回归的有效断言就是上面那条：收紧后的 ZOOM_LEVEL_AREA 能稳定读到「等级」。


def test_level_rejects_unrelated_numbers(monkeypatch) -> None:
    from zzz_ddtool import ocr

    monkeypatch.setattr(ocr, "read_rect", lambda *args: [ocr.OcrLine("15%暴击伤害", 1, 0, 0, 100, 20)])
    assert read_level(QImage()) is None
