# BreakingTrade Intraday Scanner — Session Summary

**Date:** 3 September 2026
**Source:** [breakingtrade.com](https://breakingtrade.com/Home.jsp) — Live Intraday Market Profile Scanner

## What was asked

1. Identify trending stocks based on the data visible in the BreakingTrade browser tab.
2. Set up a periodic export of the Intraday and Volume scanner tables (all rows) to an Excel file in `signal_engine/pinescripts/intraday/breaking-trade/excel` for further analysis.

## What was done

### 1. Explored the site and read its own documentation first

Rather than inventing a definition of "trending," I opened the site's own **How to Use** guides before doing any filtering:

- [Intraday Scanner Guide](https://breakingtrade.com/scanner-guide)
- [Volume Scanner Guide](https://breakingtrade.com/volume-scanner-guide)

Key rule taken from the Intraday guide: *"Day Type ... Trend ↑/↓ = go with it and hold, never fade"*, and it separately warns that *"Fading a Trend day [is] the single most expensive mistake in Market Profile."* This became the basis for the trend definition, rather than simply ranking by Change %.

### 2. Captured a full snapshot of the Intraday scanner

Set the table to show **all 220 scanned rows** (NSE F&O stocks + a few index/futures rows) and extracted the complete table, including every signal column: Sector, Opening, IB %, OpenDrive, Tail, SinglePrint, Poor H/L, Day Type, TPO Pos, TPO Pos (Prev), Price, Change %.

Snapshot captured at **2026-09-03, 12:31 IST**, with Market Bias reading Bearish (net -6).

### 3. Parsed and classified trending stocks

Wrote a parser to turn the raw table text into structured records (212 individual equities, after excluding index/futures duplicate rows such as NIFTY, BANKNIFTY, NIFTY FUT).

Applied a two-tier trend classification derived directly from the site's guide:

- **Tier 1 — confirmed trend:** Day Type = `Trend ↑` or `Trend ↓`
- **Tier 2 — resolving trend:** Day Type = `Neutral Ext ↑/↓` with TPO Pos still aligned in that direction (the guide's own "Neutral Day Resolution" scan)

Each candidate was then scored for conviction using the guide's "three questions" framework (who took control at the open → did they follow through → are they still in control now), adding points for:
- OpenDrive follow-through (Open Drive ↑/↓, or Rejection/Test Drive in the trend direction)
- TPO Pos (Prev) showing real acceptance above/below yesterday's high or low (Above PDH / Below PDL)
- A confirming Tail (Buy Tail / Sell Tail)
- A narrow IB % (<30%, the guide's "coiled spring" trend-day candidate)

**Result:** 18 stocks qualified as genuinely trending — 14 bearish, 4 bullish. Strongest bearish: ZYDUSLIFE, BHARATFORG, HINDZINC, INDUSTOWER, KOTAKBANK (all confirmed Trend ↓). Strongest bullish: IDFCFIRSTB (confirmed Trend ↑), BLUESTARCO, DELHIVERY (Neutral Ext ↑ resolving above value, above PDH).

### 4. Built and delivered an Excel workbook

Using the `xlsx` skill, produced `BreakingTrade_Intraday_Scan_20260903_1231IST.xlsx` with three tabs:

| Tab | Contents |
|---|---|
| **Trending Stocks** | The 18 classified candidates, colour-coded bullish/bearish, with tier, conviction score, and a plain-language reasoning column citing the specific guide rule applied |
| **Intraday Scanner (Full)** | The complete raw 212-row snapshot, all signal columns, unfiltered |
| **Methodology** | Written explanation of the Tier 1/Tier 2 rules, the conviction scoring, and caveats (see below) |

This was sent to you as a chat download.

### 5. Periodic export to a local folder — blocked, pending your action

You asked for recurring exports to land in `signal_engine/pinescripts/intraday/breaking-trade/excel`. This session is **not currently linked to your computer**, so there is no way to write directly into a local folder path — only to deliver files into this chat.

I asked how you'd like to handle this, and you chose to **link your computer first**. That step is still outstanding: open this task in the Claude desktop app and choose "Link to this computer." Once linked, I can set up a recurring scheduled task that:
- refreshes the Intraday scanner (and, if wanted, the Volume scanner)
- re-runs this same extraction and classification
- writes the resulting `.xlsx` straight into that folder on a recurring cadence (e.g. every 15–30 minutes during market hours)

## Known gaps / caveats

- **Volume Scanner not yet incorporated.** The Intraday guide explicitly recommends cross-checking any Market Profile signal against the Volume Scanner's Session Volume Factor before trading it, to rule out a "Ghost Rally" (price moving on thin participation). The current export only covers the Intraday scanner.
- **Not investment advice.** Per BreakingTrade's own disclaimer, this scanner "identifies market structure, it does not predict outcomes or give investment advice," and every setup can fail. I'm not a licensed financial advisor, and this summary/document should be read as a description of what the scanner methodology flags, not a trade recommendation.
- **Single snapshot only.** Everything above reflects one point-in-time read (12:31 IST); recurring automation (pending the device link) is what would keep this current through the trading day.
