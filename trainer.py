"""Offline replay trainer ("night school") — runs only while the market is closed.

Each cycle it picks historical trading days and symbols it has not studied
yet, replays them with the production detectors (via backtest.py), and quizzes
the LLM judge on every qualifying setup: predict WIN or LOSS before the
outcome is revealed. EVERY graded prediction — correct or wrong — produces a
written lesson recording why the call was made, why that reasoning held up or
failed, and what to keep in mind next time. The most recent lessons are fed
back into every subsequent prediction, so the judge studies its own track
record and accuracy can compound. Every replayed setup also feeds
deterministic per-bucket statistics (strategy / direction / regime /
time-of-day), which are distilled into playbook.md — statistical AVOID/EDGE
lines the live judge reads before approving real entries.

Hard rules, matching the rest of the repo:
- config.yaml is never written. Learning artifacts are text + statistics only.
- Lessons go to a SEPARATE file (replay_learnings.md), never learnings.md,
  so replayed losses can never trip the live judge's real-loss counter.
- The trainer stops itself before the next market open (and refuses to start
  while the market is open), so it never competes with the live bot.

Run:  python trainer.py            # train until market open / max_run_hours
      python trainer.py --cycles 1 # one study cycle, then exit
      python trainer.py --no-llm   # plumbing test: replay + stats, no LLM
Stop: kill $(cat trainer.pid)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import time as time_mod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import yaml

import backtest
import bar_cache
import judge
import strategies
from broker import Bar, Broker
from main import load_tickers, time_bucket

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(ROOT, "trainer.pid")

_terminate = False


def log(msg: str) -> None:
    print(f"[{datetime.now(ET).strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def _handle_sigterm(_sig, _frame) -> None:
    global _terminate
    _terminate = True
    log("SIGTERM received — finishing current step, then saving and exiting")


@dataclass
class ReplaySetup:
    day: date
    ticker: str
    strategy: str
    direction: str
    entry: float
    stop: float
    target: float
    r_multiple: float
    regime: str
    bucket: str
    reasoning: dict
    # review payload: the session chart and the moment the decision was made
    signal_time: datetime | None = None
    bars: list[Bar] | None = None
    # quiz results, filled in by _quiz_judge when this setup is selected
    prediction: str | None = None
    prediction_reason: str | None = None
    correct: bool | None = None
    lesson: str | None = None


# ---------------------------------------------------------------------------
# deterministic stats + playbook (pure functions, unit-tested)
# ---------------------------------------------------------------------------

def split_lessons(text: str) -> list[str]:
    """Split a lessons markdown file into its '## '-headed entries."""
    return ["## " + part.strip() for part in text.split("## ") if part.strip()]


def bucket_key(setup: ReplaySetup) -> str:
    return f"{setup.strategy}|{setup.direction}|{setup.regime}|{setup.bucket}"


def update_stats(stats: dict, setup: ReplaySetup) -> None:
    entry = stats.setdefault("buckets", {}).setdefault(
        bucket_key(setup), {"n": 0, "wins": 0, "sum_r": 0.0})
    entry["n"] += 1
    entry["wins"] += 1 if setup.r_multiple > 0 else 0
    entry["sum_r"] += setup.r_multiple


def update_prediction_stats(stats: dict, strategy: str, correct: bool) -> None:
    for key in (strategy, "_total"):
        entry = stats.setdefault("predictions", {}).setdefault(
            key, {"n": 0, "correct": 0})
        entry["n"] += 1
        entry["correct"] += 1 if correct else 0


def build_playbook(stats: dict, training_cfg: dict) -> str:
    """Distill bucket statistics into AVOID/EDGE lines, one section per
    strategy. Only combinations with enough independent observations qualify —
    a losing streak alone never produces a warning."""
    min_n = int(training_cfg.get("min_bucket_n", 20))
    warn_r = float(training_cfg.get("warn_expectancy_r", -0.05))
    edge_r = float(training_cfg.get("edge_expectancy_r", 0.20))
    by_strategy: dict[str, list[str]] = {}
    total_n = 0
    for key, entry in sorted(stats.get("buckets", {}).items()):
        strategy, direction, regime, bucket = key.split("|")
        total_n += entry["n"]
        if entry["n"] < min_n:
            continue
        expectancy = entry["sum_r"] / entry["n"]
        win_rate = entry["wins"] / entry["n"] * 100
        detail = (f"{direction} in {regime} during {bucket} — "
                  f"{entry['n']} replays, expectancy {expectancy:+.2f}R, "
                  f"win rate {win_rate:.0f}%")
        if expectancy <= warn_r:
            by_strategy.setdefault(strategy, []).append(f"- AVOID: {detail}")
        elif expectancy >= edge_r:
            by_strategy.setdefault(strategy, []).append(f"- EDGE: {detail}")
    prediction = stats.get("predictions", {}).get("_total", {})
    accuracy = (f"{prediction['correct'] / prediction['n'] * 100:.0f}% "
                f"({prediction['correct']}/{prediction['n']})"
                if prediction.get("n") else "n/a")
    lines = [
        "# REPLAY PLAYBOOK — auto-generated by trainer.py. Do not hand-edit.",
        f"Updated: {datetime.now(ET).strftime('%Y-%m-%d %H:%M')} ET | "
        f"setups replayed: {total_n} | judge prediction accuracy: {accuracy}",
        "",
    ]
    for strategy in sorted(by_strategy):
        lines.append(f"## {strategy.upper()}")
        lines.extend(by_strategy[strategy])
        lines.append("")
    if not by_strategy:
        lines.append(f"(no strategy/regime/time combination has {min_n}+ "
                     "replays yet — keep training)")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def _previous_session_close(symbol: str, day: date) -> float | None:
    for back in range(1, 6):
        prior = day - timedelta(days=back)
        if not bar_cache.has_day(symbol, prior):
            continue
        sessions = backtest._session_bars(bar_cache.load_day(symbol, prior))
        bars = sessions.get(prior)
        if bars:
            return bars[-1].close
    return None


def replay_symbol_day(cfg: dict, symbol: str, day: date, day_bars: list[Bar],
                      spy_bars: list[Bar]) -> list[ReplaySetup]:
    """Replay one full session with the production detectors. Unlike
    backtest.run (which keeps only the first setup per symbol/day for
    statistical independence), the trainer studies EVERY qualifying setup:
    after a simulated trade exits, scanning resumes once the live bot's
    symbol cooldown has elapsed, exactly as the real bot could re-enter."""
    session_open = datetime.combine(day, time(9, 30), tzinfo=ET)
    state = strategies.OpeningScalpState()
    previous_close = _previous_session_close(symbol, day)
    cooldown_min = int(float(
        cfg.get("entry_guards", {}).get("symbol_cooldown_min", 15)))
    setups: list[ReplaySetup] = []
    i = 15
    while i < len(day_bars) - 1:
        prefix = day_bars[:i + 1]
        spy_prefix = [bar for bar in spy_bars if bar.ts <= prefix[-1].ts]
        now = prefix[-1].ts + timedelta(minutes=1)
        sig = backtest._choose(backtest._signals(
            cfg, symbol, prefix, spy_prefix, session_open, now, state,
            previous_close))
        if sig is None or backtest._prepare_signal(cfg, sig, prefix, session_open):
            i += 1
            continue
        r_value, bars_used = backtest._simulate_exit_detail(
            cfg, sig, day_bars[i + 1:], session_open)
        setups.append(ReplaySetup(
            day=day, ticker=symbol, strategy=sig.strategy,
            direction=sig.direction, entry=sig.entry, stop=sig.stop,
            target=sig.target, r_multiple=r_value,
            regime=str(sig.reasoning.get("market_regime", "unknown")),
            bucket=time_bucket(now.astimezone(ET).time()),
            reasoning=dict(sig.reasoning),
            signal_time=now, bars=day_bars,
        ))
        i += 1 + max(bars_used, 1) + cooldown_min
    return setups


def summarize_replay(setup: ReplaySetup) -> str:
    rr = abs(setup.target - setup.entry) / max(abs(setup.entry - setup.stop), 1e-9)
    lines = [
        f"Ticker: {setup.ticker}",
        f"Strategy: {setup.strategy}",
        f"Direction: {setup.direction}",
        f"Entry: {setup.entry:.2f}  Stop: {setup.stop:.2f}  Target: {setup.target:.2f}",
        f"Reward:risk = {rr:.2f}",
        f"Market regime: {setup.regime}",
        f"Entry time bucket: {setup.bucket}",
        "Conditions met:",
    ]
    for key, value in setup.reasoning.items():
        lines.append(f"  - {key}: {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# persistence helpers (trainer-owned files only)
# ---------------------------------------------------------------------------

def _load_json(path: str, default: dict) -> dict:
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _save_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _append_text(path: str, text: str) -> None:
    with open(path, "a") as f:
        f.write(text.rstrip() + "\n\n")


def review_record(setup: ReplaySetup, risk_usd: float) -> dict:
    """Everything the dashboard needs to replay this decision visually:
    the session chart, the moment the setup fired, the levels, the judge's
    prediction + reasoning, the actual outcome, and the dollar impact at the
    live bot's per-trade risk budget."""
    rr = abs(setup.target - setup.entry) / max(abs(setup.entry - setup.stop), 1e-9)
    return {
        "recorded_at": datetime.now(ET).isoformat(timespec="seconds"),
        "day": setup.day.isoformat(),
        "ticker": setup.ticker,
        "strategy": setup.strategy,
        "direction": setup.direction,
        "entry": round(setup.entry, 4),
        "stop": round(setup.stop, 4),
        "target": round(setup.target, 4),
        "reward_risk": round(rr, 2),
        "r_multiple": round(setup.r_multiple, 4),
        "regime": setup.regime,
        "bucket": setup.bucket,
        "reasoning": {k: str(v) for k, v in setup.reasoning.items()},
        "signal_time": setup.signal_time.astimezone(ET).isoformat(timespec="seconds")
                       if setup.signal_time else None,
        "bars": [[bar.ts.astimezone(ET).isoformat(timespec="seconds"),
                  bar.open, bar.high, bar.low, bar.close, bar.volume]
                 for bar in (setup.bars or [])],
        "prediction": setup.prediction,
        "prediction_reason": setup.prediction_reason,
        "correct": setup.correct,
        "lesson": setup.lesson,
        "risk_usd": risk_usd,
        "simulated_pnl_usd": round(setup.r_multiple * risk_usd, 2),
        "max_pnl_usd": round(rr * risk_usd, 2),
    }


# ---------------------------------------------------------------------------
# training loop
# ---------------------------------------------------------------------------

class Trainer:
    def __init__(self, cfg: dict, use_llm: bool = True):
        self.cfg = cfg
        self.use_llm = use_llm
        self.training_cfg = cfg.get("offline_training", {})
        self.broker = Broker(cfg)
        self.universe = load_tickers(cfg)
        files = cfg.get("files", {})
        self.ledger_path = os.path.join(ROOT, files.get("replay_ledger", "replay_ledger.json"))
        self.stats_path = os.path.join(ROOT, files.get("replay_stats", "replay_stats.json"))
        self.lessons_path = os.path.join(ROOT, files.get("replay_learnings", "replay_learnings.md"))
        self.playbook_path = os.path.join(ROOT, files.get("playbook", "playbook.md"))
        self.review_path = os.path.join(ROOT, files.get("replay_review", "replay_review.jsonl"))
        self.ledger = _load_json(self.ledger_path, {})
        self.stats = _load_json(self.stats_path, {})
        self.rng = random.Random()
        self.stop_deadline: datetime | None = None
        # Night school may quiz with a cheaper/higher-quota model than the
        # live judge so overnight training never burns the live bot's quota.
        self.judge_cfg = dict(cfg["judge"])
        override = self.training_cfg.get("judge_model")
        if override:
            self.judge_cfg["model"] = str(override)

    # ---- stop conditions --------------------------------------------------

    def refresh_stop_deadline(self, started: datetime) -> str:
        """Recompute when training must end. Returns a reason if it is
        already time to stop, else ''."""
        clk = self.broker.clock()
        now = datetime.now(ET)
        if clk.is_open:
            return "market is OPEN — offline training never runs during a session"
        buffer_min = float(self.training_cfg.get("stop_before_open_minutes", 15))
        open_deadline = clk.next_open.astimezone(ET) - timedelta(minutes=buffer_min)
        max_hours = float(self.training_cfg.get("max_run_hours", 4))
        hours_deadline = started + timedelta(hours=max_hours)
        self.stop_deadline = min(open_deadline, hours_deadline)
        if now >= open_deadline:
            return (f"market opens at {clk.next_open.astimezone(ET).strftime('%H:%M')} ET "
                    f"(within {buffer_min:g}m buffer)")
        if now >= hours_deadline:
            return f"max_run_hours ({max_hours:g}h) reached"
        return ""

    def _past_deadline(self) -> bool:
        return (_terminate or
                (self.stop_deadline is not None
                 and datetime.now(ET) >= self.stop_deadline))

    # ---- study material selection -----------------------------------------

    def pick_days(self) -> list[date]:
        lookback = int(self.training_cfg.get("lookback_days", 120))
        per_cycle = int(self.training_cfg.get("days_per_cycle", 3))
        today = datetime.now(ET).date()
        candidates = []
        for back in range(1, lookback + 1):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            record = self.ledger.get(day.isoformat())
            if record == "no_data":
                continue
            trained = set(record or [])
            if len(set(self.universe) - trained) == 0:
                continue
            candidates.append(day)
        self.rng.shuffle(candidates)
        return candidates[:per_cycle]

    def pick_symbols(self, day: date) -> list[str]:
        per_cycle = int(self.training_cfg.get("symbols_per_cycle", 25))
        trained = set(self.ledger.get(day.isoformat()) or [])
        available = [s for s in self.universe if s not in trained]
        self.rng.shuffle(available)
        return available[:per_cycle]

    # ---- one cycle ---------------------------------------------------------

    def run_cycle(self) -> int:
        days = self.pick_days()
        if not days:
            log("nothing left to study inside the lookback window")
            return 0
        plan = {day: self.pick_symbols(day) for day in days}
        cache_days = set()
        cache_symbols = {"SPY"}
        for day, symbols in plan.items():
            cache_symbols.update(symbols)
            for back in range(0, 6):  # day itself + up to 5 prior for prev close
                cache_days.add(day - timedelta(days=back))
        log(f"cycle: {len(days)} day(s) x {sum(len(s) for s in plan.values())} "
            f"symbol-sessions — fetching/caching bars")
        bar_cache.ensure_days(self.broker, sorted(cache_symbols), sorted(cache_days))

        setups: list[ReplaySetup] = []
        for day, symbols in plan.items():
            spy_sessions = backtest._session_bars(bar_cache.load_day("SPY", day))
            spy_bars = spy_sessions.get(day, [])
            if not spy_bars:
                self.ledger[day.isoformat()] = "no_data"
                log(f"{day}: no SPY session data (holiday?) — skipping day")
                continue
            for symbol in symbols:
                if _terminate:
                    break
                sessions = backtest._session_bars(bar_cache.load_day(symbol, day))
                day_bars = sessions.get(day, [])
                if day_bars:
                    for setup in replay_symbol_day(
                            self.cfg, symbol, day, day_bars, spy_bars):
                        setups.append(setup)
                        update_stats(self.stats, setup)
                trained = self.ledger.get(day.isoformat())
                trained = [] if not isinstance(trained, list) else trained
                trained.append(symbol)
                self.ledger[day.isoformat()] = trained
            if _terminate:
                break

        wins = sum(s.r_multiple > 0 for s in setups)
        log(f"replayed {len(setups)} setup(s): {wins} wins, {len(setups) - wins} losses")
        if self.use_llm:
            self._quiz_judge(setups)
        self._write_review(setups)
        self._save_all()
        return len(setups)

    def _write_review(self, setups: list[ReplaySetup]) -> None:
        """Append full review records (chart + decision + outcome) for the
        dashboard's Night School page, keeping the file bounded."""
        if not setups:
            return
        risk_usd = float(self.cfg["risk"].get("risk_per_trade_usd", 500))
        with open(self.review_path, "a") as f:
            for setup in setups:
                f.write(json.dumps(review_record(setup, risk_usd)) + "\n")
        keep = int(self.training_cfg.get("review_keep", 300))
        with open(self.review_path) as f:
            lines = f.readlines()
        if len(lines) > keep:
            _save_text(self.review_path, "".join(lines[-keep:]))

    def _recent_lessons(self, max_entries: int) -> str:
        """Tail of replay_learnings.md: the last max_entries lesson entries,
        fed back into each prediction so the judge studies its own record."""
        if max_entries <= 0 or not os.path.exists(self.lessons_path):
            return ""
        with open(self.lessons_path) as f:
            entries = split_lessons(f.read())
        return "\n\n".join(entries[-max_entries:])

    def _trim_lessons(self) -> None:
        """Keep the lessons file bounded now that every graded prediction
        writes an entry (oldest entries drop first)."""
        keep = int(self.training_cfg.get("lessons_keep", 500))
        if keep <= 0 or not os.path.exists(self.lessons_path):
            return
        with open(self.lessons_path) as f:
            entries = split_lessons(f.read())
        if len(entries) > keep:
            _save_text(self.lessons_path,
                       "\n\n".join(entries[-keep:]) + "\n")

    def _quiz_judge(self, setups: list[ReplaySetup]) -> None:
        max_predictions = int(self.training_cfg.get("max_predictions_per_cycle", 40))
        max_lessons = int(self.training_cfg.get("max_lessons_per_cycle", 40))
        lessons_in_prompt = int(self.training_cfg.get("lessons_in_prompt", 10))
        pause = float(self.training_cfg.get("llm_seconds_between_calls", 1.5))
        max_strikes = int(self.training_cfg.get("max_consecutive_rate_limits", 3))
        playbook = ""
        if os.path.exists(self.playbook_path):
            with open(self.playbook_path) as f:
                playbook = f.read()
        lessons_excerpt = self._recent_lessons(lessons_in_prompt)
        lessons_written = 0
        backoff = 30.0
        strikes = 0  # consecutive setups lost to 429 even after backing off
        for setup in setups[:max_predictions]:
            if self._past_deadline():
                log("stop deadline reached mid-quiz — saving progress")
                break
            prediction, reason = judge.predict_outcome(
                self.judge_cfg, summarize_replay(setup), playbook,
                lessons_excerpt)
            time_mod.sleep(pause)
            if prediction is None and "429" in reason:
                # Per-minute limit: an escalating backoff usually clears it.
                log(f"judge rate-limited — backing off {backoff:.0f}s")
                time_mod.sleep(backoff)
                backoff = min(backoff * 2, 240.0)
                prediction, reason = judge.predict_outcome(
                    self.judge_cfg, summarize_replay(setup), playbook,
                    lessons_excerpt)
                time_mod.sleep(pause)
                if prediction is None and "429" in reason:
                    strikes += 1
                    if strikes >= max_strikes:
                        # Daily quota is likely exhausted; retrying only burns
                        # the night. Setups still count toward playbook stats.
                        log(f"judge rate-limited {strikes}x in a row — "
                            "skipping the quiz for the rest of this cycle "
                            "(replay statistics are still recorded)")
                        break
            if prediction is None:
                log(f"prediction unavailable for {setup.ticker} {setup.day}: {reason}")
                continue
            backoff = 30.0
            strikes = 0
            actual = "WIN" if setup.r_multiple > 0 else "LOSS"
            correct = prediction == actual
            setup.prediction = prediction
            setup.prediction_reason = reason
            setup.correct = correct
            update_prediction_stats(self.stats, setup.strategy, correct)
            grade = "CORRECT" if correct else "WRONG"
            log(f"quiz {setup.ticker} {setup.day} {setup.strategy} "
                f"{setup.direction}: predicted {prediction}, actual {actual} "
                f"({setup.r_multiple:+.2f}R) -> {grade}")
            # Every graded prediction — right or wrong — becomes a written
            # lesson: why the call was made, why the reasoning held or failed,
            # and what to keep in mind next time.
            if lessons_written >= max_lessons:
                continue
            facts = (
                f"Replay day: {setup.day}\nTicker: {setup.ticker}\n"
                f"Strategy: {setup.strategy}\nDirection: {setup.direction}\n"
                f"Entry: {setup.entry:.2f}  Stop: {setup.stop:.2f}  "
                f"Target: {setup.target:.2f}\n"
                f"Market regime: {setup.regime}\nEntry time bucket: {setup.bucket}\n"
                f"Prediction made: {prediction} — {reason}\n"
                f"Actual outcome: {actual} ({setup.r_multiple:+.2f}R)\n"
                f"Grade: the prediction was {grade}\n"
                f"Conditions at entry: {setup.reasoning}"
            )
            lesson = judge.write_replay_lesson(self.judge_cfg, facts)
            time_mod.sleep(pause)
            if lesson:
                setup.lesson = lesson
                _append_text(self.lessons_path, lesson)
                lessons_written += 1
                # Later predictions in this same cycle see this lesson too.
                lessons_excerpt = self._recent_lessons(lessons_in_prompt)
        if lessons_written:
            self._trim_lessons()
            log(f"wrote {lessons_written} lesson(s) to "
                f"{os.path.basename(self.lessons_path)}")

    def _save_all(self) -> None:
        _save_json(self.ledger_path, self.ledger)
        _save_json(self.stats_path, self.stats)
        _save_text(self.playbook_path, build_playbook(self.stats, self.training_cfg))
        prediction = self.stats.get("predictions", {}).get("_total", {})
        if prediction.get("n"):
            log(f"lifetime judge prediction accuracy: "
                f"{prediction['correct'] / prediction['n'] * 100:.1f}% "
                f"({prediction['correct']}/{prediction['n']})")

    # ---- outer loop --------------------------------------------------------

    def run(self, cycles: int) -> None:
        started = datetime.now(ET)
        completed = 0
        log(f"night school starting — universe={len(self.universe)} tickers, "
            f"llm={'on' if self.use_llm else 'off'}, pid={os.getpid()}")
        # Launched right at the 4:00 PM bell, Alpaca's clock can still report
        # the market open for a moment. If the close is imminent, wait it out
        # instead of skipping the whole night.
        clk = self.broker.clock()
        if clk.is_open:
            until_close = (clk.next_close - datetime.now(clk.next_close.tzinfo)
                           ).total_seconds()
            if until_close <= 45 * 60:
                log(f"market closes in {until_close / 60:.1f}m — waiting for the bell")
                deadline = time_mod.monotonic() + until_close + 60
                while time_mod.monotonic() < deadline and not _terminate:
                    time_mod.sleep(5)
                started = datetime.now(ET)
        while not _terminate:
            stop_reason = self.refresh_stop_deadline(started)
            if stop_reason:
                log(f"stopping: {stop_reason}")
                break
            if self.run_cycle() == 0 and not self.pick_days():
                log("stopping: study material exhausted")
                break
            completed += 1
            if cycles and completed >= cycles:
                log(f"stopping: requested {cycles} cycle(s) done")
                break
        log(f"night school done — {completed} cycle(s) this run. "
            "The live bot will read playbook.md at the next session.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cycles", type=int, default=0,
                        help="study cycles to run (0 = until market open / max hours)")
    parser.add_argument("--no-llm", action="store_true",
                        help="replay + statistics only; skip judge quiz and lessons")
    args = parser.parse_args()
    with open(os.path.join(ROOT, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    if not cfg.get("offline_training", {}).get("enabled", True):
        log("offline_training.enabled is false — exiting")
        return
    signal.signal(signal.SIGTERM, _handle_sigterm)
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            old_pid = f.read().strip()
        if old_pid.isdigit():
            try:
                os.kill(int(old_pid), 0)
                log(f"another trainer is already running (pid {old_pid}) — exiting")
                return
            except OSError:
                pass
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    try:
        Trainer(cfg, use_llm=not args.no_llm).run(args.cycles)
    finally:
        try:
            os.remove(PID_FILE)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped by user (Ctrl-C). Progress up to the last completed "
              "cycle is saved.")
