# deriv-research

Read-only data tools for testing whether Deriv Volatility Indices have any tradable edge.
**Nothing in this repository can place a trade.** Calls such as `buy`, `proposal` and `authorize` are refused in code, and a test proves it.

## What it does

- **`dresearch probe`** measures what Deriv's public API actually provides: the endpoint, symbol codes, tick schedule, price rounding, page size, how far back history goes, and whether gold is served. It writes the collector config.
- **`dresearch collect`** records every tick 24/7 as a systemd service. It reconnects by itself. It re-downloads every closed hour, which repairs gaps and gives an independent check of the live feed.
- **`dresearch audit`** is the daily data-quality report: completeness, gaps, live-vs-history agreement, and whether gaps are linked to price moves. It also compresses and hashes finished days.
- **`dresearch status`** tells you whether it is running and whether data is arriving.

## Install on the VPS (Ubuntu, as root)

```bash
curl -fsSL https://raw.githubusercontent.com/pedrocti/deriv-research/main/deploy/install.sh | bash
dresearch probe
systemctl enable --now dresearch-collector dresearch-audit.timer
dresearch status
```

**Update to a new version:** run the same `curl ... | bash` line again. It restarts the collector itself.

**Stop collecting:** `systemctl disable --now dresearch-collector dresearch-audit.timer`

## Where things are

| What | Where |
| --- | --- |
| Raw ticks (never edited) | `/var/lib/dresearch/raw/{live,history}/<SYMBOL>/<YYYY-MM-DD>/<HH>.csv[.gz]` |
| Hashes of sealed files | `/var/lib/dresearch/raw/MANIFEST.jsonl` |
| Connection events | `/var/lib/dresearch/logs/events.csv` |
| Probe and audit reports | `/var/lib/dresearch/reports/` |
| Service log | `journalctl -u dresearch-collector -n 50` |

All timestamps are UTC Unix epochs as sent by Deriv.

## Limits that are enforced

The collector is capped at 512 MB of RAM and half a CPU core, runs at low priority, and runs as an unprivileged user that can write only to `/var/lib/dresearch`. It opens no network ports. API usage stays under Deriv's documented 220 calls a minute.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `probe` says "no endpoint reachable" | Run `curl -sI https://api.derivws.com` on the VPS. If it prints nothing, outbound HTTPS is blocked |
| `status` shows CHECK NEEDED | `journalctl -u dresearch-collector -n 50` and send the output |
| A symbol says "never" | It may not exist on your account's feed. Check the probe summary |
| Audit says `WARN: completeness` | Normal on the first, partial day. Look again after the first full UTC day |

## Development

```bash
pip install -e .[dev] && pytest -q
```

The tests run against `tests/fake_deriv.py`, a local imitation of Deriv's documented message formats, so they need no internet.
