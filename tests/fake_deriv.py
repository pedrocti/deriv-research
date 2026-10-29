"""A local stand-in for Deriv's public WebSocket, used only by the tests.

It mimics the documented message shapes (msg_type, req_id echo, history.times /
history.prices, tick.epoch / tick.quote / pip_size, error.code / error.message)
and the documented "latest ticks first" paging of ticks_history. Its clock is
simulated so hours of feed can be replayed in seconds.
"""
from __future__ import annotations

import asyncio
import json
import math

from websockets.asyncio.server import serve

SYMBOLS = {
    "R_75": {"interval": 2, "pip": 4, "base": 40000.0, "name": "Volatility 75 Index"},
    "1HZ75V": {"interval": 1, "pip": 2, "base": 5000.0, "name": "Volatility 75 (1s) Index"},
    "frxXAUUSD": {"interval": 1, "pip": 2, "base": 2400.0, "name": "Gold/USD"},
    "frxEURUSD": {"interval": 1, "pip": 5, "base": 1.08, "name": "EUR/USD"},
}


def price(sym: str, epoch: int) -> float:
    s = SYMBOLS[sym]
    x = s["base"] * (1 + 0.004 * math.sin(epoch / 37.0) + 0.003 * math.sin(epoch / 11.3)
                     + 0.001 * math.sin(epoch * 0.7311))
    return round(x, s["pip"])


class FakeDeriv:
    def __init__(self, now: int, history_floor: int, page_cap: int = 1000,
                 drop_after_ticks: int | None = None, step_s: float = 0.002):
        self.now = now                    # simulated server time (epoch seconds)
        self.floor = history_floor        # no history before this epoch
        self.page_cap = page_cap
        self.drop_after = drop_after_ticks
        self.step_s = step_s
        self.sent_live = 0
        self.dropped = False
        self.clock_running = False
        self.stop_at = None
        self.conns: dict = {}             # ws -> set(subscribed symbols)
        self.server = None
        self.port = None
        self.requests: list[dict] = []

    # ---- helpers
    def epochs(self, sym, start, end):
        iv = SYMBOLS[sym]["interval"]
        lo = max(start, self.floor)
        lo += (-lo) % iv
        return list(range(lo, min(end, self.now) + 1, iv))

    async def _send(self, ws, obj):
        try:
            await ws.send(json.dumps(obj))
        except Exception:
            pass

    async def handler(self, ws):
        self.conns[ws] = set()
        try:
            async for raw in ws:
                req = json.loads(raw)
                self.requests.append(req)
                rid = req.get("req_id")
                base = {"echo_req": req, "req_id": rid} if rid is not None else {"echo_req": req}
                if "ping" in req:
                    await self._send(ws, {**base, "msg_type": "ping", "ping": "pong"})
                elif "active_symbols" in req:
                    lst = [{"symbol": s, "display_name": v["name"], "pip": 10 ** -v["pip"],
                            "exchange_is_open": 1, "market": "synthetic_index" if "V" in s or s.startswith("R_") else "forex"}
                           for s, v in SYMBOLS.items()]
                    await self._send(ws, {**base, "msg_type": "active_symbols", "active_symbols": lst})
                elif "ticks_history" in req:
                    sym = req["ticks_history"]
                    if sym not in SYMBOLS:
                        await self._send(ws, {**base, "msg_type": "history",
                                              "error": {"code": "InvalidSymbol", "message": "Symbol invalid"}})
                        continue
                    end = self.now if req.get("end") in (None, "latest") else int(req["end"])
                    start = int(req.get("start", end - 86400))
                    count = min(int(req.get("count", 1000)), self.page_cap)
                    ep = self.epochs(sym, start, end)[-count:]       # latest ticks first, as documented
                    await self._send(ws, {**base, "msg_type": "history", "pip_size": SYMBOLS[sym]["pip"],
                                          "history": {"times": ep, "prices": [price(sym, e) for e in ep]}})
                elif "ticks" in req:
                    sym = req["ticks"]
                    self.conns[ws].add(sym)
                    e = self.now
                    await self._send(ws, {**base, "msg_type": "tick", "subscription": {"id": f"sub-{sym}"},
                                          "tick": {"symbol": sym, "epoch": e, "quote": price(sym, e),
                                                   "pip_size": SYMBOLS[sym]["pip"]}})
                else:
                    await self._send(ws, {**base, "error": {"code": "UnrecognisedRequest", "message": "?"}})
        finally:
            self.conns.pop(ws, None)

    async def clock(self):
        """Advance simulated time one second per step; push ticks to subscribers."""
        self.clock_running = True
        while self.stop_at is None or self.now < self.stop_at:
            await asyncio.sleep(self.step_s)
            self.now += 1
            for ws, syms in list(self.conns.items()):
                for sym in syms:
                    if self.now % SYMBOLS[sym]["interval"]:
                        continue
                    await self._send(ws, {"msg_type": "tick", "subscription": {"id": f"sub-{sym}"},
                                          "tick": {"symbol": sym, "epoch": self.now,
                                                   "quote": price(sym, self.now), "pip_size": SYMBOLS[sym]["pip"]}})
                    self.sent_live += 1
            if self.drop_after and not self.dropped and self.sent_live >= self.drop_after:
                self.dropped = True
                for ws in list(self.conns):
                    await ws.close()
        self.clock_running = False

    async def start(self):
        self.server = await serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{self.port}"

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()
