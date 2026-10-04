"""Part B — LLM judge (veto gate) and learnings writer.

Talks to any OpenAI-compatible chat endpoint: LM Studio locally by default
(no API key), or a hosted provider such as Groq.

OPTIONAL: with `judge.enabled: false` in config.yaml (or `python main.py
--no-judge`) the rules engine runs alone — every candidate that passes the
deterministic gates is approved and logged as "rules_only".

HARD CONSTRAINT, enforced structurally: this module performs NO file I/O of
any kind. It receives text and returns text. It cannot touch config.yaml
(or anything else) because it has no code paths that write. main.py is the
only writer, and it only appends to learnings.md and trades.csv.

Fail-closed WHEN ENABLED: any error, timeout, or non-conforming answer = veto.
"""

from __future__ import annotations

import os
import re

import requests

VETO_SYSTEM_PROMPT = """You are a strict trade-approval gate for a mechanical paper-trading bot.
You are NOT a trading advisor. You do not invent analysis. You only verify a checklist.

You will receive a proposed trade summary and past-trade learnings excerpts.

Work through these checks ONE AT A TIME. For each, write the numbers you are
comparing, then PASS or FAIL. Do not skip any check.

CHECK 1 — conditions: every entry condition in the summary is stated as met
  (none marked unmet, unclear, or missing).
CHECK 2 — stop side: for a LONG the stop number must be LESS THAN the entry
  number; for a SHORT the stop must be GREATER THAN the entry. Compare the
  actual numbers digit by digit.
CHECK 3 — target side: for a LONG the target must be GREATER THAN the entry;
  for a SHORT the target must be LESS THAN the entry.
CHECK 4 — history: the summary states "Past losses on this exact setup: N".
  If N >= 2, FAIL. Also FAIL if the learnings text describes a warning that
  clearly matches this same ticker + strategy + direction.
CHECK 5 — clarity: nothing in the summary is ambiguous or contradictory.
CHECK 6 — recent activity: read "Recent same-symbol activity". FAIL if this
  proposal flips direction versus an entry less than 30 minutes ago, or if
  any trade on this symbol closed less than 15 minutes ago.
CHECK 7 — reward:risk band: compare the stated Reward:risk to the stated
  Allowed reward:risk band. FAIL if it is below the minimum or above the maximum.
CHECK 8 — replay playbook: the learnings may include a REPLAY PLAYBOOK section
  with AVOID lines produced by offline historical training. FAIL only if an
  AVOID line matches this trade's strategy AND direction AND the market regime
  or entry time bucket stated in the summary. If there is no playbook section
  or no line matches all of those, PASS.

If unsure on any check, that check is FAIL (except CHECK 8, which PASSes when
no playbook line clearly matches).

Respond in EXACTLY this format, nothing else:
CHECK 1: PASS or FAIL — <few words>
CHECK 2: PASS or FAIL — <entry vs stop numbers>
CHECK 3: PASS or FAIL — <entry vs target numbers>
CHECK 4: PASS or FAIL — <N and any matching warning>
CHECK 5: PASS or FAIL — <few words>
CHECK 6: PASS or FAIL — <recent symbol timing and direction>
CHECK 7: PASS or FAIL — <actual R:R vs allowed band>
CHECK 8: PASS or FAIL — <matching playbook line, or none>
REASON: <one short sentence>
VERDICT: YES (only if all eight checks are PASS) or VERDICT: NO"""

PREDICT_SYSTEM_PROMPT = """You are being quizzed on a HISTORICAL trade setup that a mechanical
paper-trading bot detected in past market data. The outcome is already known
to the quizmaster but hidden from you. Using ONLY the facts given (setup
conditions, market regime, time of day, any replay playbook lines, and any
lessons from previously graded predictions), predict whether the trade reached
its target (WIN) or was stopped out / closed at a loss (LOSS).

Consider: does the regime favor the direction? Is the time of day historically
good for this kind of setup? Do any playbook lines warn against it? Do the
lessons describe a similar setup — and was the reasoning used there proven
right or wrong? Apply what the lessons say to keep in mind.

Respond in EXACTLY this format, nothing else:
PREDICTION: WIN or LOSS
REASON: <one short sentence naming the deciding factor>"""

REPLAY_LESSON_SYSTEM_PROMPT = """You review replayed historical trade setups for a mechanical
paper-trading bot. A WIN/LOSS prediction about this setup was just graded
against the actual outcome; the facts state whether it was CORRECT or WRONG.
Using ONLY the facts given — never invent prices or events — write a short
lesson entry so future predictions and trade approvals do better: state why
the prediction was made, why that reasoning held up or failed, and what to
keep in mind for similar setups in the future. Output ONLY the entry, in
EXACTLY this markdown format. Fill in the real replay day, ticker, strategy,
prediction, outcome, R multiple, and grade from the facts — for example a
header looks like:
## 2026-06-01 — AAPL — support_hold — PREDICTED WIN, ACTUAL LOSS (-1.00R) — WRONG

## <replay day> — <ticker> — <strategy> — PREDICTED <WIN/LOSS>, ACTUAL <WIN/LOSS> (<R multiple>R) — <CORRECT/WRONG>
- Setup: which conditions fired, regime, and time bucket
- Why the prediction was made:
- What actually happened:
- Why the reasoning was right or wrong:
- Keep in mind for the future:"""

LEARNINGS_SYSTEM_PROMPT = """You write a post-trade journal entry for a mechanical paper-trading bot.
Use ONLY the facts given. Do not invent prices or events. Be concise.
You may suggest a parameter adjustment, but it is a suggestion only — it is
never auto-applied. Output ONLY the entry, in EXACTLY this markdown format:

## [DATE TIME] — TICKER — STRATEGY — WIN/LOSS (P&L)
- Setup: which conditions fired and the key levels
- Direction & reasoning: why this trade was taken
- Outcome: what price actually did
- Why it worked / why it failed:
- Suggested adjustment (NOT auto-applied):"""


RULES_ONLY_REASON = "judge disabled — rules engine only"


def is_enabled(cfg: dict) -> bool:
    return bool(cfg.get("enabled", True))


def probe(cfg: dict) -> str | None:
    """Startup reachability check. Returns None if the endpoint answers,
    else a short human-readable problem description."""
    if not is_enabled(cfg):
        return None
    try:
        headers = {}
        key = os.environ.get(cfg.get("api_key_env") or "", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        r = requests.get(cfg["base_url"].rstrip("/") + "/models",
                         headers=headers, timeout=5)
        r.raise_for_status()
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def _chat(cfg: dict, system: str, user: str) -> str:
    """One chat completion against an OpenAI-compatible endpoint (LM Studio
    locally, or a hosted provider like Groq). Raises on any failure.

    Auth: if cfg["api_key_env"] names an env var that is set, its value is
    sent as a Bearer token. LM Studio needs no key, so leaving it unset (or
    the var empty) simply sends no Authorization header.
    """
    if not is_enabled(cfg):
        raise RuntimeError(RULES_ONLY_REASON)
    headers = {}
    key_env = cfg.get("api_key_env")
    if key_env:
        key = os.environ.get(key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
    resp = requests.post(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        headers=headers,
        json={
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": cfg.get("temperature", 0.0),
            "max_tokens": cfg.get("max_tokens", 1024),
        },
        timeout=cfg.get("timeout_seconds", 60),
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    # strip <think>...</think> blocks some local models (Qwen etc.) emit
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def veto_check(cfg: dict, setup_summary: str, learnings_excerpt: str) -> tuple[bool, str]:
    """Returns (approved, reason). Approved only on an explicit VERDICT: YES.
    With the judge disabled, the rules engine's candidate is approved as-is."""
    if not is_enabled(cfg):
        return True, RULES_ONLY_REASON
    user = (
        "PROPOSED TRADE\n--------------\n" + setup_summary.strip() +
        "\n\nPAST LEARNINGS FOR THIS STRATEGY (most recent last; may be empty)\n"
        "----------------------------------------------------------------\n" +
        (learnings_excerpt.strip() or "(none yet)") +
        "\n\nApply the checklist and respond in the required format."
    )
    try:
        answer = _chat(cfg, VETO_SYSTEM_PROMPT, user)
    except Exception as e:  # unreachable, timeout, bad JSON -> fail closed
        return False, f"judge unavailable ({type(e).__name__}: {e})"

    m = re.search(r"VERDICT:\s*(YES|NO)", answer, re.IGNORECASE)
    if not m:
        return False, f"judge answer not in required format: {answer[:200]!r}"
    reason_m = re.search(r"REASON:\s*(.+)", answer)
    reason = reason_m.group(1).strip() if reason_m else "(no reason given)"
    verdict_yes = m.group(1).upper() == "YES"
    # fail closed on internal inconsistency: any FAILed check overrides a YES
    if verdict_yes and re.search(r"CHECK \d:\s*FAIL", answer, re.IGNORECASE):
        return False, f"judge inconsistent (a check FAILed but verdict YES): {reason}"
    return verdict_yes, reason


def _parse_prediction(answer: str) -> tuple[str | None, str]:
    """Extract (WIN|LOSS, reason) from a prediction answer; (None, why) if bad."""
    m = re.search(r"PREDICTION:\s*(WIN|LOSS)", answer, re.IGNORECASE)
    if not m:
        return None, f"prediction not in required format: {answer[:200]!r}"
    reason_m = re.search(r"REASON:\s*(.+)", answer)
    reason = reason_m.group(1).strip() if reason_m else "(no reason given)"
    return m.group(1).upper(), reason


def predict_outcome(cfg: dict, setup_summary: str, playbook_excerpt: str,
                    lessons_excerpt: str = "") -> tuple[str | None, str]:
    """Offline-training quiz: predict WIN or LOSS for a historical setup.
    Returns (prediction, reason); prediction is None on any failure."""
    user = (
        "HISTORICAL SETUP (outcome hidden)\n---------------------------------\n"
        + setup_summary.strip()
        + "\n\nREPLAY PLAYBOOK (may be empty)\n------------------------------\n"
        + (playbook_excerpt.strip() or "(none yet)")
        + "\n\nLESSONS FROM PAST GRADED PREDICTIONS (most recent last; may be empty)\n"
        "---------------------------------------------------------------------\n"
        + (lessons_excerpt.strip() or "(none yet)")
        + "\n\nMake your prediction now in the required format."
    )
    try:
        answer = _chat(cfg, PREDICT_SYSTEM_PROMPT, user)
    except Exception as e:
        return None, f"judge unavailable ({type(e).__name__}: {e})"
    return _parse_prediction(answer)


def write_replay_lesson(cfg: dict, replay_facts: str) -> str | None:
    """Ask the model for a lesson entry about a graded (correct OR wrong)
    replay prediction. Returns the entry text, or None on failure."""
    try:
        entry = _chat(cfg, REPLAY_LESSON_SYSTEM_PROMPT,
                      "REPLAY FACTS\n------------\n" + replay_facts.strip() +
                      "\n\nWrite the lesson entry now.")
    except Exception:
        return None
    return entry if entry.startswith("## ") else None


def write_learnings_entry(cfg: dict, trade_facts: str) -> str | None:
    """Ask the model for a learnings entry. Returns the entry text, or None
    on failure (main.py then writes a plain fallback entry itself)."""
    try:
        entry = _chat(cfg, LEARNINGS_SYSTEM_PROMPT,
                      "TRADE FACTS\n-----------\n" + trade_facts.strip() +
                      "\n\nWrite the journal entry now.")
    except Exception:
        return None
    return entry if entry.startswith("## ") else None
