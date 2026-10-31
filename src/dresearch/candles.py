"""Track B data: free candle history for gold and crypto from Deriv's public feed.

`dresearch candles` finds spot gold (frxXAU...) and Bitcoin / Ethereum (cryBTCUSD,
cryETHUSD) in active_symbols, then downloads:
  * daily candles, as far back as Deriv serves them (paged backwards);
  * hourly candles for the last `hourly_days` days.
Files: <data>/candles/<SYMBOL>_<granularity>.csv  (epoch,open,high,low,close; UTC)
Report: <data>/reports/candles_<UTC>.json with years of history and gap counts.

Read-only, like everything else here. Rate-limit refusals are waited out and the
same page retried (see backfill.py for why).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import DEFAULT_ENDPOINTS, DerivError, connect_any
from .storage import EventLog, load_json, save_json

TRACK_B = [re.compile(r"^frxXAU[A-Z]{3}$"), re.compile(r"^cry(BTC|ETH)USD$")]
PREFERRED = ["frxXAUUSD", "cryBTCUSD", "cryETHUSD"]


def pick_symbols(entries: list[dict]) -> list[str]:
    syms = [e.get("symbol") or e.get("underlying_symbol") for e in entries]
    found = [s for s in syms if s and any(rx.match(s) for rx in TRACK_B)]
    return [s for s in PREFERRED if s in found] + sorted(set(found) - set(PREFERRED))


async def fetch_candles(client, symbol: str, granularity: int, start: int = 1,
                        page: int = 5000, max_pages: int = 2000, on_rate_limit=None) -> dict[int, tuple]:
    """All candles from `start` to now, newest first, paged backwards."""
    from .backfill import is_rate_limit
    out: dict[int, tuple] = {}
    end: int | str = "latest"
    for _ in range(max_pages):
        while True:
            try:
                resp = await client.request({"ticks_history": symbol, "style": "candles",
                                             "granularity": granularity, "start": int(start),
                                             "end": end, "count": page})
                break
            except DerivError as e:
                if on_rate_limit is None or not is_rate_limit(e):
                    raise
                await on_rate_limit(e)
        rows = resp.get("candles") or []
        before = len(out)
        for c in rows:
            ep = int(c["epoch"])
            if ep >= start and (end == "latest" or ep <= end):
                out[ep] = (float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]))
        if not rows or len(out) == before:
            break
        earliest = min(int(c["epoch"]) for c in rows)
        if earliest <= start:
            break
        end = earliest - 1
    return out


def write_candles(path: Path, candles: dict[int, tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("epoch,open,high,low,close\n")
        for ep in sorted(candles):
            o, h, l, c = candles[ep]
            fh.write(f"{ep},{o!r},{h!r},{l!r},{c!r}\n")
    os.replace(tmp, path)


def describe(candles: dict[int, tuple], granularity: int) -> dict:
    if not candles:
        return {"n": 0}
    eps = sorted(candles)
    steps = [b - a for a, b in zip(eps, eps[1:])]
    return {"n": len(eps),
            "first_utc": datetime.fromtimestamp(eps[0], timezone.utc).isoformat(),
            "last_utc": datetime.fromtimestamp(eps[-1], timezone.utc).isoformat(),
            "years": round((eps[-1] - eps[0]) / (365.25 * 86400), 2),
            "gaps_over_1_step": sum(1 for s in steps if s > granularity),
            "longest_gap_days": round(max(steps) / 86400, 1) if steps else 0}


async def run_candles(data_dir: Path, hourly_days: int = 730, verbose=print,
                      urls: list[str] | None = None, wait_start: float = 30.0) -> dict:
    data_dir = Path(data_dir)
    cfg = load_json(data_dir / "config" / "symbols.json", {}) or {}
    urls = urls or ([cfg["endpoint"]] if cfg.get("endpoint") else []) + DEFAULT_ENDPOINTS
    events = EventLog(data_dir)
    client, url, _ = await connect_any(urls, per_minute=60)
    report: dict = {"started_utc": datetime.now(timezone.utc).isoformat(), "endpoint": url, "symbols": {}}
    wait = {"s": wait_start}

    async def on_limit(e):
        events.log("candles_rate_limited", f"wait={wait['s']:.0f}s")
        verbose(f"  Deriv rate limit: waiting {wait['s']:.0f}s, then retrying the same request")
        await asyncio.sleep(wait["s"])
        wait["s"] = min(900.0, wait["s"] * 2)

    try:
        resp = await client.request({"active_symbols": "brief"})
        syms = pick_symbols(resp.get("active_symbols") or [])
        report["symbols_found"] = syms
        verbose(f"Gold/crypto symbols on Deriv's feed: {', '.join(syms) or 'none found'}")
        for sym in syms:
            report["symbols"][sym] = {}
            for g, start in ((86400, 1), (3600, int(time.time()) - hourly_days * 86400)):
                label = "daily" if g == 86400 else "hourly"
                verbose(f"  {sym}: downloading {label} candles ...")
                try:
                    c = await fetch_candles(client, sym, g, start=start, on_rate_limit=on_limit)
                except DerivError as e:
                    report["symbols"][sym][label] = {"error": str(e)}
                    verbose(f"    error: {e}")
                    continue
                wait["s"] = wait_start
                write_candles(data_dir / "candles" / f"{sym}_{g}.csv", c)
                d = describe(c, g)
                report["symbols"][sym][label] = d
                verbose(f"    {d.get('n', 0):,} candles, {d.get('first_utc', '-')[:10]} to "
                        f"{d.get('last_utc', '-')[:10]} ({d.get('years', 0)} years)")
    finally:
        await client.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_json(data_dir / "reports" / f"candles_{stamp}.json", report)
    return report
