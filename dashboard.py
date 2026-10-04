"""Local dashboard and control panel for stockbot.

Serves one page at http://localhost:8000 that shows:
  - live Alpaca account (equity, cash, buying power, day P&L)
  - current open position (if any)
  - every trade / candidate / veto logged to trades.csv
  - the learnings.md journal entries the judge wrote
  - the tail of the bot's console log (bot.log)

The only trading action available here is an explicitly confirmed manual
close of one existing position. It cannot create or enlarge positions.
Run:  .venv/bin/python dashboard.py     (then open http://localhost:8000)
"""

from __future__ import annotations

import csv
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import yaml
from flask import Flask, jsonify, render_template_string

from broker import Broker

ET = ZoneInfo("America/New_York")

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))
TRADES = os.path.join(ROOT, CFG["files"]["trades_csv"])
LEARNINGS = os.path.join(ROOT, CFG["files"]["learnings"])
BOT_LOG = os.path.join(ROOT, "bot.log")
PID_FILE = os.path.join(ROOT, "bot.pid")
CONTROL_FILE = os.path.join(ROOT, "bot.control")
STATE_FILE = os.path.join(ROOT, "bot_state.json")
MANUAL_CLOSE_PREFIX = os.path.join(ROOT, "manual_close_")
REVIEW = os.path.join(ROOT, CFG["files"].get("replay_review", "replay_review.jsonl"))
REPLAY_STATS = os.path.join(ROOT, CFG["files"].get("replay_stats", "replay_stats.json"))
PLAYBOOK = os.path.join(ROOT, CFG["files"].get("playbook", "playbook.md"))
TRAINER_LOG = os.path.join(ROOT, "trainer.log")
TRAINER_PID = os.path.join(ROOT, "trainer.pid")
PYTHON = sys.executable  # the venv python running this dashboard


def read_state() -> dict:
    """The bot's own view of its open positions (has each position's TP/SL)."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        return {d["ticker"]: d for d in data}
    except Exception:
        return {}

app = Flask(__name__)
_broker: Broker | None = None


def broker() -> Broker:
    global _broker
    if _broker is None:
        _broker = Broker(CFG)
    return _broker


def read_trades() -> list[dict]:
    if not os.path.exists(TRADES):
        return []
    with open(TRADES) as f:
        rows = list(csv.DictReader(f))
    return rows[::-1]  # newest first


def read_learnings() -> list[dict]:
    if not os.path.exists(LEARNINGS):
        return []
    text = open(LEARNINGS).read()
    # split on the "## [" headers; keep the header with its body
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    entries = []
    for p in parts:
        p = p.strip()
        if p.startswith("## "):
            lines = p.splitlines()
            entries.append({"header": lines[0].lstrip("# ").strip(),
                            "body": "\n".join(lines[1:]).strip()})
    return entries[::-1]  # newest first


def account_snapshot() -> dict:
    try:
        b = broker()
        acct = b.account()
        clock = b.clock()
        equity = float(acct.equity)
        last_equity = float(acct.last_equity)
        state = read_state()  # bot's TP/SL per ticker
        positions = []
        for p in b.open_positions():
            st = state.get(p.symbol, {})
            positions.append({
                "symbol": p.symbol,
                "side": p.side.value if hasattr(p.side, "value") else str(p.side),
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "current": float(p.current_price) if p.current_price else None,
                "market_value": float(p.market_value) if p.market_value else None,
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                "unrealized_plpc": float(p.unrealized_plpc) * 100 if p.unrealized_plpc else 0.0,
                "stop": st.get("stop"),
                "target": st.get("target"),
                "bracket": st.get("bracket", False),
                "strategy": st.get("strategy"),
            })
        orders = []
        try:
            for o in b.open_orders():
                otype = o.type.value if hasattr(o.type, "value") else str(o.type)
                oside = o.side.value if hasattr(o.side, "value") else str(o.side)
                price = o.limit_price or o.stop_price
                # label take-profit (limit) vs stop-loss (stop) for readability
                kind = "take-profit" if "limit" in otype else ("stop-loss" if "stop" in otype else otype)
                orders.append({
                    "symbol": o.symbol, "kind": kind, "type": otype, "side": oside,
                    "price": float(price) if price else None,
                    "qty": float(o.qty) if o.qty else None,
                    "status": o.status.value if hasattr(o.status, "value") else str(o.status),
                })
        except Exception:
            pass
        return {
            "ok": True,
            "market_open": bool(clock.is_open),
            "equity": equity,
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "day_pl": equity - last_equity,
            "day_pl_pct": (equity - last_equity) / last_equity * 100 if last_equity else 0.0,
            "positions": positions,
            "orders": orders,
            "mode": "PAPER" if b.paper else "LIVE",
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def bot_log_tail(n: int = 60) -> list[str]:
    if not os.path.exists(BOT_LOG):
        return ["(bot.log not found — is the bot running with output redirected to bot.log?)"]
    with open(BOT_LOG, errors="replace") as f:
        return f.read().splitlines()[-n:]


def realized_pnl_today() -> float:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    total = 0.0
    for row in read_trades():
        if row.get("timestamp_close", "").startswith(today) and row.get("pnl"):
            try:
                total += float(row["pnl"])
            except ValueError:
                pass
    return total


# ---- bot process control -------------------------------------------------

def _read_pid() -> int | None:
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0 = existence check, doesn't actually signal
    except (OSError, ProcessLookupError):
        return False
    # A zombie still answers signal 0, so the check above is not enough: the
    # bot is spawned as a child of this dashboard and nothing calls wait() on
    # it, so a dead bot lingers as <defunct> and would read as "running"
    # forever — exactly the state that blocked the 2026-08-04 session.
    # Reap it if it is ours, then confirm it is really gone.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass  # not our child (e.g. dashboard restarted) — fall through to ps
    except OSError:
        return False
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True  # can't tell; assume alive rather than double-spawn a bot
    return bool(state) and not state.startswith("Z")


def bot_status() -> dict:
    pid = _read_pid()
    running = bool(pid and _pid_alive(pid))
    paused = False
    if running:
        try:
            with open(CONTROL_FILE) as f:
                paused = f.read().strip().lower() == "paused"
        except FileNotFoundError:
            paused = False
    return {"running": running, "paused": paused, "pid": pid if running else None,
            "state": "paused" if (running and paused) else "running" if running else "stopped"}


def _manual_close_marker(symbol: str) -> str:
    return f"{MANUAL_CLOSE_PREFIX}{symbol}.request"


HEARTBEAT_FILE = os.path.join(ROOT, "bot.heartbeat")
# A tick is ~5s. Ten minutes of silence from a live pid means wedged, not busy.
STALE_HEARTBEAT_SECONDS = 600


def _heartbeat_age() -> float | None:
    """Seconds since the bot last completed a poll tick, or None if unknown."""
    try:
        return time.time() - os.path.getmtime(HEARTBEAT_FILE)
    except OSError:
        return None


def _reap_wedged_bot(pid: int) -> bool:
    """Kill a bot that holds a live pid but stopped ticking.

    On 2026-08-03 a timeout-less Alpaca read blocked the poll loop for 22
    hours. The process stayed alive, so this scheduler said "already
    running" the next morning and the session never started. SIGTERM cannot
    be relied on here: the handler only sets a flag, and Python resumes the
    interrupted syscall (PEP 475), so a hard kill is the fallback.
    """
    for sig, wait_s in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.kill(pid, sig)
        except OSError:
            return True
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if not _pid_alive(pid):
                return True
            time.sleep(0.25)
    return not _pid_alive(pid)


def spawn_bot(source: str = "dashboard") -> tuple[bool, str]:
    st = bot_status()
    if st["running"]:
        age = _heartbeat_age()
        if age is None or age <= STALE_HEARTBEAT_SECONDS:
            return False, f"bot already running (pid {st['pid']})"
        # Live pid, dead loop. Reap it so this session is not lost too.
        pid = st["pid"]
        if not _reap_wedged_bot(pid):
            return False, (f"bot pid {pid} is wedged (no tick for {age/60:.0f} min) "
                           "and could not be killed — needs manual intervention")
        with open(BOT_LOG, "a") as f:
            f.write(f"\n===== WATCHDOG: killed wedged bot pid {pid} "
                    f"(no tick for {age/60:.0f} min) =====\n")
    # reset control to "run", then spawn main.py detached, logging to bot.log
    with open(CONTROL_FILE, "w") as f:
        f.write("run")
    logf = open(BOT_LOG, "a")
    logf.write(f"\n===== started via {source} {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    logf.flush()
    subprocess.Popen([PYTHON, "-u", "main.py"], cwd=ROOT, stdout=logf, stderr=logf,
                     start_new_session=True)
    return True, "bot starting"


def spawn_trainer(source: str = "dashboard") -> tuple[bool, str]:
    st = trainer_status()
    if st["running"]:
        return False, f"trainer already running (pid {st['pid']})"
    logf = open(TRAINER_LOG, "a")
    logf.write(f"===== started via {source} {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    logf.flush()
    subprocess.Popen([PYTHON, "-u", "trainer.py"], cwd=ROOT, stdout=logf, stderr=logf,
                     start_new_session=True)
    return True, ("night school starting — it checks the market clock itself "
                  "and stops before the next open")


@app.route("/api/bot/start", methods=["POST"])
def api_start():
    ok, msg = spawn_bot()
    time.sleep(1.5)
    return jsonify({"ok": ok, "msg": msg, "status": bot_status()})


@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    st = bot_status()
    if not st["running"]:
        return jsonify({"ok": False, "msg": "bot is not running", "status": st})
    try:
        os.kill(st["pid"], signal.SIGTERM)  # bot flattens any open position, then exits
    except ProcessLookupError:
        return jsonify({"ok": True, "msg": "bot already gone", "status": bot_status()})
    return jsonify({"ok": True, "msg": "stop signal sent — bot will flatten any open "
                    "position and exit", "status": bot_status()})


@app.route("/api/bot/pause", methods=["POST"])
def api_pause():
    st = bot_status()
    if not st["running"]:
        return jsonify({"ok": False, "msg": "bot is not running", "status": st})
    with open(CONTROL_FILE, "w") as f:
        f.write("paused")
    return jsonify({"ok": True, "msg": "paused — no new entries; open position still "
                    "managed to stop/target", "status": bot_status()})


@app.route("/api/bot/resume", methods=["POST"])
def api_resume():
    st = bot_status()
    if not st["running"]:
        return jsonify({"ok": False, "msg": "bot is not running", "status": st})
    with open(CONTROL_FILE, "w") as f:
        f.write("run")
    return jsonify({"ok": True, "msg": "resumed — scanning for new entries",
                    "status": bot_status()})


@app.route("/api/positions/<symbol>/close", methods=["POST"])
def api_close_position(symbol: str):
    """Cancel one symbol's protection, then flatten only that position."""
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
        return jsonify({"ok": False, "msg": "invalid ticker symbol"}), 400
    try:
        b = broker()
        held = {position.symbol.upper() for position in b.open_positions()}
        if symbol not in held:
            return jsonify({
                "ok": False, "msg": f"{symbol} is not an open position"}), 404
        if not b.cancel_orders_for_symbol_and_wait(symbol):
            return jsonify({
                "ok": False,
                "msg": f"close aborted: {symbol} protective orders did not cancel",
            }), 409

        marker = _manual_close_marker(symbol)
        with open(marker, "w") as file:
            file.write(str(time.time()))
        try:
            order = b.close_position(symbol)
        except Exception:
            try:
                os.remove(marker)
            except FileNotFoundError:
                pass
            raise

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if not any(
                    position.symbol.upper() == symbol
                    for position in b.open_positions()):
                return jsonify({
                    "ok": True,
                    "msg": f"{symbol} closed at market",
                    "order_id": str(order.id),
                })
            time.sleep(0.25)
        return jsonify({
            "ok": True,
            "msg": f"{symbol} close submitted; waiting for broker confirmation",
            "order_id": str(order.id),
        }), 202
    except Exception as exc:
        return jsonify({
            "ok": False,
            "msg": f"could not close {symbol}: {type(exc).__name__}: {exc}",
        }), 500


def read_review(limit: int = 120) -> list[dict]:
    """Newest-first replayed-setup records written by trainer.py."""
    if not os.path.exists(REVIEW):
        return []
    records = []
    with open(REVIEW) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[::-1][:limit]


def trainer_status() -> dict:
    try:
        with open(TRAINER_PID) as f:
            pid = int(f.read().strip())
        running = _pid_alive(pid)
    except (FileNotFoundError, ValueError):
        pid, running = None, False
    return {"running": running, "pid": pid if running else None}


@app.route("/api/trainer/start", methods=["POST"])
def api_trainer_start():
    ok, msg = spawn_trainer()
    time.sleep(1.5)
    return jsonify({"ok": ok, "msg": msg, "trainer": trainer_status()})


@app.route("/api/trainer/stop", methods=["POST"])
def api_trainer_stop():
    st = trainer_status()
    if not st["running"]:
        return jsonify({"ok": False, "msg": "trainer is not running", "trainer": st})
    try:
        os.kill(st["pid"], signal.SIGTERM)  # trainer saves progress, then exits
    except ProcessLookupError:
        pass
    return jsonify({"ok": True, "msg": "stop signal sent — trainer saves its "
                    "progress and exits", "trainer": trainer_status()})


# ---- built-in scheduler ----------------------------------------------------
# launchd LaunchAgents cannot read this Documents folder on this Mac (macOS
# TCC blocks /bin/zsh under launchd: "can't open input file"), so the
# dashboard schedules both processes itself. As long as the dashboard is
# running: the bot is launched weekday mornings before the open, and night
# school is launched every day once the market is closed.
#
# Re-verified 2026-07-21 with probe LaunchAgents. Careful: `[[ -r ]]`/access()
# reports the file readable while the real open() is denied, so a readability
# test will wrongly suggest TCC is not the problem. This stays the primary
# launch path until the project moves out of ~/Documents (see start_bot.sh).

SCHED_BOT_AT = dtime(9, 25)          # weekdays, before the 9:30 bell
# Upper bound is generous on purpose: if the Mac wakes late, a bot that starts
# at 10:00 still catches most of the 09:30-11:00 window. The `fired` set plus
# start_bot.sh's pid check keep this from resurrecting a deliberately stopped
# bot within a single dashboard lifetime.
SCHED_BOT_UNTIL = dtime(10, 30)
SCHED_TRAINER_WEEKDAY = dtime(16, 5)  # right after the 16:00 close
SCHED_TRAINER_WEEKEND = dtime(10, 0)


def _scheduler_loop() -> None:
    fired: set[tuple[str, str]] = set()  # (YYYY-MM-DD, job) already launched
    while True:
        try:
            now = datetime.now(ET)
            day_key = now.strftime("%Y-%m-%d")
            weekday = now.weekday() < 5
            # narrow window: a dashboard restarted mid-day must not resurrect
            # a bot the user deliberately stopped
            if (weekday and SCHED_BOT_AT <= now.time() < SCHED_BOT_UNTIL
                    and (day_key, "bot") not in fired):
                fired.add((day_key, "bot"))
                ok, msg = spawn_bot("dashboard scheduler")
                print(f"[scheduler] bot: {msg}", flush=True)
            trainer_at = SCHED_TRAINER_WEEKDAY if weekday else SCHED_TRAINER_WEEKEND
            if (now.time() >= trainer_at
                    and (day_key, "trainer") not in fired):
                fired.add((day_key, "trainer"))
                ok, msg = spawn_trainer("dashboard scheduler")
                print(f"[scheduler] trainer: {msg}", flush=True)
        except Exception as exc:  # never let the scheduler thread die
            print(f"[scheduler] error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(30)


@app.route("/api/nightschool")
def api_nightschool():
    stats = {}
    if os.path.exists(REPLAY_STATS):
        try:
            with open(REPLAY_STATS) as f:
                stats = json.load(f)
        except (json.JSONDecodeError, OSError):
            stats = {}
    playbook = ""
    if os.path.exists(PLAYBOOK):
        with open(PLAYBOOK) as f:
            playbook = f.read()
    log_tail = []
    if os.path.exists(TRAINER_LOG):
        with open(TRAINER_LOG, errors="replace") as f:
            log_tail = f.read().splitlines()[-40:]
    return jsonify({
        "records": read_review(),
        "stats": stats,
        "playbook": playbook,
        "trainer": trainer_status(),
        "log": log_tail,
        "risk_per_trade_usd": CFG["risk"].get("risk_per_trade_usd", 500),
    })


@app.route("/nightschool")
def nightschool():
    return render_template_string(NIGHT_PAGE)


@app.route("/api/state")
def api_state():
    return jsonify({
        "account": account_snapshot(),
        "trades": read_trades(),
        "learnings": read_learnings(),
        "log": bot_log_tail(),
        "realized_today": realized_pnl_today(),
        "bot": bot_status(),
        "judge_model": CFG["judge"]["model"],
        "limits": {
            "max_per_trade": CFG["risk"]["max_per_trade_usd"],
            "daily_loss_limit": CFG["risk"]["daily_loss_limit_usd"],
        },
    })


@app.route("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>stockbot dashboard</title>
<style>
  :root {
    --bg:#0b0e14; --panel:#141a24; --panel2:#1b2330; --line:#26303f;
    --text:#e6edf3; --muted:#8b98a9; --green:#3fb950; --red:#f85149;
    --accent:#58a6ff; --amber:#d29922;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 24px; border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; font-weight:650; letter-spacing:.3px; }
  .badge { font-size:11px; font-weight:700; padding:3px 8px; border-radius:6px;
    background:var(--panel2); color:var(--muted); text-transform:uppercase; }
  .badge.paper { color:var(--accent); }
  .badge.open { color:var(--green); }
  .badge.closed { color:var(--red); }
  .judge { color:var(--muted); font-size:12px; font-family:ui-monospace,Menlo,monospace; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--muted); display:inline-block; }
  .dot.running { background:var(--green); box-shadow:0 0 8px var(--green); }
  .dot.paused { background:var(--amber); box-shadow:0 0 8px var(--amber); }
  .dot.stopped { background:var(--red); }
  .controls { display:flex; gap:8px; margin-left:8px; }
  .btn { font-size:12px; font-weight:650; padding:6px 12px; border-radius:7px;
    border:1px solid var(--line); background:var(--panel2); color:var(--text);
    cursor:pointer; transition:.12s; }
  .btn:hover:not(:disabled) { border-color:var(--accent); }
  .btn:disabled { opacity:.35; cursor:not-allowed; }
  .btn.start { color:var(--green); } .btn.stop { color:var(--red); }
  .btn.pause,.btn.resume { color:var(--amber); }
  .btn.close-pos { color:var(--red); padding:4px 9px; }
  .updated { margin-left:auto; color:var(--muted); font-size:12px; }
  .toast { position:fixed; top:14px; right:14px; z-index:50; background:var(--panel2);
    border:1px solid var(--line); border-radius:8px; padding:11px 15px; font-size:13px;
    max-width:420px; opacity:0; transform:translateY(-8px); transition:.2s;
    pointer-events:none; }
  .toast.show { opacity:1; transform:translateY(0); }
  .toast.ok { border-color:var(--green); } .toast.err { border-color:var(--red); }
  .wrap { padding:20px 24px; max-width:1400px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
    gap:14px; margin-bottom:22px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:14px 16px; }
  .card .label { color:var(--muted); font-size:11px; text-transform:uppercase;
    letter-spacing:.5px; margin-bottom:6px; }
  .card .value { font-size:24px; font-weight:650; font-variant-numeric:tabular-nums; }
  .value.green { color:var(--green); } .value.red { color:var(--red); }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:22px; }
  @media (max-width:1000px){ .grid2 { grid-template-columns:1fr; } }
  section { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    margin-bottom:22px; overflow:hidden; }
  section h2 { font-size:13px; text-transform:uppercase; letter-spacing:.5px;
    color:var(--muted); margin:0; padding:12px 16px; border-bottom:1px solid var(--line);
    background:var(--panel2); }
  .body { padding:4px 0; max-height:420px; overflow:auto; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th,td { text-align:left; padding:7px 16px; border-bottom:1px solid var(--line);
    font-size:13px; white-space:nowrap; }
  th { color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; }
  tr:last-child td { border-bottom:none; }
  .pill { font-size:11px; font-weight:700; padding:2px 7px; border-radius:5px; }
  .pill.long,.pill.approved,.pill.WIN { background:rgba(63,185,80,.15); color:var(--green); }
  .pill.short { background:rgba(210,153,34,.15); color:var(--amber); }
  .pill.vetoed,.pill.LOSS,.pill.failed_sanity,.pill.not_sent { background:rgba(248,81,73,.15); color:var(--red); }
  .pill.closed_external { background:rgba(88,166,255,.15); color:var(--accent); }
  .learn { padding:14px 16px; border-bottom:1px solid var(--line); }
  .learn:last-child { border-bottom:none; }
  .learn .h { font-weight:650; margin-bottom:6px; color:var(--text); }
  .learn pre { margin:0; white-space:pre-wrap; color:var(--muted); font-size:12.5px;
    font-family:inherit; }
  .log { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
    padding:10px 16px; color:#c9d4e0; }
  .log div { padding:1px 0; white-space:pre-wrap; }
  .empty { padding:22px 16px; color:var(--muted); font-style:italic; }
  .pos-row td { font-size:14px; }
  .err { color:var(--red); padding:14px 16px; }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
</style>
</head>
<body>
<header>
  <h1>📈 stockbot</h1>
  <span class="badge" id="mode">—</span>
  <span class="badge" id="mkt">—</span>
  <span class="dot" id="botdot"></span><span class="badge" id="botstate">—</span>
  <span class="judge" id="judge"></span>
  <div class="controls">
    <button class="btn start" id="btnStart" onclick="ctl('start')">▶ Start</button>
    <button class="btn pause" id="btnPause" onclick="ctl('pause')">⏸ Pause</button>
    <button class="btn resume" id="btnResume" onclick="ctl('resume')">⏵ Resume</button>
    <button class="btn stop" id="btnStop" onclick="ctlStop()">⏹ Stop &amp; flatten</button>
  </div>
  <a class="btn" href="/nightschool" style="text-decoration:none">🌙 Night school</a>
  <span class="updated" id="updated">connecting…</span>
</header>
<div id="toast" class="toast"></div>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <section>
    <h2>Open positions <span id="poscount" style="color:var(--muted)"></span></h2>
    <div class="body" id="position"></div>
  </section>
  <section>
    <h2>Resting broker orders (take-profit / stop-loss)</h2>
    <div class="body" id="orders"></div>
  </section>
  <div class="grid2">
    <section>
      <h2>Trades &amp; candidates <span id="tcount" style="color:var(--muted)"></span></h2>
      <div class="body" id="trades"></div>
    </section>
    <section>
      <h2>Learnings (judge journal) <span id="lcount" style="color:var(--muted)"></span></h2>
      <div class="body" id="learnings"></div>
    </section>
  </div>
  <section>
    <h2>Bot log (live tail)</h2>
    <div class="log" id="log"></div>
  </section>
</div>
<script>
const $ = id => document.getElementById(id);
const money = n => (n<0?"-":"")+"$"+Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const cls = n => n>0?"green":n<0?"red":"";

let toastTimer = null;
function toast(msg, ok=true){
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show " + (ok?"ok":"err");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>{ t.className = "toast"; }, 5000);
}

async function ctl(action){
  try {
    const r = await (await fetch("/api/bot/"+action, {method:"POST"})).json();
    toast(r.msg, r.ok);
    refresh();
  } catch(e){ toast("control error: "+e, false); }
}
function ctlStop(){
  if(confirm("Stop the bot? Any OPEN POSITION will be market-closed (flattened) first. Continue?"))
    ctl("stop");
}

async function closePosition(symbol, button){
  if(!confirm(`Close ${symbol} at market now? Its stop-loss and take-profit orders will be canceled first.`))
    return;
  button.disabled = true;
  try {
    const response = await fetch(`/api/positions/${encodeURIComponent(symbol)}/close`, {method:"POST"});
    const result = await response.json();
    toast(result.msg, result.ok);
  } catch(e) {
    toast("close error: "+e, false);
  } finally {
    refresh();
  }
}

function applyBotState(b){
  const dot = $("botdot"), lbl = $("botstate");
  dot.className = "dot " + b.state;
  lbl.textContent = "bot " + b.state + (b.pid?(" · pid "+b.pid):"");
  lbl.className = "badge";
  $("btnStart").disabled  = b.running;
  $("btnPause").disabled  = !b.running || b.paused;
  $("btnResume").disabled = !b.running || !b.paused;
  $("btnStop").disabled   = !b.running;
}

function card(label, value, extraClass=""){
  return `<div class="card"><div class="label">${label}</div><div class="value ${extraClass}">${value}</div></div>`;
}

async function refresh(){
  let d;
  try { d = await (await fetch("/api/state")).json(); }
  catch(e){ $("updated").textContent = "fetch error: "+e; return; }
  const a = d.account;

  $("mode").textContent = a.ok ? a.mode : "—";
  $("mode").className = "badge " + (a.ok && a.mode==="PAPER" ? "paper" : "");
  $("mkt").textContent = a.ok ? (a.market_open?"market open":"market closed") : "no data";
  $("mkt").className = "badge " + (a.ok ? (a.market_open?"open":"closed") : "");
  $("judge").textContent = d.judge_model ? ("judge: "+d.judge_model) : "";
  applyBotState(d.bot);
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();

  if(!a.ok){
    $("cards").innerHTML = `<div class="err">Alpaca error: ${a.error}</div>`;
  } else {
    let c = "";
    c += card("Equity", money(a.equity));
    c += card("Cash", money(a.cash));
    c += card("Buying power", money(a.buying_power));
    c += card("Day P&L (account)", money(a.day_pl)+` (${a.day_pl_pct.toFixed(2)}%)`, cls(a.day_pl));
    c += card("Realized today (bot)", money(d.realized_today), cls(d.realized_today));
    c += card("Loss shutoff at", "-"+money(d.limits.daily_loss_limit).slice(1));
    $("cards").innerHTML = c;
  }

  // position
  $("poscount").textContent = (a.ok && a.positions.length) ? "("+a.positions.length+")" : "";
  if(a.ok && a.positions.length){
    let rows = a.positions.map(p=>`<tr class="pos-row">
      <td class="mono">${p.symbol}</td>
      <td><span class="pill ${p.side==='long'?'long':'short'}">${p.side}</span></td>
      <td>${p.qty}</td><td>${money(p.avg_entry)}</td>
      <td>${p.current!=null?money(p.current):"—"}</td>
      <td class="red">${p.stop!=null?money(p.stop):"—"}</td>
      <td class="green">${p.target!=null?money(p.target):"—"}</td>
      <td>${p.bracket?'<span class="pill long">broker+sw</span>':'<span class="pill short">sw-only</span>'}</td>
      <td class="${cls(p.unrealized_pl)}">${money(p.unrealized_pl)} (${p.unrealized_plpc.toFixed(2)}%)</td>
      <td><button class="btn close-pos" onclick="closePosition('${p.symbol}', this)">Close</button></td>
    </tr>`).join("");
    $("position").innerHTML = `<table><thead><tr>
      <th>Symbol</th><th>Side</th><th>Qty</th><th>Avg entry</th><th>Current</th>
      <th>Stop-loss</th><th>Take-profit</th><th>Protection</th><th>Unrealized P&L</th><th>Action</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  } else {
    $("position").innerHTML = `<div class="empty">No open positions — scanning for setups.</div>`;
  }

  // resting broker orders (bracket TP/SL legs)
  const ords = (a.ok && a.orders) ? a.orders : [];
  if(ords.length){
    let rows = ords.map(o=>{
      const k = o.kind==="take-profit" ? "long" : (o.kind==="stop-loss" ? "short" : "");
      return `<tr>
        <td class="mono">${o.symbol}</td>
        <td>${k?`<span class="pill ${k}">${o.kind}</span>`:o.kind}</td>
        <td>${o.type}</td><td>${o.side}</td>
        <td>${o.qty!=null?o.qty:""}</td>
        <td>${o.price!=null?money(o.price):"—"}</td>
        <td>${o.status}</td>
      </tr>`;
    }).join("");
    $("orders").innerHTML = `<table><thead><tr>
      <th>Symbol</th><th>Kind</th><th>Type</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  } else {
    $("orders").innerHTML = `<div class="empty">No resting orders. New trades place a take-profit limit + stop-loss stop here; positions carried over from before use software monitoring instead.</div>`;
  }

  // trades
  $("tcount").textContent = d.trades.length ? "("+d.trades.length+")" : "";
  if(d.trades.length){
    let rows = d.trades.map(t=>{
      const dir = (t.direction||"").toLowerCase();
      const dec = t.veto_decision||"";
      const pnl = t.pnl!==""&&t.pnl!=null ? parseFloat(t.pnl) : null;
      const when = (t.timestamp_close||t.timestamp_open||"").replace("T"," ").slice(5,16);
      return `<tr>
        <td class="mono">${when}</td>
        <td class="mono">${t.ticker||""}</td>
        <td>${t.strategy||""}</td>
        <td>${dir?`<span class="pill ${dir}">${dir}</span>`:""}</td>
        <td>${t.entry_price?money(parseFloat(t.entry_price)):""}</td>
        <td>${t.exit_price?money(parseFloat(t.exit_price)):""}</td>
        <td class="${pnl!=null?cls(pnl):''}">${pnl!=null?money(pnl):""}</td>
        <td>${dec?`<span class="pill ${dec}">${dec}</span>`:""}</td>
      </tr>`;
    }).join("");
    $("trades").innerHTML = `<table><thead><tr>
      <th>Time</th><th>Ticker</th><th>Strategy</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Decision</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  } else {
    $("trades").innerHTML = `<div class="empty">No trades or candidates logged yet.</div>`;
  }

  // learnings
  $("lcount").textContent = d.learnings.length ? "("+d.learnings.length+")" : "";
  if(d.learnings.length){
    $("learnings").innerHTML = d.learnings.map(l=>`<div class="learn">
      <div class="h">${l.header}</div><pre>${l.body.replace(/</g,"&lt;")}</pre></div>`).join("");
  } else {
    $("learnings").innerHTML = `<div class="empty">No learnings yet — written after the first trade closes.</div>`;
  }

  // log
  $("log").innerHTML = d.log.map(l=>`<div>${l.replace(/</g,"&lt;")}</div>`).join("");
  const lg = $("log"); lg.scrollTop = lg.scrollHeight;
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


NIGHT_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>stockbot — night school</title>
<style>
  :root {
    --bg:#0b0e14; --panel:#141a24; --panel2:#1b2330; --line:#26303f;
    --text:#e6edf3; --muted:#8b98a9; --green:#3fb950; --red:#f85149;
    --accent:#58a6ff; --amber:#d29922;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 24px; border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; font-weight:650; }
  a.back { color:var(--accent); text-decoration:none; font-size:13px; }
  .badge { font-size:11px; font-weight:700; padding:3px 8px; border-radius:6px;
    background:var(--panel2); color:var(--muted); text-transform:uppercase; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--red); display:inline-block; }
  .dot.running { background:var(--green); box-shadow:0 0 8px var(--green); }
  .updated { margin-left:auto; color:var(--muted); font-size:12px; }
  .btn { font-size:12px; font-weight:650; padding:6px 12px; border-radius:7px;
    border:1px solid var(--line); background:var(--panel2); color:var(--text);
    cursor:pointer; transition:.12s; }
  .btn:hover:not(:disabled) { border-color:var(--accent); }
  .btn:disabled { opacity:.35; cursor:not-allowed; }
  .btn.start { color:var(--green); } .btn.stop { color:var(--red); }
  .toast { position:fixed; top:14px; right:14px; z-index:50; background:var(--panel2);
    border:1px solid var(--line); border-radius:8px; padding:11px 15px; font-size:13px;
    max-width:420px; opacity:0; transform:translateY(-8px); transition:.2s;
    pointer-events:none; }
  .toast.show { opacity:1; transform:translateY(0); }
  .toast.ok { border-color:var(--green); } .toast.err { border-color:var(--red); }
  .wrap { padding:20px 24px; max-width:1200px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
    gap:14px; margin-bottom:22px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:14px 16px; }
  .card .label { color:var(--muted); font-size:11px; text-transform:uppercase;
    letter-spacing:.5px; margin-bottom:6px; }
  .card .value { font-size:22px; font-weight:650; font-variant-numeric:tabular-nums; }
  .value.green { color:var(--green); } .value.red { color:var(--red); }
  section { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    margin-bottom:22px; overflow:hidden; }
  section h2 { font-size:13px; text-transform:uppercase; letter-spacing:.5px;
    color:var(--muted); margin:0; padding:12px 16px; border-bottom:1px solid var(--line);
    background:var(--panel2); }
  .filters { display:flex; gap:8px; margin-bottom:16px; flex-wrap:wrap; align-items:center; }
  .fbtn { font-size:12px; font-weight:650; padding:6px 12px; border-radius:7px;
    border:1px solid var(--line); background:var(--panel2); color:var(--text); cursor:pointer; }
  .fbtn.active { border-color:var(--accent); color:var(--accent); }
  select { background:var(--panel2); color:var(--text); border:1px solid var(--line);
    border-radius:7px; padding:6px 10px; font-size:12px; }
  .setup { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    margin-bottom:18px; overflow:hidden; }
  .setup .head { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
    padding:12px 16px; border-bottom:1px solid var(--line); background:var(--panel2); }
  .setup .head .sym { font-weight:700; font-size:15px;
    font-family:ui-monospace,Menlo,monospace; }
  .pill { font-size:11px; font-weight:700; padding:2px 8px; border-radius:5px; }
  .pill.long,.pill.WIN,.pill.CORRECT { background:rgba(63,185,80,.15); color:var(--green); }
  .pill.short { background:rgba(210,153,34,.15); color:var(--amber); }
  .pill.LOSS,.pill.WRONG { background:rgba(248,81,73,.15); color:var(--red); }
  .pill.skip { background:rgba(139,152,169,.15); color:var(--muted); }
  .money { margin-left:auto; text-align:right; font-variant-numeric:tabular-nums; font-size:13px; }
  .money .big { font-size:16px; font-weight:700; }
  .green { color:var(--green); } .red { color:var(--red); } .muted { color:var(--muted); }
  canvas { display:block; width:100%; height:300px; }
  .details { padding:10px 16px 14px; border-top:1px solid var(--line); }
  .details .row { margin:4px 0; font-size:12.5px; }
  .details .k { color:var(--muted); }
  details summary { cursor:pointer; color:var(--accent); font-size:12.5px;
    padding:8px 16px; }
  pre.lesson { margin:6px 0 0; white-space:pre-wrap; color:var(--muted);
    font-size:12.5px; font-family:inherit; }
  .cond { display:inline-block; background:var(--panel2); border:1px solid var(--line);
    border-radius:5px; padding:1px 7px; margin:2px 3px 2px 0; font-size:11.5px;
    font-family:ui-monospace,Menlo,monospace; color:var(--muted); }
  .empty { padding:26px 16px; color:var(--muted); font-style:italic; }
  .log { font-family:ui-monospace,Menlo,monospace; font-size:12px; padding:10px 16px;
    color:#c9d4e0; max-height:260px; overflow:auto; }
  pre.playbook { margin:0; padding:14px 16px; white-space:pre-wrap; font-size:12.5px;
    color:var(--muted); font-family:ui-monospace,Menlo,monospace; max-height:280px; overflow:auto; }
  .morebtn { display:block; margin:0 auto 30px; }
</style>
</head>
<body>
<header>
  <h1>🌙 Night school</h1>
  <a class="back" href="/">← dashboard</a>
  <span class="dot" id="tdot"></span><span class="badge" id="tstate">—</span>
  <button class="btn start" id="btnTrain" onclick="trainerCtl('start')">▶ Start training</button>
  <button class="btn stop" id="btnTrainStop" onclick="trainerCtl('stop')">⏹ Stop training</button>
  <span class="updated" id="updated">connecting…</span>
</header>
<div id="toast" class="toast"></div>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <section>
    <h2>Replay playbook (what the live judge reads)</h2>
    <pre class="playbook" id="playbook">—</pre>
  </section>
  <div class="filters" id="filters">
    <button class="fbtn active" data-f="all">All</button>
    <button class="fbtn" data-f="wrong">Wrong predictions</button>
    <button class="fbtn" data-f="win">Wins</button>
    <button class="fbtn" data-f="loss">Losses</button>
    <select id="stratsel"><option value="">every strategy</option></select>
    <span class="muted" id="fcount" style="font-size:12px"></span>
  </div>
  <div id="setups"></div>
  <button class="fbtn morebtn" id="more" style="display:none">Show 20 more</button>
  <section>
    <h2>Trainer log (tail)</h2>
    <div class="log" id="tlog"></div>
  </section>
</div>
<script>
const $ = id => document.getElementById(id);
const money = n => (n<0?"-":"+")+"$"+Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
let DATA = null, filter = "all", strat = "", shown = 20;

let toastTimer = null;
function toast(msg, ok=true){
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show " + (ok?"ok":"err");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>{ t.className = "toast"; }, 5000);
}

async function trainerCtl(action){
  try {
    const r = await (await fetch("/api/trainer/"+action, {method:"POST"})).json();
    toast(r.msg, r.ok);
    setTimeout(refresh, 1200);
  } catch(e){ toast("trainer control error: "+e, false); }
}

function drawChart(canvas, rec){
  const bars = rec.bars || [];
  if(!bars.length){ return; }
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W*dpr; canvas.height = H*dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const padR = 62, padT = 10, volH = 46, padB = 20;
  const plotW = W - padR - 8, plotH = H - padT - volH - padB;
  const n = bars.length;
  let lo = Math.min(rec.stop, rec.target, rec.entry), hi = Math.max(rec.stop, rec.target, rec.entry);
  let vmax = 0;
  for(const b of bars){ lo = Math.min(lo, b[3]); hi = Math.max(hi, b[2]); vmax = Math.max(vmax, b[5]); }
  const span = (hi-lo) || 1; lo -= span*0.04; hi += span*0.04;
  const x = i => 8 + (i+0.5)/n*plotW;
  const y = p => padT + (hi-p)/(hi-lo)*plotH;
  const cw = Math.max(1, Math.min(7, plotW/n - 0.6));

  // signal marker index
  let sigIdx = n-1;
  if(rec.signal_time){
    const st = rec.signal_time;
    for(let i=0;i<n;i++){ if(bars[i][0] >= st){ sigIdx = i; break; } }
  }

  // shade the "future" the bot could NOT see when it decided
  ctx.fillStyle = "rgba(88,166,255,.045)";
  ctx.fillRect(x(sigIdx), padT, plotW+8-x(sigIdx), plotH+volH);

  // horizontal gridlines + price labels
  ctx.strokeStyle = "#26303f"; ctx.fillStyle = "#8b98a9";
  ctx.font = "10px ui-monospace,Menlo,monospace"; ctx.textBaseline = "middle";
  for(let g=0; g<=4; g++){
    const p = lo + (hi-lo)*g/4, yy = y(p);
    ctx.beginPath(); ctx.moveTo(8, yy); ctx.lineTo(8+plotW, yy);
    ctx.lineWidth = 0.5; ctx.stroke();
    ctx.fillText(p.toFixed(2), 8+plotW+6, yy);
  }
  // hour labels
  ctx.textBaseline = "alphabetic";
  for(let i=0; i<n; i+=60){
    const t = bars[i][0].slice(11,16);
    ctx.fillText(t, x(i)-12, H-6);
  }

  // volume
  for(let i=0;i<n;i++){
    const b = bars[i];
    ctx.fillStyle = b[4] >= b[1] ? "rgba(63,185,80,.35)" : "rgba(248,81,73,.35)";
    const vh = vmax ? (b[5]/vmax)*(volH-6) : 0;
    ctx.fillRect(x(i)-cw/2, padT+plotH+volH-vh, cw, vh);
  }

  // candles
  for(let i=0;i<n;i++){
    const b = bars[i], up = b[4] >= b[1];
    ctx.fillStyle = up ? "#3fb950" : "#f85149";
    ctx.fillRect(x(i)-0.5, y(b[2]), 1, Math.max(1, y(b[3])-y(b[2])));  // wick
    const top = y(Math.max(b[1],b[4])), bot = y(Math.min(b[1],b[4]));
    ctx.fillRect(x(i)-cw/2, top, cw, Math.max(1, bot-top));            // body
  }

  // levels: entry / stop / target
  function level(price, color, label){
    const yy = y(price);
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash([5,4]);
    ctx.beginPath(); ctx.moveTo(8, yy); ctx.lineTo(8+plotW, yy); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color; ctx.font = "bold 10px ui-monospace,Menlo,monospace";
    ctx.textBaseline = "middle";
    ctx.fillText(label+" "+price.toFixed(2), 8+plotW+6, yy>padT+8?yy-9:yy+9);
  }
  level(rec.target, "#3fb950", "TP");
  level(rec.stop,   "#f85149", "SL");
  level(rec.entry,  "#58a6ff", "IN");

  // vertical signal marker
  ctx.strokeStyle = "#d29922"; ctx.lineWidth = 1.2; ctx.setLineDash([2,3]);
  ctx.beginPath(); ctx.moveTo(x(sigIdx), padT); ctx.lineTo(x(sigIdx), padT+plotH+volH); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#d29922"; ctx.font = "bold 10px ui-monospace,Menlo,monospace";
  ctx.fillText("▼ decision", Math.min(x(sigIdx)+4, plotW-58), padT+10);
}

function match(r){
  if(strat && r.strategy !== strat) return false;
  if(filter === "wrong") return r.correct === false;
  if(filter === "win")   return r.r_multiple > 0;
  if(filter === "loss")  return r.r_multiple <= 0;
  return true;
}

function render(){
  if(!DATA) return;
  const recs = DATA.records.filter(match);
  $("fcount").textContent = recs.length + " setup(s)";
  const slice = recs.slice(0, shown);
  $("more").style.display = recs.length > shown ? "block" : "none";
  const host = $("setups");
  host.innerHTML = slice.length ? "" :
    `<div class="setup"><div class="empty">Nothing here yet — night school starts automatically after the close (4:05 PM weekdays, 10:00 AM weekends) while this dashboard is running, or press ▶ Start training above.</div></div>`;
  slice.forEach((r, idx) => {
    const actual = r.r_multiple > 0 ? "WIN" : "LOSS";
    const quizzed = r.prediction != null;
    const el = document.createElement("div");
    el.className = "setup";
    el.innerHTML = `
      <div class="head">
        <span class="sym">${r.ticker}</span>
        <span class="muted">${r.day}</span>
        <span class="pill ${r.direction}">${r.direction}</span>
        <span class="badge">${r.strategy}</span>
        <span class="badge">${r.regime}</span>
        <span class="badge">${(r.bucket||"").replace(/_/g," ")}</span>
        ${quizzed
          ? `<span class="pill ${r.prediction}">bot said ${r.prediction}</span>
             <span class="pill ${actual}">actual ${actual}</span>
             <span class="pill ${r.correct?"CORRECT":"WRONG"}">${r.correct?"✓ correct":"✗ wrong"}</span>`
          : `<span class="pill skip">stats only (not quizzed)</span>
             <span class="pill ${actual}">actual ${actual}</span>`}
        <div class="money">
          <div class="big ${r.simulated_pnl_usd>=0?"green":"red"}">${money(r.simulated_pnl_usd)} (${r.r_multiple>=0?"+":""}${r.r_multiple.toFixed(2)}R)</div>
          <div class="muted">max if target hit: ${money(r.max_pnl_usd)} · risk $${r.risk_usd}</div>
        </div>
      </div>
      <canvas id="c${idx}"></canvas>
      <div class="details">
        ${quizzed ? `<div class="row"><span class="k">bot's reasoning:</span> ${(r.prediction_reason||"").replace(/</g,"&lt;")}</div>` : ""}
        <div class="row"><span class="k">setup fired at:</span> ${(r.signal_time||"").slice(11,16)} ET ·
          <span class="k">entry</span> ${r.entry.toFixed(2)} · <span class="k">stop</span> ${r.stop.toFixed(2)} ·
          <span class="k">target</span> ${r.target.toFixed(2)} · <span class="k">R:R</span> ${r.reward_risk}</div>
        <div class="row">${Object.entries(r.reasoning||{}).map(([k,v])=>`<span class="cond">${k}: ${String(v).replace(/</g,"&lt;")}</span>`).join("")}</div>
        ${r.lesson ? `<details><summary>lesson the bot wrote about this ${r.correct ? "call" : "mistake"}</summary><pre class="lesson">${r.lesson.replace(/</g,"&lt;")}</pre></details>` : ""}
      </div>`;
    host.appendChild(el);
    drawChart(el.querySelector("canvas"), r);
  });
}

async function refresh(){
  let d;
  try { d = await (await fetch("/api/nightschool")).json(); }
  catch(e){ $("updated").textContent = "fetch error: "+e; return; }
  DATA = d;
  $("tdot").className = "dot " + (d.trainer.running ? "running" : "");
  $("tstate").textContent = d.trainer.running ? ("training · pid "+d.trainer.pid) : "trainer idle";
  $("btnTrain").disabled = d.trainer.running;
  $("btnTrainStop").disabled = !d.trainer.running;
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();

  const recs = d.records, quizzed = recs.filter(r=>r.prediction!=null);
  const correct = quizzed.filter(r=>r.correct).length;
  const totalPnl = recs.reduce((s,r)=>s+r.simulated_pnl_usd, 0);
  const totalR = recs.reduce((s,r)=>s+r.r_multiple, 0);
  const lifetime = (d.stats.predictions||{})._total || {};
  let c = "";
  c += `<div class="card"><div class="label">Setups studied (kept)</div><div class="value">${recs.length}</div></div>`;
  c += `<div class="card"><div class="label">Quizzed this batch</div><div class="value">${quizzed.length}</div></div>`;
  c += `<div class="card"><div class="label">Accuracy (this batch)</div><div class="value">${quizzed.length?Math.round(correct/quizzed.length*100)+"%":"—"}</div></div>`;
  c += `<div class="card"><div class="label">Accuracy (lifetime)</div><div class="value">${lifetime.n?Math.round(lifetime.correct/lifetime.n*100)+"% <span style='font-size:12px;color:var(--muted)'>("+lifetime.correct+"/"+lifetime.n+")</span>":"—"}</div></div>`;
  c += `<div class="card"><div class="label">Simulated P&L (kept setups)</div><div class="value ${totalPnl>=0?"green":"red"}">${money(totalPnl)}</div></div>`;
  c += `<div class="card"><div class="label">Total R (kept setups)</div><div class="value ${totalR>=0?"green":"red"}">${totalR>=0?"+":""}${totalR.toFixed(1)}R</div></div>`;
  $("cards").innerHTML = c;

  $("playbook").textContent = d.playbook || "(no playbook yet — needs 20+ replays in a bucket)";
  $("tlog").innerHTML = d.log.map(l=>`<div>${l.replace(/</g,"&lt;")}</div>`).join("") || "(no trainer.log yet)";
  const tl = $("tlog"); tl.scrollTop = tl.scrollHeight;

  const strategies = [...new Set(recs.map(r=>r.strategy))].sort();
  const sel = $("stratsel"), cur = sel.value;
  sel.innerHTML = `<option value="">every strategy</option>` +
    strategies.map(s=>`<option ${s===cur?"selected":""}>${s}</option>`).join("");
  render();
}

document.querySelectorAll(".fbtn[data-f]").forEach(b => b.onclick = () => {
  document.querySelectorAll(".fbtn[data-f]").forEach(x=>x.classList.remove("active"));
  b.classList.add("active"); filter = b.dataset.f; shown = 20; render();
});
$("stratsel").onchange = e => { strat = e.target.value; shown = 20; render(); };
$("more").onclick = () => { shown += 20; render(); };
refresh();
setInterval(refresh, 60000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    threading.Thread(target=_scheduler_loop, daemon=True,
                     name="scheduler").start()
    print("stockbot dashboard -> http://localhost:8000  (Ctrl-C to stop)")
    print("built-in scheduler: bot weekdays 9:25 ET · night school 16:05 "
          "weekdays / 10:00 weekends")
    app.run(host="127.0.0.1", port=8000, debug=False, use_reloader=False)
