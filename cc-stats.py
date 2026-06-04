#!/usr/bin/env python3
"""
cc-stats — today's REAL totals + per-session context level + 7-day trend for the
Claude progress widget, read straight from Claude Code's own transcripts
(~/.claude/projects/*/*.jsonl).

Every place you run Claude Code — Terminal, the Claude desktop App, VS Code —
writes its session transcript here, so scanning this folder AGGREGATES all
three channels automatically, and covers the whole day retroactively (unlike
the hook counter, which only sees rounds after it was switched on).

Outputs one JSON line the widget appends after its @@STATS@@ marker:
  {"date","rounds","busy_sec","tok_fresh","tok_total","tok_in","tok_out",
   "tok_cache_create","tok_cache_read","calls","sessions","trend",
   "ctx_window","computed_at","files"}

Three caches keep the widget's 1s refresh cheap:
  - whole output: cached TTL_SEC (15s); the widget almost always reads this.
  - 7-day history: the heavy 8-day scan runs at most HIST_TTL (1h); today's bar
    is always overlaid LIVE on top of the cached history so it grows in realtime.
  - per-session context only scans files touched in the last SESS_WINDOW (24h).

Definitions (today, local time):
  rounds   = real human prompts on the main chain (not tool-results, not sub-agents)
  busy_sec = Σ over those turns of (last activity in the turn − prompt time);
             idle time BETWEEN turns is excluded, sub-agent time inside a turn is included
  tok_*    = summed token usage from assistant messages (deduped by message id)
  sessions = {sid:{ctx,window,t,model,entry,cwd,title}} — ctx = latest MAIN-CHAIN
             assistant call's (input+cache_creation+cache_read) = how full the
             window is. window (200K vs 1M) is inferred per CLIENT (entrypoint:
             claude-desktop/cli/claude-vscode) from whether that client has ever
             crossed 200K — a 1M session sitting under 200K looks identical to a
             200K one, so the client tier is the only reliable signal. title =
             first human prompt, so each bar tells you which conversation it is.
             Stays listed until SessionEnd fires — a clean close (/exit, /clear)
             OR deleting the conversation both fire it (delete → reason "other")
             → instant drop, for sessions started after the hook was wired.
             SESS_WINDOW (24h) is just the backstop for sessions left un-closed.
  trend    = last 7 calendar days [{date,rounds,busy_sec}] oldest→newest.
"""
import sys, os, glob, json, time, datetime, bisect

HOME = os.path.expanduser("~")
PROJ = os.path.join(HOME, ".claude", "projects")
CACHE = os.path.join(HOME, ".claude", "cc-stats-cache.json")
HIST = os.path.join(HOME, ".claude", "cc-history.json")
ENDEDF = os.path.join(HOME, ".claude", "cc-ended.json")  # sids the SessionEnd hook marked terminated

TTL_SEC = 15            # recompute the whole output at most this often
HIST_TTL = 3600         # recompute the heavy 7-day history at most this often
BUSY_CAP = 3600         # clamp any single turn's span (guards against clock skew)
SESS_WINDOW = 24 * 3600  # idle backstop. /exit, /clear, AND deleting a convo all fire SessionEnd
#                          (delete → reason "other") → instant drop, for sessions started after the
#                          hook. This 24h only catches sessions abandoned without ever closing them.
TREND_DAYS = 7          # how many days the sparkline shows
CTX_WINDOW = 200000     # context window size the water-level is measured against
# entrypoint (present in every transcript) → which channel/colour the widget shows.
# More reliable than the live log's sid→src, which ages out and falls back to "其它".
ENTRY_SRC = {"cli": "terminal", "claude-desktop": "claude",
             "claude-vscode": "vscode", "vscode": "vscode"}


def iso_epoch(s):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        try:
            return (datetime.datetime
                    .strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
                    .replace(tzinfo=datetime.timezone.utc).timestamp())
        except Exception:
            return None


def is_real_user(e):
    """A human prompt that starts a round — not a tool-result, not a sub-agent."""
    if e.get("type") != "user" or e.get("isSidechain"):
        return False
    msg = e.get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        return True
    if isinstance(c, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
    return False


def local_date(ep):
    return time.strftime("%Y-%m-%d", time.localtime(ep))


def scan_file(path, today0, want_tokens):
    """Read ONE transcript once. Returns (day_rounds, day_busy, usage, sess):
      day_rounds {date:count}   real prompts bucketed by local date
      day_busy   {date:seconds} turn spans bucketed by the prompt's local date
      usage      {msgid:[in,out,cc,cr]}  today only, deduped (empty if not want_tokens)
      sess       (sid, last_ep, ctx, model) latest MAIN-CHAIN assistant ctx, or None
    """
    sid = os.path.splitext(os.path.basename(path))[0]
    act_ts, real_ts = [], []     # act_ts = assistant ACTIVITY only (drives turn-end)
    usage = {}
    last_assistant = (0.0, 0, None)   # (ep, ctx, model) of newest main-chain reply
    max_ctx = 0                       # biggest prompt this session ever sent → window tier
    min_ctx = None                    # smallest main-chain ctx ≈ overhead floor (post-compact level)
    compact_ep = 0.0                  # time of the latest /compact (a context reset)
    entry = None                      # entrypoint: claude-desktop / cli / claude-vscode
    cwd = None                        # working dir of the session
    title = None                      # first human prompt → which conversation this is
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if entry is None:
                    entry = e.get("entrypoint")
                if cwd is None:
                    cwd = e.get("cwd")
                ep = iso_epoch(e.get("timestamp"))
                if ep is None:
                    continue
                if e.get("isCompactSummary"):              # /compact reset marker
                    if ep > compact_ep:
                        compact_ep = ep
                    continue                               # not a prompt / not activity / not a title
                if title is None and e.get("type") == "user" and not e.get("isSidechain"):
                    c = (e.get("message") or {}).get("content")
                    if isinstance(c, str):
                        cs = c.strip().replace("\n", " ")
                        if cs and not cs.startswith("<"):
                            title = cs[:40]
                typ = e.get("type")
                if typ == "assistant":
                    act_ts.append(ep)                      # thinking / text / tool_use (incl. sub-agents)
                    m = e.get("message") or {}
                    u = m.get("usage") or {}
                    if u:
                        inp = u.get("input_tokens", 0) or 0
                        out = u.get("output_tokens", 0) or 0
                        cc = u.get("cache_creation_input_tokens", 0) or 0
                        cr = u.get("cache_read_input_tokens", 0) or 0
                        # context water-level = total input the model saw on its
                        # latest MAIN-CHAIN reply (sub-agents have their own window)
                        if not e.get("isSidechain"):
                            ctxv = inp + cc + cr
                            if ctxv > max_ctx:
                                max_ctx = ctxv
                            if min_ctx is None or ctxv < min_ctx:
                                min_ctx = ctxv
                            if ep >= last_assistant[0]:
                                last_assistant = (ep, ctxv, m.get("model"))
                        if want_tokens and ep >= today0:
                            # ONE API call is written as several transcript lines
                            # (thinking / text / tool_use) that REPEAT the same
                            # usage — dedup by message id so we don't 2-3x count.
                            mid = m.get("id") or e.get("requestId") or e.get("uuid")
                            vals = [inp, out, cc, cr]
                            prev = usage.get(mid)
                            if prev is None:
                                usage[mid] = vals
                            else:                          # output streams up → keep the fullest
                                for j in range(4):
                                    if vals[j] > prev[j]:
                                        prev[j] = vals[j]
                elif typ == "user":
                    if is_real_user(e):
                        real_ts.append(ep)                 # a human prompt = round boundary
                    else:
                        act_ts.append(ep)                  # tool-result / sub-agent prompt = still working
                # everything else (queue-operation, attachment, ai-title…) is NOT activity
    except Exception:
        return {}, {}, {}, None

    act_ts.sort()
    real_ts.sort()
    day_rounds, day_busy = {}, {}
    for i, u in enumerate(real_ts):
        d = local_date(u)
        day_rounds[d] = day_rounds.get(d, 0) + 1
        nxt = real_ts[i + 1] if i + 1 < len(real_ts) else float("inf")
        lo = bisect.bisect_left(act_ts, u)
        hi = bisect.bisect_left(act_ts, nxt)
        end = act_ts[hi - 1] if hi > lo else u
        span = end - u
        if span < 0:
            span = 0
        if span > BUSY_CAP:
            span = BUSY_CAP
        day_busy[d] = day_busy.get(d, 0.0) + span

    sess = None
    if last_assistant[0] > 0:
        ctx_now, t_now = last_assistant[1], last_assistant[0]
        if compact_ep > last_assistant[0]:                 # compacted, no reply yet:
            t_now = compact_ep                             #   it's recent activity, and
            if min_ctx is not None:                        #   show the reset floor, not
                ctx_now = min_ctx                          #   the stale pre-compact value
        sess = {"sid": sid, "t": t_now, "ctx": ctx_now,
                "max_ctx": max_ctx, "model": last_assistant[2],
                "entry": entry, "cwd": cwd, "title": title}
    return day_rounds, day_busy, usage, sess


def load_hist():
    try:
        with open(HIST) as f:
            return json.load(f)
    except Exception:
        return {}


def save_hist(h):
    try:
        tmp = HIST + ".tmp"
        with open(tmp, "w") as f:
            json.dump(h, f, ensure_ascii=False)
        os.replace(tmp, HIST)
    except Exception:
        pass


def load_history(all_files, today0):
    """Heavy scan, cached hourly in HIST. Returns (days, entry_max):
      days      {date:{rounds,busy_sec}} for the trend sparkline (8-day window)
      entry_max {entrypoint:max_ctx_ever} — which clients run the 1M window.
    entry_max is sticky (only grows) and is bootstrapped from a wide history the
    first time, so a 1M client is remembered even when its current sessions are
    small (per-session max can't reveal the window until it crosses 200K)."""
    now = time.time()
    h = load_hist()
    have_tiers = isinstance(h.get("entry_max"), dict) and bool(h.get("entry_max"))
    if now - h.get("computed_at", 0) < HIST_TTL and "days" in h and have_tiers:
        return h.get("days", {}), h.get("entry_max", {})

    day_floor = today0 - (TREND_DAYS + 1) * 86400          # +1 day buffer for tz/spans
    tier_floor = day_floor if have_tiers else today0 - 45 * 86400  # 1st run: learn tiers wide
    dr_all, db_all = {}, {}
    entry_max = dict(h.get("entry_max") or {})             # keep prior knowledge (sticky)
    for f in all_files:
        try:
            mt = os.path.getmtime(f)
        except Exception:
            continue
        for_days = mt >= day_floor
        for_tier = mt >= tier_floor
        if not (for_days or for_tier):
            continue
        dr, db, _u, sess = scan_file(f, today0, want_tokens=False)
        if for_days:
            for d, c in dr.items():
                dr_all[d] = dr_all.get(d, 0) + c
            for d, c in db.items():
                db_all[d] = db_all.get(d, 0.0) + c
        if for_tier and sess:
            ent, mc = sess.get("entry"), sess.get("max_ctx", 0)
            if ent and mc > entry_max.get(ent, 0):
                entry_max[ent] = mc
    days = {}
    for d in set(list(dr_all) + list(db_all)):
        days[d] = {"rounds": dr_all.get(d, 0), "busy_sec": round(db_all.get(d, 0.0), 1)}
    save_hist({"computed_at": now, "days": days, "entry_max": entry_max})
    return days, entry_max


def build_trend(days, today0, today_str, today_rounds, today_busy):
    """7 calendar days oldest→newest; today's bar is always the LIVE value."""
    base = datetime.date.fromtimestamp(today0)
    out = []
    for i in range(TREND_DAYS - 1, -1, -1):
        d = (base - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        if d == today_str:
            out.append({"date": d, "rounds": today_rounds, "busy_sec": round(today_busy, 1)})
        else:
            rec = days.get(d) or {}
            out.append({"date": d, "rounds": rec.get("rounds", 0), "busy_sec": rec.get("busy_sec", 0)})
    return out


def compute(today0):
    today_str = time.strftime("%Y-%m-%d")
    now = time.time()
    all_files = set(glob.glob(os.path.join(PROJ, "*", "*.jsonl")))
    all_files |= set(glob.glob(os.path.join(PROJ, "**", "*.jsonl"), recursive=True))

    days, entry_max = load_history(all_files, today0)      # trend days + per-client window tiers

    # one scan covers today's tokens/rounds AND the last-SESS_WINDOW sessions (the
    # start-of-day edge means "active recently" can reach back before midnight)
    recent_floor = min(today0, now - SESS_WINDOW)
    recent_files = [f for f in all_files if os.path.getmtime(f) >= recent_floor]

    seen = {}            # msgid -> [in,out,cc,cr]; dedups split/streamed entries
    today_rounds = 0
    today_busy = 0.0
    live = []            # session dicts active within SESS_WINDOW
    for f in recent_files:
        dr, db, usage, sess = scan_file(f, today0, want_tokens=True)
        for mid, vals in usage.items():
            prev = seen.get(mid)
            if prev is None:
                seen[mid] = vals
            else:
                for j in range(4):
                    if vals[j] > prev[j]:
                        prev[j] = vals[j]
        today_rounds += dr.get(today_str, 0)
        today_busy += db.get(today_str, 0.0)
        if sess and sess["t"] >= now - SESS_WINDOW:
            ent, mc = sess.get("entry"), sess.get("max_ctx", 0)
            if ent and mc > entry_max.get(ent, 0):         # a live session just grew the tier
                entry_max[ent] = mc
            live.append(sess)

    try:
        with open(ENDEDF) as f:
            ended = json.load(f)
        if not isinstance(ended, dict):
            ended = {}
    except Exception:
        ended = {}

    sessions = {}
    for sess in live:
        et = ended.get(sess["sid"])
        if et is not None and et >= sess["t"]:     # session ended & nothing since → drop it
            continue
        ent = sess.get("entry")
        # window = 1M if THIS session ever crossed 200K, OR its client is known 1M-capable
        tier = max(sess.get("max_ctx", 0), entry_max.get(ent, 0))
        window = 1000000 if tier > CTX_WINDOW else CTX_WINDOW
        sessions[sess["sid"]] = {
            "ctx": sess["ctx"], "window": window, "t": round(sess["t"], 1),
            "model": sess.get("model"), "entry": ent, "src": ENTRY_SRC.get(ent),
            "cwd": sess.get("cwd"), "title": sess.get("title"),
        }

    tin = sum(v[0] for v in seen.values())
    tout = sum(v[1] for v in seen.values())
    tcc = sum(v[2] for v in seen.values())
    tcr = sum(v[3] for v in seen.values())

    trend = build_trend(days, today0, today_str, today_rounds, today_busy)

    return {
        "date": today_str,
        "rounds": today_rounds,
        "busy_sec": round(today_busy, 1),
        "tok_fresh": tin + tout + tcc,        # new tokens (≈ what the API console reports)
        "tok_total": tin + tout + tcc + tcr,  # all-in (adds cached context re-reads)
        "tok_in": tin,
        "tok_out": tout,
        "tok_cache_create": tcc,
        "tok_cache_read": tcr,
        "calls": len(seen),
        "sessions": sessions,                 # per-session context water-level
        "trend": trend,                       # 7-day rounds/busy sparkline
        "ctx_window": CTX_WINDOW,
        "computed_at": now,
        "files": len(recent_files),
    }


def main():
    today = time.strftime("%Y-%m-%d")
    today0 = time.mktime(datetime.date.today().timetuple())

    # serve cache if fresh
    try:
        with open(CACHE) as f:
            c = json.load(f)
        if c.get("date") == today and time.time() - c.get("computed_at", 0) < TTL_SEC:
            sys.stdout.write(json.dumps(c, ensure_ascii=False))
            return
    except Exception:
        pass

    try:
        d = compute(today0)
    except Exception:
        d = {"date": today, "rounds": 0, "busy_sec": 0, "tok_total": 0,
             "tok_fresh": 0, "sessions": {}, "trend": [], "ctx_window": CTX_WINDOW}

    try:
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, CACHE)
    except Exception:
        pass

    sys.stdout.write(json.dumps(d, ensure_ascii=False))


if __name__ == "__main__":
    main()
