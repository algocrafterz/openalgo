---
name: strategy-from-video
description: Turn a trading strategy described in a YouTube video (or article, thread, PDF) into a tested PineScript strategy for OpenAlgo. Use whenever the user shares a video URL and asks to understand, transcribe, implement, or backtest the strategy in it. Covers pulling the real transcript and comments, converting vague discretionary rules into testable ones, choosing timeframe and universe on evidence, and reporting the result honestly. Chains into the pinescript-strategy skill for the implementation itself.
---

# Strategy from a video

The job is not "write the PineScript in the video". It is "find out whether the idea in
the video survives contact with data, and leave behind something the user can trade or
confidently discard".

Work in this order. Do not skip to the Pine file — implementing first and testing later
means the test is judging code you are already attached to.

---

## 1. Get the actual source material

Never work from the title, the thumbnail, or a summary. Get the transcript and the
comments.

```bash
uv tool install yt-dlp     # once

# transcript (auto-captions are fine)
yt-dlp --skip-download --write-auto-sub --write-sub --sub-lang "en.*" \
       --sub-format vtt -o "vid.%(ext)s" "<URL>"

# comments, most-liked first
yt-dlp --skip-download --write-comments \
       --extractor-args "youtube:comment_sort=top;max_comments=400,all,100" \
       -o "c.%(ext)s" "<URL>"
```

Auto-caption VTT repeats each line as it scrolls. De-duplicate before reading:

```python
import re
seen, out = set(), []
for l in open("vid.en.vtt", encoding="utf-8"):
    l = re.sub(r"<[^>]+>", "", l).strip()
    if not l or "-->" in l or l.startswith(("WEBVTT", "Kind:", "Language:")) or l in seen:
        continue
    seen.add(l); out.append(l)
print(" ".join(out))
```

Comments land in `c.info.json` under `comments` — each has `text`, `author`,
`like_count`, and `parent` (`"root"` for a top-level comment). Sort by `like_count`.

Metadata worth capturing for the file header: title, channel, upload date, view count,
comment count. It tells the reader how much scrutiny the idea has already had.

## 2. Read the comments as seriously as the video

This is the step people skip, and it is usually where the value is. A teaching video is
a sales pitch for an idea; the comment section is thousands of hours of other people
having already tried it.

- **Rank by likes and read the top 50-80**, then keyword-scan the rest for `vwap`,
  `rsi`, `adx`, `filter`, `timeframe`, `chop`, `whipsaw`, `backtest`, `win rate`.
- **Harvest three things**: concrete rule refinements, claimed win rates with the
  conditions attached, and the specific failure mode critics describe.
- **The critics are usually right and are usually specific.** On the 9 EMA video, the
  sceptics said the strategy dies in chop and that the demos were cherry-picked. Both
  were confirmed by the backtest; most of the popular *improvements* were not.
- **Attribute suggestions to the commenter by handle** in code comments and tooltips.
  It records where a rule came from, so when a filter is later removed for failing a
  test there is a trail explaining what it was and why it seemed good.

Treat every claimed win rate as a hypothesis to test, never as a fact to encode.

## 3. Convert vague rules into testable ones, and say what you assumed

Video rules are written for a human who will fill the gaps with judgment. "Identify
areas of support and resistance", "wait for a pullback", "I don't care where you put
your stop loss" cannot be executed by a machine.

Every gap you close is an assumption that could be the reason the result differs from
the video. So:

- **Enumerate the gaps explicitly** before writing code, and pick a concrete rule for
  each (e.g. S/R := previous-day high/low, opening range, and confirmed pivots).
- **Make each assumption an input**, not a constant, so it can be tested rather than
  argued about.
- **Write the assumption into the tooltip** alongside the quote from the video it
  implements.
- If the video demonstrates several variants, implement them as selectable modes and
  let the data rank them. Do not pre-judge from the presenter's enthusiasm.

## 4. Choose timeframe and universe from evidence, not from the video

Videos usually demo a market with different microstructure from the user's — FX majors
cost ~1-2 bps round trip, NSE intraday equity ~8-10 bps. A strategy can be genuinely
profitable in one and hopeless in the other with identical rules.

- **Timeframe**: test it. Report gross edge in bps per timeframe against the cost line,
  rather than accepting whatever the demo used.
- **Universe**: define it by a RULE, before looking at results. For NSE intraday that is
  the F&O list (`signal_engine/backtest/data.NSE_FNO`, read from the instance's own
  symbol master), not a hand-picked set of familiar names.

  This matters more than it sounds. The 9 EMA strategy measured +8.71 bps gross on 29
  hand-picked liquid large caps and read as "borderline". On the full 208-name F&O
  universe the gross edge was -0.12 bps — no edge at all — and the ranking of the
  video's three modes reversed. Nothing changed but the basket.

## 5. Backtest before shipping, and expect it to fail

Write the adapter for `signal_engine/backtest` (see the `pinescript-strategy` skill and
its `adapter-template.py`) and run the staged evaluation:

```bash
uv run --group analysis python -m signal_engine.backtest <name> --full
```

- Read the **out-of-sample** row. The in-sample row chose the settings.
- `|t| < 2` means indistinguishable from zero. Say so; do not narrate it as promising.
- Judge each comment-suggested filter with `ablation()`. Only **BOTH** counts.
- Diagnose with `by_reason()`. An exit mix that is mostly time-exits means the signal
  fires and then goes nowhere — a target tweak will not fix that.

**Most public strategies do not survive this, and that is a successful outcome.** A
clear negative in a day is worth far more than a live account discovering it slowly.

## 6. Report honestly, and write the verdict into the artefact

- State the verdict in the first paragraph, with the trade count and t-statistic.
- Put a `BACKTEST VERDICT` block at the top of the `.pine` file with the real numbers
  and the reproduce command, so the next reader cannot mistake a study for a system.
- If an earlier conclusion of yours is overturned by better data, **correct it plainly
  and say what changed**. Do not quietly ship the newer number.
- Separate what held from what did not: a mechanically-justified finding (wider stops
  cut cost-in-R because `cost_R = cost_pct x price / risk`) is worth keeping; an
  in-sample-only improvement is not.
- Name what is salvageable. An indicator can be a poor entry trigger and a good
  trade-management tool at the same time.

## 7. Then implement

Only now write the production script, following the **`pinescript-strategy`** skill: the
alert contract, strategy registration, Pine v6 pitfalls, and the pre-ship checklist.

---

## Checklist

- [ ] Real transcript pulled and read, not a summary
- [ ] Comments pulled, top ~80 read by likes, rest keyword-scanned
- [ ] Community suggestions listed with handles, treated as hypotheses
- [ ] Every underspecified rule enumerated, made concrete, and exposed as an input
- [ ] Timeframe chosen by measurement against the cost line
- [ ] Universe defined by a rule before results were seen
- [ ] Backtest adapter written; OOS reported; filters judged with `ablation()`
- [ ] Verdict with trade count and t-stat in the response AND in the `.pine` header
- [ ] Handed off to `pinescript-strategy` for the implementation details
