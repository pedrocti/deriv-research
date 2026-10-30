"""Paginated tick-history download.

Deriv's docs say ticks_history "will always return the latest possible set of
ticks" matching the request, so a range larger than one page must be walked
backwards from the end. We never assume the page size the server honours:
we stop only when we reach `start` or a page adds nothing new.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional, Union

from .client import DerivClient, DerivError


async def fetch_ticks(client: DerivClient, symbol: str, start: int,
                      end: Union[int, str], page: int = 5000, max_pages: int = 100_000,
                      on_rate_limit: Optional[Callable[[DerivError], Awaitable[None]]] = None,
                      ) -> tuple[dict[int, float], int | None]:
    """If `on_rate_limit` is given, a rate-limit refusal awaits it and then retries the SAME
    page, keeping pages already received (restarting the range could livelock under a
    strict limit). Without it, the refusal is raised as before."""
    from .backfill import is_rate_limit
    out: dict[int, float] = {}
    pip = None
    cur_end: Union[int, str] = end
    for _ in range(max_pages):
        while True:
            try:
                resp = await client.request({"ticks_history": symbol, "start": int(start),
                                             "end": cur_end, "count": int(page), "style": "ticks"})
                break
            except DerivError as e:
                if on_rate_limit is None or not is_rate_limit(e):
                    raise
                await on_rate_limit(e)
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
