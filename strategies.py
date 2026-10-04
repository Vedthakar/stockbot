"""Deterministic strategy detectors. Pure math, no I/O, no LLM.

Each strategy function returns either None or a Signal. All conditions must
be cleanly met; any ambiguity or missing data returns None (skipping is
always safe). Timestamps everywhere in this module are US/Eastern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from broker import Bar


STRATEGY_NAMES = ("higher_low", "support_hold", "support_break", "ob_fvg", "opening_scalp")
SUPPORT_SIGNALS = ("support_break", "support_hold", "higher_low")


def toggles(cfg: dict) -> dict[str, bool]:
    """The `strategies:` block in config.yaml — one on/off switch per strategy.
    Anything missing from the block is OFF."""
    block = cfg.get("strategies") or {}
    return {name: bool(block.get(name, False)) for name in STRATEGY_NAMES}


@dataclass
class Signal:
    ticker: str
    strategy: str          # e.g. "opening_scalp"
    direction: str         # "long" | "short"
    entry: float           # reference entry price (actual fill is market)
    stop: float
    target: float
    reasoning: dict        # the inputs that fired, for the judge + logs

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def aggregate(bars_1m: list[Bar], minutes: int, session_open: datetime) -> list[Bar]:
    """Aggregate 1-minute bars into N-minute bars aligned to session open.

    Only buckets that are fully in the past relative to the last 1m bar are
    "complete"; the caller gets every bucket and can slice off the last one
    if it isn't finished. We keep it simple: drop the final bucket unless
    its time window is fully covered by elapsed time.
    """
    if not bars_1m:
        return []
    buckets: dict[int, list[Bar]] = {}
    for b in bars_1m:
        idx = int((b.ts - session_open).total_seconds() // (minutes * 60))
        if idx < 0:
            continue
        buckets.setdefault(idx, []).append(b)
    out = []
    last_ts = bars_1m[-1].ts
    for idx in sorted(buckets):
        bs = buckets[idx]
        bucket_start = session_open + timedelta(minutes=idx * minutes)
        bucket_end = bucket_start + timedelta(minutes=minutes)
        # complete only if we have 1m data at/after the bucket's end
        if last_ts + timedelta(minutes=1) < bucket_end:
            continue
        expected = {
            bucket_start + timedelta(minutes=offset)
            for offset in range(minutes)
        }
        observed = {bar.ts.replace(second=0, microsecond=0) for bar in bs}
        if not expected.issubset(observed):
            continue  # missing minute(s): never manufacture a completed candle
        out.append(Bar(
            ts=bucket_start,
            open=bs[0].open,
            high=max(x.high for x in bs),
            low=min(x.low for x in bs),
            close=bs[-1].close,
            volume=sum(x.volume for x in bs),
        ))
    return out


def _pct(x: float) -> float:
    return x / 100.0


def atr(bars: list[Bar], period: int = 14) -> float | None:
    """Average true range over completed bars. Returns None when there is not
    enough history to calculate a full period."""
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for previous, current in zip(bars[-period - 1:-1], bars[-period:]):
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return sum(true_ranges) / len(true_ranges)


def relative_volume_at(bars: list[Bar], index: int, lookback: int) -> float | None:
    """Volume of one completed bar divided by its preceding-bar average."""
    if index < lookback or index >= len(bars):
        return None
    prior = [b.volume for b in bars[index - lookback:index]]
    average = sum(prior) / len(prior)
    return bars[index].volume / average if average > 0 else None


# ---------------------------------------------------------------------------
# Strategy 1 — 90-minute opening scalp (15m levels -> 5m breakout -> 1m retest)
# ---------------------------------------------------------------------------

@dataclass
class OpeningScalpState:
    """Per-ticker state machine, persisted across polls by main.py."""
    level_high: float | None = None
    level_low: float | None = None
    breakout: str | None = None          # "up" | "down"
    breakout_end: datetime | None = None  # end time of the breakout 5m candle
    breakout_relative_volume: float | None = None
    dead_directions: set = field(default_factory=set)
    # Set ONLY once a position is actually opened (main.py does this), never
    # when a Signal is merely emitted. Emitting is not trading: a signal still
    # has to clear the context gate, the entry guards, sizing and the judge.
    # Marking done on emit meant one rejection — often a single bogus IEX
    # spread sample — permanently killed that symbol's scalp for the whole
    # session. Measured 2026-08-03: every opening_scalp symbol appeared
    # exactly once in the log while other strategies retried 2-19 times.
    done: bool = False


def opening_scalp(cfg: dict, ticker: str, state: OpeningScalpState,
                  bars_1m: list[Bar], session_open: datetime,
                  now: datetime) -> Signal | None:
    if state.done or not bars_1m:
        return None

    cutoff_h, cutoff_m = map(int, cfg["entry_cutoff"].split(":"))
    past_cutoff = now.time() >= time(cutoff_h, cutoff_m)
    # The cutoff stops the strategy only when no breakout is in flight: a
    # breakout that fired before the cutoff may still complete its 1-minute
    # retest entry afterwards. No NEW breakout may start at/after the cutoff.
    if past_cutoff and state.breakout is None:
        state.done = True
        return None

    first_candle_end = session_open + timedelta(minutes=15)

    # Step 1-3: mark the first 15m candle's high/low once it has fully closed.
    if state.level_high is None:
        if now < first_candle_end + timedelta(minutes=1):
            return None
        window = [b for b in bars_1m if session_open <= b.ts < first_candle_end]
        if len(window) < 12:  # too many missing minutes -> ambiguous -> skip today
            if now > first_candle_end + timedelta(minutes=5):
                state.done = True
            return None
        state.level_high = max(b.high for b in window)
        state.level_low = min(b.low for b in window)
        opening_atr = atr(window, int(cfg.get("atr_period", 14)))
        if opening_atr is None:
            state.done = True
            return None
        range_atr = (state.level_high - state.level_low) / opening_atr
        if not (float(cfg.get("min_range_atr_mult", 2.0)) <= range_atr
                <= float(cfg.get("max_range_atr_mult", 15.0))):
            state.done = True
            return None

    tol = _pct(cfg["retest_tolerance_pct"])
    buf = _pct(cfg["stop_buffer_pct"])

    # Step 4: 5m candle CLOSING beyond a level = breakout.
    if state.breakout is None:
        bars_5m = [b for b in aggregate(bars_1m, 5, session_open) if b.ts >= first_candle_end]
        all_5m = aggregate(bars_1m, 5, session_open)
        volume_lookback = int(cfg.get("volume_lookback", 3))
        volume_factor = float(cfg.get("volume_factor", 1.5))
        for b in bars_5m:
            index = all_5m.index(b)
            rvol = relative_volume_at(all_5m, index, volume_lookback)
            if rvol is None or rvol < volume_factor:
                continue
            if b.close > state.level_high and "up" not in state.dead_directions:
                state.breakout = "up"
                state.breakout_end = b.ts + timedelta(minutes=5)
                state.breakout_relative_volume = rvol
                break
            if b.close < state.level_low and "down" not in state.dead_directions:
                state.breakout = "down"
                state.breakout_end = b.ts + timedelta(minutes=5)
                state.breakout_relative_volume = rvol
                break
        if state.breakout is None:
            return None

    # Step 5: 1m retest of the broken level that holds.
    level = state.level_high if state.breakout == "up" else state.level_low
    # The retest has to arrive reasonably soon after the break, or the setup
    # is stale. `done` used to bound this implicitly by firing once; now that
    # a rejected signal may be re-offered, the window needs a real deadline.
    max_retest = int(cfg.get("max_retest_minutes", 30))
    if now > state.breakout_end + timedelta(minutes=max_retest):
        state.dead_directions.add(state.breakout)
        state.breakout = None
        state.breakout_relative_volume = None
        return None
    retest_bars = [b for b in bars_1m if b.ts >= state.breakout_end]
    for b in retest_bars:
        if state.breakout == "up":
            if b.close < level:                      # closed back through -> failed
                state.dead_directions.add("up")
                state.breakout = None
                state.breakout_relative_volume = None
                return None
            if b.low <= level * (1 + tol):           # came back to the level and held
                entry = b.close
                stop = level * (1 - buf)
                if entry <= stop:
                    return None
                target = entry + cfg["reward_risk"] * (entry - stop)
                return Signal(ticker, "opening_scalp", "long", entry, stop, target, {
                    "first_15m_high": state.level_high, "first_15m_low": state.level_low,
                    "breakout": "5m close above high", "retest": f"1m held {level:.2f}",
                    "breakout_relative_volume": round(
                        state.breakout_relative_volume or 0.0, 3),
                })
        else:
            if b.close > level:
                state.dead_directions.add("down")
                state.breakout = None
                state.breakout_relative_volume = None
                return None
            if b.high >= level * (1 - tol):
                entry = b.close
                stop = level * (1 + buf)
                if entry >= stop:
                    return None
                target = entry - cfg["reward_risk"] * (stop - entry)
                return Signal(ticker, "opening_scalp", "short", entry, stop, target, {
                    "first_15m_high": state.level_high, "first_15m_low": state.level_low,
                    "breakout": "5m close below low", "retest": f"1m held {level:.2f}",
                    "breakout_relative_volume": round(
                        state.breakout_relative_volume or 0.0, 3),
                })
    return None


# ---------------------------------------------------------------------------
# Strategy 2 — order block + fair value gap (long only)
# ---------------------------------------------------------------------------

def ob_fvg(cfg: dict, ticker: str, bars_1m: list[Bar],
           session_open: datetime) -> Signal | None:
    bars = aggregate(bars_1m, 5, session_open)
    lookback = cfg["impulse_lookback"]
    if len(bars) < lookback + 5:
        return None

    factor = cfg["impulse_body_factor"]
    touch_tol = _pct(cfg["ob_touch_tolerance_pct"])
    buf = _pct(cfg["stop_buffer_pct"])
    price = bars[-1].close

    # find the most recent impulsive UP candle (body >= factor * avg body)
    impulse_i = None
    impulse_atr_mult = float(cfg.get("impulse_atr_mult", 0.8))
    volume_lookback = int(cfg.get("volume_lookback", 3))
    volume_factor = float(cfg.get("volume_factor", 1.5))
    impulse_rvol = None
    for i in range(len(bars) - 3, max(lookback, volume_lookback), -1):
        b = bars[i]
        bodies = [abs(x.close - x.open) for x in bars[i - lookback:i]]
        avg_body = sum(bodies) / len(bodies)
        prior_atr = atr(bars[:i + 1], int(cfg.get("atr_period", 14)))
        rvol = relative_volume_at(bars, i, volume_lookback)
        body = b.close - b.open
        if (avg_body > 0 and prior_atr is not None and rvol is not None
                and b.close > b.open and body >= factor * avg_body
                and body >= impulse_atr_mult * prior_atr and rvol >= volume_factor):
            impulse_i = i
            impulse_rvol = rvol
            break
    if impulse_i is None:
        return None

    # bullish order block = last down candle before the impulse
    ob = None
    for j in range(impulse_i - 1, -1, -1):
        if impulse_i - j > int(cfg.get("max_ob_age_bars", 6)):
            break
        if bars[j].close < bars[j].open:
            ob = bars[j]
            break
    if ob is None:
        return None
    ob_mid = (ob.high + ob.low) / 2

    # The impulse must be candle 2 of the three-candle FVG, not merely near it.
    fvg_end = impulse_i + 1
    if fvg_end >= len(bars) or bars[fvg_end - 2].high >= bars[fvg_end].low:
        return None
    fvg = (bars[fvg_end - 2].high, bars[fvg_end].low)
    fvg_pct = (fvg[1] - fvg[0]) / price * 100 if price else 0.0
    if fvg_pct < float(cfg.get("min_fvg_pct", 0.05)):
        return None

    # both conditions at once: price touching OB midpoint AND inside the FVG
    touching_mid = abs(price - ob_mid) <= ob_mid * touch_tol
    inside_fvg = fvg[0] <= price <= fvg[1]
    returned = len(bars) - 1 >= impulse_i + 2  # at least two bars after impulse
    if not (touching_mid and inside_fvg and returned):
        return None

    entry = price
    stop = ob.low * (1 - buf)
    if entry <= stop:
        return None
    target = entry + cfg["reward_risk"] * (entry - stop)
    return Signal(ticker, "ob_fvg", "long", entry, stop, target, {
        "order_block": f"{ob.low:.2f}-{ob.high:.2f} (mid {ob_mid:.2f})",
        "fvg": f"{fvg[0]:.2f}-{fvg[1]:.2f}",
        "fvg_pct": round(fvg_pct, 4),
        "impulse_relative_volume": round(impulse_rvol or 0.0, 3),
        "impulse_candle": str(bars[impulse_i].ts),
    })


# ---------------------------------------------------------------------------
# Strategy 3 — support / structure (3a hold, 3b break, 3c higher low)
# ---------------------------------------------------------------------------

def _swing_lows(bars: list[Bar], strength: int) -> list[int]:
    idxs = []
    for i in range(strength, len(bars) - strength):
        if all(bars[i].low < bars[i - k].low for k in range(1, strength + 1)) and \
           all(bars[i].low < bars[i + k].low for k in range(1, strength + 1)):
            idxs.append(i)
    return idxs


def support_structure(cfg: dict, ticker: str, bars_1m: list[Bar],
                      session_open: datetime) -> Signal | None:
    bars = aggregate(bars_1m, 5, session_open)
    strength = cfg["swing_strength"]
    if len(bars) < strength * 2 + 6:
        return None

    swings = _swing_lows(bars, strength)
    if not swings:
        return None  # no clean level -> no trade

    touch_tol = _pct(cfg["support_touch_tolerance_pct"])
    break_tol = _pct(cfg["break_threshold_pct"])
    buf = _pct(cfg["stop_buffer_pct"])
    rr = cfg["reward_risk"]
    enabled_signals = set(cfg.get(
        "enabled_signals", ["support_break", "support_hold", "higher_low"]))
    last = bars[-1]

    support_i = swings[-1]
    support = bars[support_i].low
    cluster_tol = _pct(cfg.get("level_cluster_pct", 0.20))
    min_touches = int(cfg.get("min_support_touches", 2))
    touches = sum(1 for b in bars[:support_i + 1]
                  if abs(b.low - support) <= support * cluster_tol)
    is_session_low = support <= min(b.low for b in bars)
    if touches < min_touches and not is_session_low:
        return None
    candidates: list[Signal] = []

    # 3b — decisive close BELOW support -> short
    if "support_break" in enabled_signals and last.close < support * (1 - break_tol):
        volume_lookback = int(cfg.get("volume_lookback", 3))
        rvol = relative_volume_at(bars, len(bars) - 1, volume_lookback)
        if rvol is not None and rvol >= float(cfg.get("volume_factor", 1.5)):
            entry = last.close
            stop = support * (1 + buf)
            candidates.append(Signal(ticker, "support_break", "short", entry, stop,
                                     entry - rr * (stop - entry),
                                     {"support": round(support, 2),
                                      "support_touches": touches,
                                      "close_below": round(last.close, 2),
                                      "break_relative_volume": round(rvol, 3)}))

    # 3a — support holds: touched support recently, never closed below it,
    # and the last candle is a turn-up confirmation.
    after = bars[support_i + 1:]
    if "support_hold" in enabled_signals and after:
        consolidation_bars = int(cfg.get("consolidation_bars", 3))
        recent = after[-consolidation_bars:]
        touched = any(b.low <= support * (1 + touch_tol) for b in recent)
        no_close_below = len(recent) == consolidation_bars and all(
            b.close >= support for b in recent)
        consolidation_tol = _pct(cfg.get("consolidation_pct", 0.30))
        consolidated = no_close_below and all(
            abs(b.close - support) <= support * consolidation_tol for b in recent[:-1])
        confirm = last.close > last.open and len(after) >= 2 and last.close > after[-2].high
        near = last.close <= support * (1 + 4 * touch_tol)  # still near the level, not chasing
        if touched and consolidated and confirm and near:
            entry = last.close
            stop = support * (1 - buf)
            if entry > stop:
                candidates.append(Signal(ticker, "support_hold", "long", entry, stop,
                                         entry + rr * (entry - stop),
                                         {"support": round(support, 2),
                                          "support_touches": touches,
                                          "consolidation_bars": consolidation_bars,
                                          "confirmation_close": round(last.close, 2)}))

    # 3c — higher low vs prior swing low, with confirmation candle
    if "higher_low" in enabled_signals and len(swings) >= 2:
        hl_i, prior_i = swings[-1], swings[-2]
        higher_low = bars[hl_i].low > bars[prior_i].low
        confirm = last.close > last.open and last.close > bars[hl_i].high
        if higher_low and confirm and hl_i >= len(bars) - 6:  # recent structure only
            entry = last.close
            stop = bars[hl_i].low * (1 - buf)
            if entry > stop:
                candidates.append(Signal(ticker, "higher_low", "long", entry, stop,
                                         entry + rr * (entry - stop),
                                         {"higher_low": round(bars[hl_i].low, 2),
                                          "prior_swing_low": round(bars[prior_i].low, 2)}))

    if not candidates:
        return None
    directions = {c.direction for c in candidates}
    if len(directions) > 1:
        return None  # internal conflict -> skip (fail safe)
    # same direction: tightest stop wins
    return min(candidates, key=lambda c: c.risk_per_share)


# ---------------------------------------------------------------------------
# Strategy 4 — configured price-level entries
# ---------------------------------------------------------------------------

def price_level(cfg_levels: list, cfg: dict, ticker: str,
                bars_1m: list[Bar]) -> Signal | None:
    if not bars_1m:
        return None
    last = bars_1m[-1]
    tol = _pct(cfg["trigger_tolerance_pct"])
    stop_pct = _pct(cfg["stop_pct"])
    rr = cfg["reward_risk"]
    for lv in cfg_levels:
        if lv["ticker"].upper() != ticker:
            continue
        level = float(lv["price"])
        direction = lv["direction"]
        hit = last.low <= level <= last.high or abs(last.close - level) <= level * tol
        if not hit:
            continue
        entry = last.close
        if direction == "long":
            stop = level * (1 - stop_pct)
            if entry <= stop:
                continue
            target = entry + rr * (entry - stop)
        elif direction == "short":
            stop = level * (1 + stop_pct)
            if entry >= stop:
                continue
            target = entry - rr * (stop - entry)
        else:
            continue
        return Signal(ticker, "price_level", direction, entry, stop, target,
                      {"configured_level": level})
    return None
