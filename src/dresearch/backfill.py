"""Historical backfill: download past hours of ticks for the configured synthetics.

* Resumable: hours already on disk are skipped, so it can be stopped and restarted.
* Newest hours first, so a partial run still gives every symbol the same recent window.
* Throttled below Deriv's documented limits, leaving headroom for the live collector.
* Writes the same immutable hourly history files the collector uses.
Progress is written to <data>/reports/backfill_progress.json.
"""
from __future__ import annotations

import asyncio
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import DEFAULT_ENDPOINTS, DerivError, backoff_delays, connect_any
from .history import fetch_ticks
from .storage import EventLog, hour_sealed, load_json, save_json, write_history_hour


def plan_hours(now: int, days: int, settle_hours: int = 2) -> list[int]:
    last = (now // 3600 - settle_hours) * 3600
    first = last - (days * 24 - 1) * 3600
    return list(range(last, first - 1, -3600))          # newest first


async def run_backfill(data_dir: Path, days: int, per_minute: int = 120,
                       clock=time.time, verbose=print) -> dict:
    data_dir = Path(data_dir)
    cfg = load_json(data_dir / "config" / "symbols.json", None)
    if not cfg:
        raise SystemExit("No config. Run  dresearch probe  first.")
    symbols = list(cfg.get("watchdog") or [])
    page = int(cfg.get("history_page_size", 1000))
    urls = [cfg["endpoint"]] + [u for u in DEFAULT_ENDPOINTS if u != cfg.get("endpoint")]
    events = EventLog(data_dir)
    hours = plan_hours(int(clock()), days)
    todo = [(h, s) for h in hours for s in symbols if not hour_sealed(data_dir, s, h)]
    total = len(hours) * len(symbols)
    state = {"days": days, "symbols": symbols, "hours_total": total,
             "hours_done_before_start": total - len(todo), "hours_written": 0,
             "hours_empty": 0, "errors": 0, "started_utc": datetime.now(timezone.utc).isoformat()}
    prog_path = data_dir / "reports" / "backfill_progress.json"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    events.log("backfill_start", f"days={days} todo={len(todo)} of {total}")
    verbose(f"Backfill: {days} days x {len(symbols)} symbols = {total} symbol-hours; "
            f"{len(todo)} still to download")
    t0 = time.monotonic()
    i = 0
    delays = backoff_delays(1.0)
    while i < len(todo) and not stop.is_set():
        try:
            client, url, _ = await connect_any(urls, per_minute=per_minute)
        except Exception as exc:
            events.log("backfill_connect_error", str(exc))
            await asyncio.sleep(next(delays))
            continue
        try:
            while i < len(todo) and not stop.is_set():
                h, sym = todo[i]
                try:
                    ticks, pip = await fetch_ticks(client, sym, h, h + 3599, page=page)
                except DerivError as e:
                    state["errors"] += 1
                    events.log("backfill_error", f"{sym} {h} {e}")
                    i += 1
                    continue
                if ticks:
                    write_history_hour(data_dir, sym, h, ticks, pip)
                    state["hours_written"] += 1
                else:
                    state["hours_empty"] += 1      # not available that far back; leave it unsealed
                i += 1
                if i % 50 == 0 or i == len(todo):
                    rate = i / max(1e-9, time.monotonic() - t0)
                    state.update({"hours_processed": i, "hours_remaining": len(todo) - i,
                                  "eta_hours": round((len(todo) - i) / rate / 3600, 1) if rate else None,
                                  "oldest_hour_reached_utc": datetime.fromtimestamp(h, timezone.utc).isoformat(),
                                  "updated_utc": datetime.now(timezone.utc).isoformat()})
                    save_json(prog_path, state)
                    if i % 500 == 0:
                        verbose(f"  {i}/{len(todo)} symbol-hours, ETA {state['eta_hours']} h")
            delays = backoff_delays(1.0)
        except (ConnectionError, asyncio.TimeoutError) as exc:
            events.log("backfill_reconnect", type(exc).__name__)
            await asyncio.sleep(next(delays))
        finally:
            await client.close()
    state["finished"] = i >= len(todo)
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    save_json(prog_path, state)
    events.log("backfill_stop", f"written={state['hours_written']} empty={state['hours_empty']} "
                                f"errors={state['errors']} finished={state['finished']}")
    return state
