# zzz-ddtool

绝区零驱动盘自动强化辅助：本地截屏 + OCR + 模拟点击。

游戏里循环处理每一件盘：读等级和副词条 → 对照你自己的预设判断 → 继续强化，或打弃置标记。
开始每个任务时，自动根据右侧 0 级组合设置游戏副词条筛选，减少需要读取的盘子；所有等级一起处理，筛出的盘仍按各档规则精确判断。
**不分解**、不读内存、不改游戏文件、不抓包。全程本机可离线，热键随时停。

## 文档目录

| 入口 | 内容 |
|------|------|
| [任务管理器使用说明](手册/任务管理器使用说明.md) | **客户端使用说明**：从配置任务到保存、执行，以及卡片预览与任务记录的关系 |
| [doc/README.md](doc/README.md) | **技术手册**：识别标定、截图与坐标、日志与截图（开发/排查先看这里） |
| [手册/README.md](手册/README.md) | **用户手册**：页面识别、运行限制与筛选 |

## 装与跑（开发用）

开发与打包统一 **Python 3.13.5**，使用项目内 `.venv`；系统缩放 100%、游戏窗口化 16:9（坐标按 1920×1080 标定）。

```powershell
cd zzz-ddtool
uv venv --python 3.13.5 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-lock.txt "pytest==9.1.1" -e .
.venv\Scripts\python.exe -m zzz_ddtool   # 开源码窗口，也可用开发用「打开窗口.bat」
```

依赖：`PySide6-Essentials`（窗口）、`opencv-python` + `numpy` + `rapidocr`（OCR）、`pyyaml`（配置）。

> **坑**：包名是 `rapidocr`，不是 `rapidocr-onnxruntime`。
> 后者是旧版，装了会报 `No module named 'rapidocr'`。
>
> 打包成 exe 的方案（给别人用、检查更新）见工作区
> `dev-docs/打包与更新改造手册.md`。

## 窗口里能做什么

- **左侧**：词条单价（写了权重的才是有用词条）、硬过滤、本次最多处理几个盘、游戏内筛选（套装 + 部位/主词条）
- **单价、额度、套装**三者各有一个「应用于所有任务」，一次铺给任务链里已有的任务；之后新建的任务仍用自己的默认值
- **右侧**：按「初始 3 词 / 4 词」×「+0 +3 +6 +9 +12 +15」分组的 4 槽卡片，改「本档要求权重」即时藏卡，取消勾选不要的组合
- **试判**：手填一件盘，直接看会升还是打标记
- **任务链**：按部位（1–6 号）排多个任务，每个部位一套自己的权重和勾选

产出都在 `log/`，文本和图片两个明确路径：文本 `log/text/ddtool.log`，
图片 `log/captures/<时间戳>/`。每一步都存一张图，命名 `0007-进强化页.png`，
前缀就是日志里的步骤号。

容量**纯按数量**（默认 300 张），超了从最旧的轮次整轮删。
不做「失败永远保留」——排错靠开详细级别重跑复现，不靠翻旧图。
窗口顶部「日志设置」里能改级别和上限。见 [doc/日志与截图.md](doc/日志与截图.md)。

## 命令行

任务存档统一放在根目录的 `user/plan/`，`example` 和其他存档没有区别。
首次打开客户端会导入旧 `data/` 中的 YAML；后续版本共用这些存档，发布包不再包含 YAML。

```powershell
.venv\Scripts\python.exe -m zzz_ddtool capture          # 截一张游戏窗口
.venv\Scripts\python.exe -m zzz_ddtool goto-warehouse   # 导航到仓库页
.venv\Scripts\python.exe -m zzz_ddtool snapshot-pages   # 逐个页面存样张
.venv\Scripts\python.exe -m zzz_ddtool run --config user/plan/example.yaml   # 跑任务链
.venv\Scripts\python.exe -m zzz_ddtool preview --config user/plan/example.yaml  # 导出组合卡片 HTML
.venv\Scripts\python.exe -m zzz_ddtool decide --config user/plan/example.yaml --subs 暴击率,暴击伤害,生命值,防御力 --level 0
```

跑任务时 **F9 暂停 / F10 停止**，检查点在格子级——当前这件处理完才停，不会在弹层中间撂挑子。

## 动作

| 动作 | 含义 |
|------|------|
| `upgrade` | 升 1 阶段（+3），升完在强化页原地重新判断 |
| `mark_discard` | 只打弃置标记，**不分解** |
| `skip` | 锁定 / 已装备 / 已弃置 / 非 S / 满级 |
| `stop` | 3 词 / 4 词来源对不上，停机不猜 |

## 锁定与稀有度（2026-10-04 修，待真机样本定案）

1. **锁定盘**：仓库页右下角是 **R/T 提示文字**（不是图标）——未锁定显示「T 锁定」、已锁定显示
   「T 解除锁定」。判据按「解除」这个**只在已锁定态出现**的词匹配，不硬匹配整串：OCR 会把
   「T」和「锁定」拆成两行（实测如此），只匹配「解除锁定」会漏。弃置同理走「取消」。
2. **稀有度**：由等级行推出——面板显示「等级03 / 15」，**分母就是等级上限**（S=15 / A=12 / B=9）。
   和等级共用同一次识别，不额外加 OCR、也不猜徽章颜色（徽章是纯图标，OCR 读不出）。上限读不出
   时稀有度为「未知」，`require_s_rank` **不据此跳过**——宁可不跳，也不把读不准的盘当非 S 误丢。

两条都还缺真机样本（一张锁定盘、一张 A/B 盘）定案；判据逻辑与「未锁定仓库页真图重放」已过回归。
详情见 [doc/识别标定.md](doc/识别标定.md)。
