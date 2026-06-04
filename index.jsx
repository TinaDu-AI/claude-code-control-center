// Claude Code progress — desktop widget for Übersicht.
// Reads ~/.claude/cc-progress.jsonl (live events from cc-progress-hook.py) plus
// today's REAL totals from cc-stats.py (scans Claude Code transcripts across all
// channels), once a second, and draws a live progress card. Same Material-3
// glass card as the reading widget, same 264px width.
//
// MULTI-SOURCE: one card shows in-flight tasks from all three places you run
// Claude Code at once — Terminal / the Claude desktop App / VS Code — each row
// tagged on the LEFT with its source name in its own shade of purple.

// ============================ CONFIG (tweak freely) =========================
const SHOW_AFTER_SEC = 6;          // a single task must run this long to surface
const STALE_SEC = 1800;            // in-flight item older than this = crashed → hide
const TURN_FRESH_SEC = 150;        // hide "正在回复" this long after my last sign of life
const MAX_ROWS = 5;                // most task rows to list at once
const DESIGN_W = 280;              // internal layout width — leave this alone
const WIDTH = 264;                 // 屏幕上卡片宽度(px),= 阅读卡(border-box,真·总宽)
const SCALE = WIDTH / DESIGN_W;    // 整张卡按比例缩放,不变形

const C = {                        // base palette — mirrors the reading widget's M3 tokens
  primary: "#6750A4",
  tertiary: "#7D5260",
  onSurface: "#1D1B20",
  onSurfaceVar: "#49454F",
  outline: "#79747E",
  ok: "#3FA968",
  warn: "#E69100",                 // context 60-85% (getting full)
  danger: "#B3261E",               // context >85% (near limit / compaction soon)
};

// the monitored sources — left label + its own purple (light→deep = a family)
const SRC = {
  terminal: { label: "终端",    color: "#4F378B" },  // deepest
  claude:   { label: "Claude",  color: "#6750A4" },  // brand primary
  vscode:   { label: "VS Code", color: "#9A82DB" },  // lightest
  other:    { label: "其它",    color: "#79747E" },  // fallback (gray)
};
const SRC_ORDER = ["terminal", "claude", "vscode", "other"];
// ============================================================================

export const refreshFrequency = 1000;

// $HOME is expanded by the shell Übersicht runs this through. If your build of
// Übersicht doesn't expand it, replace $HOME with your home path (echo $HOME).
export const command =
  "cat $HOME/.claude/cc-progress.jsonl 2>/dev/null; " +
  "printf '@@STATS@@'; " +
  "/usr/bin/python3 $HOME/.claude/cc-stats.py 2>/dev/null";

export const className = `
  top: 40px;
  left: 40px;
  z-index: 1;
`;

// ---- drag (module-level so it survives the 1s re-renders) -------------------
let pos = (() => {
  try { return JSON.parse(localStorage.getItem("ccpos")) || { x: 0, y: 0 }; }
  catch (e) { return { x: 0, y: 0 }; }
})();
// one-time: snap the card's left edge back to its home X (flush with the reading
// card at left:40), keeping whatever vertical position you dragged it to.
try {
  if (localStorage.getItem("ccalign") !== "2") {
    pos = { x: 0, y: (pos && pos.y) || 0 };
    localStorage.setItem("ccpos", JSON.stringify(pos));
    localStorage.setItem("ccalign", "2");
  }
} catch (e) {}

const xf = (p) => `translate(${p.x}px, ${p.y}px) scale(${SCALE})`;

function startDrag(e) {
  e.preventDefault();
  const sx = e.clientX, sy = e.clientY, ox = pos.x, oy = pos.y;
  const card = document.getElementById("cc-card");
  const move = (ev) => {
    pos = { x: ox + ev.clientX - sx, y: oy + ev.clientY - sy };
    if (card) card.style.transform = xf(pos);
  };
  const up = () => {
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", up);
    try { localStorage.setItem("ccpos", JSON.stringify(pos)); } catch (e) {}
  };
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
}

// ---- parse the event log into per-source in-flight state --------------------
function computeState(text, now) {
  const evs = [];
  (text || "").split("\n").forEach((l) => {
    l = l.trim();
    if (!l) return;
    try { evs.push(JSON.parse(l)); } catch (e) {}
  });

  const by = {};                                   // src -> {items:[], turns:[], prog, last}
  const sidSrc = {};                               // sid -> src, so an idle session still gets its colour
  const get = (k) => (by[k] = by[k] || { items: [], turns: [], prog: null, last: 0 });
  for (const e of evs) {
    const k = SRC[e.src] ? e.src : "other";
    const S = get(k);
    if (e.sid) sidSrc[e.sid] = k;                   // remember which channel each session belongs to
    if (e.t && e.t > S.last) S.last = e.t;          // newest sign of life for this source
    if (e.ev === "start") {
      S.items.push({ tool: e.tool, desc: e.desc, t: e.t, sid: e.sid });
      for (const tn of S.turns) if (tn.sid === e.sid) tn.acted = true;  // this turn has acted → 回复, not 思考
    } else if (e.ev === "end") {
      let i = S.items.findIndex((s) => s.tool === e.tool && s.desc === e.desc);
      if (i < 0) i = S.items.findIndex((s) => s.tool === e.tool);
      if (i >= 0) S.items.splice(i, 1); else if (S.items.length) S.items.shift();
    } else if (e.ev === "turn_start") {
      // a fresh prompt means the previous turn is over — drop any leftover tools
      // from this session whose end-event we missed (orphans), then open the turn
      S.items = S.items.filter((it) => it.sid !== e.sid);
      S.turns.push({ sid: e.sid, t: e.t, acted: false });
    } else if (e.ev === "turn_end") {
      let i = S.turns.findIndex((x) => x.sid === e.sid);
      if (i < 0 && S.turns.length) i = 0;
      if (i >= 0) S.turns.splice(i, 1);
      S.items = S.items.filter((it) => it.sid !== e.sid);  // tool can't outlive its turn
    } else if (e.ev === "progress") {
      S.prog = { pct: e.pct, label: e.label, t: e.t };
    }
  }

  const groups = [];
  let total = 0, maxElapsed = 0, prog = null;
  for (const k of SRC_ORDER) {
    const S = by[k];
    if (!S) continue;
    const items = S.items.filter((it) => now - it.t < STALE_SEC);  // drop crashed leftovers
    let rows;
    if (items.length) {
      // detailed: one row per in-flight tool (command / 子任务 / 改文件 / 查资料…)
      rows = items.map((it) => ({ desc: it.desc, t: it.t, kind: "tool", sid: it.sid }));
    } else {
      // coarse: no tool running, but the turn is open -> one heartbeat row.
      // Before this turn's first action it's "正在思考"; once a tool has run it's
      // "正在回复" (composing). Only WHILE I'm actually alive — if there's been no
      // sign of life for a while (normal: Stop closed the turn; failure: Stop missed
      // on compaction/crash), treat it as done so the card is idle while YOU compose.
      const turns = S.turns.filter((x) => now - x.t < STALE_SEC);
      if (!turns.length) continue;
      if (now - (S.last || 0) >= TURN_FRESH_SEC) continue;
      const t0 = Math.min.apply(null, turns.map((x) => x.t));
      const recent = turns.reduce((a, b) => (b.t >= a.t ? b : a));   // newest open turn
      rows = [{ desc: recent.acted ? "正在回复" : "正在思考", t: t0, kind: "turn", sid: recent.sid }];
    }
    total += rows.length;
    for (const r of rows) maxElapsed = Math.max(maxElapsed, now - r.t);
    if (S.prog && now - S.prog.t < 30) prog = { pct: S.prog.pct, label: S.prog.label, src: k };
    groups.push({ src: k, rows });
  }
  return { groups, total, maxElapsed, prog, sidSrc };
}

// weekday letter for a YYYY-MM-DD (local) — 日一二三四五六
function weekday(dateStr) {
  const wd = ["日", "一", "二", "三", "四", "五", "六"];
  const d = new Date(dateStr + "T00:00:00");
  return isNaN(d) ? "" : wd[d.getDay()];
}

function fmt(sec) {
  sec = Math.max(0, Math.floor(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m ? `${m}m${String(s).padStart(2, "0")}s` : `${s}s`;
}

// footer total — coarser ("3 分" / "1h20m" / "2 小时"), reads at a glance
function fmtBusy(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.round(sec / 60);
  if (m < 60) return `${m} 分`;
  const h = Math.floor(m / 60), mm = m % 60;
  return mm ? `${h}h${mm}m` : `${h} 小时`;
}

// token count -> "936" / "8.5K" / "847K" / "84.6M"
function fmtTok(n) {
  n = Math.max(0, Math.round(n || 0));
  if (n < 1000) return `${n}`;
  if (n < 1e4) return `${(n / 1e3).toFixed(1)}K`;
  if (n < 1e6) return `${Math.round(n / 1e3)}K`;
  return `${(n / 1e6).toFixed(1)}M`;
}

const STYLE = `
  .cc-card,.cc-card *{box-sizing:border-box;font-family:-apple-system,"SF Pro Display","PingFang SC",sans-serif;-webkit-font-smoothing:antialiased;}
  .cc-card{position:relative;width:280px;color:${C.onSurface};transform-origin:top left;
    background:linear-gradient(157deg,rgba(252,252,255,0.86) 0%,rgba(246,247,253,0.87) 100%);
    backdrop-filter:blur(24px) saturate(140%);-webkit-backdrop-filter:blur(24px) saturate(140%);
    border:1px solid rgba(103,80,164,0.18);border-radius:20px;padding:16px;
    box-shadow:0 20px 50px rgba(70,70,110,0.18),inset 0 1px 0 rgba(255,255,255,0.7);overflow:hidden;}
  .cc-head{display:flex;align-items:center;gap:8px;cursor:grab;user-select:none;-webkit-user-select:none;}
  .cc-head:active{cursor:grabbing;}
  .cc-dot{width:8px;height:8px;border-radius:50%;background:${C.ok};box-shadow:0 0 6px rgba(63,169,104,0.5);flex-shrink:0;}
  .cc-dot.busy{background:${C.primary};box-shadow:0 0 8px rgba(103,80,164,0.6);animation:ccpulse 1.1s ease-in-out infinite;}
  @keyframes ccpulse{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.4;transform:scale(.75);}}
  .cc-title{font-size:13px;font-weight:600;letter-spacing:.3px;}
  .cc-elapsed{margin-left:auto;font-size:11px;color:${C.onSurfaceVar};font-variant-numeric:tabular-nums;}
  .cc-prow{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:14px 0 6px;}
  .cc-plabel{font-size:11px;color:${C.onSurfaceVar};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:6px;min-width:0;}
  .cc-ppct{font-size:11px;font-weight:600;color:${C.primary};font-variant-numeric:tabular-nums;flex-shrink:0;}
  .cc-bar{height:6px;background:rgba(103,80,164,0.22);border-radius:3px;overflow:hidden;}
  .cc-fill{height:100%;background:linear-gradient(90deg,${C.primary},${C.tertiary});border-radius:3px;transition:width .3s ease;}
  .cc-list{margin-top:12px;display:flex;flex-direction:column;gap:7px;}
  .cc-item{display:flex;align-items:center;gap:7px;background:rgba(103,80,164,0.10);border-radius:11px;padding:7px 9px;}
  .cc-tag{font-size:9px;font-weight:600;color:#fff;border-radius:6px;padding:2px 6px;line-height:1.4;flex-shrink:0;letter-spacing:.2px;white-space:nowrap;}
  .cc-spin{width:10px;height:10px;border-radius:50%;border:2px solid rgba(103,80,164,0.22);animation:ccspin .8s linear infinite;flex-shrink:0;}
  @keyframes ccspin{to{transform:rotate(360deg);}}
  .cc-idesc{font-size:11px;color:${C.onSurface};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;}
  .cc-turn{font-style:italic;color:${C.onSurfaceVar};}
  .cc-ielapsed{font-size:10px;color:${C.outline};font-variant-numeric:tabular-nums;flex-shrink:0;}
  .cc-idle{font-size:11px;color:${C.onSurfaceVar};margin-top:13px;line-height:1.5;}
  .cc-more{font-size:10px;color:${C.outline};text-align:center;margin-top:2px;}
  .cc-foot{display:flex;align-items:center;gap:6px;margin-top:13px;padding-top:11px;
    border-top:1px solid rgba(73,69,79,0.12);font-size:10px;color:${C.outline};font-variant-numeric:tabular-nums;}
  .cc-fdot{width:6px;height:6px;border-radius:50%;background:${C.ok};box-shadow:0 0 6px rgba(63,169,104,0.45);}
  .cc-ftok{margin-left:auto;font-weight:600;color:${C.onSurfaceVar};white-space:nowrap;}
  /* context water-level (per active session) */
  .cc-ctx{margin-top:12px;display:flex;flex-direction:column;gap:6px;}
  .cc-ctxcap{font-size:9px;color:${C.outline};letter-spacing:.4px;}
  .cc-ctxrow{display:flex;align-items:center;gap:6px;}
  .cc-ctxname{flex:1 1 0;min-width:0;font-size:10px;color:${C.onSurfaceVar};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .cc-ctxbar{flex:1 1 0;height:5px;background:rgba(73,69,79,0.14);border-radius:3px;overflow:hidden;}
  .cc-ctxfill{height:100%;border-radius:3px;transition:width .3s ease,background .3s ease;}
  .cc-ctxpct{font-size:10px;font-weight:600;font-variant-numeric:tabular-nums;flex-shrink:0;min-width:30px;text-align:right;}
  /* 7-day trend sparkline */
  .cc-spark{display:flex;align-items:flex-end;gap:4px;margin-top:11px;}
  .cc-sparkcol{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;}
  .cc-sparkbarwrap{width:100%;height:14px;display:flex;align-items:flex-end;}
  .cc-sparkbar{width:100%;border-radius:3px 3px 0 0;min-height:2px;transition:height .3s ease;}
  .cc-sparkday{font-size:8px;line-height:1;color:${C.outline};}
  .cc-sparkday.today{color:${C.primary};font-weight:700;}
`;

export const render = ({ output }) => {
  const [evText, statsText] = (output || "").split("@@STATS@@");
  let stats = {};
  try { stats = JSON.parse((statsText || "").trim()) || {}; } catch (e) {}

  const now = Date.now() / 1000;
  const { groups, total, maxElapsed, prog, sidSrc } = computeState(evText, now);
  const busy = total > 0 && (total >= 2 || maxElapsed > SHOW_AFTER_SEC || !!prog);

  const flat = [];
  for (const g of groups) for (const r of g.rows) flat.push({ desc: r.desc, t: r.t, src: g.src, kind: r.kind });
  const shown = flat.slice(0, MAX_ROWS);
  const extra = flat.length - shown.length;

  // context water-level — persists for every recent session (busy OR idle) until it
  // goes quiet for SESS_WINDOW; cc-stats supplies the level, the live log the colour.
  const defWin = stats.ctx_window || 200000;
  const sessions = stats.sessions || {};
  const ctxBars = Object.keys(sessions)
    .map((sid) => {
      const s = sessions[sid];
      if (!s) return null;
      const win = s.window || defWin;
      const pct = Math.max(0, Math.min(100, Math.round((s.ctx / win) * 100)));
      return { sid, src: sidSrc[sid] || "other", ctx: s.ctx, win, pct, t: s.t || 0,
               title: s.title || "", cwd: s.cwd || "" };
    })
    .filter(Boolean)
    .sort((a, b) => b.t - a.t)            // most-recently-active session first
    .slice(0, 3);

  // 7-day trend (oldest -> newest); height scales to the busiest day
  const trend = Array.isArray(stats.trend) ? stats.trend : [];
  const trendMax = Math.max(1, ...trend.map((d) => d.rounds || 0));

  return (
    <div>
      <style>{STYLE}</style>
      <div id="cc-card" className="cc-card" style={{ transform: xf(pos) }}>
        <div className="cc-head" onMouseDown={startDrag}>
          <span className={busy ? "cc-dot busy" : "cc-dot"} />
          <span className="cc-title">⚡ Claude</span>
          <span className="cc-elapsed">{busy ? `${total} 个 · ${fmt(maxElapsed)}` : "空闲"}</span>
        </div>

        {busy ? (
          <div>
            {prog && (
              <div>
                <div className="cc-prow">
                  <span className="cc-plabel">
                    <span className="cc-tag" style={{ background: SRC[prog.src].color }}>{SRC[prog.src].label}</span>
                    {prog.label || "运行中"}
                  </span>
                  <span className="cc-ppct">{prog.pct}%</span>
                </div>
                <div className="cc-bar">
                  <div className="cc-fill" style={{ width: prog.pct + "%" }} />
                </div>
              </div>
            )}

            <div className="cc-list">
              {shown.map((s, i) => (
                <div className="cc-item" key={i} style={{ boxShadow: `inset 3px 0 0 ${SRC[s.src].color}` }}>
                  <span className="cc-tag" style={{ background: SRC[s.src].color }}>{SRC[s.src].label}</span>
                  <span className={s.kind === "turn" ? "cc-idesc cc-turn" : "cc-idesc"}>{s.desc}</span>
                  <span className="cc-spin" style={{ borderTopColor: SRC[s.src].color }} />
                  <span className="cc-ielapsed">{fmt(now - s.t)}</span>
                </div>
              ))}
              {extra > 0 && <div className="cc-more">还有 {extra} 个…</div>}
            </div>
          </div>
        ) : (
          <div className="cc-idle">
            {ctxBars.length > 0 ? "待命中 · 以下 session 仍开着" : "待命中 · 终端 / Claude / VS Code 有长任务就显示"}
          </div>
        )}

        {ctxBars.length > 0 && (
          <div className="cc-ctx">
            <div className="cc-ctxcap">上下文水位</div>
            {ctxBars.map((b) => {
              const col = b.pct >= 85 ? C.danger : b.pct >= 60 ? C.warn : C.ok;
              const nm = b.title || (b.cwd ? b.cwd.split("/").filter(Boolean).pop() : "") || ("会话 " + b.sid.slice(0, 4));
              return (
                <div className="cc-ctxrow" key={b.sid}
                  title={`${SRC[b.src].label} · ${nm}\n上下文 ${fmtTok(b.ctx)} / ${fmtTok(b.win)} · ${b.pct}%`}>
                  <span className="cc-tag" style={{ background: SRC[b.src].color }}>{SRC[b.src].label}</span>
                  <span className="cc-ctxname">{nm}</span>
                  <div className="cc-ctxbar">
                    <div className="cc-ctxfill" style={{ width: b.pct + "%", background: col }} />
                  </div>
                  <span className="cc-ctxpct" style={{ color: col }}>{b.pct}%</span>
                </div>
              );
            })}
          </div>
        )}

        {trend.length > 0 && (
          <div className="cc-spark" title="近 7 天 · 每日对话轮数">
            {trend.map((d, i) => {
              const isToday = i === trend.length - 1;
              const h = Math.max(2, Math.round(((d.rounds || 0) / trendMax) * 14));
              return (
                <div className="cc-sparkcol" key={d.date}
                  title={`${(d.date || "").slice(5)} 周${weekday(d.date)} · ${d.rounds || 0} 轮 · ${fmtBusy(d.busy_sec)}`}>
                  <div className="cc-sparkbarwrap">
                    <div className="cc-sparkbar" style={{ height: h + "px", background: isToday ? C.primary : C.outline, opacity: isToday ? 1 : 0.45 }} />
                  </div>
                  <div className={isToday ? "cc-sparkday today" : "cc-sparkday"}>{weekday(d.date)}</div>
                </div>
              );
            })}
          </div>
        )}

        <div className="cc-foot">
          <span className="cc-fdot" />
          <span>今日忙 {fmtBusy(stats.busy_sec)} · {stats.rounds || 0} 轮对话</span>
          <span className="cc-ftok" title="今日全渠道新增 token：输入+输出+缓存写入，≈ 控制台用量（不含缓存重复读取）">{fmtTok(stats.tok_fresh)} tok</span>
        </div>
      </div>
    </div>
  );
};
