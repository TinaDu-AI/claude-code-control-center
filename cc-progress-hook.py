#!/usr/bin/env python3
"""
Claude Code progress hook -> feeds the Übersicht "cc-progress" desktop widget.

Wired in ~/.claude/settings.json:
  PreToolUse  (Task|Bash)  ->  cc-progress-hook.py start
  PostToolUse (Task|Bash)  ->  cc-progress-hook.py end
  SessionStart             ->  cc-progress-hook.py reset

MULTI-SOURCE: the same ~/.claude/settings.json is shared by every Claude Code
instance — whether it runs in a Terminal, inside the Claude desktop app, or in
VS Code. They all write to ONE log. We stamp each event with:
  - "src": which macOS app launched this Claude Code (terminal/claude/vscode)
  - "sid": the session id
so the widget can tell the three apart, and a new session's reset only clears
its OWN leftovers instead of wiping the other apps' in-flight tasks.

Writes (stdlib + osascript only, nothing installed):
  ~/.claude/cc-progress.jsonl        live event log
  ~/.claude/cc-progress-daily.json   per-day totals (survives sessions)

When a busy stretch (per source) lasting >= PING_AFTER_SEC finishes, it fires a
native macOS notification.
"""
import sys, json, time, os, subprocess

# ---- config (tweak freely) --------------------------------------------------
PING_AFTER_SEC = 30        # only ding when the busy stretch lasted at least this long
STALE_TRIM_SEC = 6 * 3600  # on reset, drop log lines older than this (keeps file small)

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".claude", "cc-progress.jsonl")
DAILY = os.path.join(HOME, ".claude", "cc-progress-daily.json")

SRC_LABEL = {"terminal": "终端", "claude": "Claude", "vscode": "VS Code", "other": "其它"}


def detect_src():
    """Which app launched this Claude Code? -> terminal / claude / vscode / other.
    macOS sets __CFBundleIdentifier to the GUI app at the root of the process tree;
    children (including this hook) inherit it. TERM_PROGRAM is the fallback."""
    bid = (os.environ.get("__CFBundleIdentifier") or "").lower()
    tp = (os.environ.get("TERM_PROGRAM") or "").lower()
    if "claudefordesktop" in bid or "com.anthropic.claude" in bid:
        return "claude"
    if ("vscode" in bid or "vscodium" in bid or "cursor" in bid
            or "todesktop" in bid or tp == "vscode"):
        return "vscode"
    if ("apple.terminal" in bid or "iterm" in bid or "tabby" in bid or "warp" in bid
            or tp in ("apple_terminal", "iterm.app")):
        return "terminal"
    if tp:                       # any other real terminal emulator
        return "terminal"
    return "other"


def read_payload():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def lark_noun_cn(cmd):
    """`lark-cli <noun> ...` -> 飞书 中文名 (noun = which Feishu surface)."""
    toks = cmd.split()
    noun = toks[1] if len(toks) > 1 else ""
    return {"base": "多维表格", "minutes": "妙记", "docs": "文档", "sheets": "表格",
            "wiki": "知识库", "vc": "会议", "auth": "登录", "api": "API"}.get(noun, noun or "操作")


def mcp_action_cn(name):
    """Best-effort Chinese verb for an MCP tool's action part (after the last __)."""
    n = (name or "").lower()
    for k, v in (("batch_search", "批量搜"), ("search", "搜"), ("extract", "抓正文"),
                 ("transcript", "逐字稿"), ("minutes", "纪要"), ("notes", "纪要"),
                 ("list", "列"), ("fetch", "读"), ("read", "读"), ("get", "取"),
                 ("create", "建"), ("update", "改"), ("delete", "删"),
                 ("query", "查"), ("send", "发"), ("reply", "回复"), ("write", "写")):
        if k in n:
            return v
    return (name or "").replace("-", " ").replace("_", " ")


def short_desc(tool, ti):
    try:
        if tool == "Bash":
            cmd = (ti.get("command") or "").strip().replace("\n", " ")
            if not cmd:
                return "跑 命令"
            if cmd.startswith("lark-cli"):
                return ("飞书 " + lark_noun_cn(cmd))[:48]
            return ("跑 " + cmd)[:48]
        if tool in ("Task", "Agent"):
            d = (ti.get("description") or ti.get("subagent_type") or "").strip()
            return ("子任务 " + d)[:48] if d else "派子任务"
        if tool == "Read":
            fn = os.path.basename(ti.get("file_path") or "").strip()
            return ("读 " + fn)[:48] if fn else "读文件"
        if tool == "Write":
            fn = os.path.basename(ti.get("file_path") or "").strip()
            return ("写 " + fn)[:48] if fn else "写文件"
        if tool in ("Edit", "MultiEdit"):
            fn = os.path.basename(ti.get("file_path") or "").strip()
            return ("改 " + fn)[:48] if fn else "改文件"
        if tool == "NotebookEdit":
            fn = os.path.basename(ti.get("notebook_path") or ti.get("file_path") or "").strip()
            return ("改 " + fn)[:48] if fn else "改 notebook"
        if tool == "Grep":
            pat = (ti.get("pattern") or "").strip()
            return ("搜 " + pat)[:48] if pat else "搜代码"
        if tool == "Glob":
            pat = (ti.get("pattern") or "").strip()
            return ("找 " + pat)[:48] if pat else "找文件"
        if tool == "WebSearch":
            q = (ti.get("query") or "").strip()
            return ("搜 " + q)[:48] if q else "联网搜"
        if tool == "WebFetch":
            u = ti.get("url") or ""
            try:
                from urllib.parse import urlparse
                u = urlparse(u).netloc or u
            except Exception:
                pass
            u = (u or "").strip()
            return ("抓 " + u)[:48] if u else "抓网页"
        if tool == "TodoWrite":
            return "记清单"
        if tool in ("ExitPlanMode", "EnterPlanMode"):
            return "出方案"
        if tool == "Skill":
            sk = (ti.get("skill") or ti.get("command") or "").strip()
            return ("技能 " + sk)[:48] if sk else "调用技能"
        if tool == "AskUserQuestion":
            return "问你问题"
        if tool.startswith("mcp__"):
            parts = [p for p in tool.split("__") if p]
            name = parts[-1] if parts else "mcp"
            low = tool.lower()
            q = (ti.get("query") or ti.get("q") or ti.get("search") or ti.get("text") or "").strip()
            if "wechat" in low and name == "reply":
                return "发微信给你"
            if "weixin" in low or "wechat" in low:
                return ("搜微信 " + q)[:48] if q else "搜微信"
            if "tencent" in low and "meeting" in low:
                return ("腾讯会议 " + (q or mcp_action_cn(name)))[:48]
            if "anysearch" in low:
                if "extract" in name.lower():
                    return ("抓正文 " + q)[:48] if q else "抓正文"
                return ("联网搜 " + q)[:48] if q else "联网搜"
            if "notion" in low:
                return ("Notion " + (q or mcp_action_cn(name)))[:48]
            if "computer" in low:           # computer-use: screenshots / clicks / typing
                cmap = {"screenshot": "截图", "left_click": "点", "double_click": "双击",
                        "right_click": "右键", "type": "输入", "key": "按键", "scroll": "滚动",
                        "mouse_move": "移动", "left_click_drag": "拖拽", "wait": "等待",
                        "open_application": "开应用", "zoom": "放大",
                        "read_clipboard": "读剪贴板", "write_clipboard": "写剪贴板"}
                return "电脑 " + cmap.get(name, mcp_action_cn(name))
            if "chrome" in low:             # Claude-in-Chrome browser control
                bmap = {"navigate": "开页", "find": "找", "get_page_text": "读页",
                        "read_page": "读页", "javascript_tool": "跑JS", "form_input": "填表",
                        "tabs_create_mcp": "开标签", "tabs_close_mcp": "关标签",
                        "read_console_messages": "看控制台"}
                return "浏览器 " + bmap.get(name, mcp_action_cn(name))
            if ("draft" in low or "thread" in low or "gmail" in low
                    or name in ("list_labels", "create_label", "update_label", "delete_label")):
                return ("邮件 " + (q or mcp_action_cn(name)))[:48]
            label = name.replace("-", " ").replace("_", " ")
            return (label + (" " + q if q else ""))[:48]
    except Exception:
        pass
    return (tool or "tool")[:48]


def append(ev):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def load_events():
    evs = []
    try:
        with open(STATE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evs.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return evs


def episode_so_far(evs, src):
    """(running, episode_start_ts, episode_total_starts) for ONE source's current
    busy stretch — a continuous period where that source's running > 0."""
    running, ep_start, total = 0, None, 0
    for e in evs:
        if e.get("src", "other") != src:
            continue
        if e.get("ev") == "start":
            if running == 0:
                ep_start, total = e.get("t"), 0
            running += 1
            total += 1
        elif e.get("ev") == "end":
            running = max(0, running - 1)
    return running, ep_start, total


def daily_load():
    today = time.strftime("%Y-%m-%d")
    d = {"date": today, "steps": 0, "rounds": 0, "busy_sec": 0}
    try:
        with open(DAILY) as f:
            old = json.load(f)
        if old.get("date") == today:
            d.update(old)
    except Exception:
        pass
    d["date"] = today
    return d


def daily_save(d):
    try:
        with open(DAILY, "w") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def daily_step():
    """One finished tool action (kept for future use; not shown in the card)."""
    d = daily_load()
    d["steps"] = d.get("steps", 0) + 1
    daily_save(d)


def daily_round(dur):
    """One finished conversation turn (you asked -> I answered) + its duration."""
    d = daily_load()
    d["rounds"] = d.get("rounds", 0) + 1
    if dur:
        d["busy_sec"] = round(d.get("busy_sec", 0) + dur, 1)
    daily_save(d)


def last_turn_start_t(evs, sid):
    """Timestamp of the most recent still-open turn_start for this session."""
    t0 = None
    for e in evs:
        if e.get("sid") != sid:
            continue
        if e.get("ev") == "turn_start":
            t0 = e.get("t")
        elif e.get("ev") == "turn_end":
            t0 = None
    return t0


def notify(title, msg):
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}" sound name "Glass"'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def do_reset(sid):
    """Clear only THIS session's leftovers (+ ancient lines); keep other apps'
    in-flight events intact. Atomic via temp file + rename."""
    cutoff = time.time() - STALE_TRIM_SEC
    keep = []
    for e in load_events():
        if sid and e.get("sid") == sid:
            continue
        if e.get("t", 0) < cutoff:
            continue
        keep.append(e)
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            for e in keep:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, STATE)
    except Exception:
        pass


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "start"
    p = read_payload()
    sid = p.get("session_id")
    src = detect_src()

    if mode == "reset":
        do_reset(sid)
        return

    # B — the whole turn: UserPromptSubmit -> turn_start, Stop -> turn_end.
    # Brackets "you hit enter ... I finished replying", so the card is busy even
    # when I'm only thinking / writing text and using no tools at all.
    if mode == "turn_start":
        append({"t": time.time(), "ev": "turn_start", "src": src, "sid": sid})
        return

    if mode == "turn_end":
        t0 = last_turn_start_t(load_events(), sid)
        append({"t": time.time(), "ev": "turn_end", "src": src, "sid": sid})
        if t0 is not None:
            dur = time.time() - t0
            if dur < 0:
                dur = 0
            if dur > 3600:
                dur = 3600
            daily_round(dur)
        return

    tool = p.get("tool_name") or "tool"
    ti = p.get("tool_input") or {}

    if mode == "start":
        append({"t": time.time(), "ev": "start", "tool": tool,
                "desc": short_desc(tool, ti), "src": src, "sid": sid})
        return

    if mode == "end":
        running_before, ep_start, total = episode_so_far(load_events(), src)
        append({"t": time.time(), "ev": "end", "tool": tool,
                "desc": short_desc(tool, ti), "src": src, "sid": sid})
        closed = running_before - 1 <= 0 and ep_start is not None
        dur = (time.time() - ep_start) if closed else None
        daily_step()
        if closed and dur is not None and dur >= PING_AFTER_SEC:
            mm, ss = int(dur // 60), int(dur % 60)
            dur_s = f"{mm}m{ss}s" if mm else f"{ss}s"
            notify(f"✅ {SRC_LABEL.get(src, 'Claude')} 任务完成", f"{total} 步 · 用时 {dur_s}")


if __name__ == "__main__":
    main()
