from datetime import datetime, timedelta
import unittest

import filters

from broker import Bar, Broker
from main import Bot, ET
from strategies import (
    OpeningScalpState, Signal, aggregate, atr, opening_scalp,
)
from structure import Zone, detect_zones, stop_for, target_for


class FakeBroker:
    def __init__(self):
        self.replaced = []

    def replace_bracket_stop(self, order_id, price):
        self.replaced.append((order_id, price))


class OrderStateBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.close_calls = 0
        self.cancel_result = True

    def latest_price(self, ticker):
        return 98.0

    def cancel_orders_for_symbol_and_wait(self, ticker):
        return self.cancel_result

    def close_position(self, ticker):
        self.close_calls += 1
        return type("Order", (), {"id": "close"})()

    def submit_market_order(self, *args, **kwargs):
        raise RuntimeError("submission failed")

    def open_positions(self):
        return [type("Position", (), {"symbol": "ABC", "qty": "10"})()]


def bare_bot():
    bot = Bot.__new__(Bot)
    bot.cfg = {
        "stops": {"atr_period": 14, "atr_mult": 0.8, "min_stop_pct": 0.15},
        "orders": {
            "round_take_profit_to_whole": True,
            "target_rr": 2.0,
            "min_reward_risk": 1.5,
            "max_reward_risk": 2.5,
            "breakeven_trigger_r": 1.0,
            "giveback_trigger_r": 1.0,
            "giveback_fraction": 0.5,
        },
        "entry_guards": {
            "symbol_cooldown_min": 15,
            "no_flip_min": 30,
            "max_combo_losses_per_day": 2,
        },
    }
    bot.trade_history = []
    bot.positions = {}
    bot.broker = FakeBroker()
    bot._save_state = lambda: None
    return bot


class RiskMathTests(unittest.TestCase):
    def test_atr(self):
        now = datetime(2026, 7, 20, 10, tzinfo=ET)
        bars = [
            Bar(now + timedelta(minutes=5 * i), 100, 101, 99, 100, 1)
            for i in range(15)
        ]
        self.assertAlmostEqual(atr(bars, 14), 2.0)

    def test_aggregate_rejects_bucket_with_missing_minute(self):
        session = datetime(2026, 7, 20, 9, 30, tzinfo=ET)
        bars = [
            Bar(session, 100, 101, 99, 100, 1),
            Bar(session + timedelta(minutes=4), 100, 101, 99, 100, 1),
            Bar(session + timedelta(minutes=5), 100, 101, 99, 100, 1),
        ]
        self.assertEqual(aggregate(bars, 5, session), [])

    def test_stop_floor_and_whole_target(self):
        bot = bare_bot()
        floored = Signal("ABC", "support_hold", "long", 100.20, 100.15, 0, {})
        bot._apply_legacy_stop_floor(
            floored, [], datetime(2026, 7, 20, 9, 30, tzinfo=ET))
        self.assertAlmostEqual(floored.stop, 100.0497)
        sig = Signal("ABC", "support_hold", "long", 100.20, 99.80, 0, {})
        bot._set_legacy_take_profit(sig)
        self.assertEqual(sig.target, 101.0)
        rr = (sig.target - sig.entry) / (sig.entry - sig.stop)
        self.assertGreaterEqual(rr, 1.5)
        self.assertLessEqual(rr, 2.5)

    def test_target_falls_back_when_no_whole_dollar_fits(self):
        bot = bare_bot()
        sig = Signal("ABC", "support_hold", "long", 100.10, 100.05, 0, {})
        bot._set_legacy_take_profit(sig)
        self.assertEqual(sig.target, 100.20)

    def test_exact_two_r_target_when_whole_rounding_disabled(self):
        bot = bare_bot()
        bot.cfg["orders"]["round_take_profit_to_whole"] = False
        sig = Signal("ABC", "support_hold", "long", 401.84, 400.42, 0, {})
        bot._set_legacy_take_profit(sig)
        self.assertEqual(sig.target, 404.68)

    def test_price_grid_targets_and_minimum_rr(self):
        bot = bare_bot()
        bot.cfg["orders"]["target_mode"] = "price_grid"
        below_half = Signal("ABC", "support_hold", "long", 136.20, 135.70, 0, {})
        bot._set_legacy_take_profit(below_half)
        self.assertEqual(below_half.target, 137.00)
        above_half = Signal("ABC", "support_hold", "long", 136.70, 136.20, 0, {})
        bot._set_legacy_take_profit(above_half)
        self.assertEqual(above_half.target, 137.50)
        low_rr = Signal("ABC", "support_hold", "long", 136.70, 135.70, 0, {})
        bot._set_legacy_take_profit(low_rr)
        self.assertEqual(low_rr.target, 138.20)
        mirrored_short = Signal("ABC", "support_break", "short", 136.80, 137.30, 0, {})
        bot._set_legacy_take_profit(mirrored_short)
        self.assertEqual(mirrored_short.target, 136.00)


SCALP_CFG = {
    "entry_cutoff": "11:00",
    "reward_risk": 2.0,
    "stop_buffer_pct": 0.05,
    "retest_tolerance_pct": 0.05,
    "atr_period": 14,
    "min_range_atr_mult": 2.0,
    "max_range_atr_mult": 15.0,
    "volume_lookback": 3,
    "volume_factor": 1.5,
}


class OpeningScalpCutoffTests(unittest.TestCase):
    """The 11:00 cutoff only stops the strategy when no breakout is in flight."""

    @staticmethod
    def _state_with_breakout():
        session = datetime(2026, 7, 21, 9, 30, tzinfo=ET)
        state = OpeningScalpState(
            level_high=101.0, level_low=99.0, breakout="up",
            breakout_end=datetime(2026, 7, 21, 10, 58, tzinfo=ET),
            breakout_relative_volume=2.0,
        )
        return session, state

    def test_no_breakout_by_cutoff_stops_strategy(self):
        session = datetime(2026, 7, 21, 9, 30, tzinfo=ET)
        state = OpeningScalpState(level_high=101.0, level_low=99.0)
        bars = [Bar(datetime(2026, 7, 21, 10, 59, tzinfo=ET),
                    100, 100.5, 99.5, 100, 1000)]
        result = opening_scalp(
            SCALP_CFG, "ABC", state, bars, session,
            datetime(2026, 7, 21, 11, 0, tzinfo=ET))
        self.assertIsNone(result)
        self.assertTrue(state.done)

    def test_pre_cutoff_breakout_still_completes_retest_after_cutoff(self):
        session, state = self._state_with_breakout()
        retest = Bar(datetime(2026, 7, 21, 11, 1, tzinfo=ET),
                     101.20, 101.30, 100.98, 101.10, 1000)
        sig = opening_scalp(
            SCALP_CFG, "ABC", state, [retest], session,
            datetime(2026, 7, 21, 11, 2, tzinfo=ET))
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "long")
        # Emitting a signal must NOT retire the state machine: the signal can
        # still be rejected by the context gate, guards, sizing or the judge,
        # and the setup has to survive that to be re-offered next tick.
        # main.py sets `done` only once a position actually opens.
        self.assertFalse(state.done)

    def test_rejected_signal_can_be_reoffered_next_tick(self):
        """Regression: a rejected scalp used to be dead for the whole session.

        `done` was set the moment a Signal was emitted, so one bogus IEX
        spread sample permanently killed that symbol's scalp. Measured
        2026-08-03: every opening_scalp symbol appeared exactly once in the
        log while other strategies on the same symbols retried 2-19 times.
        """
        session, state = self._state_with_breakout()
        retest = Bar(datetime(2026, 7, 21, 11, 1, tzinfo=ET),
                     101.20, 101.30, 100.98, 101.10, 1000)
        now = datetime(2026, 7, 21, 11, 2, tzinfo=ET)
        first = opening_scalp(SCALP_CFG, "ABC", state, [retest], session, now)
        self.assertIsNotNone(first)
        # Caller rejects it (e.g. a garbage spread sample) and does not enter.
        second = opening_scalp(SCALP_CFG, "ABC", state, [retest], session, now)
        self.assertIsNotNone(second)
        self.assertEqual(second.direction, "long")

    def test_stale_breakout_expires_without_retest(self):
        """A break whose retest never arrives is abandoned, not carried all day."""
        session, state = self._state_with_breakout()
        late = Bar(datetime(2026, 7, 21, 11, 40, tzinfo=ET),
                   101.20, 101.30, 100.98, 101.10, 1000)
        result = opening_scalp(
            SCALP_CFG, "ABC", state, [late], session,
            datetime(2026, 7, 21, 11, 45, tzinfo=ET))  # >30m after breakout_end
        self.assertIsNone(result)
        self.assertIsNone(state.breakout)

    def test_failed_retest_after_cutoff_ends_strategy(self):
        session, state = self._state_with_breakout()
        fail = Bar(datetime(2026, 7, 21, 11, 1, tzinfo=ET),
                   101.0, 101.1, 100.4, 100.50, 1000)
        result = opening_scalp(
            SCALP_CFG, "ABC", state, [fail], session,
            datetime(2026, 7, 21, 11, 2, tzinfo=ET))
        self.assertIsNone(result)
        self.assertIsNone(state.breakout)
        # next poll: past cutoff with no live breakout -> done
        opening_scalp(
            SCALP_CFG, "ABC", state, [fail], session,
            datetime(2026, 7, 21, 11, 2, tzinfo=ET))
        self.assertTrue(state.done)


class StructureLevelTests(unittest.TestCase):
    def test_detects_equal_low_liquidity_zone(self):
        now = datetime(2026, 7, 20, 9, 30, tzinfo=ET)
        lows = [101.0, 99.0, 101.0, 99.1, 101.0, 100.0, 101.0]
        bars = [
            Bar(now + timedelta(minutes=5 * i), low + 0.5, low + 1.0,
                low, low + 0.5, 100)
            for i, low in enumerate(lows)
        ]
        zones = detect_zones(
            bars, {"swing_strength": 1, "cluster_pct": 0.20})
        equal_lows = [zone for zone in zones if zone.source == "equal_lows"]
        self.assertEqual(len(equal_lows), 1)
        self.assertEqual(equal_lows[0].touches, 2)
        self.assertAlmostEqual(equal_lows[0].low, 99.0)
        self.assertAlmostEqual(equal_lows[0].high, 99.1)

    def test_stop_uses_zone_beyond_defining_stop(self):
        zone = Zone(99.0, 99.1, 2, "support", "equal_lows")
        selected = stop_for(
            100.5, "long", 99.5, [zone], 1.0,
            {
                "stop_buffer_pct": 0.10,
                "stop_buffer_atr_mult": 0.10,
                "max_stop_atr_mult": 2.0,
                "max_stop_pct": 2.0,
            },
            {"atr_mult": 0.8, "min_stop_pct": 0.15},
        )
        self.assertIsNotNone(selected)
        self.assertLess(selected.price, zone.low)
        self.assertLessEqual(selected.price, 99.5)

    def test_target_uses_opposing_zone_and_rejects_blocked_reward(self):
        resistance = Zone(103.0, 103.1, 2, "resistance", "equal_highs")
        selected = target_for(
            100.5, "long", 98.9, [resistance],
            {"target_front_run_pct": 0.05, "max_target_pct": 5.0},
            {"target_rr": 2.0, "min_reward_risk": 1.5, "max_reward_risk": 2.5},
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.zone, resistance)
        self.assertLess(selected.price, resistance.low)

        blocked = Zone(101.0, 101.1, 1, "resistance", "swing_high")
        self.assertIsNone(target_for(
            100.5, "long", 99.5, [blocked],
            {"target_front_run_pct": 0.05, "max_target_pct": 5.0},
            {"target_rr": 2.0, "min_reward_risk": 1.5, "max_reward_risk": 2.5},
        ))

    def test_intraday_caps_reject_wide_stop_and_cap_target(self):
        support = Zone(99.0, 99.0, 1, "support", "swing_low")
        self.assertIsNone(stop_for(
            100.0, "long", 99.5, [support], 0.5,
            {
                "stop_buffer_pct": 0.05,
                "stop_buffer_atr_mult": 0.05,
                "max_stop_atr_mult": 1.0,
                "max_stop_pct": 0.35,
            },
            {"atr_mult": 0.5, "min_stop_pct": 0.10},
        ))

        far_resistance = Zone(102.0, 102.0, 1, "resistance", "swing_high")
        target = target_for(
            100.0, "long", 99.7, [far_resistance],
            {
                "target_front_run_pct": 0.05,
                "max_target_atr_mult": 1.5,
                "max_target_pct": 0.50,
            },
            {"target_rr": 1.5, "min_reward_risk": 1.5, "max_reward_risk": 2.0},
            atr_5m=0.5,
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.price, 100.5)
        self.assertLessEqual(target.reward_risk, 2.0)


class GuardAndExitTests(unittest.TestCase):
    def test_cooldown_no_flip_and_combo_loss(self):
        bot = bare_bot()
        now = datetime(2026, 7, 20, 12, 0, tzinfo=ET)
        sig = Signal("ABC", "support_hold", "long", 100, 99, 102, {})
        bot.trade_history = [{
            "ticker": "ABC", "strategy": "support_break", "direction": "short",
            "opened": now - timedelta(minutes=20),
            "closed": now - timedelta(minutes=5), "pnl": -1,
        }]
        self.assertIn("cooldown", bot._entry_guard(sig, now))
        bot.trade_history[0]["closed"] = now - timedelta(minutes=16)
        self.assertIn("no-flip", bot._entry_guard(sig, now))
        bot.trade_history[0]["opened"] = now - timedelta(minutes=31)
        bot.trade_history.extend([
            {"ticker": "ABC", "strategy": "support_hold", "direction": "long",
             "opened": now - timedelta(hours=2), "closed": now - timedelta(hours=1), "pnl": -2},
            {"ticker": "ABC", "strategy": "support_hold", "direction": "long",
             "opened": now - timedelta(hours=3), "closed": now - timedelta(hours=2), "pnl": -3},
        ])
        self.assertIn("combo block", bot._entry_guard(sig, now))

    def test_breakeven_and_giveback(self):
        bot = bare_bot()
        sig = Signal("ABC", "support_hold", "long", 100, 99, 102, {"support": 99})
        pos = {
            "signal": sig, "entry_fill": 100.0, "initial_risk": 1.0,
            "bracket": True, "bracket_order_id": "parent", "be_armed": False,
            "max_favorable_r": 0.0,
        }
        self.assertIsNone(bot._update_active_exit_state(pos, 102.0))
        self.assertTrue(pos["be_armed"])
        self.assertEqual(bot.broker.replaced, [("parent", 100.0)])
        self.assertIn("profit giveback", bot._update_active_exit_state(pos, 101.0))

    def test_completed_bar_invalidation(self):
        bot = bare_bot()
        session = datetime(2026, 7, 20, 9, 30, tzinfo=ET)
        opened = datetime(2026, 7, 20, 10, 1, tzinfo=ET)
        sig = Signal("ABC", "support_hold", "long", 100, 99, 102, {"support": 100})
        bars = [
            Bar(datetime(2026, 7, 20, 10, 5 + i, tzinfo=ET),
                100, 100.2, 98.8, 99.0, 1)
            for i in range(5)
        ]
        reason = bot._invalidation_reason({"signal": sig, "opened": opened}, bars, session)
        self.assertIn("thesis invalidated", reason)

    def test_close_timeout_keeps_position_tracked(self):
        bot = bare_bot()
        bot.broker = OrderStateBroker()
        bot.cfg["risk"] = {"max_holding_minutes": 120}
        bot.cfg["orders"]["software_stop_margin_pct"] = 0.2
        bot.flatten_t = datetime.strptime("15:55", "%H:%M").time()
        sig = Signal("ABC", "support_hold", "long", 100, 99, 102, {})
        bot.positions = {"ABC": {
            "signal": sig, "qty": 10.0, "entry_qty": 10.0, "entry_fill": 100.0,
            "opened": datetime(2026, 7, 20, 10, tzinfo=ET), "bracket": False,
            "initial_risk": 1.0,
        }}
        bot._wait_fill = lambda *args, **kwargs: None
        bot.manage_one(
            "ABC", {"ABC": []}, datetime(2026, 7, 20, 10, 5, tzinfo=ET),
            datetime(2026, 7, 20, 9, 30, tzinfo=ET))
        self.assertIn("ABC", bot.positions)
        self.assertEqual(bot.broker.close_calls, 1)

    def test_bracket_cancel_timeout_prevents_competing_close(self):
        bot = bare_bot()
        broker = OrderStateBroker()
        broker.cancel_result = False
        bot.broker = broker
        bot.cfg["risk"] = {"max_holding_minutes": 120}
        bot.cfg["orders"]["software_stop_margin_pct"] = 0.2
        bot.flatten_t = datetime.strptime("15:55", "%H:%M").time()
        sig = Signal("ABC", "support_hold", "long", 100, 99, 102, {})
        bot.positions = {"ABC": {
            "signal": sig, "qty": 10.0, "entry_qty": 10.0, "entry_fill": 100.0,
            "opened": datetime(2026, 7, 20, 10, tzinfo=ET), "bracket": True,
            "bracket_order_id": "parent", "initial_risk": 1.0,
        }}
        bot.manage_one(
            "ABC", {"ABC": []}, datetime(2026, 7, 20, 10, 5, tzinfo=ET),
            datetime(2026, 7, 20, 9, 30, tzinfo=ET))
        self.assertEqual(broker.close_calls, 0)

    def test_failed_partial_records_bracket_as_canceled(self):
        bot = bare_bot()
        bot.broker = OrderStateBroker()
        bot.shutoff = False
        bot.cfg["orders"].update({
            "enable_partial_exit": True, "partial_at_r": 1.0,
            "partial_fraction": 0.5,
        })
        sig = Signal("ABC", "support_hold", "long", 100, 99, 102, {})
        pos = {
            "signal": sig, "qty": 10.0, "entry_qty": 10.0, "entry_fill": 100.0,
            "opened": datetime(2026, 7, 20, 10, tzinfo=ET), "bracket": True,
            "bracket_order_id": "parent", "initial_risk": 1.0,
            "partial_taken": False, "max_favorable_r": 0.0,
        }
        bot._update_active_exit_state(pos, 101.0)
        self.assertFalse(pos["bracket"])
        self.assertIsNone(pos["bracket_order_id"])


class SpreadSamplingTests(unittest.TestCase):
    """The IEX book is often one-sided; gate on the best recent sample."""

    def _broker(self):
        b = Broker.__new__(Broker)
        b._spread_samples = {}
        return b

    def test_returns_tightest_sample_in_window(self):
        b = self._broker()
        for sample in (6.17, 1.85, 0.03):
            b.observe_spread("JNJ", sample, 30.0)
        # a later bad sample must not undo a good one inside the window
        self.assertAlmostEqual(b.observe_spread("JNJ", 9.0, 30.0), 0.03)

    def test_samples_expire_out_of_window(self):
        b = self._broker()
        b.observe_spread("X", 0.01, ttl_seconds=0.0)
        self.assertAlmostEqual(b.observe_spread("X", 4.0, ttl_seconds=0.0), 4.0)

    def test_symbols_do_not_share_samples(self):
        b = self._broker()
        b.observe_spread("A", 0.01, 30.0)
        self.assertAlmostEqual(b.observe_spread("B", 3.0, 30.0), 3.0)


class TargetVersusSpreadTests(unittest.TestCase):
    """Edge must clear the cost of crossing the book twice."""

    CFG = {"context_filters": {
        "enabled": True, "min_target_spread_mult": 10.0,
        "min_atr_pct": 0.0, "max_spread_pct": 99,
    }}

    def _reasons(self, spread_pct):
        sig = Signal("CSCO", "higher_low", "long", 100.0, 99.0, 100.5, {})
        now = datetime(2026, 7, 21, 10, tzinfo=ET)
        result = filters.evaluate_signal(
            self.CFG, sig, [], [], now, now, spread_pct=spread_pct)
        return [r for r in result.reasons if "target" in r]

    def test_calm_but_tight_name_passes(self):
        # 0.5% target against a 0.02% spread = 25x cost. Tradeable.
        self.assertEqual(self._reasons(0.02), [])

    def test_wide_spread_rejected_at_same_target(self):
        # same 0.5% target, 0.20% spread = only 2.5x cost.
        self.assertTrue(self._reasons(0.20))

    def test_disabled_by_default(self):
        cfg = {"context_filters": {"enabled": True, "min_atr_pct": 0.0,
                                   "max_spread_pct": 99}}
        sig = Signal("CSCO", "higher_low", "long", 100.0, 99.0, 100.5, {})
        now = datetime(2026, 7, 21, 10, tzinfo=ET)
        result = filters.evaluate_signal(
            cfg, sig, [], [], now, now, spread_pct=5.0)
        self.assertEqual([r for r in result.reasons if "target" in r], [])


class OpeningRegimeWarmupTests(unittest.TestCase):
    """An unknown regime blocks every gate, so the open must be classifiable."""

    SESSION = datetime(2026, 7, 21, 9, 30, tzinfo=ET)

    def _bars(self, n):
        return [Bar(self.SESSION + timedelta(minutes=i), 100 + i * 0.05,
                    100.2 + i * 0.05, 99.8 + i * 0.05, 100.1 + i * 0.05, 1000)
                for i in range(n)]

    def test_full_ema_needs_23_bars(self):
        cfg = {"ema_period": 20, "ema_slope_bars": 3}
        self.assertEqual(
            filters.market_context(self._bars(22), self.SESSION, cfg).regime,
            "unknown")
        self.assertNotEqual(
            filters.market_context(self._bars(23), self.SESSION, cfg).regime,
            "unknown")

    def test_warmup_classifies_eleven_minutes_earlier(self):
        cfg = {"ema_period": 20, "ema_slope_bars": 3, "opening_ema_period": 9}
        ctx = filters.market_context(self._bars(12), self.SESSION, cfg)
        self.assertNotEqual(ctx.regime, "unknown")
        self.assertTrue(ctx.timeframe.endswith("_fast"))

    def test_warmup_disabled_by_zero(self):
        cfg = {"ema_period": 20, "ema_slope_bars": 3, "opening_ema_period": 0}
        self.assertEqual(
            filters.market_context(self._bars(12), self.SESSION, cfg).regime,
            "unknown")


class StrategyOverrideTests(unittest.TestCase):
    """One strategy's tuning must not loosen the gates protecting the others."""

    @staticmethod
    def _cfg():
        return {"context_filters": {
            "enabled": True, "min_atr_pct": 0.15, "max_spread_pct": 0.05,
            "relative_strength_threshold_pct": 0.0,
            "regime": {"ema_period": 20, "ema_slope_bars": 3,
                       "opening_ema_period": 9},
            "strategy_overrides": {"opening_scalp": {
                "min_atr_pct": 0.05, "relative_strength_threshold_pct": None,
            }},
        }}

    SESSION = datetime(2026, 7, 21, 9, 30, tzinfo=ET)

    def _reasons(self, strategy):
        now = datetime(2026, 7, 21, 9, 45, tzinfo=ET)
        # flat, calm ticker against a rising SPY: trips both the shared ATR
        # floor and the relative-strength screen.
        flat = [Bar(self.SESSION + timedelta(minutes=i), 100, 100.02, 99.98,
                    100, 1000) for i in range(20)]
        spy = [Bar(self.SESSION + timedelta(minutes=i), 100 + i * 0.1,
                   100.1 + i * 0.1, 99.9 + i * 0.1, 100.05 + i * 0.1, 1000)
               for i in range(20)]
        sig = Signal("ABC", strategy, "long", 100.0, 99.5, 101.0, {})
        return filters.evaluate_signal(
            self._cfg(), sig, flat, spy, self.SESSION, now,
            spread_pct=0.02).reasons

    def test_override_exempts_only_the_named_strategy(self):
        scalp = " ".join(self._reasons("opening_scalp"))
        other = " ".join(self._reasons("support_hold"))
        self.assertNotIn("relative strength", scalp)
        self.assertIn("relative strength", other)

    def test_spread_gate_still_applies_to_overridden_strategy(self):
        now = datetime(2026, 7, 21, 9, 45, tzinfo=ET)
        bars = [Bar(self.SESSION + timedelta(minutes=i), 100, 101, 99, 100, 1000)
                for i in range(20)]
        sig = Signal("ABC", "opening_scalp", "long", 100.0, 99.5, 101.0, {})
        reasons = filters.evaluate_signal(
            self._cfg(), sig, bars, bars, self.SESSION, now,
            spread_pct=6.0).reasons
        self.assertTrue(any("spread" in r for r in reasons))


class OwnStopTests(unittest.TestCase):
    """Strategies that mark their own level keep it, clamped to the risk band."""

    STRUCT = {"stop_buffer_pct": 0.12, "stop_buffer_atr_mult": 0.05,
              "max_stop_atr_mult": 2.5, "max_stop_pct": 0.80}
    STOPS = {"atr_mult": 0.5, "min_stop_pct": 0.10}

    def test_own_stop_kept_when_inside_band(self):
        # 100.00 entry, strategy stop 99.70 -> 0.30 risk, inside [0.10, 0.80]
        sel = stop_for(100.0, "long", 99.70, [], 0.4,
                       self.STRUCT, self.STOPS, own_stop=True)
        self.assertIsNotNone(sel)
        self.assertAlmostEqual(sel.price, 99.70, places=4)
        self.assertEqual(sel.zone.source, "strategy_level")

    def test_own_stop_widened_to_min_distance(self):
        # a 0.01 stop is tighter than the floor; must widen, not reject
        sel = stop_for(100.0, "long", 99.99, [], 0.4,
                       self.STRUCT, self.STOPS, own_stop=True)
        self.assertAlmostEqual(sel.distance, 0.20, places=4)  # 0.5 * ATR 0.4
        self.assertAlmostEqual(sel.price, 99.80, places=4)

    def test_own_stop_capped_at_max_distance(self):
        sel = stop_for(100.0, "long", 90.0, [], 0.4,
                       self.STRUCT, self.STOPS, own_stop=True)
        self.assertAlmostEqual(sel.distance, 0.80, places=4)  # 0.80% of 100

    def test_short_mirrors(self):
        sel = stop_for(100.0, "short", 100.30, [], 0.4,
                       self.STRUCT, self.STOPS, own_stop=True)
        self.assertAlmostEqual(sel.price, 100.30, places=4)
        self.assertEqual(sel.zone.side, "resistance")

    def test_zone_search_unchanged_when_flag_off(self):
        # no zones supplied: the old path must still reject
        self.assertIsNone(stop_for(100.0, "long", 99.70, [], 0.4,
                                   self.STRUCT, self.STOPS, own_stop=False))


if __name__ == "__main__":
    unittest.main()
