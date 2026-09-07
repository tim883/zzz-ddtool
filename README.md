# zzz-ddtool

绝区零驱动盘自动强化辅助：本地截屏 + OCR + 模拟点击。

游戏里循环处理每一件盘：读等级和副词条 → 对照你自己的预设判断 → 继续强化，或打弃置标记。
**不分解**、不读内存、不改游戏文件、不抓包。全程本机可离线，热键随时停。

## 文档目录

| 入口 | 内容 |
|------|------|
| [doc/README.md](doc/README.md) | **技术手册**：识别标定、截图与坐标、日志与截图（开发/排查先看这里） |
| [手册/README.md](手册/README.md) | **用户手册**：页面识别、运行限制与筛选 |

## 装与跑（开发用）

需要 **Python 3.11+**，以及系统缩放 100%、游戏窗口化 16:9（坐标按 1920×1080 标定）。

```powershell
cd zzz-ddtool
py -3.13 -m pip install -e ".[dev]"
py -3.13 -m zzz_ddtool        # 开窗口（默认子命令就是 gui）
```

依赖：`PySide6-Essentials`（窗口）、`opencv-python` + `numpy` + `rapidocr`（OCR）、`pyyaml`（配置）。

> **坑**：包名是 `rapidocr`，不是 `rapidocr-onnxruntime`。
> 后者是旧版，装了会报 `No module named 'rapidocr'`。
>
> 打包成 exe 的方案（给别人用、检查更新）见工作区
> `dev-docs/打包与更新改造手册.md`。

## 窗口里能做什么

- **左侧**：词条单价（写了权重的才是有用词条）、硬过滤、本次最多处理几个盘
- **右侧**：按「初始 3 词 / 4 词」×「+0 +3 +6 +9 +12 +15」分组的 4 槽卡片，改「本档要求权重」即时藏卡，取消勾选不要的组合
- **试判**：手填一件盘，直接看会升还是打标记
- **任务链**：按部位（1–6 号）排多个任务，每个部位一套自己的权重和勾选

点「截取游戏」把当前游戏窗口存到本仓库 `out/captures/`，日志在 `out/logs/`。
每一步都存一张图，命名 `0007-进强化页.png`，前缀就是日志里的步骤号。
容量：路径类（导航/筛选/扫描/翻页）全存，盘只留最初 5 个 + 最近 5 个，
失败的一律保留，只留最近 3 轮。见 [doc/日志与截图.md](doc/日志与截图.md)。

## 命令行

```powershell
py -3.13 -m zzz_ddtool capture          # 截一张游戏窗口
py -3.13 -m zzz_ddtool goto-warehouse   # 导航到仓库页
py -3.13 -m zzz_ddtool snapshot-pages   # 逐个页面存样张
py -3.13 -m zzz_ddtool run --config configs/example.yaml   # 跑任务链
py -3.13 -m zzz_ddtool preview --config configs/example.yaml  # 导出组合卡片 HTML
py -3.13 -m zzz_ddtool decide --config configs/example.yaml --subs 暴击率,暴击伤害,生命值,防御力 --level 0
```

跑任务时 **F9 暂停 / F10 停止**，检查点在格子级——当前这件处理完才停，不会在弹层中间撂挑子。

## 动作

| 动作 | 含义 |
|------|------|
| `upgrade` | 升 1 阶段（+3），升完在强化页原地重新判断 |
| `mark_discard` | 只打弃置标记，**不分解** |
| `skip` | 锁定 / 已装备 / 已弃置 / 非 S / 满级 |
| `stop` | 3 词 / 4 词来源对不上，停机不猜 |

## 已知未修

1. **锁定盘不跳过**：详情页 R/T 是图标按钮没有文字，`read_action_state` 读不到「解除锁定」，`disc.locked` 恒为 `False`，`skip_locked` 实际不起作用。
2. **非 S 盘不跳过**：`parse_disc` 不读稀有度，恒默认 `"S"`，`require_s_rank` 实际不起作用。

两处都只影响「少跳几个盘」，不会误强化，但会误点你不想动的盘。详情见 [doc/识别标定.md](doc/识别标定.md)。
