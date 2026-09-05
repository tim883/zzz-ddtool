# zzz-ddtool

绝区零驱动盘自动强化辅助：本地截屏 + OCR + 模拟点击。

当前可跑的是**独立窗口**（权重、门槛、组合勾选、试判）。OCR 和点击还没做。本机请用 Python 3.11+（`py -3.11`）。

开发说明在工作区 `dev-docs/`，不随本仓库给用户。

## 打开窗口

```powershell
cd zzz-ddtool
py -3.11 -m pip install -e ".[dev]"
py -3.11 -m zzz_ddtool
```

也可以双击 `打开窗口.bat`。默认加载 `configs/example.yaml`。

窗口里：左边改词条单价和硬过滤，右边按 3 词 / 4 词看 4 槽卡片，改「本档要求权重」会即时藏卡，取消勾选不要的组合。第三页「试判」可以手填一件盘看会升还是打标记。

## 两轮配置

1. 给有效词条写权重（没写 = 废词条）。
2. 在窗口里取消勾选不要的组合。
3. 运行时只认勾选结果。

命令行仍可用：

```powershell
py -3.11 -m pytest
py -3.11 -m zzz_ddtool decide --config configs/example.yaml --subs 暴击率,暴击伤害,生命值,防御力 --level 0
```

动作：`upgrade`（升 1 阶段）、`mark_discard`（只打弃置标记）、`skip`（锁定/已装备）、`stop`（3/4 词来源对不上则停机）。
