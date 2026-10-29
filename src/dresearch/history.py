"""Paginated tick-history download.

Deriv's docs say ticks_history "will always return the latest possible set of
ticks" matching the request, so a range larger than one page must be walked
backwards from the end. We never assume the page size the server honours:
we stop only when we reach `start` or a page adds nothing new.
"""
from __future__ import annotations

from typing import Union

from .client import DerivClient


async def fetch_ticks(client: DerivClient, symbol: str, start: int,
                      end: Union[int, str], page: int = 5000,
                      max_pages: int = 100_000) -> tuple[dict[int, float], int | None]:
    out: dict[int, float] = {}
    pip = None
    cur_end: Union[int, str] = end
    for _ in range(max_pages):
        resp = await client.request({"ticks_history": symbol, "start": int(start),
                                     "end": cur_end, "count": int(page), "style": "ticks"})
        hist = resp.get("history") or {}
        times = hist.get("times") or []
        prices = hist.get("prices") or []
        if resp.get("pip_size") is not None:
            pip = int(resp["pip_size"])
        before = len(out)
        upper = cur_end if isinstance(cur_end, int) else None
        for t, p in zip(times, prices):
            t = int(t)
            if t >= start and (upper is None or t <= upper):
                out[t] = float(p)
        if not times or len(out) == before:
            break
        earliest = min(int(t) for t in times)
        if earliest <= start:
            break
        cur_end = earliest - 1
    return out, pip
