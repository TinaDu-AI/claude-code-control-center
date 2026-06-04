---
name: cc-progress-widget
description: 在 macOS 桌面上放一个 Claude Code 任务进度小卡片（Übersicht，264px 宽，Material 3 浅色）。同时盯三个渠道——终端 / Claude 桌面 App / VS Code——里正在跑的长任务，每行标出来源 + 此刻在干嘛（跑/读/写/改/搜/抓/子任务…）；下面是每个活跃会话的「上下文水位」（按客户端自动识别 200K 还是 1M 窗口，快满了变橙/红），再下面是近 7 天活跃度迷你柱状图，底栏汇总今日忙时长 / 对话轮数 / token 消耗。触发：用户说"装 cc 进度 widget"、"Claude Code 进度桌面卡片"、"看 claude code 在跑啥的 widget"、"cc-progress widget"、"做个 claude 任务进度小卡片"。
version: 1.0.0
---

# CC Progress Widget — Claude Code 任务进度桌面卡片

Claude Code 经常有长任务（跑测试、改一堆文件、开子 agent），执行时用户盯着终端也看不出进度、更看不出同时在别处（Claude App / VS Code）还有没有别的会话在忙。这个 widget 把这些都摊到桌面一张卡上：

- **实时任务行**：三个渠道里此刻在跑的工具，每行 `[来源标签] [在干嘛] [转圈] [已用时]`。没有工具在跑但还在这一轮里时，显示「正在思考」（还没动手）/「正在回复」（动过手在收尾）。
- **上下文水位**：每个最近活跃的会话占了多少上下文窗口。**窗口大小按客户端自动识别**——某个客户端历史上越过过 200K，就判定它跑在 1M 窗口，它的所有会话都按 1M 算（否则 200K）。<60% 绿、60–85% 橙、≥85% 红。每条后面带那个会话的开场白，一眼知道是哪个对话。会话结束（`/exit`、`/clear`、退出、删除）后会自动从卡片撤下（靠 `SessionEnd` 钩子）；手动 `/compact` 后水位即时降到开销地板，不再卡在旧值。
- **近 7 天趋势**：每天对话轮数的迷你柱状图，今天高亮。
- **底栏**：今日全渠道忙时长 · 对话轮数 · 新增 token（≈ 控制台用量）。

数据全部来自本机：实时行读一个 hook 写的事件日志，统计/水位/趋势由 `cc-stats.py` 直接扫 Claude Code 自己的转录文件（`~/.claude/projects/*/*.jsonl`）。**没有任何数据离开你的电脑，也不需要任何 API key。**

## 组成

| 文件 | 作用 |
|------|------|
| `index.jsx` | Übersicht 卡片本体（渲染 + 拖拽 + 1s 刷新） |
| `cc-progress-hook.py` | Claude Code 钩子：把每次工具开始/结束、每轮对话开始/结束写进 `~/.claude/cc-progress.jsonl`，并在长任务完成时发原生通知 |
| `cc-stats.py` | 扫转录算今日总量 + 每会话上下文水位 + 7 天趋势，带两级缓存（整体 15s、7 天历史 1h） |
| `settings.hooks.json` | 要并进 `~/.claude/settings.json` 的 5 个 hook 片段 |

## 前置依赖

1. **Übersicht**（必须，本 skill 不替你装）——免费的 macOS 桌面 widget 宿主：<http://tracesof.net/uebersicht/>。装好并运行一次。
2. **python3**——macOS 自带（`which python3` 应有 `/usr/bin/python3`）。
3. **Claude Code**——本 widget 监控的就是它（终端 / Claude 桌面 App / VS Code 任一即可，三个一起更好）。

## 安装流程（Claude 按顺序执行）

### Step 1 — 确认 Übersicht 在
```bash
ls -d "/Applications/Übersicht.app" 2>/dev/null || ls -d "$HOME/Applications/Übersicht.app" 2>/dev/null
```
没有就停下，告诉用户先去 <http://tracesof.net/uebersicht/> 下载安装并打开，再继续。**不要尝试自动安装。**

### Step 2 — 放两个脚本到 `~/.claude/`
```bash
cp cc-progress-hook.py "$HOME/.claude/cc-progress-hook.py"
cp cc-stats.py        "$HOME/.claude/cc-stats.py"
/usr/bin/python3 -m py_compile "$HOME/.claude/cc-progress-hook.py" "$HOME/.claude/cc-stats.py" && echo OK
```

### Step 3 — 放卡片到 Übersicht widgets 目录
```bash
DEST="$HOME/Library/Application Support/Übersicht/widgets/cc-progress.widget"
mkdir -p "$DEST"
cp index.jsx "$DEST/index.jsx"
# 把命令里的 $HOME 换成真实绝对路径，避免个别 Übersicht 构建不展开 $HOME
HOMEPATH="$(echo $HOME)"
/usr/bin/python3 - "$DEST/index.jsx" "$HOMEPATH" <<'PY'
import sys
p, home = sys.argv[1], sys.argv[2]
s = open(p).read().replace("$HOME/.claude/", home + "/.claude/")
open(p, "w").write(s)
print("patched widget path ->", home)
PY
```

### Step 4 — 把 5 个 hook 并进 `~/.claude/settings.json`
> ⚠️ **合并，不要覆盖**。用户的 `settings.json` 里可能已有别的 hook / 权限 / `env`（甚至密钥）。**绝不整文件覆盖**，只把 `settings.hooks.json` 里 6 个事件（PreToolUse / PostToolUse / SessionStart / SessionEnd / UserPromptSubmit / Stop）追加进去，已存在同名事件就往它的数组里 append 一条。命令里的 `$HOME` 同样换成绝对路径。（`SessionEnd` 用来在会话结束/删除后把它的水位条撤下。）

读 `settings.hooks.json`，读用户现有 `settings.json`，用 python 合并后写回（保留原有所有字段）。合并示例逻辑：
```python
import json, os
home = os.path.expanduser("~")
sp = os.path.join(home, ".claude", "settings.json")
cur = json.load(open(sp)) if os.path.exists(sp) else {}
add = json.load(open("settings.hooks.json"))["hooks"]
cur.setdefault("hooks", {})
for ev, blocks in add.items():
    for b in blocks:
        for h in b.get("hooks", []):
            h["command"] = h["command"].replace("$HOME", home)
    cur["hooks"].setdefault(ev, []).extend(blocks)
json.dump(cur, open(sp, "w"), ensure_ascii=False, indent=2)
```
合并前最好先备份：`cp ~/.claude/settings.json ~/.claude/settings.json.bak`。

### Step 5 — 让它显示
- Übersicht 会自动加载 `widgets/` 下的新 widget（菜单栏 Übersicht 图标 → Refresh All 可强制刷新）。
- hook 改了 `settings.json`，**新开一个 Claude Code 会话**才会挂上（已开的会话读的是启动时的 settings）。新会话里随便跑个长一点的命令，卡片就会亮起来。
- 卡片可以拖：按住卡片头部拖到喜欢的位置，位置记在 `localStorage`。

装完告诉用户：卡片已在桌面，**新开一个 Claude Code 会话**后开始有数据；首次扫历史（建 7 天趋势 + 学习各客户端窗口档位）约 1–2 秒，之后走缓存。

## 卡片显示了什么

| 模块 | 数据源 / 逻辑 |
|------|--------------|
| 实时任务行（来源 + 在干嘛 + 用时） | `~/.claude/cc-progress.jsonl`，hook 在每次 PreToolUse/PostToolUse 写；`short_desc()` 把工具映射成中文动词 |
| 正在思考 / 正在回复 | 这一轮还没调用过工具=思考；调用过、此刻没工具在跑=回复 |
| 上下文水位 + 颜色 | `cc-stats.py` 取每会话最新主链调用的 `input+cache_creation+cache_read`，除以该客户端的窗口（200K / 1M，自动识别） |
| 会话开场白 | 转录里第一条人类 prompt，截断显示，用来区分同渠道的多个会话 |
| 近 7 天柱状图 | 按本地日期统计每天真实人类 prompt 数（不含工具结果/子 agent），今天实时叠加 |
| 底栏 token | 当日全渠道新增 token（输入+输出+缓存写入，按 message id 去重，≈ 控制台用量，不含缓存重复读取） |

## 自定义

全在文件顶部，改完保存即生效（Übersicht 热重载；hook 改动下次工具调用生效）：

- **卡片宽度 / 缩放**：`index.jsx` 顶部 `WIDTH`（默认 264）。
- **多久才算"长任务"才显示**：`index.jsx` 的 `SHOW_AFTER_SEC`（默认 6s）。
- **水位最多显示几个会话**：`index.jsx` 的 `MAX_CTX_ROWS`（默认 8）。卡片**没有固定高度**，会随活跃会话数自动伸缩；这个值只是个安全上限，超了才显示「还有 N 个 session…」。想要紧凑就调小。
- **水位颜色阈值**：`index.jsx` 里 `b.pct >= 85 ? danger : b.pct >= 60 ? warn : ok`。
- **会话挂多久没动就撤下**：`cc-stats.py` 的 `SESS_WINDOW`（默认 4h）。
- **窗口判定基准**：`cc-stats.py` 的 `CTX_WINDOW`（默认 200000，超过即判 1M）。
- **工具 → 中文标签**：`cc-progress-hook.py` 的 `short_desc()`。已内置：跑/读/写/改/搜/找/抓/子任务、飞书(lark-cli)/腾讯会议/anysearch/Notion/微信/Gmail/电脑(computer-use)/浏览器(Chrome) 等中文映射，按自己常用的 MCP / CLI 改即可。
- **来源配色**：`index.jsx` 的 `SRC`（终端深紫 / Claude 品牌紫 / VS Code 浅紫）。
- **长任务完成通知阈值**：`cc-progress-hook.py` 的 `PING_AFTER_SEC`（默认 30s，busy 连续超过才叮）。

## 触发用户调用本 skill 的场景

- 安装/重装：用户说"装这个 widget"、"我也想要这个进度卡片"。
- 排错：卡片不亮（多半是 hook 没并进 settings、或没新开会话）、水位百分比看着不对（窗口档位可在 `cc-history.json` 的 `entry_max` 里看学到了啥）、卡片显示但 token/轮数为 0（多半 `~/.claude/projects` 路径或权限问题，把脚本报错原样给用户）。
- 改样式/阈值/标签：见「自定义」。
- 卸载：删 `~/Library/Application Support/Übersicht/widgets/cc-progress.widget/`；删 `~/.claude/cc-progress-hook.py`、`~/.claude/cc-stats.py`、以及运行时数据 `~/.claude/cc-progress*.json*`、`~/.claude/cc-stats-cache.json`、`~/.claude/cc-history.json`、`~/.claude/cc-ended.json`；从 `~/.claude/settings.json` 的 `hooks` 里拿掉这 6 个 `cc-progress-hook.py` 条目。

## 隐私

- 全程本机，无网络请求，无需 API key。
- `cc-stats.py` 只读 Claude Code 自己写的转录（`~/.claude/projects`）做统计，不上传。
- 运行时数据文件（`cc-progress.jsonl` / `cc-stats-cache.json` / `cc-history.json` / `cc-progress-daily.json` / `cc-ended.json`）含你的会话标题与路径，**已被 `.gitignore` 挡住，不会进仓库**。
