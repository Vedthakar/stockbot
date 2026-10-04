"""Summarize the included PAPER-trading log. Every number in the README comes
from running this file — nothing is hand-typed.

    python sample_logs/summarize.py
"""

import csv
import os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "paper_trades.csv"), newline="") as f:
    rows = list(csv.DictReader(f))

decisions = Counter(r["veto_decision"] for r in rows)
closed = [r for r in rows if r["veto_decision"] in ("approved", "closed_external") and r["pnl"]]
wins = [r for r in closed if float(r["pnl"]) > 0]
total = sum(float(r["pnl"]) for r in closed)
vetoes = [r for r in rows if r["veto_decision"] == "vetoed"]
judge_down = [r for r in vetoes if r["judge_reason"].startswith("judge unavailable")]

print("PAPER TRADING ONLY — Alpaca paper account, simulated fills. Not live results.")
print(f"period            : {rows[0]['timestamp_open'][:10]} .. {rows[-1]['timestamp_open'][:10]}")
print(f"rows              : {len(rows)}")
for k, v in decisions.most_common():
    print(f"  {k:<16}: {v}")
print(f"LLM vetoes        : {len(vetoes)} ({len(judge_down)} of them fail-closed because the judge was unreachable)")
print(f"closed trades     : {len(closed)}")
print(f"winners           : {len(wins)} ({len(wins) / len(closed):.0%})")
print(f"net paper P&L     : ${total:+,.2f}")
