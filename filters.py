"""Deterministic market-context filters shared by live trading and backtests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from broker import Bar
from strategies import Signal, aggregate, atr


@dataclass
class MarketContext:
    regime: str
    spy_change_pct: float
    spy_vwap: float | None
    spy_ema_slope_pct: float | None
    timeframe: str = "5m"


@dataclass
class FilterResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def _session_vwap(bars: list[Bar]) -> float | None:
    total_volume = sum(max(b.volume, 0) for b in bars)
    if total_volume <= 0:
        return None
    return sum(((b.high + b.low + b.close) / 3) * max(b.volume, 0)
               for b in bars) / total_volume


def market_context(spy_bars_1m: list[Bar], session_open: datetime,
                   cfg: dict) -> MarketContext:
    """Classify SPY using its VWAP position and 5m EMA slope."""
    bars_5m = aggregate(spy_bars_1m, 5, session_open)
    if not bars_5m:
        return MarketContext("unknown", 0.0, None, None)
    period = int(cfg.get("ema_period", 20))
    slope_bars = int(cfg.get("ema_slope_bars", 3))
    # Never classify from an under-warmed EMA. During the opening hour, use
    # a fully warmed 1m EMA(20); switch to 5m after it has enough observations.
    if len(bars_5m) >= period + slope_bars:
        bars = bars_5m
        timeframe = "5m"
    else:
        bars = spy_bars_1m
        timeframe = "1m"
    if len(bars) < period + slope_bars:
        # Right after the bell there aren't 23 one-minute bars yet, so a full
        # EMA(20) leaves the regime "unknown" until ~09:53 — and an unknown
        # regime blocks every gate. That silently kills the first 23 minutes
        # of the morning window. Fall back to a shorter, fully-warmed EMA so
        # the open is classifiable; noisier, but a noisy regime beats no regime.
        opening_period = int(cfg.get("opening_ema_period", 0))
        if opening_period and len(bars) >= opening_period + slope_bars:
            period = opening_period
            timeframe = f"{timeframe}_fast"
        else:
            return MarketContext("unknown", 0.0, None, None, timeframe)
    closes = [b.close for b in bars]
    emas = _ema(closes, period)
    slope = None
    if len(emas) > slope_bars and emas[-slope_bars - 1]:
        slope = (emas[-1] / emas[-slope_bars - 1] - 1) * 100
    vwap = _session_vwap(bars)
    change = (closes[-1] / bars[0].open - 1) * 100 if bars[0].open else 0.0
    threshold = float(cfg.get("ema_slope_threshold_pct", 0.03))
    if vwap is None or slope is None:
        regime = "unknown"
    elif closes[-1] > vwap and slope >= threshold:
        regime = "trend_up"
    elif closes[-1] < vwap and slope <= -threshold:
        regime = "trend_down"
    else:
        regime = "chop"
    return MarketContext(regime, change, vwap, slope, timeframe)


def relative_volume(bars_5m: list[Bar], lookback: int = 20) -> float | None:
    if len(bars_5m) < lookback + 1:
        return None
    baseline = [b.volume for b in bars_5m[-lookback - 1:-1]]
    average = sum(baseline) / len(baseline)
    return bars_5m[-1].volume / average if average > 0 else None


def _in_no_trade_window(now: datetime, windows: list[dict]) -> bool:
    for window in windows:
        try:
            start = datetime.fromisoformat(str(window["start"]))
            end = datetime.fromisoformat(str(window["end"]))
            if start.tzinfo is None:
                start = start.replace(tzinfo=now.tzinfo)
            if end.tzinfo is None:
                end = end.replace(tzinfo=now.tzinfo)
            if start <= now <= end:
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def evaluate_signal(
    cfg: dict,
    signal: Signal,
    ticker_bars_1m: list[Bar],
    spy_bars_1m: list[Bar],
    session_open: datetime,
    now: datetime,
    spread_pct: float | None = None,
    earnings_symbols: set[str] | None = None,
) -> FilterResult:
    """Apply shared context gates and return measured features for logging."""
    result = FilterResult(True)
    bars_5m = aggregate(ticker_bars_1m, 5, session_open)
    context_cfg = cfg.get("context_filters", {})
    context = market_context(spy_bars_1m, session_open, context_cfg.get("regime", {}))
    ticker_change = 0.0
    if bars_5m and bars_5m[0].open:
        ticker_change = (bars_5m[-1].close / bars_5m[0].open - 1) * 100
    rs = ticker_change - context.spy_change_pct
    atr_period = int(context_cfg.get("atr_period", 14))
    atr_value = atr(bars_5m, atr_period)
    atr_source = "5m"
    atr_price = bars_5m[-1].close if bars_5m else None
    # Opening strategies cannot have 14 completed 5m bars yet. Use a full
    # 14-period 1m ATR until enough 5m history exists; never use a partial ATR.
    # A 1m ATR runs ~sqrt(5) smaller than the 5m ATR the min/max thresholds
    # were calibrated against, so scale it to a 5m equivalent before gating.
    # Without this the min_atr_pct floor silently rejected almost every
    # signal until 10:45 (14 completed 5m bars): liquid names measured
    # 0.05-0.15% on 1m against a 0.15% floor meant for 5m.
    if atr_value is None:
        atr_value = atr(ticker_bars_1m, atr_period)
        if atr_value is not None:
            atr_value *= 5 ** 0.5
        atr_price = ticker_bars_1m[-1].close if ticker_bars_1m else None
        atr_source = "1m_scaled_to_5m"
    atr_pct = (atr_value / atr_price * 100
               if atr_value is not None and atr_price else None)
    rvol = relative_volume(bars_5m, int(context_cfg.get("volume_lookback", 20)))
    gap_pct = 0.0
    if ticker_bars_1m and ticker_bars_1m[0].open:
        previous_close = signal.reasoning.get("previous_close")
        if previous_close:
            gap_pct = (ticker_bars_1m[0].open / float(previous_close) - 1) * 100

    result.features.update({
        "market_regime": context.regime,
        "regime_timeframe": context.timeframe,
        "spy_change_pct": round(context.spy_change_pct, 4),
        "relative_strength_pct": round(rs, 4),
        "atr_pct": round(atr_pct, 4) if atr_pct is not None else None,
        "atr_timeframe": atr_source,
        "relative_volume": round(rvol, 4) if rvol is not None else None,
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "gap_pct": round(gap_pct, 4),
    })

    if not context_cfg.get("enabled", True):
        return result

    if context.regime == "unknown":
        result.reasons.append("market regime unavailable")
    blocked = context_cfg.get("blocked_regimes", {}).get(signal.strategy, {})
    if context.regime in blocked.get(signal.direction, []):
        result.reasons.append(
            f"{signal.strategy} {signal.direction} blocked in {context.regime}")

    # Per-strategy gate overrides. A breakout-retest scalp and a mean-reversion
    # support hold do not want the same context filters, and tuning one strategy
    # in should not loosen the gates protecting the others.
    overrides = context_cfg.get("strategy_overrides", {}).get(signal.strategy, {})

    def gate(key, default):
        value = overrides.get(key, context_cfg.get(key, default))
        return None if value is None else float(value)

    min_atr = gate("min_atr_pct", 0.15)
    max_atr = gate("max_atr_pct", 5.0)
    # A None threshold disables that gate for this strategy.
    if min_atr is not None or max_atr is not None:
        if atr_pct is None:
            result.reasons.append("ATR unavailable")
        elif (min_atr is not None and atr_pct < min_atr) or \
             (max_atr is not None and atr_pct > max_atr):
            result.reasons.append(
                f"ATR {atr_pct:.3f}% outside "
                f"{min_atr if min_atr is not None else 0:.3f}-"
                f"{max_atr if max_atr is not None else 99:.3f}%")

    rs_threshold = gate("relative_strength_threshold_pct", 0.0)
    if rs_threshold is not None:
        if signal.direction == "long" and rs < rs_threshold:
            result.reasons.append(
                f"relative strength {rs:.3f}% below {rs_threshold:.3f}%")
        if signal.direction == "short" and rs > -rs_threshold:
            result.reasons.append(
                f"relative strength {rs:.3f}% above {-rs_threshold:.3f}%")

    max_spread = gate("max_spread_pct", 0.05)
    if max_spread is not None:
        if spread_pct is None:
            result.reasons.append("spread unavailable")
        elif spread_pct > max_spread:
            result.reasons.append(
                f"spread {spread_pct:.3f}% above {max_spread:.3f}%")

    # Edge-vs-cost. A bare ATR floor rejects calm-but-tight names (CSCO trades
    # at a 0.02% spread) while admitting volatile-but-wide ones, where the
    # round-trip spread eats the move. Requiring the target to clear a multiple
    # of the spread gates on what actually matters: whether the expected move
    # is large enough to pay for crossing twice.
    min_target_mult = float(context_cfg.get("min_target_spread_mult", 0.0))
    if min_target_mult and spread_pct is not None and signal.entry:
        target_pct = abs(signal.target - signal.entry) / signal.entry * 100
        required = spread_pct * min_target_mult
        result.features["target_pct"] = round(target_pct, 4)
        result.features["target_spread_mult"] = (
            round(target_pct / spread_pct, 2) if spread_pct else None)
        if target_pct < required:
            result.reasons.append(
                f"target {target_pct:.3f}% under {min_target_mult:g}x spread "
                f"({required:.3f}%)")

    if _in_no_trade_window(now, context_cfg.get("no_trade_windows", [])):
        result.reasons.append("scheduled no-trade event window")
    if signal.ticker in (earnings_symbols or set()):
        result.reasons.append("ticker on earnings blacklist")

    result.allowed = not result.reasons
    return result
