"""stockbot main loop (multi-position).

Each poll: fetch bars for the whole watchlist -> reconcile + manage every open
position -> risk gates -> scan every un-held ticker for setups -> conflict
check -> judge veto -> enter (bracket order) -> on close, log + learnings.

The bot can hold several positions at once (one per ticker, up to
risk.max_open_positions) across the configured universe (the S&P 100 by default).

Run:  python main.py              # PAPER trading (the only default)
      python main.py --no-judge   # rules engine only, no LLM needed
      python main.py --live       # REAL MONEY: asks you to type a confirmation
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import subprocess
import sys
import time as time_mod
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import yaml

import filters
import judge
import strategies
import structure
from broker import Broker
from strategies import OpeningScalpState, Signal

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))

CSV_FIELDS = [
    "timestamp_open", "timestamp_close", "ticker", "strategy", "direction",
    "qty", "entry_price", "exit_price", "pnl", "r_multiple", "hold_minutes",
    "signal_price", "slippage_pct", "atr_pct", "relative_volume", "spread_pct",
    "market_regime", "relative_strength_pct", "gap_pct", "sector",
    "entry_time_bucket", "veto_decision", "judge_reason", "notes",
]

LIVE_CONFIRM_PHRASE = "I UNDERSTAND THIS IS REAL MONEY"

# Control-plane files the dashboard reads/writes to drive the bot:
#   bot.pid        — this process's PID (present only while running)
#   bot.control    — "run" or "paused"; the loop reads it every tick
#   bot_state.json — all open positions, so a stop/restart can resume mgmt
#   manual_close_<ticker>.request — dashboard initiated a one-position close
PID_FILE = os.path.join(ROOT, "bot.pid")
CONTROL_FILE = os.path.join(ROOT, "bot.control")
STATE_FILE = os.path.join(ROOT, "bot_state.json")
# Touched at the top of every tick. A live pid is not proof of liveness — a
# blocked socket read keeps the process alive while it does nothing at all —
# so the dashboard checks this file's age to tell running from wedged.
HEARTBEAT_FILE = os.path.join(ROOT, "bot.heartbeat")
MANUAL_CLOSE_PREFIX = os.path.join(ROOT, "manual_close_")


def log(msg: str) -> None:
    print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] {msg}", flush=True)


def play_sound(path: str, volume: float = 1.0) -> None:
    """Fire-and-forget trade sound via macOS afplay. Never blocks the trading
    loop and never raises — a missing file or dead audio device must not be
    able to interfere with order handling."""
    try:
        subprocess.Popen(
            ["afplay", "-v", str(volume), path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def time_bucket(t: time) -> str:
    """Session bucket used in trade logs, replay stats, and judge summaries."""
    return (
        "open_0930_1000" if t < time(10, 0)
        else "morning_1000_1130" if t < time(11, 30)
        else "midday_1130_1400" if t < time(14, 0)
        else "afternoon_1400_close"
    )


def load_tickers(cfg: dict) -> list[str]:
    """Universe = a tickers_file (one symbol per line) if set, else the inline
    `tickers` list. Deduped, uppercased, blanks/comments dropped."""
    tf = cfg.get("tickers_file")
    syms: list[str] = []
    if tf:
        path = tf if os.path.isabs(tf) else os.path.join(ROOT, tf)
        with open(path) as f:
            for line in f:
                s = line.strip().upper()
                if s and not s.startswith("#"):
                    syms.append(s)
    else:
        syms = [t.upper() for t in cfg.get("tickers", [])]
    seen, out = set(), []
    for s in syms:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def confirm_live_trading() -> bool:
    """Second half of the live-trading gate (the first is the --live flag).
    Needs a human at an interactive terminal typing the exact phrase."""
    if not sys.stdin.isatty():
        print("Refusing --live: no interactive terminal to confirm on.")
        return False
    print("\n" + "!" * 64)
    print("  LIVE TRADING: this bot will place REAL orders with REAL money")
    print("  on the account behind ALPACA_API_KEY. Losses are your own.")
    print("!" * 64)
    try:
        typed = input(f'Type "{LIVE_CONFIRM_PHRASE}" to continue: ')
    except EOFError:
        return False
    return typed.strip() == LIVE_CONFIRM_PHRASE


class Bot:
    def __init__(self, live: bool = False, use_judge: bool | None = None):
        with open(os.path.join(ROOT, "config.yaml")) as f:
            self.cfg = yaml.safe_load(f)
        if use_judge is not None:
            self.cfg["judge"]["enabled"] = use_judge
        self.strategies_on = strategies.toggles(self.cfg)
        self.broker = Broker(self.cfg, live=live)
        self.tickers = load_tickers(self.cfg)
        self.poll = int(self.cfg["poll_interval_seconds"])
        self.heartbeat_interval = int(self.cfg.get("heartbeat_seconds", 20))
        self._last_heartbeat = 0.0
        self.max_open = int(self.cfg["risk"].get("max_open_positions", 1))
        self.max_entries_per_tick = int(self.cfg.get("max_entries_per_tick", 3))
        self.max_judge_calls_per_tick = int(self.cfg.get("max_judge_calls_per_tick", 6))
        tw = self.cfg["trading_window"]
        self.open_t = parse_hhmm(tw["open"])
        self.close_t = parse_hhmm(tw["close"])
        self.flatten_t = parse_hhmm(tw["flatten_at"])
        self.learnings_path = os.path.join(ROOT, self.cfg["files"]["learnings"])
        self.trades_path = os.path.join(ROOT, self.cfg["files"]["trades_csv"])
        self.playbook_path = os.path.join(
            ROOT, self.cfg["files"].get("playbook", "playbook.md"))
        self.sounds_cfg = self.cfg.get("sounds", {})
        self._ensure_csv_schema()
        self.sectors = self._load_sectors()
        self.earnings_symbols = self._load_earnings_blacklist()
        try:
            self.previous_closes = self.broker.previous_daily_closes(self.tickers)
        except Exception as exc:
            self.previous_closes = {}
            log(f"warning: previous closes unavailable; gap feature omitted ({exc})")

        self.scalp_states = {t: OpeningScalpState() for t in self.tickers}
        self.positions: dict[str, dict] = {}     # ticker -> open-trade record
        self.shutoff = False                      # daily-loss rule tripped
        self.terminate = False                    # set by SIGTERM (dashboard Stop)
        self.blocked_setups: set = set()          # vetoed/duplicate setups, don't re-ask
        self.last_skip_msg: dict[str, str] = {}   # anti-spam for skip logs
        self.daily_pnl = self._seed_daily_pnl()
        self.trade_history = self._seed_trade_history()
        self._paused_logged = False

    def _ensure_csv_schema(self) -> None:
        """Upgrade an older trades CSV header without discarding its rows."""
        if not os.path.exists(self.trades_path):
            return
        with open(self.trades_path, newline="") as f:
            rows = list(csv.DictReader(f))
            existing = list(rows[0].keys()) if rows else []
        if existing == CSV_FIELDS:
            return
        with open(self.trades_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})

    def _load_sectors(self) -> dict[str, str]:
        path = self.cfg.get("risk", {}).get("sectors_file")
        if not path:
            return {}
        full = path if os.path.isabs(path) else os.path.join(ROOT, path)
        try:
            with open(full, newline="") as f:
                return {row["ticker"].upper(): row["sector"]
                        for row in csv.DictReader(f) if row.get("ticker")}
        except (FileNotFoundError, KeyError):
            return {}

    def _load_earnings_blacklist(self) -> set[str]:
        path = self.cfg.get("context_filters", {}).get("earnings_blacklist_file")
        if not path:
            return set()
        full = path if os.path.isabs(path) else os.path.join(ROOT, path)
        try:
            with open(full) as f:
                return {line.strip().upper() for line in f
                        if line.strip() and not line.lstrip().startswith("#")}
        except FileNotFoundError:
            return set()

    # ------------------------------------------------------------------ setup

    def _seed_daily_pnl(self) -> float:
        """Sum realized P&L already logged today (survives a restart)."""
        if not os.path.exists(self.trades_path):
            return 0.0
        today = datetime.now(ET).strftime("%Y-%m-%d")
        total = 0.0
        with open(self.trades_path) as f:
            for row in csv.DictReader(f):
                if row.get("timestamp_close", "").startswith(today) and row.get("pnl"):
                    try:
                        total += float(row["pnl"])
                    except ValueError:
                        pass
        return total

    def _seed_trade_history(self) -> list[dict]:
        """Load today's completed trades so cooldown/loss guards survive restarts."""
        if not os.path.exists(self.trades_path):
            return []
        today = datetime.now(ET).date()
        history = []
        with open(self.trades_path) as f:
            for row in csv.DictReader(f):
                try:
                    opened = datetime.fromisoformat(row["timestamp_open"])
                    closed_text = row.get("timestamp_close", "")
                    closed = datetime.fromisoformat(closed_text) if closed_text else None
                except (KeyError, ValueError):
                    continue
                if opened.astimezone(ET).date() != today or not closed:
                    continue
                try:
                    pnl = float(row["pnl"])
                except (KeyError, ValueError):
                    pnl = 0.0
                history.append({
                    "ticker": row.get("ticker", "").upper(),
                    "strategy": row.get("strategy", ""),
                    "direction": row.get("direction", ""),
                    "opened": opened.astimezone(ET),
                    "closed": closed.astimezone(ET),
                    "pnl": pnl,
                })
        return history

    def _append_trade_row(self, **row) -> None:
        new_file = not os.path.exists(self.trades_path)
        with open(self.trades_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in CSV_FIELDS})

    def _append_learnings(self, entry: str) -> None:
        with open(self.learnings_path, "a") as f:
            f.write(entry.rstrip() + "\n\n")

    def _read_learnings_for(self, strategy: str, max_entries: int = 5) -> str:
        if not os.path.exists(self.learnings_path):
            return ""
        with open(self.learnings_path) as f:
            content = f.read()
        sections = ["## " + s for s in content.split("## ") if s.strip()]
        relevant = [s for s in sections if strategy.upper() in s.upper()]
        return "\n".join(relevant[-max_entries:])

    def _read_playbook_for(self, strategy: str) -> str:
        """Strategy section of the auto-generated replay playbook (see
        trainer.py). Statistical AVOID/EDGE lines only — never ticker-level."""
        if not os.path.exists(self.playbook_path):
            return ""
        with open(self.playbook_path) as f:
            content = f.read()
        sections = ["## " + s for s in content.split("## ") if s.strip()]
        relevant = [s for s in sections if strategy.upper() in s.upper()]
        return "\n".join(relevant)

    def _skip(self, key: str, msg: str) -> None:
        """Log a skip reason, but only when it changes (no per-tick spam)."""
        if self.last_skip_msg.get(key) != msg:
            self.last_skip_msg[key] = msg
            log(msg)

    # ------------------------------------------------- control-plane / state

    def _is_paused(self) -> bool:
        try:
            with open(CONTROL_FILE) as f:
                return f.read().strip().lower() == "paused"
        except FileNotFoundError:
            return False

    def _pos_to_dict(self, pos: dict) -> dict:
        sig: Signal = pos["signal"]
        return {
            "ticker": sig.ticker, "strategy": sig.strategy, "direction": sig.direction,
            "entry": sig.entry, "stop": sig.stop, "target": sig.target,
            "reasoning": sig.reasoning, "qty": pos["qty"], "entry_fill": pos["entry_fill"],
            "opened": pos["opened"].isoformat(), "bracket": pos.get("bracket", False),
            "bracket_order_id": pos.get("bracket_order_id"),
            "initial_risk": pos.get("initial_risk"),
            "be_armed": pos.get("be_armed", False),
            "max_favorable_r": pos.get("max_favorable_r", 0.0),
            "active_stop": pos.get("active_stop"),
            "partial_taken": pos.get("partial_taken", False),
            "realized_partial_pnl": pos.get("realized_partial_pnl", 0.0),
            "entry_qty": pos.get("entry_qty", pos["qty"]),
        }

    def _save_state(self) -> None:
        """Persist ALL open positions so a stop/restart resumes managing them."""
        try:
            if not self.positions:
                self._clear_state()
                return
            with open(STATE_FILE, "w") as f:
                json.dump([self._pos_to_dict(p) for p in self.positions.values()], f)
        except Exception as e:
            log(f"warning: could not write state file: {e}")

    def _clear_state(self) -> None:
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass

    def _adopt_persisted_positions(self) -> None:
        """On startup, resume managing any saved positions the broker still holds."""
        data = []
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
            except Exception as exc:
                log(f"warning: state file is corrupt; broker recovery will flatten "
                    f"untracked positions ({exc})")
                data = []
        try:
            held = {p.symbol: p for p in self.broker.open_positions()}
            open_orders = list(self.broker.open_orders())
        except Exception as e:
            log(f"warning: could not check broker state: {e}")
            self.shutoff = True
            return
        open_order_symbols = {order.symbol for order in open_orders}
        orphan_order_symbols = open_order_symbols - set(held)
        for ticker in sorted(orphan_order_symbols):
            try:
                if not self.broker.cancel_orders_for_symbol_and_wait(ticker):
                    raise TimeoutError("orphan-order cancellation timed out")
                log(f"RECOVERY CANCELED orphan open order(s) for {ticker}")
            except Exception as exc:
                self.shutoff = True
                log(f"SAFETY SHUTOFF: could not cancel orphan {ticker} order ({exc})")
        if isinstance(data, dict):          # migrate old single-position format
            data = [data]
        adopted = 0
        for s in data:
            if s["ticker"] not in held:
                continue
            broker_pos = held[s["ticker"]]
            actual_direction = (
                "short" if "short" in str(getattr(broker_pos, "side", "")).lower()
                or float(broker_pos.qty) < 0 else "long")
            if actual_direction != s["direction"]:
                log(f"RECOVERY: {s['ticker']} direction differs from state; "
                    "treating it as untracked")
                continue
            bracket_verified = bool(
                s.get("bracket", False) and s["ticker"] in open_order_symbols)
            if s.get("bracket", False) and not bracket_verified:
                log(f"RECOVERY: {s['ticker']} has no live protective orders; "
                    "switching to software protection")
            sig = Signal(s["ticker"], s["strategy"], s["direction"], s["entry"],
                         s["stop"], s["target"], s.get("reasoning", {}))
            self.positions[s["ticker"]] = {
                "signal": sig, "qty": abs(float(broker_pos.qty)),
                "entry_fill": float(broker_pos.avg_entry_price),
                "opened": datetime.fromisoformat(s["opened"]), "bracket": bracket_verified,
                "bracket_order_id": s.get("bracket_order_id") if bracket_verified else None,
                "initial_risk": s.get("initial_risk", abs(s["entry_fill"] - s["stop"])),
                "be_armed": s.get("be_armed", False),
                "max_favorable_r": s.get("max_favorable_r", 0.0),
                "active_stop": s.get("active_stop"),
                "partial_taken": s.get("partial_taken", False),
                "realized_partial_pnl": s.get("realized_partial_pnl", 0.0),
                "entry_qty": s.get("entry_qty", s["qty"])}
            self.daily_pnl += float(s.get("realized_partial_pnl", 0.0))
            self.blocked_setups.add(self.setup_key(sig))
            prot = "broker bracket + software" if bracket_verified else "software monitor"
            log(f"RESUMED {sig.direction} {abs(float(broker_pos.qty)):g} {sig.ticker} "
                f"@ {float(broker_pos.avg_entry_price):.2f} "
                f"(SL {sig.stop:.2f} / TP {sig.target:.2f}) [{prot}]")
            adopted += 1
        if adopted:
            log(f"resumed {adopted} open position(s) from previous run")
        untracked = set(held) - set(self.positions)
        if untracked:
            close_unknown = self.cfg.get("risk", {}).get(
                "close_untracked_positions_on_startup", True)
            for ticker in sorted(untracked):
                if not close_unknown:
                    self.shutoff = True
                    log(f"SAFETY SHUTOFF: broker holds untracked {ticker}; "
                        "manual reconciliation required")
                    continue
                try:
                    if not self.broker.cancel_orders_for_symbol_and_wait(ticker):
                        raise TimeoutError("protective-order cancellation timed out")
                    order = self.broker.close_position(ticker)
                    filled = self._wait_fill(order.id, timeout_s=60)
                    if filled is None:
                        raise TimeoutError("recovery close not confirmed")
                    log(f"RECOVERY FLATTENED untracked broker position {ticker} @ "
                        f"{float(filled.filled_avg_price):.2f}")
                except Exception as exc:
                    self.shutoff = True
                    log(f"SAFETY SHUTOFF: could not flatten untracked {ticker} ({exc})")
        self._save_state()  # drop any state entries no longer held

    def _touch_heartbeat(self) -> None:
        """Record that the poll loop is genuinely cycling (see HEARTBEAT_FILE)."""
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(datetime.now(ET).isoformat(timespec="seconds"))
        except OSError:
            pass  # never let heartbeat bookkeeping break a trading tick

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in 1s slices so SIGTERM (Stop) is reacted to promptly."""
        end = time_mod.time() + seconds
        while time_mod.time() < end and not self.terminate:
            time_mod.sleep(min(1.0, end - time_mod.time()))

    # ------------------------------------------------------------------- data

    def fetch_bars(self, session_open: datetime) -> dict[str, list]:
        from datetime import timezone
        start = session_open.astimezone(timezone.utc)
        symbols = list(dict.fromkeys([*self.tickers, "SPY"]))
        raw = self.broker.minute_bars(symbols, start)
        # drop the current, still-forming minute
        cur_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        return {t: [b for b in bs if b.ts < cur_minute] for t, bs in raw.items()}

    # -------------------------------------------------------------- strategies

    def collect_signals(self, ticker: str, bars_1m_et: list, session_open: datetime,
                        now: datetime) -> list[Signal]:
        signals = []
        runs = []
        on = self.strategies_on
        support_signals = [n for n in strategies.SUPPORT_SIGNALS if on[n]]
        if on["opening_scalp"]:
            runs.append(lambda: strategies.opening_scalp(
                self.cfg["strategy_1_opening_scalp"], ticker,
                self.scalp_states[ticker], bars_1m_et, session_open, now))
        if on["ob_fvg"]:
            runs.append(lambda: strategies.ob_fvg(
                self.cfg["strategy_2_ob_fvg"], ticker, bars_1m_et, session_open))
        if support_signals:
            runs.append(lambda: strategies.support_structure(
                {**self.cfg["strategy_3_support"], "enabled_signals": support_signals},
                ticker, bars_1m_et, session_open))
        if self.cfg["price_levels"]:
            runs.append(lambda: strategies.price_level(
                self.cfg["price_levels"], self.cfg["price_level_settings"],
                ticker, bars_1m_et))
        for run in runs:
            try:
                sig = run()
            except Exception as e:  # fail safe: a broken detector never trades
                log(f"{ticker}: strategy error, skipping ({type(e).__name__}: {e})")
                sig = None
            if sig:
                if ticker in self.previous_closes:
                    sig.reasoning["previous_close"] = self.previous_closes[ticker]
                signals.append(sig)
        return signals

    @staticmethod
    def resolve_conflict(signals: list[Signal]) -> tuple[Signal | None, str]:
        if not signals:
            return None, ""
        directions = {s.direction for s in signals}
        if len(directions) > 1:
            names = ", ".join(f"{s.strategy}:{s.direction}" for s in signals)
            return None, f"conflict ({names}) -> no trade"
        if len(signals) > 1:
            best = min(signals, key=lambda s: s.risk_per_share)
            return best, f"{len(signals)} aligned signals, tightest stop wins ({best.strategy})"
        return signals[0], ""

    # ---------------------------------------------------------------- executing

    def setup_key(self, sig: Signal) -> tuple:
        return (sig.ticker, sig.strategy, sig.direction, round(sig.stop, 2))

    def size_order(self, sig: Signal) -> tuple[float | None, float | None, str]:
        """Returns (qty, notional, why_skipped). Sizes each trade up to
        max_per_trade_usd, bounded by available buying power (so many
        concurrent positions can't over-commit the account)."""
        budget = float(self.cfg["risk"]["max_per_trade_usd"])
        # Absolute per-order notional ceiling, independent of every other
        # budget input. Backstop for the 2026-07-20 event where four ~$100k
        # orders went out under a mis-set max_per_trade_usd.
        hard_cap = float(self.cfg["risk"].get("max_notional_per_trade_usd", 15000))
        budget = min(budget, hard_cap)
        risk_budget = float(self.cfg["risk"].get("risk_per_trade_usd", budget))
        current_stop_risk = sum(
            float(pos.get("initial_risk", pos["signal"].risk_per_share)) * float(pos["qty"])
            for pos in self.positions.values())
        portfolio_risk_cap = float(self.cfg["risk"].get(
            "max_portfolio_stop_risk_usd", risk_budget * self.max_open))
        risk_budget = min(risk_budget, max(0.0, portfolio_risk_cap - current_stop_risk))
        current_gross = sum(
            float(pos["entry_fill"]) * float(pos["qty"]) for pos in self.positions.values())
        gross_cap = float(self.cfg["risk"].get("max_gross_exposure_usd", budget * self.max_open))
        budget = min(budget, max(0.0, gross_cap - current_gross))
        try:
            bp = float(self.broker.account().buying_power)
        except Exception as e:
            return None, None, f"cannot read buying power ({e})"
        budget = min(budget, bp)
        price = sig.entry
        fill_buffer = float(self.cfg["risk"].get("fill_risk_buffer_pct", 5.0)) / 100
        risk_qty = risk_budget * (1 - fill_buffer) / sig.risk_per_share
        budget_qty = budget / price
        desired_qty = min(risk_qty, budget_qty)
        if desired_qty <= 0:
            return None, None, "portfolio risk or gross-exposure budget exhausted"
        if budget < price and not (sig.direction == "long" and self.cfg["alpaca"]["allow_fractional"]):
            return None, None, (f"insufficient buying power ${budget:.2f} for 1 share of "
                                f"{sig.ticker} (${price:.2f})")
        whole = int(desired_qty)
        if whole >= 1:
            return float(whole), None, ""
        if sig.direction == "long" and self.cfg["alpaca"]["allow_fractional"]:
            return None, round(desired_qty * price, 2), ""
        return None, None, (f"1 share of {sig.ticker} (${price:.2f}) exceeds ${budget:.2f} budget "
                            f"and fractional not possible for {sig.direction}")

    # map a signal's strategy name to the config section holding its reward_risk
    _STRATEGY_CFG = {
        "opening_scalp": "strategy_1_opening_scalp",
        "ob_fvg": "strategy_2_ob_fvg",
        "support_break": "strategy_3_support",
        "support_hold": "strategy_3_support",
        "higher_low": "strategy_3_support",
        "price_level": "price_level_settings",
    }

    def _required_rr(self, strategy: str) -> float:
        section = self._STRATEGY_CFG.get(strategy)
        if section and section in self.cfg:
            return float(self.cfg[section].get("reward_risk", 2.0))
        return 2.0

    def _apply_legacy_stop_floor(
            self, sig: Signal, bars_1m: list, session_open: datetime) -> None:
        """Keep the structural stop unless it is inside normal 5m noise."""
        cfg = self.cfg.get("stops", {})
        bars_5m = strategies.aggregate(bars_1m, 5, session_open)
        atr_value = strategies.atr(bars_5m, int(cfg.get("atr_period", 14)))
        if atr_value is None:
            atr_value = strategies.atr(bars_1m, int(cfg.get("atr_period", 14)))
        atr_floor = (atr_value or 0.0) * float(cfg.get("atr_mult", 0.8))
        pct_floor = sig.entry * float(cfg.get("min_stop_pct", 0.15)) / 100.0
        floor_risk = max(atr_floor, pct_floor)
        if sig.risk_per_share >= floor_risk:
            return
        old_stop = sig.stop
        sig.stop = (sig.entry - floor_risk if sig.direction == "long"
                    else sig.entry + floor_risk)
        sig.reasoning["stop_floor"] = (
            f"widened {old_stop:.2f}->{sig.stop:.2f}; "
            f"ATR floor {atr_floor:.2f}, pct floor {pct_floor:.2f}")

    def _set_legacy_take_profit(self, sig: Signal) -> None:
        """Set the target near 2R. Prefer a whole dollar only when its R:R is
        inside the configured band; otherwise retain the exact 2R target."""
        cfg = self.cfg.get("orders", {})
        target_rr = float(cfg.get("target_rr", self._required_rr(sig.strategy)))
        min_rr = float(cfg.get("min_reward_risk", 1.5))
        max_rr = float(cfg.get("max_reward_risk", 2.5))
        risk = sig.risk_per_share
        sign = 1 if sig.direction == "long" else -1
        if cfg.get("target_mode") == "price_grid":
            base = math.floor(sig.entry)
            fraction = sig.entry - base
            if sig.direction == "long":
                grid_target = base + (1.0 if fraction < 0.5 else 1.5)
            else:
                grid_target = base - (0.5 if fraction < 0.5 else 0.0)
            desired_reward = abs(grid_target - sig.entry)
            bounded_reward = min(max(desired_reward, min_rr * risk), max_rr * risk)
            sig.target = round(sig.entry + sign * bounded_reward, 2)
            sig.reasoning["target_rule"] = (
                f"price grid {grid_target:.2f}, bounded to "
                f"{min_rr:.1f}-{max_rr:.1f}R")
            return
        ideal = sig.entry + sign * target_rr * risk
        sig.target = round(ideal, 2)
        if not cfg.get("round_take_profit_to_whole", True):
            return
        low_price = sig.entry + sign * min_rr * risk
        high_price = sig.entry + sign * max_rr * risk
        lower, upper = sorted((low_price, high_price))
        whole_candidates = range(math.ceil(lower), math.floor(upper) + 1)
        valid = [float(p) for p in whole_candidates
                 if (p > sig.entry if sig.direction == "long" else p < sig.entry)]
        if valid:
            sig.target = min(valid, key=lambda p: abs(p - ideal))

    def _apply_structure_levels(
            self, sig: Signal, bars_1m: list, session_open: datetime) -> str:
        """Place SL/TP from completed-bar structure; return why entry must skip."""
        orders_cfg = self.cfg.get("orders", {})
        if orders_cfg.get("target_mode") != "structure":
            self._apply_legacy_stop_floor(sig, bars_1m, session_open)
            self._set_legacy_take_profit(sig)
            return ""

        sig.reasoning.setdefault("strategy_stop", sig.stop)
        structural_stop = float(sig.reasoning["strategy_stop"])
        stop_cfg = self.cfg.get("stops", {})
        structure_cfg = self.cfg.get("structure", {})
        bars_5m = strategies.aggregate(bars_1m, 5, session_open)
        atr_value = strategies.atr(
            bars_5m, int(stop_cfg.get("atr_period", 14)))
        if atr_value is None:
            atr_value = strategies.atr(
                bars_1m, int(stop_cfg.get("atr_period", 14)))
        previous_close = sig.reasoning.get("previous_close")
        zones = structure.detect_zones(
            bars_5m, structure_cfg,
            float(previous_close) if previous_close else None)
        stop = structure.stop_for(
            sig.entry, sig.direction, structural_stop, zones, atr_value,
            structure_cfg, stop_cfg,
            own_stop=sig.strategy in structure_cfg.get(
                "strategies_define_own_stop", []))
        if stop is None:
            return "no valid structural stop zone within configured distance"
        sig.stop = stop.price
        sig.reasoning["stop_zone"] = stop.zone.describe()
        sig.reasoning["stop_rule"] = (
            f"beyond {stop.zone.source}; risk {stop.distance:.2f}"
            + (f", ATR {atr_value:.2f}" if atr_value is not None else ""))

        target = structure.target_for(
            sig.entry, sig.direction, sig.stop, zones, structure_cfg, orders_cfg,
            atr_value)
        if target is None:
            return "target is outside quick-trade cap or below minimum reward:risk"
        sig.target = target.price
        sig.reasoning["target_zone"] = (
            target.zone.describe() if target.zone else "none")
        sig.reasoning["target_rule"] = (
            f"{target.source}; {target.reward_risk:.2f}R")
        return ""

    def sanity_check(self, sig: Signal) -> str:
        """Deterministic pre-checks Part A owns (never delegated to the LLM).
        Returns '' if fine, else the reason to skip."""
        order_cfg = self.cfg.get("orders", {})
        min_rr = float(order_cfg.get("min_reward_risk", 1.5))
        max_rr = float(order_cfg.get("max_reward_risk", 2.5))
        if sig.entry <= 0 or sig.risk_per_share <= 0:
            return "non-positive entry or zero risk"
        if sig.direction == "long" and not (sig.stop < sig.entry < sig.target):
            return f"long but not stop<entry<target ({sig.stop:.2f}/{sig.entry:.2f}/{sig.target:.2f})"
        if sig.direction == "short" and not (sig.target < sig.entry < sig.stop):
            return f"short but not target<entry<stop ({sig.target:.2f}/{sig.entry:.2f}/{sig.stop:.2f})"
        rr = abs(sig.target - sig.entry) / sig.risk_per_share
        if rr < min_rr - 0.01 or rr > max_rr + 0.01:
            return f"reward:risk {rr:.2f} outside allowed {min_rr:.2f}-{max_rr:.2f}"
        return ""

    def _count_past_losses(self, sig: Signal) -> int:
        """Count LOSS entries in learnings.md for this ticker+strategy."""
        text = self._read_learnings_for(sig.strategy, max_entries=50)
        n = 0
        for line in text.splitlines():
            if line.startswith("## ") and "LOSS" in line.upper() \
                    and sig.ticker.upper() in line.upper():
                n += 1
        return n

    def _entry_guard(self, sig: Signal, now: datetime) -> str:
        cfg = self.cfg.get("entry_guards", {})
        risk_cfg = self.cfg.get("risk", {})
        used_trade_slots = len(self.trade_history) + len(self.positions)
        trade_cap = int(risk_cfg.get("max_trades_per_day", 0))
        if trade_cap > 0 and used_trade_slots >= trade_cap:
            return f"daily trade cap reached ({used_trade_slots})"
        sectors = getattr(self, "sectors", {})
        sector = sectors.get(sig.ticker, "Unknown")
        if sector == "Unknown" and risk_cfg.get("sectors_file"):
            return "sector unavailable (fail-closed)"
        same_sector = sum(
            1 for pos in self.positions.values()
            if sectors.get(pos["signal"].ticker, "Unknown") == sector)
        cap = int(risk_cfg.get("max_positions_per_sector", 3))
        if same_sector >= cap:
            return f"sector cap reached for {sector} ({same_sector}/{cap})"
        same_direction = sum(
            1 for pos in self.positions.values()
            if pos["signal"].direction == sig.direction)
        direction_cap = int(risk_cfg.get(
            "max_net_positions_one_direction", getattr(self, "max_open", 1)))
        if same_direction >= direction_cap:
            return f"{sig.direction} exposure cap reached ({same_direction}/{direction_cap})"
        symbol_history = [h for h in self.trade_history if h["ticker"] == sig.ticker]
        if symbol_history:
            latest_close = max(h["closed"] for h in symbol_history)
            cooldown = float(cfg.get("symbol_cooldown_min", 15))
            minutes = (now - latest_close).total_seconds() / 60
            if minutes < cooldown:
                return f"symbol cooldown: closed {minutes:.1f}m ago (need {cooldown:g}m)"
            latest_trade = max(symbol_history, key=lambda h: h["opened"])
            no_flip = float(cfg.get("no_flip_min", 30))
            since_entry = (now - latest_trade["opened"]).total_seconds() / 60
            if latest_trade["direction"] != sig.direction and since_entry < no_flip:
                return (f"no-flip: last {latest_trade['direction']} entry was "
                        f"{since_entry:.1f}m ago (need {no_flip:g}m)")
        losses = sum(
            1 for h in self.trade_history
            if h["ticker"] == sig.ticker and h["strategy"] == sig.strategy and h["pnl"] < 0)
        max_losses = int(cfg.get("max_combo_losses_per_day", 2))
        if losses >= max_losses:
            return f"daily combo block: {losses} losses on {sig.ticker}/{sig.strategy}"
        return ""

    def _recent_symbol_activity(self, ticker: str, now: datetime) -> str:
        lookback = float(self.cfg.get("entry_guards", {}).get("judge_history_minutes", 60))
        lines = []
        for h in sorted(self.trade_history, key=lambda x: x["opened"], reverse=True):
            if h["ticker"] != ticker:
                continue
            minutes = (now - h["opened"]).total_seconds() / 60
            if minutes <= lookback:
                outcome = "WIN" if h["pnl"] >= 0 else "LOSS"
                lines.append(
                    f"{minutes:.1f}m ago: {h['direction']} {h['strategy']} "
                    f"{outcome} ({h['pnl']:+.2f})")
        return "; ".join(lines) if lines else "none in lookback"

    def summarize_setup(self, sig: Signal, qty_desc: str) -> str:
        rr = abs(sig.target - sig.entry) / max(sig.risk_per_share, 1e-9)
        min_rr = self.cfg["orders"].get("min_reward_risk", 1.5)
        max_rr = self.cfg["orders"].get("max_reward_risk", 2.5)
        lines = [
            f"Ticker: {sig.ticker}",
            f"Strategy: {sig.strategy}",
            f"Direction: {sig.direction}",
            f"Entry: {sig.entry:.2f}  Stop: {sig.stop:.2f}  Target: {sig.target:.2f}",
            f"Reward:risk = {rr:.2f} (allowed band {min_rr}-{max_rr})",
            f"Size: {qty_desc} (max ${self.cfg['risk']['max_per_trade_usd']})",
            f"Open positions now: {len(self.positions)}/{self.max_open}",
            f"Daily realized P&L so far: ${self.daily_pnl:+.2f} "
            f"(shutoff at -${self.cfg['risk']['daily_loss_limit_usd']})",
            f"Past losses on this exact setup: {self._count_past_losses(sig)}",
            f"Recent same-symbol activity: {self._recent_symbol_activity(sig.ticker, datetime.now(ET))}",
            f"Allowed reward:risk band: {min_rr}-{max_rr}",
            f"Entry time bucket: {time_bucket(datetime.now(ET).time())}",
            "Conditions met:",
        ]
        for k, v in sig.reasoning.items():
            lines.append(f"  - {k}: {v}")
        return "\n".join(lines)

    def _context_gate(self, sig: Signal, bars_1m: list, spy_bars_1m: list,
                      session_open: datetime, now: datetime) -> bool:
        try:
            bid, ask, quote_ts = self.broker.latest_quote(sig.ticker)
            quote_age = (datetime.now(quote_ts.tzinfo) - quote_ts).total_seconds()
            max_age = float(self.cfg.get("context_filters", {}).get(
                "max_quote_age_seconds", 30))
            if quote_age > max_age:
                raise ValueError(f"stale quote ({quote_age:.1f}s > {max_age:g}s)")
            midpoint = (bid + ask) / 2
            spread_pct = (ask - bid) / midpoint * 100 if midpoint else None
            if spread_pct is not None:
                # Gate on the tightest spread seen recently, not this instant's
                # sample — the free IEX book is frequently one-sided and reports
                # spreads that cannot be real. See Broker.observe_spread.
                spread_pct = self.broker.observe_spread(
                    sig.ticker, spread_pct,
                    float(self.cfg.get("context_filters", {}).get(
                        "spread_sample_window_seconds", 30)))
        except Exception as exc:
            self._skip(f"context:{sig.ticker}",
                       f"SKIP {sig.ticker} {sig.strategy}: spread unavailable ({exc})")
            return False
        result = filters.evaluate_signal(
            self.cfg, sig, bars_1m, spy_bars_1m, session_open, now,
            spread_pct=spread_pct, earnings_symbols=self.earnings_symbols)
        sig.reasoning.update(result.features)
        sig.reasoning["sector"] = self.sectors.get(sig.ticker, "Unknown")
        if not result.allowed:
            self._skip(
                f"context:{sig.ticker}",
                f"SKIP {sig.ticker} {sig.strategy}: context gate — "
                + "; ".join(result.reasons))
            return False
        self.last_skip_msg.pop(f"context:{sig.ticker}", None)
        return True

    def try_enter(self, sig: Signal, bars_1m: list, session_open: datetime) -> bool:
        """Attempt one entry. Returns True only if a position was opened."""
        # State can lag the broker after a crash/restart while an entry order is
        # still in flight. Broker truth wins: never stack onto an untracked
        # position or order for the same symbol.
        try:
            broker_symbols = {p.symbol for p in self.broker.open_positions()}
            order_symbols = {o.symbol for o in self.broker.open_orders()}
        except Exception as e:
            log(f"SKIP {sig.ticker} {sig.strategy}: cannot preflight broker state ({e})")
            return False
        if sig.ticker in broker_symbols and sig.ticker not in self.positions:
            log(f"SKIP {sig.ticker}: broker already holds an untracked position")
            return False
        if sig.ticker in order_symbols and sig.ticker not in self.positions:
            log(f"SKIP {sig.ticker}: broker already has an untracked open order")
            return False
        # A completed-bar signal can be several seconds old. Re-anchor risk math
        # to a fresh quote so a market order never arrives beyond its own stop.
        signal_price = sig.entry
        sig.reasoning["signal_price"] = round(signal_price, 4)
        try:
            live_entry = self.broker.latest_price(sig.ticker)
        except Exception as e:
            log(f"SKIP {sig.ticker} {sig.strategy}: cannot verify live entry ({e})")
            return False
        crossed_stop = (live_entry <= sig.stop if sig.direction == "long"
                        else live_entry >= sig.stop)
        if crossed_stop:
            log(f"SKIP {sig.ticker} {sig.strategy}: live price {live_entry:.2f} "
                f"already crossed structural stop {sig.stop:.2f}")
            return False
        if abs(live_entry - sig.entry) >= 0.005:
            sig.reasoning["live_entry_adjustment"] = f"{sig.entry:.2f}->{live_entry:.2f}"
        sig.entry = live_entry
        structure_skip = self._apply_structure_levels(sig, bars_1m, session_open)
        if structure_skip:
            self._skip(
                f"structure:{sig.ticker}",
                f"SKIP {sig.ticker} {sig.strategy}: {structure_skip}")
            return False
        key = self.setup_key(sig)
        if key in self.blocked_setups or sig.ticker in self.positions:
            return False
        guard = self._entry_guard(sig, datetime.now(ET))
        if guard:
            self._skip(f"guard:{sig.ticker}", f"SKIP {sig.ticker} {sig.strategy}: {guard}")
            return False
        bad = self.sanity_check(sig)
        if bad:
            self.blocked_setups.add(key)
            log(f"SKIP {sig.ticker} {sig.strategy}: failed sanity check — {bad}")
            self._append_trade_row(timestamp_open=datetime.now(ET).isoformat(timespec="seconds"),
                                   ticker=sig.ticker, strategy=sig.strategy,
                                   direction=sig.direction, entry_price=f"{sig.entry:.2f}",
                                   veto_decision="failed_sanity", notes=bad)
            return False
        qty, notional, skip_why = self.size_order(sig)
        if skip_why:
            # don't permanently block on a transient buying-power shortage
            transient = "buying power" in skip_why
            if not transient:
                self.blocked_setups.add(key)
            log(f"SKIP {sig.ticker} {sig.strategy}: {skip_why}")
            self._append_trade_row(timestamp_open=datetime.now(ET).isoformat(timespec="seconds"),
                                   ticker=sig.ticker, strategy=sig.strategy,
                                   direction=sig.direction, entry_price=f"{sig.entry:.2f}",
                                   veto_decision="not_sent", notes=skip_why)
            return False

        qty_desc = f"{qty:g} shares" if qty else f"${notional:.2f} notional (fractional)"
        summary = self.summarize_setup(sig, qty_desc)
        learnings = self._read_learnings_for(sig.strategy)
        playbook = self._read_playbook_for(sig.strategy)
        if playbook:
            learnings = ((learnings + "\n\n") if learnings else "") + \
                "REPLAY PLAYBOOK (statistical, from offline replay training):\n" + playbook
        log(f"CANDIDATE {sig.ticker} {sig.strategy} {sig.direction} "
            f"entry~{sig.entry:.2f} SL {sig.stop:.2f} TP {sig.target:.2f} -> asking judge")
        approved, reason = judge.veto_check(self.cfg["judge"], summary, learnings)
        if not approved:
            self.blocked_setups.add(key)
            log(f"VETO {sig.ticker} {sig.strategy}: {reason}")
            self._append_trade_row(timestamp_open=datetime.now(ET).isoformat(timespec="seconds"),
                                   ticker=sig.ticker, strategy=sig.strategy,
                                   direction=sig.direction, entry_price=f"{sig.entry:.2f}",
                                   veto_decision="vetoed", judge_reason=reason, notes=reason)
            return False
        sig.reasoning["judge_reason"] = reason
        log(f"APPROVED ({'rules only' if reason == judge.RULES_ONLY_REASON else 'judge'}): {reason}")

        # Judge latency can be material for a scalp. Reprice and rerun every
        # deterministic risk calculation before an order leaves the process.
        try:
            approved_entry = sig.entry
            post_approval_entry = self.broker.latest_price(sig.ticker)
        except Exception as exc:
            log(f"SKIP {sig.ticker}: cannot reprice after approval ({exc})")
            return False
        drift_pct = abs(post_approval_entry - approved_entry) / approved_entry * 100
        max_drift = float(self.cfg.get("context_filters", {}).get(
            "max_post_approval_drift_pct", 0.10))
        if drift_pct > max_drift:
            log(f"SKIP {sig.ticker}: post-approval drift {drift_pct:.3f}% "
                f"exceeds {max_drift:.3f}%")
            self.blocked_setups.add(key)
            return False
        crossed_stop = (post_approval_entry <= sig.stop if sig.direction == "long"
                        else post_approval_entry >= sig.stop)
        if crossed_stop:
            log(f"SKIP {sig.ticker}: post-approval price crossed stop")
            self.blocked_setups.add(key)
            return False
        sig.entry = post_approval_entry
        structure_skip = self._apply_structure_levels(sig, bars_1m, session_open)
        if structure_skip:
            log(f"SKIP {sig.ticker}: post-approval {structure_skip}")
            self.blocked_setups.add(key)
            return False
        bad = self.sanity_check(sig)
        if bad:
            log(f"SKIP {sig.ticker}: post-approval sanity failed — {bad}")
            self.blocked_setups.add(key)
            return False
        qty, notional, skip_why = self.size_order(sig)
        if skip_why:
            log(f"SKIP {sig.ticker}: post-approval sizing failed — {skip_why}")
            return False

        side = "buy" if sig.direction == "long" else "sell"
        use_bracket = (self.cfg.get("orders", {}).get("use_bracket_orders", True)
                       and qty is not None)  # bracket needs whole shares
        bracket_placed = False
        order = None
        filled = None
        try:
            if use_bracket:
                order = self.broker.submit_bracket_order(
                    sig.ticker, side, qty=qty,
                    take_profit_price=round(sig.target, 2), stop_price=round(sig.stop, 2))
                bracket_placed = True
            else:
                if self.cfg.get("orders", {}).get("use_bracket_orders", True):
                    log(f"{sig.ticker}: fractional/notional order — bracket not possible, "
                        f"using software TP/SL only")
                order = self.broker.submit_market_order(sig.ticker, side, qty=qty, notional=notional)
            filled = self._wait_fill(order.id)
        except Exception as e:
            log(f"ORDER ERROR {sig.ticker}: {type(e).__name__}: {e} -> reconciling broker state")
        if filled is None:
            log(f"Entry for {sig.ticker} not confirmed fully filled -> cancel/reconcile")
            try:
                self.broker.cancel_orders_for_symbol_and_wait(sig.ticker)
            except Exception as exc:
                log(f"{sig.ticker}: entry-order cancellation warning ({exc})")
            try:
                held = {p.symbol: p for p in self.broker.open_positions()}
            except Exception as exc:
                # We cannot prove there is no fill. Stop all further entries and
                # leave the setup blocked until a human/broker reconciliation.
                self.shutoff = True
                self.blocked_setups.add(key)
                log(f"SAFETY SHUTOFF: cannot reconcile uncertain {sig.ticker} entry ({exc})")
                return False
            position = held.get(sig.ticker)
            if position is None or abs(float(position.qty)) <= 0:
                self.blocked_setups.add(key)
                return False
            fill_price = abs(float(position.avg_entry_price))
            fill_qty = abs(float(position.qty))
            bracket_placed = False
            log(f"RECOVERED partial/late entry: {fill_qty:g} {sig.ticker} @ "
                f"{fill_price:.2f}; software protection active")
        else:
            fill_price = float(filled.filled_avg_price)
            fill_qty = float(filled.filled_qty)
        self.blocked_setups.add(key)
        # One opening scalp per symbol per session — retire the state machine
        # now that a position actually exists, not when the signal was emitted.
        if sig.strategy == "opening_scalp" and sig.ticker in self.scalp_states:
            self.scalp_states[sig.ticker].done = True
        self.positions[sig.ticker] = {
            "signal": sig, "qty": fill_qty, "entry_fill": fill_price,
            "opened": datetime.now(ET), "bracket": bracket_placed,
            "bracket_order_id": str(order.id) if bracket_placed and order else None,
            "initial_risk": abs(fill_price - sig.stop),
            "be_armed": False, "max_favorable_r": 0.0,
            "partial_taken": False, "realized_partial_pnl": 0.0,
            "entry_qty": fill_qty,
        }
        self._save_state()
        actual_risk_dollars = abs(fill_price - sig.stop) * fill_qty
        risk_limit = float(self.cfg["risk"].get("risk_per_trade_usd", actual_risk_dollars))
        if actual_risk_dollars > risk_limit:
            self.shutoff = True
            log(f"SAFETY SHUTOFF: {sig.ticker} fill risk ${actual_risk_dollars:.2f} "
                f"exceeds ${risk_limit:.2f}; position remains protected and tracked")
        prot = ("broker bracket + software backup" if bracket_placed else "software monitor only")
        log(f"ENTERED {sig.direction.upper()} {fill_qty:g} {sig.ticker} @ {fill_price:.2f} "
            f"(SL {sig.stop:.2f} / TP {sig.target:.2f}) [{prot}]  "
            f"[{len(self.positions)}/{self.max_open} positions]")
        self._trade_sound("entry")
        return True

    def _trade_sound(self, event: str) -> None:
        """Ring the trade bell for 'entry', 'exit_win', or 'exit_loss' per the
        config.yaml `sounds:` section (TradingView-style fill alert)."""
        cfg = self.sounds_cfg
        if not cfg.get("enabled", False):
            return
        path = cfg.get(event)
        if path:
            play_sound(path, float(cfg.get("volume", 1.0)))

    def _wait_fill(self, order_id, timeout_s: int = 30):
        deadline = time_mod.time() + timeout_s
        while time_mod.time() < deadline:
            o = self.broker.get_order(order_id)
            if str(o.status) in ("OrderStatus.FILLED", "filled"):
                return o
            if str(o.status) in ("OrderStatus.CANCELED", "OrderStatus.REJECTED",
                                 "canceled", "rejected", "OrderStatus.EXPIRED", "expired"):
                return None
            time_mod.sleep(1)
        return None

    # ----------------------------------------------------------- position mgmt

    def _record_closed_trade(self, sig: Signal, qty: float, entry: float,
                             exit_price: float, exit_reason: str, opened: datetime,
                             decision: str = "approved",
                             realized_partial_pnl: float = 0.0,
                             entry_qty: float | None = None) -> float:
        """Single place that books a closed trade: P&L, CSV row, learnings
        entry, state save, and daily-loss shutoff. Used by both the software
        close path and broker-bracket reconciliation so accounting is identical."""
        judge_reason = sig.reasoning.get("judge_reason", "")
        if decision == "approved" and judge_reason == judge.RULES_ONLY_REASON:
            decision = "rules_only"
        final_leg_pnl = ((exit_price - entry) * qty if sig.direction == "long"
                         else (entry - exit_price) * qty)
        pnl = final_leg_pnl + realized_partial_pnl
        # Partial P&L was booked when that fill occurred; only book the final
        # leg now while recording total trade P&L in the CSV.
        self.daily_pnl += final_leg_pnl
        self.positions.pop(sig.ticker, None)
        self._save_state()
        self.trade_history.append({
            "ticker": sig.ticker, "strategy": sig.strategy, "direction": sig.direction,
            "opened": opened, "closed": datetime.now(ET), "pnl": pnl,
        })
        outcome = "WIN" if pnl >= 0 else "LOSS"
        closed_at = datetime.now(ET)
        hold_minutes = (closed_at - opened).total_seconds() / 60
        initial_risk = max(abs(entry - sig.stop), 1e-9)
        total_qty = entry_qty or qty
        risk_dollars = initial_risk * total_qty
        r_multiple = pnl / risk_dollars if risk_dollars else 0.0
        signal_price = float(sig.reasoning.get("signal_price", sig.entry))
        sign = 1 if sig.direction == "long" else -1
        slippage_pct = sign * (entry - signal_price) / signal_price * 100 if signal_price else 0.0
        entry_bucket = time_bucket(opened.time())
        log(f"CLOSED {sig.ticker} @ {exit_price:.2f}  P&L ${pnl:+.2f}  "
            f"(day total ${self.daily_pnl:+.2f}, {len(self.positions)} open)")
        self._trade_sound("exit_win" if pnl >= 0 else "exit_loss")
        self._append_trade_row(
            timestamp_open=opened.isoformat(timespec="seconds"),
            timestamp_close=closed_at.isoformat(timespec="seconds"),
            ticker=sig.ticker, strategy=sig.strategy, direction=sig.direction,
            qty=f"{total_qty:g}", entry_price=f"{entry:.2f}", exit_price=f"{exit_price:.2f}",
            pnl=f"{pnl:.2f}", r_multiple=f"{r_multiple:.3f}",
            hold_minutes=f"{hold_minutes:.1f}", signal_price=f"{signal_price:.4f}",
            slippage_pct=f"{slippage_pct:.4f}",
            atr_pct=sig.reasoning.get("atr_pct", ""),
            relative_volume=sig.reasoning.get(
                "breakout_relative_volume",
                sig.reasoning.get("impulse_relative_volume",
                                  sig.reasoning.get("break_relative_volume",
                                                    sig.reasoning.get("relative_volume", "")))),
            spread_pct=sig.reasoning.get("spread_pct", ""),
            market_regime=sig.reasoning.get("market_regime", ""),
            relative_strength_pct=sig.reasoning.get("relative_strength_pct", ""),
            gap_pct=sig.reasoning.get("gap_pct", ""),
            sector=sig.reasoning.get("sector", self.sectors.get(sig.ticker, "Unknown")),
            entry_time_bucket=entry_bucket,
            veto_decision=decision, judge_reason=judge_reason, notes=exit_reason)
        facts = (f"Date/time: {closed_at.strftime('%Y-%m-%d %H:%M')} ET\n"
                 f"Ticker: {sig.ticker}\nStrategy: {sig.strategy}\n"
                 f"Direction: {sig.direction}\nOutcome: {outcome} ({pnl:+.2f} USD)\n"
                 f"Entry fill: {entry:.2f}  Exit fill: {exit_price:.2f}  Qty: {qty:g}\n"
                 f"R multiple: {r_multiple:+.3f}R  Hold: {hold_minutes:.1f} minutes\n"
                 f"Slippage: {slippage_pct:+.4f}%  Partial P&L: {realized_partial_pnl:+.2f}\n"
                 f"Planned stop: {sig.stop:.2f}  Planned target: {sig.target:.2f}\n"
                 f"Exit reason: {exit_reason}\n"
                 f"Conditions at entry: {sig.reasoning}")
        entry_text = judge.write_learnings_entry(self.cfg["judge"], facts)
        if entry_text is None:
            entry_text = (f"## [{datetime.now(ET).strftime('%Y-%m-%d %H:%M')}] — {sig.ticker} — "
                          f"{sig.strategy} — {outcome} ({pnl:+.2f})\n"
                          f"- Setup: {sig.reasoning}\n"
                          f"- Direction & reasoning: {sig.direction} per {sig.strategy} rules\n"
                          f"- Outcome: entered {entry:.2f}, exited {exit_price:.2f} ({exit_reason})\n"
                          f"- Why it worked / why it failed: (judge unavailable — fill in manually)\n"
                          f"- Suggested adjustment (NOT auto-applied): (none)")
            log("judge unavailable for learnings entry -> wrote fallback entry")
        self._append_learnings(entry_text)
        if self.daily_pnl <= -float(self.cfg["risk"]["daily_loss_limit_usd"]) and not self.shutoff:
            self._trip_shutoff()
        return pnl

    def reconcile_positions(self, now: datetime) -> None:
        """Continuously confirm the broker still holds every position we think
        we hold. Detects positions that vanished (a bracket TP/SL leg filled, or
        a manual/external close), books them, and keeps share quantities synced."""
        if not self.positions:
            return
        try:
            held = {p.symbol: p for p in self.broker.open_positions()}
        except Exception as e:
            self._skip("reconcile_err", f"reconcile: cannot verify broker positions ({e}) — retrying")
            return
        self.last_skip_msg.pop("reconcile_err", None)

        for ticker in list(self.positions):
            pos = self.positions[ticker]
            sig: Signal = pos["signal"]
            if ticker not in held:
                entry = pos["entry_fill"]
                manual_close = self._consume_manual_close_marker(ticker)
                try:
                    filled_exit = self.broker.last_filled_order(ticker)
                    exit_price = (float(filled_exit.filled_avg_price)
                                  if filled_exit and filled_exit.filled_avg_price
                                  else self.broker.latest_price(ticker))
                except Exception:
                    exit_price = entry
                if manual_close:
                    reason = "manually closed from dashboard"
                    decision = "closed_external"
                elif pos.get("bracket", False):
                    near_tp = abs(exit_price - sig.target) <= abs(exit_price - sig.stop)
                    reason = ("take-profit hit (broker bracket, est. price)" if near_tp
                              else "stop-loss hit (broker bracket, est. price)")
                    decision = "approved"
                else:
                    reason = "position closed outside the bot; P&L estimated from last price"
                    decision = "closed_external"
                log(f"RECONCILE: {ticker} no longer held at broker — {reason}. Exit ~{exit_price:.2f}.")
                self._record_closed_trade(
                    sig, pos["qty"], entry, exit_price, reason, pos["opened"],
                    decision=decision,
                    realized_partial_pnl=pos.get("realized_partial_pnl", 0.0),
                    entry_qty=pos.get("entry_qty"))
            else:
                broker_qty = abs(float(held[ticker].qty))
                if abs(broker_qty - pos["qty"]) > 1e-6:
                    log(f"RECONCILE: {ticker} qty drift {pos['qty']:g} -> broker {broker_qty:g}; syncing.")
                    pos["qty"] = broker_qty
                    self._save_state()

    @staticmethod
    def _consume_manual_close_marker(ticker: str) -> bool:
        """Consume a recent dashboard-close marker used for accurate journaling."""
        path = f"{MANUAL_CLOSE_PREFIX}{ticker}.request"
        try:
            with open(path) as file:
                created = float(file.read().strip())
            os.remove(path)
            return time_mod.time() - created <= 300
        except (FileNotFoundError, ValueError):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return False

    def portfolio_heartbeat(self, bars: dict[str, list]) -> None:
        """Throttled portfolio-wide 'here's where we are' log."""
        if not self.positions:
            return
        now_s = time_mod.time()
        if now_s - self._last_heartbeat < self.heartbeat_interval:
            return
        self._last_heartbeat = now_s
        total_upl = 0.0
        parts = []
        for ticker, pos in self.positions.items():
            sig = pos["signal"]
            bs = bars.get(ticker) or []
            price = bs[-1].close if bs else pos["entry_fill"]
            entry, qty = pos["entry_fill"], pos["qty"]
            upl = (price - entry) * qty if sig.direction == "long" else (entry - price) * qty
            total_upl += upl
            tag = "L" if sig.direction == "long" else "S"
            parts.append(f"{tag}:{ticker}@{entry:.2f}~{price:.2f}({upl:+.0f})")
        log(f"PORTFOLIO {len(self.positions)}/{self.max_open} pos | unrealized ${total_upl:+.2f} "
            f"| day realized ${self.daily_pnl:+.2f}")
        log("  " + "  ".join(parts))

    def _invalidation_reason(self, pos: dict, bars_1m: list,
                             session_open: datetime) -> str | None:
        """Return a strategy-specific thesis failure after a newly completed 5m bar."""
        bars_5m = strategies.aggregate(bars_1m, 5, session_open)
        opened = pos["opened"]
        bucket = int((opened - session_open).total_seconds() // 300) + 1
        eligible = [b for b in bars_5m
                    if b.ts >= session_open + timedelta(minutes=bucket * 5)]
        if not eligible:
            return None
        close = eligible[-1].close
        sig: Signal = pos["signal"]
        r = sig.reasoning
        level = None
        if sig.strategy in ("support_hold", "support_break"):
            level = r.get("support")
        elif sig.strategy == "higher_low":
            level = r.get("higher_low")
        elif sig.strategy == "opening_scalp":
            level = r.get("first_15m_high" if sig.direction == "long" else "first_15m_low")
        elif sig.strategy == "ob_fvg":
            try:
                level = float(str(r.get("order_block", "")).split("-")[0])
            except ValueError:
                level = None
        if level is None:
            return None
        level = float(level)
        failed = close < level if sig.direction == "long" else close > level
        if failed:
            return (f"thesis invalidated: completed 5m close {close:.2f} "
                    f"{'below' if sig.direction == 'long' else 'above'} {level:.2f}")
        return None

    def _update_active_exit_state(self, pos: dict, price: float) -> str | None:
        """Arm breakeven at +1R and take a winner after a 50% MFE giveback."""
        sig: Signal = pos["signal"]
        risk = max(float(pos.get("initial_risk") or sig.risk_per_share), 1e-9)
        favorable = ((price - pos["entry_fill"]) / risk if sig.direction == "long"
                     else (pos["entry_fill"] - price) / risk)
        if favorable > float(pos.get("max_favorable_r", 0.0)):
            pos["max_favorable_r"] = favorable
            self._save_state()
        cfg = self.cfg.get("orders", {})
        partial_at = float(cfg.get("partial_at_r", 1.0))
        if (cfg.get("enable_partial_exit", False)
                and not pos.get("partial_taken")
                and favorable >= partial_at
                and pos["qty"] > 1):
            fraction = float(cfg.get("partial_fraction", 0.5))
            exit_qty = max(1.0, math.floor(pos["qty"] * fraction))
            if exit_qty < pos["qty"]:
                if pos.get("bracket"):
                    try:
                        if not self.broker.cancel_orders_for_symbol_and_wait(sig.ticker):
                            log(f"{sig.ticker}: partial skipped; bracket cancellation timed out")
                            return None
                    except Exception as exc:
                        log(f"{sig.ticker}: partial skipped; bracket cancellation failed ({exc})")
                        return None
                    # Protection is now definitely canceled. Persist that truth
                    # before submitting a partial so failures use normal
                    # software stops, never the bracket emergency-only branch.
                    pos["bracket"] = False
                    pos["bracket_order_id"] = None
                    self._save_state()
                side = "sell" if sig.direction == "long" else "buy"
                old_qty = float(pos["qty"])
                order = None
                filled = None
                try:
                    order = self.broker.submit_market_order(sig.ticker, side, qty=exit_qty)
                    filled = self._wait_fill(order.id)
                except Exception as exc:
                    log(f"{sig.ticker}: partial exit uncertain ({exc}); reconciling")
                if filled is None:
                    try:
                        self.broker.cancel_orders_for_symbol_and_wait(sig.ticker)
                        held = {p.symbol: p for p in self.broker.open_positions()}
                    except Exception as exc:
                        self.shutoff = True
                        log(f"SAFETY SHUTOFF: partial exit cannot be reconciled ({exc})")
                        return None
                    new_qty = abs(float(held[sig.ticker].qty)) if sig.ticker in held else 0.0
                    actual_exit_qty = max(0.0, old_qty - new_qty)
                    if actual_exit_qty <= 0:
                        log(f"{sig.ticker}: partial exit canceled with no fill; "
                            "software protection active")
                        return None
                    try:
                        last_fill = self.broker.last_filled_order(sig.ticker)
                        fill = float(last_fill.filled_avg_price)
                    except Exception:
                        fill = price
                else:
                    actual_exit_qty = float(filled.filled_qty)
                    new_qty = max(0.0, old_qty - actual_exit_qty)
                    fill = float(filled.filled_avg_price)
                if new_qty <= 0:
                    log(f"{sig.ticker}: partial order flattened position; "
                        "broker reconciliation will book closure")
                    return None
                partial_pnl = ((fill - pos["entry_fill"]) * actual_exit_qty
                               if sig.direction == "long"
                               else (pos["entry_fill"] - fill) * actual_exit_qty)
                pos["qty"] = new_qty
                pos["partial_taken"] = True
                pos["realized_partial_pnl"] = (
                    float(pos.get("realized_partial_pnl", 0.0)) + partial_pnl)
                self.daily_pnl += partial_pnl
                self._save_state()
                log(f"{sig.ticker}: PARTIAL {actual_exit_qty:g} @ {fill:.2f} "
                    f"({partial_pnl:+.2f}), {pos['qty']:g} remain; software protection active")
                self._trade_sound("partial")
        if not pos.get("be_armed") and favorable >= float(cfg.get("breakeven_trigger_r", 1.0)):
            moved_at_broker = False
            if pos.get("bracket") and pos.get("bracket_order_id"):
                try:
                    self.broker.replace_bracket_stop(
                        pos["bracket_order_id"], pos["entry_fill"])
                    moved_at_broker = True
                except Exception as e:
                    log(f"{sig.ticker}: broker breakeven replace failed ({e}); "
                        "software breakeven armed")
            pos["be_armed"] = True
            pos["active_stop"] = pos["entry_fill"]
            self._save_state()
            log(f"{sig.ticker}: BREAKEVEN ARMED at {pos['entry_fill']:.2f} "
                f"[{'broker + software' if moved_at_broker else 'software'}]")
        max_favorable = float(pos.get("max_favorable_r", 0.0))
        trigger = float(cfg.get("giveback_trigger_r", 1.0))
        trail_r = cfg.get("trail_distance_r")
        if max_favorable >= trigger:
            if trail_r is not None:
                # Fixed-distance trail from the peak: a winner keeps whatever
                # it reached minus trail_r, instead of surrendering a fixed
                # fraction of the whole move.
                if favorable <= max_favorable - float(trail_r):
                    return (f"peak trail: now {favorable:.2f}R after reaching "
                            f"{max_favorable:.2f}R (trail {float(trail_r):.2f}R)")
            else:
                fraction = float(cfg.get("giveback_fraction", 0.5))
                if favorable <= max_favorable * fraction:
                    return (f"profit giveback: now {favorable:.2f}R after reaching "
                            f"{max_favorable:.2f}R")
        if pos.get("be_armed"):
            crossed = (price <= pos["entry_fill"] if sig.direction == "long"
                       else price >= pos["entry_fill"])
            if crossed:
                return f"software breakeven stop hit at {price:.2f}"
        return None

    def manage_one(self, ticker: str, bars: dict[str, list], now: datetime,
                   session_open: datetime) -> None:
        """Manage a single already-reconciled position: check exits, close if hit."""
        pos = self.positions.get(ticker)
        if not pos:
            return
        sig: Signal = pos["signal"]
        bracket = pos.get("bracket", False)
        try:
            price = self.broker.latest_price(ticker)
        except Exception:
            bs = bars.get(ticker) or []
            if not bs:
                return  # no data this tick: do nothing (fail safe)
            price = bs[-1].close

        margin = float(self.cfg.get("orders", {}).get("software_stop_margin_pct", 0.20)) / 100.0
        exit_reason = None
        if now.time() >= self.flatten_t:
            exit_reason = "end-of-day flatten"
        max_hold = float(self.cfg.get("risk", {}).get("max_holding_minutes", 120))
        if not exit_reason and (now - pos["opened"]).total_seconds() / 60 >= max_hold:
            exit_reason = f"maximum holding time reached ({max_hold:g} minutes)"
        if not exit_reason:
            exit_reason = self._invalidation_reason(
                pos, bars.get(ticker) or [], session_open)
        if not exit_reason:
            exit_reason = self._update_active_exit_state(pos, price)
            bracket = pos.get("bracket", False)
        if not exit_reason:
            if not bracket:
                # software-only: the bot owns both the stop and the target
                if sig.direction == "long" and price <= sig.stop:
                    exit_reason = f"stop hit ({price:.2f} <= {sig.stop:.2f})"
                elif sig.direction == "long" and price >= sig.target:
                    exit_reason = f"target hit ({price:.2f} >= {sig.target:.2f})"
                elif sig.direction == "short" and price >= sig.stop:
                    exit_reason = f"stop hit ({price:.2f} >= {sig.stop:.2f})"
                elif sig.direction == "short" and price <= sig.target:
                    exit_reason = f"target hit ({price:.2f} <= {sig.target:.2f})"
            else:
                # Broker owns normal TP/SL; software is an emergency backup.
                if sig.direction == "long" and price <= sig.stop * (1 - margin):
                    exit_reason = f"EMERGENCY software stop ({price:.2f} well below SL {sig.stop:.2f})"
                elif sig.direction == "short" and price >= sig.stop * (1 + margin):
                    exit_reason = f"EMERGENCY software stop ({price:.2f} well above SL {sig.stop:.2f})"
        if not exit_reason:
            return

        log(f"CLOSING {ticker}: {exit_reason}")
        if bracket:
            try:
                if not self.broker.cancel_orders_for_symbol_and_wait(ticker):
                    log(f"warning: timed out waiting for {ticker} bracket cancellation")
                    return
            except Exception as e:
                log(f"warning: cancel {ticker} bracket legs before close failed: {e}")
                return
            pos["bracket"] = False
            pos["bracket_order_id"] = None
            self._save_state()
        try:
            order = self.broker.close_position(ticker)
            filled = self._wait_fill(order.id, timeout_s=60)
            if filled is None:
                log(f"CLOSE PENDING {ticker}: no confirmed full fill; retaining tracked state")
                return
            exit_price = float(filled.filled_avg_price)
        except Exception as e:
            log(f"CLOSE ERROR {ticker}: {type(e).__name__}: {e} (will retry next tick)")
            return
        self._record_closed_trade(
            sig, pos["qty"], pos["entry_fill"], exit_price, exit_reason, pos["opened"],
            realized_partial_pnl=pos.get("realized_partial_pnl", 0.0),
            entry_qty=pos.get("entry_qty"))

    def _trip_shutoff(self) -> None:
        # Daily-loss shutoff: no NEW entries for the day. Existing positions keep
        # their broker brackets and are still managed to their stop/target — we do
        # NOT cancel orders here (that would strip protective bracket legs).
        self.shutoff = True
        log(f"*** DAILY LOSS SHUTOFF: realized ${self.daily_pnl:+.2f} <= "
            f"-${self.cfg['risk']['daily_loss_limit_usd']} — no more entries today "
            f"(still managing {len(self.positions)} open position(s)) ***")

    # -------------------------------------------------------------------- loop

    def tick(self, session_open: datetime) -> None:
        now = datetime.now(ET)
        self._touch_heartbeat()
        try:
            bars = self.fetch_bars(session_open)
        except Exception as e:
            log(f"data fetch error ({type(e).__name__}: {e}) -> doing nothing this tick")
            return

        # 1) reconcile + manage every open position
        self.reconcile_positions(now)
        for ticker in list(self.positions):
            self.manage_one(ticker, bars, now, session_open)
        self.portfolio_heartbeat(bars)

        # 2) entry gates
        if self._is_paused():
            if not self._paused_logged:
                log("PAUSED — no new entries until resumed (open positions still managed)")
                self._paused_logged = True
            return
        if self._paused_logged:
            log("RESUMED — scanning for new entries again")
            self._paused_logged = False
        if self.shutoff:
            self._skip("shutoff", "daily loss shutoff active — managing open positions only")
            return
        if self.daily_pnl <= -float(self.cfg["risk"]["daily_loss_limit_usd"]):
            self._trip_shutoff()
            return
        if now.time() >= self.flatten_t:
            self._skip("eod", "past flatten time — no new entries")
            return
        if len(self.positions) >= self.max_open:
            self._skip("maxpos", f"at max positions ({len(self.positions)}/{self.max_open}) — managing only")
            return
        self.last_skip_msg.pop("maxpos", None)

        # 3) scan every un-held ticker for setups, up to capacity.
        #    Bounded per tick by entries AND judge calls so one tick stays fast
        #    even when the whole universe is firing; the rest wait for next tick.
        entries = 0
        judged = 0
        for ticker in self.tickers:
            if (entries >= self.max_entries_per_tick
                    or judged >= self.max_judge_calls_per_tick
                    or len(self.positions) >= self.max_open):
                break
            if ticker in self.positions:
                continue
            bs = bars.get(ticker) or []
            if not bs:
                continue
            signals = self.collect_signals(ticker, bs, session_open, now)
            if not signals:
                continue
            chosen, note = self.resolve_conflict(signals)
            if note:
                self._skip(f"conflict:{ticker}", f"{ticker}: {note}")
            if chosen and self.setup_key(chosen) not in self.blocked_setups:
                if not self._context_gate(
                        chosen, bs, bars.get("SPY") or [], session_open, now):
                    continue
                judged += 1  # try_enter runs the judge (unless it fails sanity first)
                if self.try_enter(chosen, bs, session_open):
                    entries += 1

    def _handle_sigterm(self, signum, frame) -> None:
        # Dashboard "Stop" sends SIGTERM. Flag it; the loop flattens & exits.
        self.terminate = True

    def _flatten_all(self, why: str) -> None:
        if not self.positions:
            log(f"{why} — no open positions, exiting clean")
            return
        log(f"{why} — flattening {len(self.positions)} open position(s)")
        for attempt in range(1, 4):
            for ticker in list(self.positions):
                pos = self.positions[ticker]
                sig: Signal = pos["signal"]
                try:
                    if not self.broker.cancel_orders_for_symbol_and_wait(ticker):
                        log(f"flatten attempt {attempt}: timed out canceling {ticker} orders")
                        continue
                except Exception as e:
                    log(f"flatten attempt {attempt}: cancel {ticker} orders failed: {e}")
                    continue
                pos["bracket"] = False
                pos["bracket_order_id"] = None
                self._save_state()
                try:
                    order = self.broker.close_position(ticker)
                    filled = self._wait_fill(order.id, timeout_s=60)
                    if filled is None:
                        log(f"flatten attempt {attempt}: {ticker} close not confirmed")
                        continue
                    exit_price = float(filled.filled_avg_price)
                except Exception as e:
                    log(f"flatten attempt {attempt} error {ticker}: "
                        f"{type(e).__name__}: {e}")
                    continue
                self._record_closed_trade(
                    sig, pos["qty"], pos["entry_fill"], exit_price, why, pos["opened"],
                    realized_partial_pnl=pos.get("realized_partial_pnl", 0.0),
                    entry_qty=pos.get("entry_qty"))
            if not self.positions:
                return
            if attempt < 3:
                time_mod.sleep(2)
                self.reconcile_positions(datetime.now(ET))
        if self.positions:
            self.shutoff = True
            log("CRITICAL: flatten retries exhausted; positions remain tracked in "
                "bot_state.json and require immediate broker reconciliation")

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        if not os.path.exists(CONTROL_FILE):
            with open(CONTROL_FILE, "w") as f:
                f.write("run")
        try:
            self._run_inner()
        finally:
            try:
                os.remove(PID_FILE)
            except FileNotFoundError:
                pass

    def _run_inner(self) -> None:
        log(f"stockbot starting — mode={'PAPER' if self.broker.paper else '*** LIVE ***'} "
            f"universe={len(self.tickers)} tickers poll={self.poll}s pid={os.getpid()}")
        self.broker.account()  # fails fast on bad keys / ALPACA_EXPECTED_ACCOUNT_ID mismatch
        on = [n for n, v in self.strategies_on.items() if v]
        log(f"strategies on: {', '.join(on) or '(none — edit `strategies:` in config.yaml)'}")
        jcfg = self.cfg["judge"]
        if not judge.is_enabled(jcfg):
            log("judge: OFF — rules engine only (every rule-passing setup is taken)")
        else:
            log(f"judge: {jcfg['model']} @ {jcfg['base_url']} (veto power, fail-closed)")
            problem = judge.probe(jcfg)
            if problem:
                log(f"WARNING: judge unreachable ({problem}). Every candidate will be "
                    f"VETOED until it answers. Start LM Studio's server, or run with "
                    f"--no-judge for rules only.")
        log(f"risk: max ${self.cfg['risk']['max_per_trade_usd']}/trade, "
            f"up to {self.max_open} concurrent positions "
            f"({self.max_entries_per_tick}/tick), "
            f"-${self.cfg['risk']['daily_loss_limit_usd']} daily shutoff "
            f"(today so far: ${self.daily_pnl:+.2f})")

        self._adopt_persisted_positions()

        clk = self.broker.clock()
        if not clk.is_open:
            nxt = clk.next_open.astimezone(ET)
            if nxt.date() != datetime.now(ET).date():
                log(f"market is closed today. Next open: {nxt}. Exiting.")
                return
            wait = (clk.next_open - datetime.now(ET).astimezone(clk.next_open.tzinfo)).total_seconds()
            log(f"market opens at {nxt.strftime('%H:%M')} ET — waiting {int(wait)}s")
            # Keep the heartbeat fresh across the pre-open wait, otherwise a
            # bot legitimately parked until 09:30 looks wedged to the watchdog.
            while wait > 0 and not self.terminate:
                self._touch_heartbeat()
                slice_s = min(30.0, wait)
                self._interruptible_sleep(slice_s)
                wait -= slice_s
            self._touch_heartbeat()
            self._interruptible_sleep(2)

        today = datetime.now(ET).date()
        session_open = datetime.combine(today, self.open_t, tzinfo=ET)

        while datetime.now(ET).time() < self.close_t and not self.terminate:
            self.tick(session_open)
            self._interruptible_sleep(self.poll)

        if self.terminate:
            self._flatten_all("Stop requested")
        elif self.positions:
            log("session over with open positions — flattening now")
            self._flatten_all("session-end flatten")
        log(f"session done. realized P&L today: ${self.daily_pnl:+.2f}")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rules engine finds the setup, an LLM gets veto power. Paper by default.")
    p.add_argument("--live", action="store_true",
                   help="trade REAL money (also requires a typed confirmation)")
    p.add_argument("--no-judge", action="store_true",
                   help="skip the LLM veto gate; rules engine only")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    if args.live and not confirm_live_trading():
        sys.exit("Live trading not confirmed. Nothing was traded. "
                 "(Run without --live for paper trading.)")
    try:
        Bot(live=args.live, use_judge=False if args.no_judge else None).run()
    except KeyboardInterrupt:
        print("\nstopped by user (Ctrl-C). Open positions may still be on the "
              "paper account — the dashboard Stop button flattens cleanly instead.")
