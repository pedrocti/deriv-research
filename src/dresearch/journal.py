"""Demo trading journal (the "coach" track), stored on Pedro's own server.

Why it exists: three preregistered tests found no edge (see HANDOFF.md), so Pedro now practises
discipline on a DEMO account under the rules of a typical two-step prop-firm challenge. The page
(journal_page.html) checks each planned trade against those rules before he enters it in MT5 and
records every break. Data lives here, not with any AI provider, so any agent can continue.

Files, all under <data>/journal/:
  settings.json   {"accountSize": 5000, "attemptStartedAt": ISO, "attempts": [{startedAt, endedAt, result}]}
  trades.json     list of trades (fields below)
  changes.jsonl   append-only record of every write (nothing is lost silently)
  shots/          screenshots, named <utc-stamp>-<random>.jpg|png|webp

Trade fields: id, created (ISO), market (XAUUSD|ETHUSD), dir (+1 buy, -1 sell), entry, stop, target,
lots, risk (USD lost if the stop is hit), exitBy, reason, shots [names], planBreaks [text],
status open|closed; when closed: exit, pnl (USD from MT5, after costs), closedAt, closeBreaks [text],
note, followed (true if no breaks at all).

The challenge rules (percent of accountSize) are duplicated in the page; keep both in step:
risk <= 0.5%/trade; one open trade; <= 2 trades/day; stop for the day after 2 losses;
fail at -5% in a day or -10% total; phase 1 target +8%, phase 2 +5%, each >= 4 trading days.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCK = threading.Lock()
SHOT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
SHOT_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_SHOT = 6 * 1024 * 1024
MAX_JSON = 256 * 1024
TRADE_FIELDS = {"market", "dir", "entry", "stop", "target", "lots", "risk", "exitBy", "reason", "created",
                "status", "planBreaks", "shots", "exit", "pnl", "closedAt", "closeBreaks", "note", "followed"}
LAGOS = timezone(timedelta(hours=1))           # Pedro's clock; trading days are counted in it


def jdir(data: Path) -> Path:
    d = Path(data) / "journal"
    (d / "shots").mkdir(parents=True, exist_ok=True)
    return d


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _log(d: Path, what: str, body) -> None:
    with open(d / "changes.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": datetime.now(timezone.utc).isoformat(), "op": what, "body": body},
                            ensure_ascii=False) + "\n")


def state(data: Path) -> dict:
    d = jdir(data)
    return {"settings": _read(d / "settings.json", {}), "trades": _read(d / "trades.json", [])}


def _clean(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k in TRADE_FIELDS}
    for k in ("shots", "planBreaks", "closeBreaks"):
        if k in out:
            if not isinstance(out[k], list):
                raise ValueError(f"{k} must be a list")
            out[k] = [str(x)[:300] for x in out[k]][:20]
    for n in out.get("shots", []):
        if not SHOT_RE.match(n):
            raise ValueError("bad screenshot name")
    for k in ("reason", "note"):
        if k in out:
            out[k] = str(out[k])[:1000]
    return out


def apply(data: Path, op: str, body: dict) -> dict:
    """One write from the page: trade | update | delete | settings."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    d = jdir(data)
    with LOCK:
        trades = _read(d / "trades.json", [])
        if op == "trade":
            t = _clean(body)
            t["id"] = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
            trades.append(t)
            _write(d / "trades.json", trades)
            result = t
        elif op in ("update", "delete"):
            tid = str(body.get("id", ""))
            idx = next((i for i, t in enumerate(trades) if t.get("id") == tid), None)
            if idx is None:
                raise KeyError("no such trade")
            if op == "update":
                trades[idx].update(_clean(body.get("patch") or {}))
                result = trades[idx]
            else:
                result = trades.pop(idx)
            _write(d / "trades.json", trades)
        elif op == "settings":
            s = {"accountSize": float(body.get("accountSize", 5000)),
                 "attemptStartedAt": str(body.get("attemptStartedAt", "1970-01-01T00:00:00.000Z"))[:40],
                 "attempts": [{k: str(a.get(k, ""))[:40] for k in ("startedAt", "endedAt", "result")}
                              for a in (body.get("attempts") or [])][:500]}
            if not 500 <= s["accountSize"] <= 1_000_000:
                raise ValueError("account size must be between 500 and 1,000,000")
            _write(d / "settings.json", s)
            result = s
        else:
            raise KeyError("unknown operation")
        _log(d, op, body)
        return result


def save_shot(data: Path, ctype: str, raw: bytes) -> str:
    ext = SHOT_TYPES.get(ctype.split(";")[0].strip().lower())
    if not ext:
        raise ValueError("screenshots must be JPEG, PNG or WebP")
    if not raw or len(raw) > MAX_SHOT:
        raise ValueError("screenshot is empty or over 6 MB")
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4) + "." + ext
    (jdir(data) / "shots" / name).write_bytes(raw)
    return name


def shot_path(data: Path, name: str) -> Path | None:
    if not SHOT_RE.match(name):
        return None
    p = jdir(data) / "shots" / name
    return p if p.is_file() else None


def export_csv(data: Path) -> str:
    cols = ["opened", "closed", "market", "direction", "lots", "entry", "stop", "target", "exit",
            "risk_usd", "pnl_usd", "R", "rules_kept", "breaks", "reason", "note", "screenshots"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for t in sorted(state(data)["trades"], key=lambda t: t.get("created", "")):
        closed = t.get("status") == "closed"
        r = (t["pnl"] / t["risk"]) if closed and t.get("risk") else ""
        w.writerow([t.get("created"), t.get("closedAt", ""), t.get("market"), "buy" if t.get("dir", 1) > 0 else "sell",
                    t.get("lots"), t.get("entry"), t.get("stop"), t.get("target"), t.get("exit", ""),
                    round(t.get("risk") or 0, 2), t.get("pnl", ""), round(r, 2) if r != "" else "",
                    ("yes" if t.get("followed") else "no") if closed else "open",
                    "; ".join((t.get("planBreaks") or []) + (t.get("closeBreaks") or [])),
                    t.get("reason", ""), t.get("note", ""), " ".join(t.get("shots") or [])])
    return buf.getvalue()


# ---------- review (for Pedro's weekly review, by any agent) ----------

def _day(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LAGOS).date().isoformat()


def challenge(st: dict) -> dict:
    s = {"accountSize": 5000.0, "attemptStartedAt": "1970", "attempts": [], **(st.get("settings") or {})}
    S = float(s["accountSize"])
    mine = [t for t in st["trades"] if t.get("created", "") >= s["attemptStartedAt"]]
    closed = sorted((t for t in mine if t.get("status") == "closed"), key=lambda t: t["closedAt"])
    bal, phase, start, days, daily, result, reason = S, 1, S, set(), {}, "running", ""
    for t in closed:
        bal += t["pnl"]
        cd = _day(t["closedAt"])
        daily[cd] = daily.get(cd, 0) + t["pnl"]
        days.add(_day(t["created"]))
        if daily[cd] <= -0.05 * S:
            result, reason = "failed", f"daily loss limit hit on {cd}"
            break
        if bal <= 0.9 * S:
            result, reason = "failed", "total loss limit hit"
            break
        if phase == 1 and bal - start >= 0.08 * S and len(days) >= 4:
            phase, start, days = 2, bal, set()
        elif phase == 2 and bal - start >= 0.05 * S and len(days) >= 4:
            result = "passed"
            break
    streak = 0
    for a in reversed(s["attempts"]):
        if a.get("result") != "passed":
            break
        streak += 1
    return {"attempt": len(s["attempts"]) + 1, "phase": phase, "balance": round(bal, 2), "result": result,
            "reason": reason, "account": S, "passes_in_a_row": streak}


def summary(st: dict) -> dict:
    closed = [t for t in st["trades"] if t.get("status") == "closed" and t.get("risk")]
    n = len(closed)
    R = [t["pnl"] / t["risk"] for t in closed]
    kept = [t for t in closed if t.get("followed")]
    weeks: dict[str, list] = {}
    for t in closed:
        d = datetime.fromisoformat(_day(t["closedAt"]))
        weeks.setdefault((d - timedelta(days=d.weekday())).date().isoformat(), []).append(t)
    breaks: dict[str, int] = {}
    for t in closed:
        for b in (t.get("planBreaks") or []) + (t.get("closeBreaks") or []):
            key = re.sub(r"[-$0-9.,()%]+", "#", b)
            breaks[key] = breaks.get(key, 0) + 1
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    return {"challenge": challenge(st), "closed": n, "open": sum(t.get("status") == "open" for t in st["trades"]),
            "rules_kept_pct": round(100 * len(kept) / n, 1) if n else None,
            "win_rate_pct": round(100 * sum(t["pnl"] > 0 for t in closed) / n, 1) if n else None,
            "avg_R": avg(R), "total_pnl": round(sum(t["pnl"] for t in closed), 2),
            "avg_R_rules_kept": avg([t["pnl"] / t["risk"] for t in kept]),
            "avg_R_rules_broken": avg([t["pnl"] / t["risk"] for t in closed if not t.get("followed")]),
            "by_market": {m: {"n": len(xs), "avg_R": avg([t["pnl"] / t["risk"] for t in xs])}
                          for m in {t["market"] for t in closed}
                          for xs in [[t for t in closed if t["market"] == m]]},
            "weeks": {k: {"n": len(v), "pnl": round(sum(t["pnl"] for t in v), 2),
                          "avg_R": avg([t["pnl"] / t["risk"] for t in v]),
                          "rules_kept_pct": round(100 * sum(bool(t.get("followed")) for t in v) / len(v), 1)}
                      for k, v in sorted(weeks.items())},
            "common_breaks": sorted(breaks.items(), key=lambda kv: -kv[1])[:8]}


def report(data: Path) -> str:
    st = state(data)
    s = summary(st)
    c = s["challenge"]
    f = lambda x: "-" if x is None else f"{x:+.2f}R"
    out = ["=== DEMO JOURNAL REVIEW ===",
           f"attempt {c['attempt']}  phase {c['phase']}  {c['result'].upper()}{' (' + c['reason'] + ')' if c['reason'] else ''}"
           f"  balance ${c['balance']:,.2f} of ${c['account']:,.0f}  passes in a row: {c['passes_in_a_row']}/2",
           f"closed trades {s['closed']}  open {s['open']}  rules kept {s['rules_kept_pct']}%  win rate {s['win_rate_pct']}%"
           f"  avg {f(s['avg_R'])}  total ${s['total_pnl']:+,.2f}",
           f"avg when rules kept {f(s['avg_R_rules_kept'])}   when broken {f(s['avg_R_rules_broken'])}"]
    for m, v in sorted(s["by_market"].items()):
        out.append(f"  {m}: {v['n']} trades, avg {f(v['avg_R'])}")
    out.append("weeks (Monday):")
    for k, v in s["weeks"].items():
        out.append(f"  {k}: {v['n']:>3} trades  ${v['pnl']:+9,.2f}  {f(v['avg_R'])}  rules kept {v['rules_kept_pct']}%")
    if s["common_breaks"]:
        out.append("most common rule breaks:")
        out += [f"  {n}x  {b}" for b, n in s["common_breaks"]]
    out.append("last 10 trades:")
    for t in sorted(st["trades"], key=lambda t: t.get("created", ""))[-10:]:
        res = (f"${t['pnl']:+.2f} ({t['pnl'] / t['risk']:+.2f}R)" if t.get("status") == "closed" and t.get("risk")
               else "OPEN")
        out.append(f"  {t.get('created', '')[:16]} {'BUY' if t.get('dir', 1) > 0 else 'SELL'} {t.get('market')} "
                   f"{t.get('lots')} @ {t.get('entry')} stop {t.get('stop')}  {res}"
                   f"{'  BROKE: ' + '; '.join((t.get('planBreaks') or []) + (t.get('closeBreaks') or [])) if not t.get('followed') and t.get('status') == 'closed' else ''}")
        if t.get("reason"):
            out.append(f"      why: {t['reason'][:200]}")
        if t.get("shots"):
            out.append(f"      screenshots: {', '.join(str(jdir(data) / 'shots' / n) for n in t['shots'])}")
    return "\n".join(out)
