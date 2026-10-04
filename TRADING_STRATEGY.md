# Trading Strategy Specification

**Status:** Alpaca paper trading only. Stocks and ETFs; no options.
**Simulated capital:** approximately $100,000.
**Universe:** `sp100.txt` (current S&P 100 constituents).
**Trading session:** 9:30 AM–4:00 PM ET; all positions flatten by 3:55 PM.

This document defines hypotheses to test, not proven edges. A strategy is not
promoted toward live trading until an out-of-sample backtest and a sufficiently
large paper sample show positive expectancy after costs.

## Non-negotiable rules

1. Every entry condition is deterministic. Missing data or ambiguity means skip.
2. Opposing signals on one ticker cancel each other. Aligned signals use the
   tightest valid stop.
3. Every entry has a broker bracket when possible and a software monitor.
4. The language-model judge can veto a trade but cannot create a signal, alter
   configuration, or override a deterministic risk gate.
5. Parameters are never changed automatically from trade outcomes.

## Shared context gates

These checks run after a strategy fires and before the judge. All must pass.
Values are configured under `context_filters` in `config.yaml`.

- **Regime:** SPY session VWAP plus the slope of a fully warmed EMA(20) over
  three bars. It uses 1-minute bars during the opening hour and switches to
  5-minute bars only after 23 completed observations, avoiding an under-warmed
  EMA. Close above VWAP and slope >= +0.03% is `trend_up`; close below VWAP and
  slope <= -0.03% is `trend_down`; otherwise `chop`. Unavailable data skips.
- **Regime-direction mapping:** longs are blocked in `trend_down`; shorts are
  blocked in `trend_up` for the configured strategies. Strategy 2 remains
  long-only and is therefore blocked in `trend_down`.
- **Volatility:** ATR(14) must be 0.15%–5.0% of price. It uses completed
  5-minute bars, falling back to a complete 14-bar 1-minute ATR early in the
  session.
- **Relative strength:** ticker return since the open minus SPY return since
  the open must be >= 0 for longs and <= 0 for shorts.
- **Spread:** `(ask - bid) / midpoint * 100` must be <= 0.05%. A missing,
  zero, crossed, or more-than-30-second-old quote skips the trade.
- **Events:** configured `no_trade_windows` block macro events. Any symbol in
  `earnings_blacklist.txt` is blocked for the session.
- **Volume:** breakout/impulse bars use strategy-specific relative volume:
  bar volume divided by the mean of the preceding completed bars. It must be
  >= 1.5. The lookback is three 5-minute bars for opening-session feasibility;
  this parameter must be validated rather than assumed optimal.

## Strategy 1 — 90-Minute Opening Scalp

The core 15-minute → 5-minute → 1-minute opening-range setup.

1. Build the 9:30–9:45 candle from at least 12 completed 1-minute bars.
2. Mark its wick high and low.
3. Calculate ATR(14) from those completed 1-minute bars. Opening range divided
   by ATR must be between 2.0 and 15.0.
4. A completed 5-minute candle must close above the high (long) or below the
   low (short), with relative volume >= 1.5.
5. After that candle closes, a completed 1-minute candle must return within
   0.05% of the broken level and close on the breakout side. Any earlier
   1-minute close back through the level kills that direction.
6. No new Strategy 1 breakouts at or after 11:00 ET. If no breakout has fired
   by 11:00, the strategy stops for the day on that ticker. A breakout that
   fired before 11:00 may still complete its 1-minute retest entry after
   11:00; a failed retest after the cutoff ends the strategy for that ticker.

**Entry:** retest candle close, refreshed against the live trade price.
**Defining stop:** 0.05% beyond the broken level. Final placement uses the
structure-zone rules below and can only move farther from entry.
**Target:** nearest valid opposing structure, subject to the quick-trade caps.

## Strategy 2 — Order Block + Fair Value Gap

This strategy is retained as an explicitly testable hypothesis; ICT labels are
not treated as evidence of an edge.

On completed 5-minute bars:

1. A bullish impulse candle closes above its open.
2. Its body is both >= 2.0 times the mean body of the preceding 10 bars and
   >= 0.8 times ATR(14).
3. Its volume is >= 1.5 times the mean of the preceding three bars.
4. The bullish order block is the most recent down candle no more than six
   bars before the impulse.
5. A bullish FVG is a three-candle pattern where candle 1 high is below candle
   3 low. Gap width must be >= 0.05% of current price.
6. At least two bars after the impulse, price must simultaneously be inside
   the FVG and within 0.10% of the order-block midpoint.
7. Shared context gates pass; `trend_down` blocks the long.

**Direction:** long only.
**Defining stop:** 0.05% below the order-block low. Final placement uses the
structure-zone rules below.
**Target:** nearest valid opposing structure.

## Strategy 3 — Support / Structure

Swing lows use a five-bar fractal: the center low is lower than each of the two
bars on both sides. The latest swing low is eligible support only if it has at
least two lows clustered within 0.20%, or it is the session low.

### 3a — Support hold, long

- The latest three completed 5-minute bars constitute consolidation.
- At least one low comes within 0.15% of support.
- None closes below support.
- The first two closes remain within 0.30% of support.
- The last bar is bullish, closes above the preceding bar's high, and remains
  within 0.60% of support.

Enter on the confirmation close; stop 0.10% below support.

### 3b — Support break, short

- A completed 5-minute candle closes at least 0.10% below support.
- Its relative volume is >= 1.5.
- The shared regime gate rejects this short on `trend_up` days.

Enter on the breakdown close; stop 0.10% above support.

### 3c — Higher low, long

- The latest confirmed swing low is above the prior confirmed swing low.
- It occurred within the latest six completed 5-minute bars.
- A bullish candle closes above that higher-low candle's high.

Enter on confirmation; stop 0.10% below the higher low.

## Strategy 4 — Configured Price-Level Entry

Strategy 4 remains available exactly as a manual level trigger. A configured
`ticker`, `price`, and `direction` fires when a completed 1-minute bar trades
through the level or closes within 0.05%. Its stop is 0.50% beyond the level.
It still must pass every shared context, conflict, risk, and judge gate.

`price_levels` is empty by default. This is a discretionary override, not a
claimed quantitative edge.

## Risk and portfolio limits

- Daily realized-loss shutoff: **-$3,000 (3%)**.
- Planned risk per trade: **$500 (0.5%)**.
- Quantity: `floor(min($500 / risk_per_share, $12,000 / entry_price))`;
  fractional long notional is used only when a whole share does not fit.
- Maximum 10 concurrent positions, three positions per GICS sector, and seven
  positions in one direction. There is currently no daily trade-count cap.
- Aggregate planned stop risk is capped at $2,500 and gross entry exposure at
  $100,000. A missing sector mapping fails closed.
- `sectors.csv` maps the current S&P 500 universe to GICS sectors.
- One position per ticker; 15-minute cooldown; 30-minute direction-flip block;
  two losses on one ticker/strategy block that combination for the day.
- Maximum holding time: 120 minutes.
- On startup, broker positions missing from durable bot state are canceled and
  flattened in the dedicated paper account. Failure triggers a safety shutoff.
- Completed 5-minute bars form support/resistance zones from fractal swing
  lows/highs, equal-low/high clusters, session extremes, the opening 15-minute
  range, and the previous close.
- A long stop sits beyond the nearest support zone that is also beyond the
  strategy's defining stop; a short stop mirrors this above resistance.
  The buffer is the larger of 0.12% of entry and 0.05 × ATR.
- Stop distance must be at least max(0.5 × ATR(14), 0.10% of entry) and no
  more than min(2.5 × ATR(14), 0.80% of entry). If no zone qualifies, skip.
  These ceilings are sized from the measured distribution of defining-stop
  distances (median ~0.44% / 1.95 ATR on S&P 100 replays), so a typical
  qualifying setup can actually place a stop.

## Active exits

- Base take-profit sits 0.05% in front of the nearest opposing structure zone:
  resistance for longs and support for shorts. If that zone offers less than
  1.5R, skip the trade. A farther zone is capped at the tightest of 2R,
  3.75 × ATR, and 1.60% of entry. If no opposing zone exists, use 1.5R within
  the same cap.
- At +1R, optionally realize 50% and move the remainder to breakeven. Because
  Alpaca brackets cannot be resized safely in place, the bot cancels the
  bracket before the partial and manages the remainder with software.
- After +1R, a 50% giveback from maximum favorable R exits the remainder.
- A completed 5-minute close through defining structure invalidates the trade.
- End-of-day and maximum-hold exits override targets.

## Dataset and learning loop

Each completed trade records: strategy, direction, signal and fill prices,
slippage, ATR%, trigger relative volume, spread, regime, relative strength,
gap%, sector, entry-time bucket, hold minutes, P&L, and realized R. The journal
may suggest changes, but humans review them between runs. A losing trade is not
evidence that a parameter should change.

## Offline replay training

When the market is closed, `trainer.py` replays historical sessions using the
production detectors (through `backtest.py`). Unlike `backtest.py` (one trade
per symbol/day for statistical independence), the trainer studies every
qualifying setup in a session: after a simulated trade exits, scanning resumes
once the live symbol cooldown has elapsed. It quizzes the judge model:
predict WIN or LOSS for each qualifying setup before the simulated outcome is
revealed. Wrong predictions produce written lessons in `replay_learnings.md`,
and every replayed setup feeds per-bucket statistics
(strategy × direction × regime × entry-time bucket) in `replay_stats.json`.

Buckets with at least `offline_training.min_bucket_n` replays and expectancy
at or below `warn_expectancy_r` become AVOID lines in `playbook.md`; those at
or above `edge_expectancy_r` become EDGE lines. The live judge receives the
playbook section for the candidate's strategy, and its checklist fails a
setup matching an AVOID line (CHECK 8).

This preserves rule 5: no parameter is ever changed automatically. The replay
loop only produces text and statistics that the veto gate reads; humans still
own `config.yaml`. Replay lessons are stored separately from `learnings.md`
so simulated losses never count toward the real past-loss check (CHECK 4).
The trainer refuses to run during a session and stops itself before the next
market open. Replay results inherit every backtest caveat (IEX bar model,
survivorship in the current constituent list, no intraminute ordering) and
are research signals, not proof of edge.

## Measuring whether an edge exists

Run the historical replay with explicit dates and symbols:

```shell
python3 backtest.py --start 2025-01-01 --end 2025-12-31 --symbols AAPL,MSFT,NVDA
```

`backtest.py` imports the production detectors and context filters, honors
strategy enablement, mirrors stop floors, target rounding, partial/breakeven,
giveback, structural invalidation, maximum hold, and 3:55 flattening. It applies
adverse entry and exit costs, prices stop gaps at the worse opening price, and
reports expectancy, total R, maximum drawdown, and a deterministic weekly-block
bootstrap confidence interval.

It deliberately does not simulate the LLM veto, live quote freshness, static
earnings file, portfolio concurrency limits, or intraminute event ordering, and
it takes at most one trade per symbol/day. Results therefore evaluate detector
hypotheses under a conservative bar model; they are not a tick-accurate replica
of live execution.

The predeclared per-strategy acceptance gate is at least 200 trades, positive
mean expectancy, a 95% weekly-block bootstrap lower bound above 0R, and maximum
drawdown no worse than -20R. These are minimum research gates, not proof that
future returns will be positive.

Required validation before any live consideration:

1. Use multiple years containing bull, bear, high-volatility, and low-volatility
   regimes; one week is only a plumbing test.
2. Separate parameter-development (in-sample) and untouched validation
   (out-of-sample) periods.
3. Require enough trades for stable confidence intervals; do not rank a
   strategy from a few dozen observations.
4. Run walk-forward tests and sensitivity checks around every threshold.
5. Include fees, spread, adverse slippage, missed fills, and survivorship-bias
   caveats. The current S&P 100 list is not a point-in-time historical universe.
6. Compare against simple baselines and report maximum drawdown, not only win
   rate.

Positive historical expectancy is necessary, not sufficient. Paper execution
must then confirm that latency, fills, data quality, and operational controls
match the simulation.
