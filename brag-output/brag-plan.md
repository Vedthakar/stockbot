# brag plan — stockbot

**What it is:** an open-source Alpaca trading bot where a deterministic rules engine finds intraday setups and an LLM (local LM Studio by default) can only veto them.
**For:** devs/traders who want to tinker with algo trading safely — paper by default.
**Sets it apart:** the LLM can't invent trades, change config or override risk gates. It only says no — and every yes/no is logged with the reason.
**Most impressive claim (true, from code):** fail-closed veto — if the model is down or answers off-format, the trade is skipped.
**Visual hook:** terminal log where a setup is found, then a big red VETO stamp.
**Real UI shown:** real lines from `sample_logs/decision_log_2026-08-13.log` (paper session), real CLI flags, real `strategies:` config block.
**Tone:** default/polished hybrid — dark terminal, GitHub-dark palette from `dashboard.py` (#0b0e14 bg, #3fb950 green, #f85149 red, #58a6ff accent, #d29922 amber), monospace.
**Share caption:** "A rules engine finds the setup. A local LLM gets veto power."

**Honesty rules:** no win rate or profit claim anywhere. The only P&L shown is the real paper loss on the KO trade in the learning scene. Log scenes are captioned as real paper logs. Outro carries "Paper trading by default · Not financial advice".

## Storyboard (landscape 1920×1080, 30fps, 25s (timings below pre-date the learning scene; see index.html))

| # | Time | Scene | On screen |
|---|---|---|---|
| 1 | 0.0–3.2 | Hook | Line types: "A rules engine finds the setup." (green) → second line slams in: "A local LLM gets veto power." (red). |
| 2 | 3.2–7.0 | Reveal / how it works | `stockbot` wordmark; pipeline animates left→right: 5 strategy chips → Rules engine → Risk gates → LLM judge → APPROVE / VETO. |
| 3 | 7.0–11.6 | Highlight: blocked | Terminal window. Real lines type in: `CANDIDATE NKE support_hold long entry~41.09 SL 40.91 TP 41.37 -> asking judge` → `VETO NKE support_hold: AVOID line in REPLAY PLAYBOOK matches trade details.` Red BLOCKED stamp. Caption "real paper-trading log · 2026-08-13". |
| 4 | 11.6–15.6 | Highlight: taken | Same terminal: `CANDIDATE KO higher_low long …` → `APPROVED by judge: All checks pass…` → `ENTERED LONG KO @ 87.70 (SL 87.43 / TP 88.09) [broker bracket + software backup]`. Green TAKEN stamp. Sub: "Every yes and every no is logged with the reason." |
| 4b | 15.2–19.6 | Highlight: learns from every trade | Real `learnings.md` post-mortem of that same KO trade (a LOSS) → real night-school `playbook.md` AVOID line. "Every trade gets a post-mortem. Losing patterns get vetoed." |
| 5 | 19.6–22.6 | Highlight: safe for strangers | Three cards slide up: "Paper by default" · "`--live` = flag + typed confirmation" · "`--no-judge` runs without a model". |
| 6 | 18.4–21.0 | Outro | `stockbot` + `python main.py` + "Paper trading by default · Not financial advice". |

Transitions: dip through background between scenes (no busy crossfades).

## Sound
Bundled track happy-beats vol 10 (see cue file), low SFX: soft key ticks under typing, a muted thud under each stamp, mixed under music.
