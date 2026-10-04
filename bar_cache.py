"""Local cache of historical 1-minute bars for offline replay training.

The overnight trainer replays hundreds of (symbol, day) sessions. Fetching the
same history from Alpaca every night would be slow and wasteful, so each
session is stored once under data_cache/<SYMBOL>/<YYYY-MM-DD>.csv and served
from disk afterwards. A day with no data (holiday, symbol not yet listed) is
cached as an empty file so it is never re-requested.

Timestamps are stored as UTC ISO strings, matching what Broker.minute_bars
returns, so cached bars are interchangeable with live-fetched bars.
"""

from __future__ import annotations

import csv
import os
from datetime import date, datetime, time, timedelta, timezone

from broker import Bar, Broker

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "data_cache")

_FIELDS = ["ts", "open", "high", "low", "close", "volume"]


def _path(symbol: str, day: date) -> str:
    return os.path.join(CACHE_DIR, symbol.upper(), f"{day.isoformat()}.csv")


def has_day(symbol: str, day: date) -> bool:
    return os.path.exists(_path(symbol, day))


def load_day(symbol: str, day: date) -> list[Bar]:
    """Cached bars for one symbol/day (may be empty). Raises if not cached."""
    path = _path(symbol, day)
    bars: list[Bar] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            bars.append(Bar(
                datetime.fromisoformat(row["ts"]),
                float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), float(row["volume"]),
            ))
    return bars


def _write_day(symbol: str, day: date, bars: list[Bar]) -> None:
    path = _path(symbol, day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS)
        writer.writeheader()
        for bar in bars:
            writer.writerow({
                "ts": bar.ts.isoformat(),
                "open": bar.open, "high": bar.high, "low": bar.low,
                "close": bar.close, "volume": bar.volume,
            })
    os.replace(tmp, path)


def ensure_days(broker: Broker, symbols: list[str], days: list[date]) -> None:
    """Fetch and cache any missing (symbol, day) combinations.

    One Alpaca request covers the full min..max day span for every symbol that
    has at least one gap — far fewer round trips than per-day requests.
    """
    days = sorted(set(days))
    if not days:
        return
    missing = [s for s in symbols
               if any(not has_day(s, d) for d in days)]
    if not missing:
        return
    start = datetime.combine(days[0], time(0, 0), tzinfo=timezone.utc)
    end = datetime.combine(days[-1] + timedelta(days=1), time(0, 0),
                           tzinfo=timezone.utc)
    fetched = broker.minute_bars(missing, start, end)
    for symbol in missing:
        by_day: dict[date, list[Bar]] = {d: [] for d in days}
        for bar in fetched.get(symbol, []):
            bar_day = bar.ts.astimezone(timezone.utc).date()
            if bar_day in by_day:
                by_day[bar_day].append(bar)
        for day, bars in by_day.items():
            if not has_day(symbol, day):
                _write_day(symbol, day, bars)


def get_session_bars(broker: Broker, symbols: list[str],
                     days: list[date]) -> dict[str, dict[date, list[Bar]]]:
    """Cached raw bars grouped per symbol per day, fetching gaps first."""
    ensure_days(broker, symbols, days)
    return {
        symbol: {day: load_day(symbol, day) for day in days}
        for symbol in symbols
    }
