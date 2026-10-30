"""Minimal, defensive client for Deriv's public market-data WebSocket.

Read-only by design: this module has no code path that can place a trade.
It follows Deriv's published guidance: ping every 30 s, stay under the
documented rate limits, reconnect with exponential backoff + jitter.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import time
from typing import Awaitable, Callable, Optional

import websockets

log = logging.getLogger("dresearch.client")

# Both URLs appear in Deriv's current documentation; the probe records which works.
DEFAULT_ENDPOINTS = [
    "wss://api.derivws.com/trading/v1/options/ws/public",
    "wss://ws.binaryws.com/websockets/v3",
]

# Calls that could move money or touch an account. Refused unconditionally.
FORBIDDEN_CALLS = {
    "buy", "sell", "proposal", "authorize", "transfer_between_accounts",
    "cashier", "paymentagent_transfer", "mt5_new_account", "mt5_deposit",
    "mt5_withdrawal", "cancel", "contract_update", "sell_expired", "topup_virtual",
}


class DerivError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RateLimiter:
    """Token bucket. Deriv documents 220 data calls/minute; we stay below that."""

    def __init__(self, per_minute: float = 180, burst: float | None = None):
        # burst=None allows a minute's worth at once; burst=1 spaces requests evenly
        self.capacity = max(1.0, float(per_minute) if burst is None else float(burst))
        self.tokens = self.capacity
        self.rate = per_minute / 60.0
        self.t = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
            self.t = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep((1 - self.tokens) / self.rate)


StreamHandler = Callable[[dict], None]


class DerivClient:
    def __init__(self, url: str, on_stream: Optional[StreamHandler] = None,
                 per_minute: int = 180, ping_interval: float = 30.0,
                 request_timeout: float = 60.0):
        self.url = url
        self.on_stream = on_stream
        self.limiter = RateLimiter(per_minute)
        self.ping_interval = ping_interval
        self.request_timeout = request_timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self.ws = None
        self.closed = asyncio.Event()
        self.last_message = time.monotonic()

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.url, open_timeout=15, ping_interval=None,
                                           max_size=2 ** 25, close_timeout=5)
        self._tasks = [asyncio.create_task(self._read_loop()),
                       asyncio.create_task(self._ping_loop())]

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        self._fail_pending(ConnectionError("client closed"))
        self.closed.set()

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _read_loop(self) -> None:
        try:
            async for raw in self.ws:
                self.last_message = time.monotonic()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("non-JSON message ignored")
                    continue
                mtype = msg.get("msg_type")
                if mtype in ("tick", "ohlc") and self.on_stream is not None and "error" not in msg:
                    try:
                        self.on_stream(msg)
                    except Exception:  # a bad handler must not kill the connection
                        log.exception("stream handler failed")
                rid = msg.get("req_id")
                fut = self._pending.pop(rid, None) if rid is not None else None
                if fut is not None and not fut.done():
                    err = msg.get("error")
                    if err:
                        fut.set_exception(DerivError(str(err.get("code")), str(err.get("message"))))
                    else:
                        fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("connection lost: %s", exc)
        finally:
            self._fail_pending(ConnectionError("connection lost"))
            self.closed.set()

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                await self.ws.send(json.dumps({"ping": 1}))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def request(self, payload: dict) -> dict:
        call = next(iter(payload))
        if any(k in FORBIDDEN_CALLS for k in payload):
            raise PermissionError(f"refused: '{call}' is not a read-only market-data call")
        if self.closed.is_set():
            raise ConnectionError("not connected")
        await self.limiter.acquire()
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self.ws.send(json.dumps({**payload, "req_id": rid}))
        return await asyncio.wait_for(fut, self.request_timeout)


async def connect_any(urls: list[str], on_stream: Optional[StreamHandler] = None,
                      **kw) -> tuple[DerivClient, str, list[dict]]:
    """Try each URL in order; return the first that answers a ping."""
    attempts = []
    for url in urls:
        client = DerivClient(url, on_stream=on_stream, **kw)
        try:
            await client.connect()
            await asyncio.wait_for(client.request({"ping": 1}), 15)
            attempts.append({"url": url, "ok": True})
            return client, url, attempts
        except Exception as exc:
            attempts.append({"url": url, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            await client.close()
    raise ConnectionError(f"no endpoint reachable: {attempts}")


def backoff_delays(start: float = 1.0, cap: float = 60.0):
    """Exponential backoff with full jitter, as Deriv's best-practice guide recommends."""
    d = start
    while True:
        yield random.uniform(d / 2, d)
        d = min(cap, d * 2)
