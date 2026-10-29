"""24/7 tick recorder for Deriv's public feed. Read-only; places no trades.

Two independent records are kept for every symbol:
  live    - each tick as it arrives on the subscription (with VPS receive time)
  history - each closed UTC hour re-downloaded with ticks_history ~2 minutes later
The history copy repairs any live gap (disconnects, restarts, VPS reboots up to
`lookback_hours` back) and gives an independent check of the live feed.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
import uuid
from pathlib import Path
from typing import Callable

from .client import DEFAULT_ENDPOINTS, DerivError, backoff_delays, connect_any
from .history import fetch_ticks
from .storage import EventLog, LiveWriter, fmt_quote, hour_sealed, load_json, write_history_hour

log = logging.getLogger("dresearch.collector")


class Collector:
    def __init__(self, data_dir: Path, config: dict | None = None,
                 clock: Callable[[], float] = time.time,
                 lookback_hours: int = 48, watchdog_s: float = 30.0,
                 reconcile_every_s: float = 30.0, settle_s: int = 120,
                 backoff_start: float = 1.0):
        self.data_dir = Path(data_dir)
        cfg = config or load_json(self.data_dir / "config" / "symbols.json", None)
        if not cfg or not cfg.get("collect"):
            raise SystemExit("No symbols configured. Run the probe first:  dresearch probe")
        self.cfg = cfg
        self.symbols: list[str] = list(cfg["collect"])
        self.watch = set(cfg.get("watchdog", self.symbols))
        self.page = int(cfg.get("history_page_size", 1000))
        self.urls = [cfg["endpoint"]] + [u for u in DEFAULT_ENDPOINTS if u != cfg.get("endpoint")]
        self.clock = clock
        self.lookback_hours = lookback_hours
        self.watchdog_s = watchdog_s
        self.reconcile_every_s = reconcile_every_s
        self.settle_s = settle_s
        self.backoff_start = backoff_start
        self.writer = LiveWriter(self.data_dir)
        self.events = EventLog(self.data_dir)
        self.session = ""
        self.pip: dict[str, int] = {}
        self.last_tick_mono: dict[str, float] = {}
        self.stop = asyncio.Event()
        self.ticks_written = 0
        self._retry_after: dict[tuple[str, int], float] = {}

    # ---- live stream -------------------------------------------------------
    def on_tick(self, msg: dict) -> None:
        t = msg.get("tick") or {}
        sym, epoch, quote = t.get("symbol"), t.get("epoch"), t.get("quote")
        if sym not in self.symbols or epoch is None or quote is None:
            return
        pip = t.get("pip_size")
        if pip is not None:
            pip = int(pip)
            if self.pip.get(sym) not in (None, pip):
                self.events.log("pip_size_changed", f"{sym} {self.pip.get(sym)}->{pip}")
            self.pip[sym] = pip
        self.writer.write(sym, int(epoch), fmt_quote(float(quote), self.pip.get(sym)),
                          int(time.time() * 1000), self.session)
        self.last_tick_mono[sym] = time.monotonic()
        self.ticks_written += 1

    # ---- hourly history copy ----------------------------------------------
    async def reconcile(self, client) -> int:
        now = int(self.clock())
        cur_hour = now // 3600 * 3600
        if now - cur_hour < self.settle_s:
            cur_hour -= 3600          # previous hour not settled yet
        done = 0
        for h in range(cur_hour - 3600 * self.lookback_hours, cur_hour, 3600):
            for sym in self.symbols:
                if self.stop.is_set() or client.closed.is_set():
                    return done
                if hour_sealed(self.data_dir, sym, h):
                    continue
                if self._retry_after.get((sym, h), 0) > time.monotonic():
                    continue
                try:
                    ticks, pip = await fetch_ticks(client, sym, h, h + 3599, page=self.page)
                except DerivError as e:
                    self.events.log("history_error", f"{sym} {h} {e}")
                    self._retry_after[(sym, h)] = time.monotonic() + 600
                    continue
                except (ConnectionError, asyncio.TimeoutError) as e:
                    self.events.log("history_interrupted", f"{sym} {h} {type(e).__name__}")
                    return done       # connection is gone; the next session resumes
                if not ticks and (sym in self.watch or now - h < 3 * 3600):
                    # synthetics always tick: empty means not available (yet). Retry in 10 min.
                    self._retry_after[(sym, h)] = time.monotonic() + 600
                    continue
                write_history_hour(self.data_dir, sym, h, ticks, pip if pip is not None else self.pip.get(sym))
                done += 1
        if done:
            self.events.log("history_hours_written", str(done))
        return done

    # ---- connection lifecycle ---------------------------------------------
    async def _session(self) -> None:
        client, url, _ = await connect_any(self.urls, on_stream=self.on_tick)
        self.session = uuid.uuid4().hex[:8]
        self.events.log("connected", f"{url} session={self.session}")
        log.info("connected to %s (session %s)", url, self.session)
        try:
            for sym in self.symbols:
                try:
                    await client.request({"ticks": sym, "subscribe": 1})
                except DerivError as e:
                    self.events.log("subscribe_error", f"{sym} {e}")
            start = time.monotonic()
            for sym in self.symbols:
                self.last_tick_mono[sym] = start
            next_reconcile = 0.0
            reconcile_task = None
            while not self.stop.is_set() and not client.closed.is_set():
                now = time.monotonic()
                stale = [s for s in self.watch if now - self.last_tick_mono.get(s, now) > self.watchdog_s]
                if stale:
                    self.events.log("watchdog_reconnect", " ".join(sorted(stale)))
                    break
                if now >= next_reconcile and (reconcile_task is None or reconcile_task.done()):
                    reconcile_task = asyncio.create_task(self.reconcile(client))
                    next_reconcile = now + self.reconcile_every_s
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            if reconcile_task and not reconcile_task.done():
                reconcile_task.cancel()
        finally:
            await client.close()
            self.events.log("disconnected", f"session={self.session}")

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        self.events.log("collector_start", " ".join(self.symbols))
        delays = backoff_delays(self.backoff_start)
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                await self._session()
            except Exception as exc:
                self.events.log("session_error", f"{type(exc).__name__}: {exc}")
                log.warning("session ended: %s", exc)
            if self.stop.is_set():
                break
            if time.monotonic() - t0 > 300:      # a long healthy session resets the backoff
                delays = backoff_delays(self.backoff_start)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=next(delays))
            except asyncio.TimeoutError:
                pass
        self.writer.close()
        self.events.log("collector_stop", f"ticks_written={self.ticks_written}")
