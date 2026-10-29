"""A read-only status page, served from the VPS behind a secret link.

`dresearch dashboard-serve` runs a tiny web server (standard library only) that
answers exactly one path, /<secret-token>/, with a page rebuilt at most every
30 s from the files on disk. Every other path gets 404. No forms, no actions:
the page can't change anything.
"""
from __future__ import annotations

import html
import json
import secrets
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .storage import load_json, save_json

LAGOS = timezone(timedelta(hours=1))      # West Africa Time, no daylight saving
FRIENDLY = {
    "R_10": "Volatility 10", "R_25": "Volatility 25", "R_50": "Volatility 50",
    "R_75": "Volatility 75", "R_100": "Volatility 100",
    "1HZ10V": "Volatility 10 (1s)", "1HZ25V": "Volatility 25 (1s)", "1HZ50V": "Volatility 50 (1s)",
    "1HZ75V": "Volatility 75 (1s)", "1HZ100V": "Volatility 100 (1s)", "frxXAUUSD": "Gold / US dollar",
}
INTERESTING = {"collector_start", "collector_stop", "connected", "disconnected", "watchdog_reconnect",
               "session_error", "backfill_start", "backfill_stop", "backfill_reconnect",
               "history_error", "pip_size_changed", "subscribe_error"}


# ---------------------------------------------------------------- data gathering
def _tail_lines(path: Path, n_bytes: int = 4000) -> list[str]:
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - n_bytes))
        return fh.read().decode("utf-8", "ignore").splitlines()[1:] if size > n_bytes else \
            fh.read().decode("utf-8", "ignore").splitlines()


def symbol_snapshot(data: Path, sym: str, now: float) -> dict:
    root = data / "raw" / "live" / sym
    out = {"last_epoch": None, "last_quote": None, "ticks_today": 0, "series": []}
    if not root.exists():
        return out
    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    day_dir = root / today
    if day_dir.exists():
        for f in day_dir.glob("*.csv"):
            with open(f, "rb") as fh:
                out["ticks_today"] += max(0, sum(1 for _ in fh) - 1)
    files = sorted(p for d in sorted(root.iterdir())[-2:] if d.is_dir() for p in d.glob("*.csv"))[-2:]
    pts = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                parts = line.split(",")
                if len(parts) >= 2:
                    try:
                        pts.append((int(parts[0]), float(parts[1])))
                    except ValueError:
                        pass
    if pts:
        out["last_epoch"], out["last_quote"] = pts[-1]
        cutoff = now - 3600
        recent = [p for p in pts if p[0] >= cutoff]
        step = max(1, len(recent) // 160)
        out["series"] = recent[::step] + ([recent[-1]] if recent and recent[-1] is not recent[::step][-1] else [])
    return out


def service_state(name: str) -> str:
    try:
        return subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def recent_events(data: Path, n: int = 8) -> list[tuple[str, str, str]]:
    p = data / "logs" / "events.csv"
    if not p.exists():
        return []
    rows = []
    for line in _tail_lines(p, 60000):
        parts = line.split(",", 2)
        if len(parts) == 3 and parts[1] in INTERESTING:
            rows.append((parts[0], parts[1], parts[2]))
    return rows[-n:][::-1]


def latest(data: Path, pattern: str):
    d = data / "reports"
    files = sorted(d.glob(pattern)) if d.exists() else []
    return load_json(files[-1], None) if files else None


def gather(data: Path, now: float | None = None) -> dict:
    now = now or time.time()
    cfg = load_json(data / "config" / "symbols.json", {}) or {}
    probe = latest(data, "probe_*.json") or {}
    symbols = []
    for sym in cfg.get("collect", []):
        snap = symbol_snapshot(data, sym, now)
        interval = (cfg.get("intervals_s") or {}).get(sym, 1)
        graded = sym in (cfg.get("watchdog") or [])
        age = now - snap["last_epoch"] if snap["last_epoch"] else None
        if age is None:
            state = "bad" if graded else "idle"
        elif graded:
            state = "good" if age <= 3 * interval + 10 else "warn" if age <= 120 else "bad"
        else:
            state = "good" if age <= 120 else "idle"
        st = (probe.get("tick_stats") or {}).get(sym, {})
        symbols.append({"symbol": sym, "name": FRIENDLY.get(sym, sym), "graded": graded,
                        "interval": interval, "age": age, "state": state, **snap,
                        "ann_vol": st.get("annualised_vol_365d_basis"),
                        "nominal": st.get("nominal_vol_from_name"),
                        "rounding": st.get("rounding_ratio_grid_over_tick_move")})
    raw = data / "raw"
    used = sum(p.stat().st_size for p in raw.rglob("*") if p.is_file()) if raw.exists() else 0
    return {
        "now": now, "cfg": cfg, "probe": probe, "symbols": symbols,
        "collector": service_state("dresearch-collector"),
        "backfill_service": service_state("dresearch-backfill"),
        "backfill": load_json(data / "reports" / "backfill_progress.json", None),
        "audit": latest(data, "audit_*.json"),
        "analysis": latest(data, "analysis_*.json"),
        "analysis_progress": load_json(data / "reports" / "analysis_progress.json", None),
        "analyze_service": service_state("dresearch-analyze"),
        "freeze": load_json(data / "config" / "freeze.json", None),
        "mt5": latest(data, "mt5_snapshot_*.json"),
        "events": recent_events(data),
        "bytes": used, "disk_free": shutil.disk_usage(data).free if data.exists() else 0,
    }


# ---------------------------------------------------------------- rendering
def esc(x) -> str:
    return html.escape(str(x), quote=True)


def ago(seconds) -> str:
    if seconds is None:
        return "never"
    s = int(max(0, seconds))
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def sparkline(series: list[tuple[int, float]], w: int = 168, h: int = 36) -> str:
    if len(series) < 2:
        return '<span class="muted">no data yet</span>'
    xs = [p[0] for p in series]
    ys = [p[1] for p in series]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    sx = (w - 6) / max(1, x1 - x0)
    sy = (h - 8) / (y1 - y0) if y1 > y0 else 0
    pts = [(3 + (x - x0) * sx, h - 4 - (y - y0) * sy if sy else h / 2) for x, y in series]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"3,{h - 2} {line} {pts[-1][0]:.1f},{h - 2}"
    ex, ey = pts[-1]
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="Price over the last hour">'
            f'<polygon points="{area}" fill="var(--accent)" fill-opacity="0.10"/>'
            f'<polyline points="{line}" fill="none" stroke="var(--accent)" stroke-width="1.4" '
            f'stroke-linejoin="round"/><circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.4" fill="var(--accent)"/></svg>')


def plain_summary(g: dict) -> tuple[str, str, list[str]]:
    syn = [s for s in g["symbols"] if s["graded"]]
    ok = sum(1 for s in syn if s["state"] == "good")
    healthy = g["collector"] == "active" and syn and ok == len(syn)
    lines = []
    if g["collector"] != "active":
        lines.append(f"The recorder is <b>not running</b> (service state: {esc(g['collector'])}). "
                     "Nothing new is being saved.")
    elif healthy:
        lines.append(f"Recording live prices for all {len(syn)} Volatility Indices, every tick, around the clock.")
    else:
        lines.append(f"Recording, but only {ok} of {len(syn)} indices received a tick recently. "
                     "Check the table below.")
    bf = g["backfill"]
    if bf:
        done = bf.get("hours_done_before_start", 0) + bf.get("hours_processed", 0)
        total = max(1, bf.get("hours_total", 1))
        if bf.get("finished"):
            lines.append(f"The {bf.get('days')}-day history download is complete.")
        elif g["backfill_service"] == "active":
            lines.append(f"Downloading {bf.get('days')} days of past prices: {done / total:.0%} done, "
                         f"about {bf.get('eta_hours', '?')} h left.")
        else:
            lines.append(f"History download paused at {done / total:.0%}. "
                         "Start it again with <code>systemctl start dresearch-backfill</code>.")
    elif g["backfill_service"] == "active":
        lines.append("Downloading past prices: just started; progress appears within a few minutes.")
    lines.append("No trading happens here. This system can only read prices; it has no way to place a trade.")
    label = "Healthy" if healthy else "Needs attention"
    return ("good" if healthy else "warn" if g["collector"] == "active" else "bad"), label, lines


CSS = """
/* Layout: one calm column, an instrument-panel summary up top, detail tables below. */
:root{
  --bg:#f3f5f6; --panel:#ffffff; --fg:#14202a; --muted:#5b6b77; --line:#d9e0e4;
  --accent:#0d7c74; --good:#1f8a4c; --warn:#b26b00; --bad:#c2372f; --idle:#8795a0;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Menlo,monospace;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0e1418; --panel:#151d23; --fg:#e4ebef; --muted:#93a3ae; --line:#26333c;
  --accent:#4fc8bc; --good:#53c486; --warn:#e3a33c; --bad:#ef6b61; --idle:#6f7f8a; color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 var(--sans)}
.wrap{max-width:1040px;margin:0 auto;padding-block:28px 56px;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));
  display:grid;gap:22px}
header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px 20px}
h1{font-size:22px;font-weight:600;margin:0;letter-spacing:-.01em;text-wrap:balance}
h2{font-size:13px;font-weight:600;margin:0 0 10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.muted{color:var(--muted)} code,.mono{font-family:var(--mono);font-size:.92em}
.stamp{font-family:var(--mono);font-size:12.5px;color:var(--muted)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px;min-width:0}
.hero{display:grid;gap:10px;border-left:4px solid var(--state)}
.hero p{margin:0;max-width:70ch}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:600;padding:3px 10px;
  border-radius:999px;border:1px solid var(--state);color:var(--state);width:max-content}
.dot{width:8px;height:8px;border-radius:50%;background:var(--state);display:inline-block}
.s-good{--state:var(--good)} .s-warn{--state:var(--warn)} .s-bad{--state:var(--bad)} .s-idle{--state:var(--idle)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.tile .v{font-family:var(--mono);font-size:22px;font-weight:500;font-variant-numeric:tabular-nums}
.tile .k{font-size:12.5px;color:var(--muted)}
.bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;background:var(--accent)}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:0;padding:0;list-style:none}
.steps li{border:1px solid var(--line);border-radius:8px;padding:12px 14px;display:grid;gap:4px}
.steps li.now{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.steps b{font-size:14px} .steps span{font-size:13px;color:var(--muted)}
.tbl{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-weight:500;font-size:12.5px;color:var(--muted);padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right}
tr:last-child td{border-bottom:none}
.spark{display:block}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:22px}
dl{margin:0;display:grid;gap:10px} dt{font-weight:600;font-size:14px} dd{margin:0;color:var(--muted);font-size:14px;max-width:65ch}
ul.ev{margin:0;padding:0;list-style:none;font-size:13px;display:grid;gap:6px}
ul.ev li{display:grid;grid-template-columns:auto 1fr;gap:10px;min-width:0}
ul.ev li span:last-child{overflow-wrap:anywhere;color:var(--muted)}
footer{font-size:12.5px;color:var(--muted)}
"""


def render(g: dict) -> str:
    now = g["now"]
    utc = datetime.fromtimestamp(now, timezone.utc)
    lag = utc.astimezone(LAGOS)
    state, label, lines = plain_summary(g)
    syn = [s for s in g["symbols"] if s["graded"]]
    live_ok = sum(1 for s in syn if s["state"] == "good")
    bf = g["backfill"] or {}
    bf_done = bf.get("hours_done_before_start", 0) + bf.get("hours_processed", 0)
    bf_total = bf.get("hours_total", 0)
    bf_pct = (bf_done / bf_total) if bf_total else 0.0
    audit = g["audit"]
    audit_state = (audit or {}).get("overall")

    rows = []
    for s in g["symbols"]:
        q = s["last_quote"]
        rows.append(
            f'<tr><td><span class="dot s-{s["state"]}" title="{s["state"]}"></span>&nbsp; {esc(s["name"])}'
            f' <span class="muted mono">{esc(s["symbol"])}</span></td>'
            f'<td class="num">{esc(f"{q:,.4f}" if q is not None else "–")}</td>'
            f'<td class="num">{esc(ago(s["age"]))}</td>'
            f'<td class="num">{s["ticks_today"]:,}</td>'
            f'<td>{sparkline(s["series"])}</td></tr>')

    vol_rows = []
    for s in g["symbols"]:
        if s["nominal"] and s["ann_vol"]:
            r = s["rounding"] or 0
            vol_rows.append(
                f'<tr><td>{esc(s["name"])}</td><td class="num">{s["nominal"]:.0%}</td>'
                f'<td class="num">{s["ann_vol"]:.1%}</td>'
                f'<td class="num">{r:.3f}</td>'
                f'<td>{"matters, will be simulated" if r >= 0.05 else "negligible"}</td></tr>')

    audit_html = '<p class="muted">The first audit runs at 00:20 UTC (01:20 in Lagos) and grades the previous full day.</p>'
    if audit:
        arows = []
        for sym, r in audit.get("symbols", {}).items():
            c = r.get("completeness_full_day")
            ok = str(r.get("status", "")).startswith("OK")
            arows.append(f'<tr><td>{esc(FRIENDLY.get(sym, sym))}</td>'
                         f'<td class="num">{(f"{c:.2%}" if c is not None else "–")}</td>'
                         f'<td class="num">{r.get("gap_count", "–")}</td>'
                         f'<td><span class="dot s-{"good" if ok else "warn"}"></span>&nbsp; {esc(r.get("status"))}</td></tr>')
        audit_html = (f'<p class="muted">Day graded: {esc(audit.get("day"))} (UTC). Overall: <b>{esc(audit_state)}</b>. '
                      'The first day is usually partial, because recording started part-way through it.</p>'
                      f'<div class="tbl"><table><thead><tr><th>Index</th><th>Complete</th><th>Gaps</th><th>Result</th></tr></thead>'
                      f'<tbody>{"".join(arows)}</tbody></table></div>')

    ev = "".join(f'<li><span class="stamp">{esc(t[:16].replace("T", " "))}</span>'
                 f'<span>{esc(e.replace("_", " "))}{(" · " + esc(d)) if d else ""}</span></li>'
                 for t, e, d in g["events"]) or '<li class="muted">No events yet.</li>'

    step_now = 4 if g.get("analysis") else 3 if bf.get("finished") else 1
    steps = [("Collect prices", "Live recording plus a download of past prices."),
             ("Check the data", "Is anything missing, duplicated or distorted? Graded daily."),
             ("Look for patterns", "Compare the prices with simulated prices that have no pattern at all."),
             ("Cost check and verdict", "Is any pattern big enough to beat the spread? Answer: no edge, not enough data, or candidate.")]
    steps_html = "".join(f'<li class="{"now" if i == step_now else ""}"><b>{i}. {a}</b><span>{b}'
                         f'{" <em>(current)</em>" if i == step_now else ""}</span></li>'
                         for i, (a, b) in enumerate(steps, 1))

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="refresh" content="60"><meta name="robots" content="noindex,nofollow">
<title>Deriv Research Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head><body><main class="wrap">
<header><h1>Deriv Research Monitor</h1>
<span class="stamp">Updated {lag:%d %b %Y, %H:%M} Lagos · {utc:%H:%M} UTC · refreshes every minute</span></header>

<section class="panel hero s-{state}" aria-label="Summary">
<span class="pill"><span class="dot"></span>{label}</span>
{"".join(f"<p>{l}</p>" for l in lines)}
</section>

<section class="tiles" aria-label="Key figures">
<div class="panel tile"><div class="k">Live feed</div><div class="v">{live_ok}/{len(syn)}</div><div class="k">indices ticking now</div></div>
<div class="panel tile"><div class="k">History download</div><div class="v">{bf_pct:.0%}</div>
<div class="bar"><i style="width:{bf_pct * 100:.1f}%"></i></div><div class="k">{bf_done:,} of {bf_total:,} symbol-hours</div></div>
<div class="panel tile"><div class="k">Data stored</div><div class="v">{(f"{g['bytes'] / 1e9:,.2f} GB" if g["bytes"] >= 1e9 else f"{g['bytes'] / 1e6:,.1f} MB")}</div><div class="k">{g["disk_free"] / 1e9:,.0f} GB disk free</div></div>
<div class="panel tile"><div class="k">Latest quality audit</div><div class="v">{esc(audit_state or "Tonight")}</div><div class="k">{esc((audit or {}).get("day", "first run 01:20 Lagos"))}</div></div>
</section>

<section class="panel" aria-label="Where the project is"><h2>Where the project is</h2><ol class="steps">{steps_html}</ol></section>

<section class="panel" aria-label="Live prices"><h2>Live prices being recorded</h2>
<div class="tbl"><table><thead><tr><th>Index</th><th>Last price</th><th>Last tick</th><th>Ticks today (UTC)</th><th>Last 60 minutes</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p class="muted">Green means a tick arrived within the expected interval. Gold trades only on weekdays, so grey there is normal at weekends.</p></section>

<section class="panel" aria-label="Data quality"><h2>Data quality</h2>{audit_html}</section>

{analysis_html(g)}

<div class="two">
<section class="panel" aria-label="First measurements"><h2>First measurements (1,000-tick sample)</h2>
<div class="tbl"><table><thead><tr><th>Index</th><th>Label</th><th>Measured</th><th>Rounding</th><th></th></tr></thead>
<tbody>{"".join(vol_rows) or '<tr><td class="muted">Run the probe to see these.</td></tr>'}</tbody></table></div>
<p class="muted">Measured volatility matches each index's label on a 365-day year: the prices behave as advertised so far. Rounding is the price step relative to a typical tick move; above 0.05 it can fake a tiny pattern, so the analysis simulates it.</p></section>

<section class="panel" aria-label="What the words mean"><h2>What the words mean</h2><dl>
<dt>Tick</dt><dd>One price update. 1s indices tick every second, the others every 2 seconds.</dd>
<dt>Volatility</dt><dd>How much the price swings, as a yearly percentage. "Volatility 75" is designed to swing 75% a year.</dd>
<dt>Spread</dt><dd>The gap between the buy and sell price on MT5. Every trade pays it, so a pattern must be bigger than the spread to make money.</dd>
<dt>Edge</dt><dd>A pattern that still makes money after costs. Deriv says these prices come from a random number generator, so the starting assumption is that there is none. This project tests that.</dd>
</dl></section>
</div>

<section class="panel" aria-label="Recent events"><h2>Recent events</h2><ul class="ev">{ev}</ul>
<p class="muted">Occasional reconnects are normal; the recorder repairs any gap from Deriv's history within the hour.</p></section>

<footer>Read-only page served from your VPS. It shows files on disk and cannot change anything. Keep the link private.</footer>
</main></body></html>"""


def analysis_html(g: dict) -> str:
    a, prog, svc, fz = g.get("analysis"), g.get("analysis_progress"), g.get("analyze_service"), g.get("freeze")
    head = '<section class="panel" aria-label="Pattern tests"><h2>Pattern tests</h2>'
    if svc in ("active", "activating") and prog:
        pct = (prog["symbol_index"] - 1 + prog["sims_done"] / max(1, prog["sims"])) / max(1, prog["symbols"])
        return (head + f'<p>Running now: {esc(FRIENDLY.get(prog["symbol"], prog["symbol"]))} '
                f'({prog["symbol_index"]} of {prog["symbols"]}), simulation {prog["sims_done"]} of {prog["sims"]}.</p>'
                f'<div class="bar"><i style="width:{pct * 100:.1f}%"></i></div></section>')
    if not a:
        frozen = (f'The protocol was frozen on {esc(fz["T0_utc"][:16].replace("T", " "))} UTC. '
                  if fz else "The protocol is not frozen yet. ")
        return (head + f'<p class="muted">{frozen}The tests run once the history download is finished. '
                'Each index is compared with 100 simulated price series that have no pattern at all, '
                'rounded the same way as Deriv\'s prices.</p></section>')
    mt5 = (g.get("mt5") or {}).get("symbols", {})
    rows = []
    for sym, r in a.get("symbols", {}).items():
        st = str(r.get("status", ""))
        cls = "good" if st.startswith("CONSISTENT") else "warn" if st.startswith("DEVIATION") else "idle"
        vr = (r.get("H2_vol") or {}).get("ratio")
        spread = ((mt5.get(sym) or {}).get("history_24h") or {}).get("median_pct_of_price")
        cells = []
        for e in r.get("economics", []):
            be = e["no_edge_if_round_trip_cost_above_pct"]
            if spread is None:
                cells.append(f'<td class="num">{be:.4f}%</td>')
            elif spread > be:
                cells.append(f'<td class="num" title="spread {spread:.4f}% is above break-even {be:.4f}%">'
                             f'<span class="dot s-good"></span> no edge</td>')
            else:
                cells.append(f'<td class="num" title="spread {spread:.4f}% is below break-even {be:.4f}%">'
                             f'<span class="dot s-warn"></span> open</td>')
        cost = "".join(cells) or '<td colspan="4" class="muted">–</td>'
        sp_cell = f'<td class="num">{spread:.4f}%</td>' if spread is not None else '<td class="num muted">–</td>'
        rows.append(f'<tr><td><span class="dot s-{cls}"></span>&nbsp; {esc(FRIENDLY.get(sym, sym))}</td>'
                    f'<td>{esc(st.split(" (")[0].capitalize())}</td>'
                    f'<td class="num">{(f"{vr:.3f}" if vr else "–")}</td>{sp_cell}{cost}</tr>')
    devs = [f"{FRIENDLY.get(s, s)}: {d}" for s, r in a.get("symbols", {}).items() for d in r.get("deviations", [])]
    dev_html = ("<p><b>Deviations to investigate:</b> " + esc("; ".join(devs)) +
                ". A deviation is not an edge until it survives the cost check and the holdout.</p>") if devs else \
        "<p>No index deviates from a fair random process on any of the 8 tests (Holm-corrected, 1% level).</p>"
    return (head + f'<p class="muted">{esc(a.get("mode"))} run on {esc(a["window_utc"][0][:10])} to '
            f'{esc(a["window_utc"][1][:10])}, finished {esc(str(a.get("finished_utc", ""))[:16].replace("T", " "))} UTC.</p>'
            + dev_html +
            '<div class="tbl"><table><thead><tr><th>Index</th><th>Result</th><th>Vol / label</th>'
            '<th>MT5 spread</th><th>1 min</th><th>5 min</th><th>15 min</th><th>60 min</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            + ('<p class="muted">Holding-time columns compare your real MT5 spread (median over 24 h) with the '
               'break-even cost for the simplest momentum or reversal rule. <b>No edge</b>: the spread alone is '
               'larger than anything the price pattern could pay, with 95% confidence. <b>Open</b>: the data '
               'cannot rule it out yet. Commission is not included; it only adds cost, so a "no edge" never '
               'turns into an edge when it is added.</p></section>' if mt5 else
               '<p class="muted">The four right-hand columns are break-even costs. If your total cost per round trip '
               '(spread plus commission, as a percentage of price) is above the number, trading the simplest '
               'momentum or reversal rule at that holding time loses money, with 95% confidence. '
               'Import your MT5 snapshot to compare them with your real spread.</p></section>'))


# ---------------------------------------------------------------- serving
def get_token(data: Path) -> str:
    p = data / "config" / "dashboard.json"
    cfg = load_json(p, {}) or {}
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(18)
        save_json(p, cfg)
    return cfg["token"]


def primary_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))          # no packet is sent; this only picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return "YOUR_VPS_IP"


def make_server(data: Path, host: str = "0.0.0.0", port: int = 8787) -> ThreadingHTTPServer:
    token = get_token(data)
    cache = {"t": 0.0, "html": b""}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "dresearch"
        sys_version = ""

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; img-src data:; base-uri 'none'; form-action 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == f"/{token}":
                return self._send(301, b"", extra={"Location": f"/{token}/"})
            if path != f"/{token}/":
                return self._send(404, b"Not found\n")
            with lock:
                if time.time() - cache["t"] > 30:
                    try:
                        cache["html"] = render(gather(data)).encode("utf-8")
                    except Exception as exc:    # show the problem rather than a blank page
                        cache["html"] = (f"<!doctype html><title>Monitor error</title><pre>{esc(exc)}</pre>").encode()
                    cache["t"] = time.time()
                body = cache["html"]
            self._send(200, body, "text/html; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self):
            self._send(405, b"Read-only\n")

    return ThreadingHTTPServer((host, port), Handler)


def dashboard_url(data: Path, port: int) -> str:
    return f"http://{primary_ip()}:{port}/{get_token(data)}/"
