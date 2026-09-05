from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path

from zzz_ddtool.origin import Origin
from zzz_ddtool.patterns import CHECKPOINTS, Pattern


def render_cards_html(
    patterns: list[Pattern],
    stage_require: Mapping[str, Mapping[int, float]] | None = None,
    title: str = "驱动盘组合预览",
) -> str:
    grouped: dict[tuple[str, int], list[Pattern]] = defaultdict(list)
    for pattern in patterns:
        grouped[(pattern.origin.value, pattern.checkpoint)].append(pattern)
    require = stage_require or {}

    sections = []
    for origin in Origin:
        origin_label = "初始 3 词" if origin is Origin.THREE else "初始 4 词"
        blocks = []
        for checkpoint in CHECKPOINTS:
            cards = grouped.get((origin.value, checkpoint), [])
            default_req = (require.get(origin.value) or {}).get(checkpoint)
            req_value = "" if default_req is None else str(default_req)
            items = "\n".join(_card(pattern) for pattern in cards)
            blocks.append(
                f"""<section class="stage" data-origin="{origin.value}" data-lv="{checkpoint}">
  <h3>
    <span>+{checkpoint}</span>
    <label>本档要求权重
      <input type="number" step="0.1" min="0" value="{req_value}" class="require">
    </label>
    <span class="shown"></span>
  </h3>
  <div class="grid">{items or "<p class=muted>这一档没有组合（检查词条单价）</p>"}</div>
</section>"""
            )
        sections.append(f"<h2>{origin_label}</h2>\n" + "\n".join(blocks))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <style>
    body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #121212; color: #eee; }}
    h1,h2,h3 {{ font-weight: 600; }}
    h2 {{ margin-top: 48px; border-bottom: 1px solid #333; padding-bottom: 8px; }}
    .meta {{ color: #aaa; margin-bottom: 12px; max-width: 720px; line-height: 1.5; }}
    .stage h3 {{ display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
    .stage h3 input {{ width: 88px; font-size: 16px; padding: 4px 8px; }}
    .grid {{ display: flex; flex-wrap: wrap; gap: 14px; }}
    .card {{ width: 168px; background: #1b1b1b; border: 1px solid #3a3a3a; border-radius: 8px; padding: 10px; cursor: pointer; }}
    .card input {{ margin-bottom: 8px; }}
    .card.off {{ display: none; }}
    .panel {{ background: #101010; border: 1px solid #2c2c2c; border-radius: 6px; overflow: hidden; }}
    .slot {{ padding: 7px 10px; border-bottom: 1px solid #2a2a2a; min-height: 20px; }}
    .slot:last-child {{ border-bottom: none; }}
    .junk {{ color: #777; }}
    .empty {{ color: #666; font-style: italic; }}
    .score {{ font-size: 12px; color: #8ab4f8; margin-top: 8px; }}
    .muted {{ color: #888; }}
    button {{ margin: 12px 8px 12px 0; padding: 8px 14px; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p class="meta">上面「词条单价」用来给每张卡算分。每一档的「本档要求权重」要你自己填（0 / 3 / 6 / 9 / 12 / 15，3 词和 4 词分开）。改数字后，低于门槛的卡会藏起来。</p>
  <p class="meta">剩下的卡再取消勾选。导出后运行时按勾选执行；+15 不符合同样打弃置标记。</p>
  <button type="button" onclick="checkVisible(true)">全选当前可见</button>
  <button type="button" onclick="checkVisible(false)">取消当前可见</button>
  <button type="button" onclick="exportPicks()">导出勾选和门槛</button>
  {"".join(sections)}
  <script>
    function applyFilters() {{
      document.querySelectorAll(".stage").forEach(stage => {{
        const raw = stage.querySelector(".require").value;
        const need = raw === "" ? null : Number(raw);
        let shown = 0, total = 0;
        stage.querySelectorAll(".card").forEach(card => {{
          total += 1;
          const score = Number(card.dataset.score);
          const ok = need === null || Number.isNaN(need) || score >= need;
          card.classList.toggle("off", !ok);
          if (ok) shown += 1;
        }});
        stage.querySelector(".shown").textContent = need === null ? total + " 张" : shown + " / " + total + " 张";
      }});
    }}
    function checkVisible(on) {{
      document.querySelectorAll(".card:not(.off) input[type=checkbox]").forEach(el => el.checked = on);
    }}
    function exportPicks() {{
      const stage_require = {{three: {{}}, four: {{}}}};
      document.querySelectorAll(".stage").forEach(stage => {{
        const origin = stage.dataset.origin;
        const lv = stage.dataset.lv;
        const raw = stage.querySelector(".require").value;
        if (raw !== "") stage_require[origin][lv] = Number(raw);
      }});
      const accepted = [...document.querySelectorAll(".card:not(.off) input[type=checkbox]:checked")].map(el => el.value);
      const blob = new Blob([JSON.stringify({{stage_require, accepted}}, null, 2)], {{type: "application/json"}});
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "pattern-picks.json";
      a.click();
    }}
    document.querySelectorAll(".require").forEach(el => el.addEventListener("input", applyFilters));
    applyFilters();
  </script>
</body>
</html>
"""


def _card(pattern: Pattern) -> str:
    rows = []
    for slot in pattern.slots:
        cls = "slot"
        if slot.name == "废词条":
            cls += " junk"
        elif slot.name == "未出":
            cls += " empty"
        rows.append(f'<div class="{cls}">{slot.label()}</div>')
    return (
        f'<label class="card" data-score="{pattern.score}">'
        f'<input type="checkbox" checked value="{pattern.pattern_id}"/>'
        f'<div class="panel">{"".join(rows)}</div>'
        f'<div class="score">本卡权重 {pattern.score}</div>'
        f"</label>"
    )


def write_cards_html(
    patterns: list[Pattern],
    path: str | Path,
    stage_require: Mapping[str, Mapping[int, float]] | None = None,
    title: str = "驱动盘组合预览",
) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_cards_html(patterns, stage_require=stage_require, title=title), encoding="utf-8")
    return dest
