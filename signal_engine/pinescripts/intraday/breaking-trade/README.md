# breakingtrade.com — automation feasibility assessment

> **SUPERSEDED IN PART (2026-09-06).** This file is the original 2026-08-29 buy/don't-buy
> assessment and is kept as the record of that decision. Its conclusion — "read it, do not wire
> it" — was overtaken: the subscription was bought and the scanners ARE now automated, via a
> headless browser rather than the Telegram alerts this file (correctly) judged unparseable.
> The no-API / no-webhook finding still holds and is exactly why a browser is used.
>
> For what was actually built, measured and decided, read
> [`STRATEGY-LOG.md`](STRATEGY-LOG.md) — including the confidence ledger separating what is
> measured from what is merely believed.

**Question asked:** is this a good candidate to automate — buy a subscription, define custom
filters, route them to Telegram, and let `signal_engine` trade them?

**Answer, in two parts:**

- **As a stock-SELECTION aid, feeding a human: yes, worth trialling.** It advertises per-user
  Telegram alerts on custom Market Profile filters against your own watchlist, which is
  exactly the "what is in play right now" job. At Rs 250-450/month with a free trial, the
  downside is a few hundred rupees.
- **As an automated signal source feeding `signal_engine` straight to a broker: no.** There is
  no API and no webhook. The Telegram messages are undocumented free text with no schema
  commitment, so a parser built on them breaks whenever the vendor edits a string, silently.

Assessed 2026-08-29 from public pages only. Everything below is sourced; where a claim could
not be verified that is stated rather than guessed.

> **Correction (2026-08-29).** An earlier version of this file said BreakingTrade offered no
> per-user Telegram routing, only a shared broadcast channel. That was wrong. The
> [live-intraday-scanner](https://breakingtrade.com/live-intraday-scanner) page states
> "Get instant Telegram alerts when scanner conditions trigger on your watchlist stocks" and
> "Subscribe for Telegram notifications and sound alerts when your watchlist stocks trigger
> scanner conditions", and [trading-alerts](https://breakingtrade.com/trading-alerts) adds
> "Receive alerts directly in Telegram with full trade details - entry, stop loss, and target
> levels" and "Create your own alert conditions based on price, volume, or Market Profile
> levels". Per-user, watchlist-scoped, custom-condition Telegram alerts ARE the advertised
> product. What remains unverified is the MECHANICS - no page describes how a Telegram
> account is linked, whether it is a bot or a private channel, or what the message format is.
> The no-API / no-webhook finding is unchanged and was re-checked against both pages.

---

## 1. What it is

A browser-based Market Profile (TPO) charting and scanner subscription, plus an AI chat
assistant ("SarthoAI"). It self-describes as a market-analytics and education platform and
explicitly disclaims advice: "Content, scanners and AI output are for informational purposes
only and are not investment advice"
([Terms](https://breakingtrade.com/html/TermsAndConditions.html)).

- **Universe:** "All 200+ NSE F&O stocks, NIFTY and BANKNIFTY indices, plus MCX commodities"
  ([homepage](https://breakingtrade.com)). No BSE, no currency segment, no cash-only names.
- **Overlap with what we already run:** near total. The scanner's vocabulary is the same
  vocabulary `breakout.pine` already computes locally — IB breakouts, range extensions, failed
  auctions, VAH/VAL breaks, single prints, open drive, buying/selling tails, day types, open
  types, POC and value area. See the field reference in `signal_engine/PRD.md`; those are
  literally the fields our own entry alerts already carry.

## 2. Signal taxonomy

From [live-intraday-scanner](https://breakingtrade.com/live-intraday-scanner),
[live-volume-scanner](https://breakingtrade.com/live-volume-scanner) and
[ai-option-screener](https://breakingtrade.com/ai-option-screener):

- **Market Profile:** IB breakouts, range extensions, failed auctions, VAH/VAL and prior-day
  high/low breaks, single prints, open drive, buying/selling tails, 6 day types, 4 open types.
  Filter dimensions: open type, day type, single prints, TPO position.
- **Volume:** volume ratio vs average, session volume, volume deviation vs 7-day average,
  session profile, claimed institutional accumulation/distribution detection. No numeric
  thresholds are published for any of these.
- **Options (NIFTY/BANKNIFTY only):** 15 AI-scored strategies — iron condor, short strangle,
  jade lizard, broken wing butterfly, ZEBRA, calendar straddle and others — scored 0-100 from
  IV, PCR, Greeks and structure.

No OI-shift scanner, no 52-week-high scanner, no classic RSI/MACD/ADX screener was found.

## 3. The automation question — the part that decides it

**There is no public API and no documented webhook.** No `/api`, `/docs` or `/developers`
page exists in the [sitemap](https://breakingtrade.com/sitemap.xml). No API key, no webhook
URL field, and no mechanism to route a user-defined filter to the user's own Telegram bot or
`chat_id` is documented anywhere across the pricing, features,
[trading-alerts](https://breakingtrade.com/trading-alerts) or
[automated-trading](https://breakingtrade.com/automated-trading) pages.

What does exist:

| Channel | Status |
|---|---|
| Browser / desktop push | Documented |
| Sound alerts | Documented |
| Email daily digest | Documented — "Daily email digest of all triggered alerts with performance tracking and analysis" |
| **Telegram, per user, on your own watchlist** | **Advertised** — "Get instant Telegram alerts when scanner conditions trigger on your watchlist stocks". Linking mechanics undocumented |
| **Custom alert conditions** | **Advertised** — "Create your own alert conditions based on price, volume, or Market Profile levels" |
| Public marketing channel | [@BreakingTrade_rajat](https://t.me/BreakingTrade_rajat), 531 subscribers — separate from the above |
| Webhook | **None found** on any page |
| REST API | **None found** — no `/api`, `/docs` or `/developers` in the sitemap |

The `automated-trading` page's HTML contains *commented-out* sections referencing Zerodha,
Angel One, Upstox, Fyers and 5paisa integrations plus backtesting — features marketed in the
FAQ but not shipped in the served page. Direct broker execution appears aspirational.

**What automating it would actually mean.** The per-user Telegram alert is real, so
`signal_engine`'s existing listener could in principle read it — the transport is not the
problem. The problem is the contract. Our own TradingView alerts have a schema we author, and
`tests/test_orb_alerts.py` fails the build if a field moves; that test exists because a
one-line format change silently dropped 47% of ORB exits for four months. A third-party
message format we neither control nor can test against has exactly that failure mode with
none of the protection, and the vendor publishes no format documentation and makes no
stability commitment.

There is also no exit side. The alerts carry entry, stop and target, but nothing tells you the
position closed — no TP-hit, no SL-hit, no time exit. `signal_engine` would be opening
positions it can never reconcile, which is the same class of bug the ORB channel audit found.

**So: read it, do not wire it.** Use it the way you would use a good scanner — it tells you
which names are in play, you decide. If it later proves reliable, the honest upgrade path is
to take the NAMES it surfaces and let your own PineScript generate the tradeable signal on
them, so the alert contract stays yours.

## 4. Pricing

| Plan | Price | Effective/month |
|---|---|---|
| 1 month | ₹450 | ₹450 |
| 3 months | ₹1,050 | ₹350 |
| 6 months | ₹1,500 | ₹250 |
| 1 year + Market Profile course | ₹5,000 | ~₹416 |
| SarthoAI credits | ₹500 / 500 queries | ₹1 per query |

Free trial: 3 days, no card. Payment via PayU, UPI (`breakingtrade.com@icici`) or QR, with
**WhatsApp verification required for UPI/QR** — an informal payment path for a subscription
product, and worth noting before handing over money.

## 5. Diligence flags

- **No SEBI registration disclosed** anywhere — no RA/RIA number, no legal entity name beyond
  "BreakingTrade", no registered address, in either the Terms or the
  [Privacy Policy](https://breakingtrade.com/html/PrivacyPolicy.html). It positions itself as
  informational rather than advisory, which is consistent with not needing an RA licence *if*
  it never personalises a recommendation — but an "AI-scored" option strategy carrying entry,
  stop and target sits close to that line.
- **No independent reputation exists.** No Reddit threads, no Trustpilot, no app store
  presence. Visibility is limited to its own X, Facebook, YouTube and a 531-subscriber
  Telegram channel — all self-published. No complaints either; the absence of any paper trail
  in both directions is the finding. This is a small single-operator product.
- **No named data vendor.** "Live tick-by-tick data" is claimed; no feed provider is disclosed
  and no latency figure is independently verifiable.
- **No published performance.** No win rate, no backtest, no track record. Testimonials on the
  pricing page are marketing copy.

## 6. Competitors that DO support the automation we need

| Platform | Custom filter to webhook/Telegram | INR/month for that tier | Notes |
|---|---|---|---|
| **Chartink** | **Yes** — webhook alerts POST JSON (`stocks`, `trigger_prices`, `scan_name`) | ~₹780 (Premium, real-time) | The best fit. OpenAlgo already lists Chartink as a supported external platform, so the receiving half exists |
| **TradingView** | **Yes** — native webhook per alert, arbitrary Pine conditions | ~₹995 (Essential, annual) | What we already use. Most flexible; alert count is the tier limit |
| Streak (Zerodha) | Places orders itself; no webhook out | from ~₹350 | Broker-locked, and it *is* the executor — no use as a feed |
| Trendlyne | App/email alerts; no confirmed webhook | ₹310-₹491 | Strong screener breadth, weak automation hooks |
| StockEdge / Opstra / Sensibull | Screen-only | various | Opstra states it is not designed for automated deployment |

## 7. Recommendation

**Buy it for selection, not for execution.** For "which F&O names are in play right now, on
Market Profile and volume evidence", this is a reasonable Rs 250-450/month tool and the trial
is free. Budget zero engineering time against it until the trial answers the format questions
below.

**Trial checklist — settle these in three days:**

1. Link Telegram. Does it deliver to YOUR account, and how is that configured?
2. Capture 20 real alert messages. Are symbol, level and direction in fixed, parseable
   positions? Does the format vary by scanner?
3. Is there any exit or invalidation event, or entry only?
4. Count alerts per session on a 30-name watchlist. Under ~10 is a selection aid; over ~30 is
   a firehose.
5. Cross-check five alerts against your own chart. Did the Market Profile levels it quotes
   match what `breakout.pine` computes locally? If they disagree, one of you has bad data.

If the goal is genuinely more automated signal, the money is better spent on a Chartink
Premium subscription (~₹780/month), whose webhook JSON is a documented, stable integration
target that plugs into the same pattern `signal_engine` is already built around. Note the
honest caveat though: on the evidence in `PRD.md`, our constraint has not been a shortage of
signals — every intraday breakout variant tested so far has come out at or below break-even
after costs. Buying a third-party scanner adds signal volume, not edge.

---

### What could not be verified

- **The Telegram mechanics.** That per-user, watchlist-scoped alerts exist is stated on the
  site. HOW you connect (bot token, `chat_id`, a private channel, an in-app link step) is
  documented nowhere. This is the single thing the 3-day trial should settle first.
- **The message format.** No sample alert is published. Whether it carries a symbol in a
  parseable position, which scanner fired, and any timestamp is unknown.
- Whether the alert has any exit or follow-up event at all, or only fires on entry.
- The cadence — how many alerts a typical watchlist generates per session. A scanner that
  fires 40 times before 10:30 is not a selection aid.
- Legal entity, registered office and regulatory status.
- Real-world signal latency and accuracy.
