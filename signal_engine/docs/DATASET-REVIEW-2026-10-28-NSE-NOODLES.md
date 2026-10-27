# Dataset review against NSE-noodles (2026-10-28)

Scope: compare our published datasets with https://github.com/abhij1306/NSE-noodles (commit of
2026-10-25) and decide whether ours is fit for backtesting. Read-only review; no dataset changed.

**Decision (owner, 2026-10-28): keep the datasets as they are.** No rebuild. Revisit only on the
triggers below.

## What NSE-noodles is

A pipeline, not a dataset: it ships no data. It builds a daily-only NSE equity series from the
official bhavcopy (from 22 Jun 2011), keyed by ISIN, with every listed equity kept
(survivorship-free), corporate-action factors gated against the ex-date price gap, a
total-return series, and point-in-time factor versions. No intraday data.

Its design matches ours on the essentials: NSE bhavcopy as the raw source, raw prices never
changed, factors stored apart, unreadable actions sent to review, the same rights (TERP) formula.

## Comparison

| | NSE-noodles | Ours |
|---|---|---|
| Daily source and depth | NSE bhavcopy, from 2011 | NSE bhavcopy, from 2016 (`nse-daily-20261019-001`) |
| Intraday | none | 1m and 5m from 2017, 213 symbols (`nse-intraday-*-20261021-002`) |
| Universe | every equity ever listed | today's 213 F&O symbols (survivorship bias) |
| Symbol renames | joined by ISIN on evidence | not joined |
| Split, bonus, rights | parsed, then gated on the ex-date gap | parsed from NSE text |
| Demerger | gap net of NIFTY 500 overnight move, cross-checked | raw ex-date gap (10 events) |
| Dividends | total-return series | not adjusted (price return) |
| Point-in-time factors | versioned with `available_at` | hindsight adjustment |

## NSE-noodles acceptance tests applied to our daily build

Run on `db/staging/nse-D-adjusted-20261019.duckdb` (the build published as
`nse-daily-20261019-001`, 479,703 bars, 153 corporate-action events):

- OHLC validity: 0 bars where high/low fail to bracket open/close; 0 zero-volume bars.
- Price gate (ex-date open / previous close against the combined factor of that session):
  145 accepted, 8 ambiguous, 0 off-factor. Six ambiguous are small rights issues (NSE-noodles
  accepts these too); BEL 2017-09-28 bonus (factor 0.909, gap 0.939) and ASTRAL 2019-09-16 bonus
  (factor 0.80, gap 0.887) would go to its review list. Both are plausible with the morning move.
- Jump report (adjusted close-to-close moves above 25%): 48 remain of 169 raw. Each matches a
  real market event (YESBANK 2020, March 2020, PSU bank recapitalisation 2017-10-25, Adani 2023,
  INDUSINDBK 2025-03-11, election day 2024-06-04, ...). None is an unadjusted split or bonus.
  POLICYBZR 2026-09-24 (-36%, closed at the low on 10x volume, no corporate action filed) is
  taken as genuine but was not checked against news.
- Coverage: only CGPOWER (36 sessions, BZ series) and FORCEMOT (76 sessions, no trading) have
  interior gaps; both are the owner-accepted exclusions.

Intraday 5m quality flags (445,263 symbol-sessions): 98.9% price-valid, 97.9% volume-valid,
95.7% final bar valid, 97.3% reconciled to the NSE daily bar.

## Assessment

Fit for backtesting. Price data quality is at NSE-noodles' level. The known gaps, by impact:

1. Survivorship: the universe is today's F&O list. This inflates long-biased swing and
   cross-sectional results; it matters little for intraday strategies on liquid names. Already
   handled as a verdict qualification ("within the tested universe"), not a block.
2. Dividends not adjusted: understates holding returns by roughly the dividend yield (about
   1 to 1.5% a year for large caps). Irrelevant intraday.
3. Angel 1m vendor defects (2018-19 volume, final-bar convention): flagged per session and
   enforced by each strategy's declared `field_families`.
4. Minor: demerger factor includes the ex-date market move; no point-in-time factor versions
   (rules on absolute rupee prices must read raw prices); history starts 2016.

## Revisit triggers

Act only when one of these happens, and scope the work to that strategy, not to the dataset:

1. A swing or cross-sectional strategy reaches PROMISING or better: estimate the survivorship
   effect for it (NSE-noodles' full-universe daily output can serve as the comparison source).
2. A multi-week strategy's edge is within about 1 to 1.5% a year: account for dividends in
   that test.
3. A result looks wrong on a specific symbol or date: investigate through the quality flags
   and fix as a new published dataset version.

Not planned: ISIN-based rename joins, point-in-time factor versions, re-reviewing the BEL and
ASTRAL bonus factors.

## Layman summary

Our price data is clean: every split, bonus and rights issue lines up with what the market did,
and there are no broken bars or unexplained crashes. The other project has no intraday data at
all, which is what most of our strategies use. The weak spot is that our list of stocks is
today's F&O list, so stocks that failed or shrank since 2016 are missing, and dividends are
ignored. That only flatters strategies that hold stocks for days or weeks. So nothing is rebuilt
now; if such a strategy starts to look good, we check how much those two gaps flatter it.
