"""Alpaca wrapper. Every call to Alpaca (data or trading) goes through here.

Everything account-specific comes from environment variables (a local .env
file is loaded if present — copy .env.example to .env):

  ALPACA_API_KEY / ALPACA_SECRET_KEY   required
  ALPACA_PAPER_URL                     paper trading endpoint (optional override)
  ALPACA_LIVE_URL                      live trading endpoint  (optional override)
  ALPACA_DATA_URL                      market data endpoint   (optional override)
  ALPACA_DATA_FEED                     "iex" (free) or "sip"  (optional)
  ALPACA_EXPECTED_ACCOUNT_ID           if set, refuse to start unless the keys
                                       belong to this account number (optional)

PAPER IS THE ONLY DEFAULT. Live trading needs Broker(config, live=True), which
main.py only passes after the `--live` flag AND an interactive typed
confirmation. Nothing in config.yaml or the environment can switch to live.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrderByIdRequest,
    GetOrdersRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

DEFAULT_PAPER_URL = "https://paper-api.alpaca.markets"
DEFAULT_LIVE_URL = "https://api.alpaca.markets"
DEFAULT_DATA_URL = "https://data.alpaca.markets"


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader — KEY=VALUE lines, no quoting games."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _apply_default_timeout(client, timeout_s: float) -> None:
    """Force a socket timeout onto an alpaca-py client's requests Session.

    requests.Session has no default-timeout setting, and alpaca-py never
    passes one, so we wrap Session.request to supply it when the caller
    omits it. A blocked read here is not theoretical: it is what hung the
    bot for 22 hours on 2026-08-03.
    """
    session = getattr(client, "_session", None)
    if session is None or getattr(session, "_stockbot_timeout", False):
        return
    original = session.request

    def request_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", timeout_s)
        return original(*args, **kwargs)

    session.request = request_with_timeout
    session._stockbot_timeout = True


@dataclass
class Bar:
    ts: datetime  # bar start time, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


class Broker:
    def __init__(self, config: dict, live: bool = False):
        load_dotenv()
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            sys.exit("FATAL: set ALPACA_API_KEY and ALPACA_SECRET_KEY "
                     "(cp .env.example .env, then paste your PAPER keys)")

        # Older configs carried `account: {mode: live}`. That switch is gone:
        # refuse loudly rather than silently trading paper or live.
        if config.get("account", {}).get("mode", "paper") != "paper":
            sys.exit("FATAL: config.yaml account.mode is no longer supported. "
                     "Remove it; live trading is only enabled with `python main.py --live`.")

        self.paper = not live
        trading_url = (os.environ.get("ALPACA_LIVE_URL") or DEFAULT_LIVE_URL) if live \
            else (os.environ.get("ALPACA_PAPER_URL") or DEFAULT_PAPER_URL)
        data_url = os.environ.get("ALPACA_DATA_URL") or DEFAULT_DATA_URL
        self.data_feed = (os.environ.get("ALPACA_DATA_FEED")
                          or config.get("alpaca", {}).get("data_feed", "iex"))
        self.trading = TradingClient(key, secret, paper=self.paper, url_override=trading_url)
        self.data = StockHistoricalDataClient(key, secret, url_override=data_url)
        self.expected_account_id = os.environ.get("ALPACA_EXPECTED_ACCOUNT_ID", "").strip()
        # alpaca-py's RESTClient exposes no timeout option and calls
        # Session.request() with none, so a connection that goes quiet blocks
        # the single-threaded poll loop forever. On 2026-08-03 that froze the
        # bot mid-tick at 14:43 with a position open; the process stayed alive,
        # so the next morning's scheduler saw a live pid and never started a
        # fresh session. Inject a default timeout into both clients' sessions.
        timeout_s = float(config.get("alpaca", {}).get("http_timeout_seconds", 15))
        for client in (self.trading, self.data):
            _apply_default_timeout(client, timeout_s)
        self._spread_samples: dict[str, list[tuple[float, float]]] = {}

    # ---- account / clock -------------------------------------------------

    def account(self):
        acct = self.trading.get_account()
        if self.expected_account_id and acct.account_number != self.expected_account_id:
            sys.exit("FATAL: these Alpaca keys belong to a different account than "
                     "ALPACA_EXPECTED_ACCOUNT_ID. Refusing to trade.")
        return acct

    def clock(self):
        return self.trading.get_clock()

    def market_open(self) -> bool:
        return bool(self.clock().is_open)

    # ---- market data -----------------------------------------------------

    def minute_bars(self, symbols: list[str], start: datetime, end: datetime | None = None) -> dict[str, list[Bar]]:
        """1-minute bars per symbol between start and end (UTC datetimes)."""
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed=self.data_feed,
        )
        result = self.data.get_stock_bars(req)
        out: dict[str, list[Bar]] = {s: [] for s in symbols}
        for symbol, bars in result.data.items():
            out[symbol] = [
                Bar(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
                for b in bars
            ]
        return out

    def latest_price(self, symbol: str) -> float:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.data_feed)
        trade = self.data.get_stock_latest_trade(req)[symbol]
        return float(trade.price)

    def latest_quote(self, symbol: str) -> tuple[float, float, datetime]:
        """Return current (bid, ask, timestamp); invalid/crossed quotes raise."""
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.data_feed)
        quote = self.data.get_stock_latest_quote(req)[symbol]
        bid, ask = float(quote.bid_price), float(quote.ask_price)
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError(f"invalid quote for {symbol}: bid={bid}, ask={ask}")
        return bid, ask, quote.timestamp

    def observe_spread(self, symbol: str, spread_pct: float,
                       ttl_seconds: float = 30.0) -> float:
        """Record a spread sample and return the tightest one still in window.

        IEX carries ~2% of consolidated volume, so a single sample often
        catches a one-sided or stale top-of-book and reports a spread that
        cannot be real (measured 6-11% on MA/GS/AMGN, whose true NBBO is
        ~0.01%). The poll loop re-samples every tick anyway, so keeping the
        tightest recent observation measures the name's actual liquidity
        instead of the feed's worst instant.

        Gating only — never use this for pricing, since the tightest sample
        may be older than the current quote.
        """
        now = time.monotonic()
        seen = self._spread_samples.setdefault(symbol, [])
        seen.append((now, spread_pct))
        cutoff = now - ttl_seconds
        seen[:] = [(ts, sp) for ts, sp in seen if ts >= cutoff]
        return min(sp for _, sp in seen)

    def previous_daily_closes(self, symbols: list[str]) -> dict[str, float]:
        """Most recent completed daily close for gap-feature calculation."""
        now = datetime.now(timezone.utc)
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=now - timedelta(days=10),
            end=now,
            feed=self.data_feed,
        )
        result = self.data.get_stock_bars(req)
        today = now.astimezone().date()
        out = {}
        for symbol, bars in result.data.items():
            completed = [bar for bar in bars if bar.timestamp.date() < today]
            if completed:
                out[symbol] = float(completed[-1].close)
        return out

    # ---- positions / orders ---------------------------------------------

    def open_positions(self):
        return self.trading.get_all_positions()

    def open_orders(self):
        return self.trading.get_orders()  # defaults to status=open

    def cancel_all_orders(self):
        return self.trading.cancel_orders()

    def cancel_orders_for_symbol(self, symbol: str) -> int:
        """Cancel only the open orders for one symbol (e.g. a position's bracket
        legs) — so closing one position never strips another's protection."""
        n = 0
        for o in self.trading.get_orders():  # open orders
            if o.symbol == symbol:
                try:
                    self.trading.cancel_order_by_id(o.id)
                    n += 1
                except Exception:
                    pass
        return n

    def cancel_orders_for_symbol_and_wait(self, symbol: str, timeout_s: float = 8.0) -> bool:
        """Cancel a symbol's exit orders and wait until Alpaca releases the
        position quantity. Bracket/OCO cancellation is asynchronous."""
        self.cancel_orders_for_symbol(symbol)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not any(o.symbol == symbol for o in self.trading.get_orders()):
                # Alpaca can report no open order just before held qty releases.
                time.sleep(0.35)
                return True
            time.sleep(0.25)
        return False

    def replace_bracket_stop(self, parent_order_id: str, stop_price: float):
        """Replace the active stop leg of a bracket order."""
        parent = self.trading.get_order_by_id(
            parent_order_id, GetOrderByIdRequest(nested=True))
        for leg in parent.legs or []:
            order_type = str(leg.type).lower()
            status = str(leg.status).lower()
            if "stop" in order_type and not any(
                    terminal in status for terminal in ("filled", "canceled", "expired", "rejected")):
                return self.trading.replace_order_by_id(
                    leg.id, ReplaceOrderRequest(stop_price=round(stop_price, 2)))
        raise LookupError(f"no active stop leg found for bracket {parent_order_id}")

    def submit_market_order(self, symbol: str, side: str, qty: float | None = None, notional: float | None = None):
        """Market order. Exactly one of qty / notional. side is 'buy' or 'sell'."""
        req = MarketOrderRequest(
            symbol=symbol,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            qty=qty,
            notional=notional,
        )
        return self.trading.submit_order(req)

    def submit_bracket_order(self, symbol: str, side: str, qty: float,
                             take_profit_price: float, stop_price: float):
        """Market entry with attached OCO exit legs: a take-profit LIMIT and a
        stop-loss STOP, both living at the broker. Whole shares only. side is
        'buy' (long) or 'sell' (short)."""
        req = MarketOrderRequest(
            symbol=symbol,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            qty=qty,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        )
        return self.trading.submit_order(req)

    def close_position(self, symbol: str):
        """Flatten a symbol at market."""
        return self.trading.close_position(symbol)

    def get_order(self, order_id: str):
        return self.trading.get_order_by_id(order_id)

    def last_filled_order(self, symbol: str):
        """Most recent FILLED order for a symbol — used to recover the real
        exit price/side after a broker bracket leg closes a position."""
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol],
                               limit=20, nested=False)
        for o in self.trading.get_orders(req):
            if str(o.status) in ("OrderStatus.FILLED", "filled") and o.filled_avg_price:
                return o
        return None


# ---- connection self-test: python broker.py ------------------------------

if __name__ == "__main__":
    import yaml

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    b = Broker(cfg)  # self-test is always paper
    acct = b.account()
    clk = b.clock()
    print(f"mode           : {'PAPER' if b.paper else '*** LIVE ***'}")
    print(f"account status : {acct.status}")
    print(f"cash           : ${float(acct.cash):,.2f}")
    print(f"buying power   : ${float(acct.buying_power):,.2f}")
    print(f"market open now: {clk.is_open} (next open {clk.next_open}, next close {clk.next_close})")

    # pull a few recent 1-minute bars to prove the data plumbing works
    end = datetime.now(timezone.utc) - timedelta(minutes=16)  # stay clear of recent-data limits on free feed
    start = end - timedelta(days=3)
    bars = b.minute_bars(["AAPL"], start, end)["AAPL"]
    if bars:
        last = bars[-1]
        print(f"data check     : {len(bars)} AAPL 1m bars, last {last.ts} close ${last.close}")
    else:
        print("data check     : no bars returned (market holiday/weekend window?)")
    print("connection test PASSED" if acct.status == "ACTIVE" else "connection test: account not ACTIVE")
