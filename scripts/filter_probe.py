"""实机探针：进仓库驱动盘页后运行，拍「套装选择器」弹层样张。

用法（游戏停在仓库驱动盘页）：
    py -3.11 scripts/filter_probe.py [输出名]

步骤：点漏斗 -> 点「点击选择」-> 等 0.8s 截图保存 -> Esc Esc 收场。
只截图不勾选，用来标定弹层布局。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zzz_ddtool.capture import grab_game_image
from zzz_ddtool.gamefilter import FILTER_SETS_BAR, _find_and_click
from zzz_ddtool.layout import FILTER_BTN, scale_point
from zzz_ddtool.wininput import VK_ESCAPE, click_client, press_key


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "setpicker-live"
    image, _method, hwnd = grab_game_image()
    width, height = image.width(), image.height()
    out = Path("log/captures")
    out.mkdir(parents=True, exist_ok=True)

    # 不在驱动盘页就先导航过去
    from zzz_ddtool.screen import classify_image
    page = classify_image(image)
    if page != "drive_disc":
        print(f"当前页 {page}，先导航到仓库驱动盘页…")
        from zzz_ddtool.nav import goto_warehouse
        goto_warehouse()
        image, _method, hwnd = grab_game_image()
        page = classify_image(image)
        if page != "drive_disc":
            print(f"导航后仍在 {page}，中断不点")
            return 1

    fx, fy = scale_point(*FILTER_BTN, width, height)
    click_client(hwnd, fx, fy)
    time.sleep(0.6)

    if not _find_and_click(hwnd, "点击选择", FILTER_SETS_BAR, width=width, height=height):
        print("没找到「点击选择」条，先截一张当前面板")
    time.sleep(0.8)
    image, _method, hwnd = grab_game_image()
    path = out / f"{name}.png"
    image.save(str(path))
    print(f"saved {path}")

    press_key(hwnd, VK_ESCAPE)
    press_key(hwnd, VK_ESCAPE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
