"""Historical replay using the production strategy detectors and context gates.

Example:
  python3 backtest.py --start 2026-01-01 --end 2026-06-30 --symbols AAPL,MSFT
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
import os
import random
from zoneinfo import ZoneInfo

import yaml

import filters
import strategies
import structure
from broker import Bar, Broker

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Result:
    ticker: str
    day: date
    strategy: str
    direction: str
    r_multiple: float


def _signals(cfg: dict, ticker: str, bars: list[Bar], spy: list[Bar],
             session_open: datetime, now: datetime,
             state: strategies.OpeningScalpState,
             previous_close: float | None = None) -> list[strategies.Signal]:
    found = []
    candidates = []
    on = strategies.toggles(cfg)
    support_signals = [n for n in strategies.SUPPORT_SIGNALS if on[n]]
    if on["opening_scalp"]:
        candidates.append(strategies.opening_scalp(
            cfg["strategy_1_opening_scalp"], ticker, state, bars, session_open, now))
    if on["ob_fvg"]:
        candidates.append(strategies.ob_fvg(
            cfg["strategy_2_ob_fvg"], ticker, bars, session_open))
    if support_signals:
        candidates.append(strategies.support_structure(
            {**cfg["strategy_3_support"], "enabled_signals": support_signals},
            ticker, bars, session_open))
    if cfg.get("price_levels"):
        candidates.append(strategies.price_level(
            cfg["price_levels"], cfg["price_level_settings"], ticker, bars))
    for signal in candidates:
        if signal is None:
            continue
        if previous_close is not None:
            signal.reasoning["previous_close"] = previous_close
        gate = filters.evaluate_signal(
            cfg, signal, bars, spy, session_open, now,
            spread_pct=float(cfg["backtest"].get("assumed_spread_pct", 0.03)),
            earnings_symbols=set())
        if gate.allowed:
            signal.reasoning.update(gate.features)
            found.append(signal)
    return found


def _choose(signals: list[strategies.Signal]) -> strategies.Signal | None:
    if not signals or len({s.direction for s in signals}) > 1:
        return None
    return min(signals, key=lambda s: s.risk_per_share)


def _prepare_signal(cfg: dict, signal: strategies.Signal, prefix: list[Bar],
                    session_open: datetime) -> str:
    """Mirror production SL/TP construction; return why entry must skip."""
    stop_cfg = cfg.get("stops", {})
    bars_5m = strategies.aggregate(prefix, 5, session_open)
    atr_value = strategies.atr(bars_5m, int(stop_cfg.get("atr_period", 14)))
    if atr_value is None:
        atr_value = strategies.atr(prefix, int(stop_cfg.get("atr_period", 14)))
    order_cfg = cfg.get("orders", {})
    if order_cfg.get("target_mode") == "structure":
        structure_cfg = cfg.get("structure", {})
        previous_close = signal.reasoning.get("previous_close")
        zones = structure.detect_zones(
            bars_5m, structure_cfg,
            float(previous_close) if previous_close else None)
        stop = structure.stop_for(
            signal.entry, signal.direction, signal.stop, zones, atr_value,
            structure_cfg, stop_cfg,
            own_stop=signal.strategy in structure_cfg.get(
                "strategies_define_own_stop", []))
        if stop is None:
            return "no valid structural stop zone within configured distance"
        signal.stop = stop.price
        signal.reasoning["stop_zone"] = stop.zone.describe()
        target = structure.target_for(
            signal.entry, signal.direction, signal.stop, zones,
            structure_cfg, order_cfg, atr_value)
        if target is None:
            return "target is outside quick-trade cap or below minimum reward:risk"
        signal.target = target.price
        signal.reasoning["target_zone"] = (
            target.zone.describe() if target.zone else "none")
        return ""

    # Legacy modes remain replayable for baseline comparisons.
    floor_risk = max(
        (atr_value or 0.0) * float(stop_cfg.get("atr_mult", 0.8)),
        signal.entry * float(stop_cfg.get("min_stop_pct", 0.15)) / 100,
    )
    if signal.risk_per_share < floor_risk:
        signal.stop = (signal.entry - floor_risk if signal.direction == "long"
                       else signal.entry + floor_risk)
    target_rr = float(order_cfg.get("target_rr", 2.0))
    sign = 1 if signal.direction == "long" else -1
    if order_cfg.get("target_mode") == "price_grid":
        base = math.floor(signal.entry)
        fraction = signal.entry - base
        if signal.direction == "long":
            grid_target = base + (1.0 if fraction < 0.5 else 1.5)
        else:
            grid_target = base - (0.5 if fraction < 0.5 else 0.0)
        desired_reward = abs(grid_target - signal.entry)
        min_reward = float(order_cfg.get("min_reward_risk", 1.5)) * signal.risk_per_share
        max_reward = float(order_cfg.get("max_reward_risk", 2.5)) * signal.risk_per_share
        reward = min(max(desired_reward, min_reward), max_reward)
        signal.target = round(signal.entry + sign * reward, 2)
        return ""
    ideal = signal.entry + sign * target_rr * signal.risk_per_share
    signal.target = round(ideal, 2)
    if order_cfg.get("round_take_profit_to_whole", True):
        low = signal.entry + sign * float(order_cfg.get("min_reward_risk", 1.5)) * signal.risk_per_share
        high = signal.entry + sign * float(order_cfg.get("max_reward_risk", 2.5)) * signal.risk_per_share
        lower, upper = sorted((low, high))
        choices = [
            float(value)
            for value in range(math.ceil(lower), math.floor(upper) + 1)
            if (value > signal.entry if sign == 1 else value < signal.entry)
        ]
        if choices:
            signal.target = min(choices, key=lambda value: abs(value - ideal))
    return ""


def _structure_level(signal: strategies.Signal) -> float | None:
    reasoning = signal.reasoning
    if signal.strategy in ("support_hold", "support_break"):
        return float(reasoning["support"])
    if signal.strategy == "higher_low":
        return float(reasoning["higher_low"])
    if signal.strategy == "opening_scalp":
        key = "first_15m_high" if signal.direction == "long" else "first_15m_low"
        return float(reasoning[key])
    if signal.strategy == "ob_fvg":
        return float(str(reasoning["order_block"]).split("-")[0])
    return None


def _simulate_exit(cfg: dict, signal: strategies.Signal, future: list[Bar],
                   session_open: datetime) -> float:
    """Conservative replay of production exits, including adverse exit costs."""
    return _simulate_exit_detail(cfg, signal, future, session_open)[0]


def _simulate_exit_detail(cfg: dict, signal: strategies.Signal,
                          future: list[Bar],
                          session_open: datetime) -> tuple[float, int]:
    """Like _simulate_exit, but also returns how many future bars the trade
    consumed before exiting — the replay trainer uses this to resume scanning
    the same session after the trade (plus cooldown), matching the live bot."""
    slip = (float(cfg["backtest"].get("slippage_pct", 0.02))
            + float(cfg["backtest"].get("assumed_spread_pct", 0.03)) / 2) / 100
    sign = 1 if signal.direction == "long" else -1
    entry = signal.entry * (1 + sign * slip)
    risk = abs(entry - signal.stop)
    if risk <= 0:
        return 0.0, 0
    target = signal.target
    max_bars = int(float(cfg["risk"].get("max_holding_minutes", 120)))
    partial_enabled = bool(cfg["orders"].get("enable_partial_exit", True))
    partial_at = float(cfg["orders"].get("partial_at_r", 1.0))
    partial_fraction = float(cfg["orders"].get("partial_fraction", 0.5))
    partial_r = 0.0
    remaining = 1.0
    active_stop = signal.stop
    max_favorable_r = 0.0
    level = _structure_level(signal)
    for index, bar in enumerate(future[:max_bars]):
        bars_used = index + 1
        if bar.ts.time() >= time(15, 55):
            raw_exit = bar.open
            return partial_r + remaining * sign * (
                raw_exit * (1 - sign * slip) - entry) / risk, bars_used
        stop_hit = bar.low <= active_stop if sign == 1 else bar.high >= active_stop
        target_hit = bar.high >= target if sign == 1 else bar.low <= target
        if stop_hit:
            raw_exit = min(bar.open, active_stop) if sign == 1 else max(bar.open, active_stop)
            exit_price = raw_exit * (1 - sign * slip)
            return partial_r + remaining * sign * (exit_price - entry) / risk, bars_used
        if partial_enabled and remaining == 1.0:
            partial_price = entry + sign * partial_at * risk
            if (bar.high >= partial_price if sign == 1 else bar.low <= partial_price):
                partial_r = partial_fraction * partial_at
                remaining -= partial_fraction
                active_stop = entry
        if target_hit:
            exit_price = target * (1 - sign * slip)
            return partial_r + remaining * sign * (exit_price - entry) / risk, bars_used
        favorable_price = bar.high if sign == 1 else bar.low
        favorable_r = sign * (favorable_price - entry) / risk
        max_favorable_r = max(max_favorable_r, favorable_r)
        if max_favorable_r >= float(cfg["orders"].get("breakeven_trigger_r", 1.0)):
            active_stop = entry
        close_r = sign * (bar.close - entry) / risk
        if max_favorable_r >= float(cfg["orders"].get("giveback_trigger_r", 1.0)):
            # Mirror the live manager: fixed-distance trail from the peak when
            # trail_distance_r is set, else the legacy fraction-of-peak rule.
            trail_r = cfg["orders"].get("trail_distance_r")
            if trail_r is not None:
                giveback_hit = close_r <= max_favorable_r - float(trail_r)
            else:
                giveback_hit = close_r <= max_favorable_r * float(
                    cfg["orders"].get("giveback_fraction", 0.5))
            if giveback_hit:
                exit_price = bar.close * (1 - sign * slip)
                return partial_r + remaining * sign * (exit_price - entry) / risk, bars_used
        elapsed = int((bar.ts - session_open).total_seconds() // 60) + 1
        if level is not None and elapsed % 5 == 0:
            invalid = bar.close < level if sign == 1 else bar.close > level
            if invalid:
                exit_price = bar.close * (1 - sign * slip)
                return partial_r + remaining * sign * (exit_price - entry) / risk, bars_used
    if not future:
        return 0.0, 0
    bars_used = min(len(future), max_bars)
    final = future[bars_used - 1].close
    return partial_r + remaining * sign * (final - entry) / risk, bars_used


def _session_bars(bars: list[Bar]) -> dict[date, list[Bar]]:
    grouped: dict[date, list[Bar]] = defaultdict(list)
    for bar in bars:
        local = bar.ts.astimezone(ET)
        if time(9, 30) <= local.time() < time(16, 0):
            grouped[local.date()].append(Bar(
                local, bar.open, bar.high, bar.low, bar.close, bar.volume))
    return grouped


def run(cfg: dict, broker: Broker, symbols: list[str],
        start: datetime, end: datetime) -> list[Result]:
    all_symbols = list(dict.fromkeys([*symbols, "SPY"]))
    raw = broker.minute_bars(all_symbols, start, end)
    grouped = {symbol: _session_bars(raw.get(symbol, [])) for symbol in all_symbols}
    results = []
    for symbol in symbols:
        symbol_days = sorted(grouped.get(symbol, {}).items())
        for day_index, (day, day_bars) in enumerate(symbol_days):
            spy_bars = grouped.get("SPY", {}).get(day, [])
            if not spy_bars:
                continue
            previous_close = (
                symbol_days[day_index - 1][1][-1].close
                if day_index > 0 and symbol_days[day_index - 1][1] else None
            )
            session_open = datetime.combine(day, time(9, 30), tzinfo=ET)
            state = strategies.OpeningScalpState()
            entered = False
            for i in range(15, len(day_bars) - 1):
                prefix = day_bars[:i + 1]
                spy_prefix = [bar for bar in spy_bars if bar.ts <= prefix[-1].ts]
                now = prefix[-1].ts + timedelta(minutes=1)
                signal = _choose(_signals(
                    cfg, symbol, prefix, spy_prefix, session_open, now, state,
                    previous_close))
                if signal is None:
                    continue
                if _prepare_signal(cfg, signal, prefix, session_open):
                    continue
                r_value = _simulate_exit(
                    cfg, signal, day_bars[i + 1:], session_open)
                results.append(Result(
                    symbol, day, signal.strategy, signal.direction, r_value))
                entered = True
                break  # one trade per symbol/day keeps observations independent
            if entered:
                continue
    return results


def _block_bootstrap_ci(rows: list[Result], samples: int) -> tuple[float, float]:
    """95% expectancy interval resampling whole ISO-week blocks."""
    blocks: dict[tuple[int, int], list[Result]] = defaultdict(list)
    for row in rows:
        iso = row.day.isocalendar()
        blocks[(iso.year, iso.week)].append(row)
    values = list(blocks.values())
    if len(values) <= 1:
        mean = sum(row.r_multiple for row in rows) / len(rows)
        return mean, mean
    rng = random.Random(42)
    estimates = []
    for _ in range(samples):
        sample = [rng.choice(values) for _ in values]
        flat = [row for block in sample for row in block]
        estimates.append(sum(row.r_multiple for row in flat) / len(flat))
    estimates.sort()
    return (
        estimates[int(0.025 * (len(estimates) - 1))],
        estimates[int(0.975 * (len(estimates) - 1))],
    )


def print_report(results: list[Result], cfg: dict) -> None:
    groups: dict[str, list[Result]] = defaultdict(list)
    for result in results:
        groups[result.strategy].append(result)
    print("strategy,trades,win_rate_pct,expectancy_r,weekly_bootstrap_ci95_low_r,"
          "weekly_bootstrap_ci95_high_r,total_r,max_drawdown_r,acceptance")
    acceptance = cfg["backtest"].get("acceptance", {})
    for strategy, rows in sorted(groups.items()):
        wins = sum(row.r_multiple > 0 for row in rows)
        expectancy = sum(row.r_multiple for row in rows) / len(rows)
        ci_low, ci_high = _block_bootstrap_ci(
            rows, int(cfg["backtest"].get("bootstrap_samples", 2000)))
        equity = peak = drawdown = 0.0
        for row in sorted(rows, key=lambda item: (item.day, item.ticker)):
            equity += row.r_multiple
            peak = max(peak, equity)
            drawdown = min(drawdown, equity - peak)
        passed = (
            len(rows) >= int(acceptance.get("min_trades_per_strategy", 200))
            and expectancy > float(acceptance.get("min_expectancy_r", 0.0))
            and ci_low > float(acceptance.get("min_ci95_low_r", 0.0))
            and drawdown >= -float(acceptance.get("max_drawdown_r", 20.0))
        )
        print(f"{strategy},{len(rows)},{wins / len(rows) * 100:.2f},"
              f"{expectancy:.4f},{ci_low:.4f},{ci_high:.4f},"
              f"{sum(row.r_multiple for row in rows):.4f},{drawdown:.4f},"
              f"{'PASS' if passed else 'FAIL'}")
    if not results:
        print("NO_TRADES: filters found no qualifying historical setups")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--symbols", default="AAPL,MSFT,NVDA,SPY")
    args = parser.parse_args()
    with open(os.path.join(ROOT, "config.yaml")) as file:
        cfg = yaml.safe_load(file)
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
           + timedelta(days=1))
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",")
               if symbol.strip()]
    print_report(run(cfg, Broker(cfg), symbols, start, end), cfg)


if __name__ == "__main__":
    main()
