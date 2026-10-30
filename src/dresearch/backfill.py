"""Historical backfill: download past hours of ticks for the configured synthetics.

* Resumable: hours already on disk are skipped, so it can be stopped and restarted.
* Newest hours first, so a partial run still gives every symbol the same recent window.
* Throttled below Deriv's documented limits, leaving headroom for the live collector.
* Deriv also applies an UNDOCUMENTED limit to older history ("ticks_history_expensive",
  observed 29-30 Oct 2026 for data older than about 60 days). On any rate-limit reply the
  downloader waits (30 s doubling to 15 min), halves its request rate, and retries the
  SAME hour; it speeds up again slowly after a run of successes. A rate-limited hour is
  never skipped. Other errors are retried 3 times before the hour is left for a re-run.
* Writes the same immutable hourly history files the collector uses.
Progress is written to <data>/reports/backfill_progress.json.
"""
from __future__ import annotations

import asyncio
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from collections import deque

from .client import DEFAULT_ENDPOINTS, DerivError, RateLimiter, backoff_delays, connect_any
from .history import fetch_ticks
from .storage import EventLog, hour_sealed, load_json, save_json, write_history_hour


def plan_hours(now: int, days: int, settle_hours: int = 2) -> list[int]:
    last = (now // 3600 - settle_hours) * 3600
    first = last - (days * 24 - 1) * 3600
    return list(range(last, first - 1, -3600))          # newest first


def is_rate_limit(e: DerivError) -> bool:
    return "ratelimit" in e.code.lower().replace("_", "") or "rate limit" in e.message.lower()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_backfill(data_dir: Path, days: int, per_minute: int = 120,
                       clock=time.time, verbose=print, backoff_start: float = 30.0,
                       backoff_max: float = 900.0, min_rate: float = 2.0) -> dict:
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
             "hours_empty": 0, "errors": 0, "hours_skipped": 0, "rate_limited": 0,
             "status": "running", "started_utc": datetime.now(timezone.utc).isoformat()}
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

    rate = float(per_minute)
    wait = backoff_start
    ok_streak = 0
    tries: dict[int, int] = {}
    recent = deque(maxlen=200)             # completion times, for an honest ETA
    i = 0

    def save(h=None):
        done_rate = (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) > 1 and recent[-1] > recent[0] else 0
        state.update({"hours_processed": i, "hours_remaining": len(todo) - i,
                      "eta_hours": round((len(todo) - i) / done_rate / 3600, 1) if done_rate else None,
                      "requests_per_minute_now": round(rate, 1),
                      "updated_utc": datetime.now(timezone.utc).isoformat()})
        if h is not None:
            state["oldest_hour_reached_utc"] = datetime.fromtimestamp(h, timezone.utc).isoformat()
        save_json(prog_path, state)

    delays = backoff_delays(1.0)
    while i < len(todo) and not stop.is_set():
        try:
            client, url, _ = await connect_any(urls, per_minute=max(1, int(rate)))
            client.limiter = RateLimiter(rate, burst=1)
        except Exception as exc:
            events.log("backfill_connect_error", str(exc))
            await _sleep_or_stop(stop, next(delays))
            continue
        try:
            while i < len(todo) and not stop.is_set():
                h, sym = todo[i]

                async def on_limit(e, sym=sym, h=h):
                    nonlocal rate, wait, ok_streak
                    if stop.is_set():
                        raise ConnectionError("stopping")
                    state["rate_limited"] += 1
                    rate = max(min_rate, rate / 2)
                    client.limiter = RateLimiter(rate, burst=1)
                    ok_streak = 0
                    state["status"] = f"waiting {wait:.0f}s (Deriv rate limit)"
                    events.log("backfill_rate_limited", f"{sym} {h} wait={wait:.0f}s rate={rate:.1f}/min")
                    save(h)
                    await _sleep_or_stop(stop, wait)
                    wait = min(backoff_max, wait * 2)
                    state["status"] = "running"

                try:
                    ticks, pip = await fetch_ticks(client, sym, h, h + 3599, page=page, on_rate_limit=on_limit)
                except DerivError as e:
                    tries[i] = tries.get(i, 0) + 1
                    events.log("backfill_error", f"{sym} {h} try={tries[i]} {e}")
                    if tries[i] < 3:
                        await _sleep_or_stop(stop, 5)
                        continue
                    state["errors"] += 1
                    state["hours_skipped"] += 1       # left unsealed: a re-run retries it
                    i += 1
                    continue
                if ticks:
                    write_history_hour(data_dir, sym, h, ticks, pip)
                    state["hours_written"] += 1
                else:
                    state["hours_empty"] += 1      # not available that far back; leave it unsealed
                i += 1
                recent.append(time.monotonic())
                wait = backoff_start
                ok_streak += 1
                if ok_streak >= 20 and rate < per_minute:
                    rate = min(float(per_minute), rate * 1.25)
                    client.limiter = RateLimiter(rate, burst=1)
                    ok_streak = 0
                if i % 25 == 0 or i == len(todo):
                    save(h)
                    if i % 500 == 0:
                        verbose(f"  {i}/{len(todo)} symbol-hours, ETA {state['eta_hours']} h, {rate:.0f} req/min")
            delays = backoff_delays(1.0)
        except (ConnectionError, asyncio.TimeoutError) as exc:
            events.log("backfill_reconnect", type(exc).__name__)
            await _sleep_or_stop(stop, next(delays))
        finally:
            await client.close()
    state["finished"] = i >= len(todo) and state["hours_skipped"] == 0
    state["status"] = "finished" if state["finished"] else ("stopped" if i < len(todo) else
                                                            "finished with skipped hours: re-run to retry them")
    save()
    events.log("backfill_stop", f"written={state['hours_written']} empty={state['hours_empty']} "
                                f"skipped={state['hours_skipped']} rate_limited={state['rate_limited']} "
                                f"finished={state['finished']}")
    return state
