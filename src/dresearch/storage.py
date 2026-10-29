"""Raw-data storage: append-only hourly CSV files, never edited after writing.

Layout (all times UTC):
  <data>/raw/live/<SYMBOL>/<YYYY-MM-DD>/<HH>.csv       ticks as received
  <data>/raw/history/<SYMBOL>/<YYYY-MM-DD>/<HH>.csv    same hour re-downloaded later
  <data>/raw/MANIFEST.jsonl                            sha256 of every sealed file
  <data>/logs/events.csv                               connects, drops, repairs
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

LIVE_HEADER = "epoch,quote,recv_ms,session\n"
HIST_HEADER = "epoch,quote,fetched_ms\n"


def hour_parts(epoch: int) -> tuple[str, str]:
    dt = datetime.fromtimestamp(int(epoch), timezone.utc)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H")


def fmt_quote(q: float, pip: int | None) -> str:
    return f"{q:.{pip}f}" if pip is not None else repr(float(q))


class LiveWriter:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "raw" / "live"
        self.files: dict[tuple[str, str, str], io.TextIOWrapper] = {}

    def write(self, symbol: str, epoch: int, quote: str, recv_ms: int, session: str) -> None:
        day, hh = hour_parts(epoch)
        key = (symbol, day, hh)
        fh = self.files.get(key)
        if fh is None:
            p = self.root / symbol / day / f"{hh}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            new = not p.exists() or p.stat().st_size == 0
            fh = open(p, "a", encoding="utf-8", buffering=1)
            if new:
                fh.write(LIVE_HEADER)
            self.files[key] = fh
            self._close_stale(symbol, (day, hh))
        fh.write(f"{int(epoch)},{quote},{recv_ms},{session}\n")

    def _close_stale(self, symbol: str, current: tuple[str, str]) -> None:
        for key in [k for k in self.files if k[0] == symbol and (k[1], k[2]) < current]:
            self.files.pop(key).close()

    def close(self) -> None:
        for fh in self.files.values():
            fh.close()
        self.files.clear()


def history_path(data_dir: Path, symbol: str, hour_start: int) -> Path:
    day, hh = hour_parts(hour_start)
    return Path(data_dir) / "raw" / "history" / symbol / day / f"{hh}.csv"


def hour_sealed(data_dir: Path, symbol: str, hour_start: int) -> bool:
    p = history_path(data_dir, symbol, hour_start)
    return p.exists() or p.with_suffix(".csv.gz").exists()


def write_history_hour(data_dir: Path, symbol: str, hour_start: int,
                       ticks: dict[int, float], pip: int | None) -> Path:
    """Write a whole hour atomically (temp file + rename) so a crash never leaves half a file."""
    p = history_path(data_dir, symbol, hour_start)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    now_ms = int(time.time() * 1000)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(HIST_HEADER)
        for t in sorted(ticks):
            fh.write(f"{t},{fmt_quote(ticks[t], pip)},{now_ms}\n")
    os.replace(tmp, p)
    return p


def read_rows(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def day_files(data_dir: Path, source: str, symbol: str, day: str) -> list[Path]:
    d = Path(data_dir) / "raw" / source / symbol / day
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.name.endswith((".csv", ".csv.gz")))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def seal_closed_days(data_dir: Path, today: str) -> int:
    """Gzip every raw .csv belonging to a day before `today` (UTC) and record its hash.

    Runs over all past days, so files that arrive late (e.g. a delayed history
    repair) are sealed on the next run. Returns the number of files sealed.
    """
    raw = Path(data_dir) / "raw"
    manifest = raw / "MANIFEST.jsonl"
    n = 0
    for p in sorted(raw.glob("*/*/*/*.csv")):
        if p.parent.name >= today:
            continue
        gz = p.with_suffix(".csv.gz")
        with open(p, "rb") as src, gzip.open(gz, "wb") as dst:
            dst.write(src.read())
        rec = {"file": str(gz.relative_to(raw)), "sha256": sha256(gz),
               "bytes": gz.stat().st_size, "sealed_utc": datetime.now(timezone.utc).isoformat()}
        with open(manifest, "a", encoding="utf-8") as m:
            m.write(json.dumps(rec) + "\n")
        p.unlink()
        n += 1
    return n


class EventLog:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "logs" / "events.csv"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("utc,event,detail\n", encoding="utf-8")

    def log(self, event: str, detail: str = "") -> None:
        detail = detail.replace("\n", " ").replace(",", ";")[:500]
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()},{event},{detail}\n")


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def save_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
