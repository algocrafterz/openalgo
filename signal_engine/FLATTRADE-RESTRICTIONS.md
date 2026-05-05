# Flattrade MIS Reject List

Symbols Flattrade will reject for MIS (intraday) orders even when sufficient capital is available. The signal engine reads the configured list from `config.yaml → broker_restrictions.flattrade.mis_rejected` and **skips the order before sending it to the broker**, freeing the position slot for the next valid signal.

> Why this exists: a rejected order still consumes a slot attempt, generates a Telegram notice, and adds noise to the day summary. Skipping known-bad symbols keeps the daily slots productive.

## Categories of rejection

Flattrade rejects MIS orders in roughly these buckets. Add the symbol to the config when you confirm the rejection is **persistent** (multiple sessions), not a one-off margin issue.

### 1. T2T (Trade-to-Trade / BE series)
- NSE flag: symbol ends in `-BE`
- MIS not allowed at any Indian broker
- **Already handled separately** by `_is_be_series()` in `main.py` via the OpenAlgo `symtoken` table — do NOT duplicate these here.

### 2. GSM (Graded Surveillance Measure) — Stage 1+
- NSE / BSE place stocks in GSM stages 1–4 based on price/volume anomalies, low fundamentals, or speculative buildup.
- Stage 1 already removes intraday leverage at most brokers, including Flattrade.
- Source list: https://www.nseindia.com/regulations/exchange-communique-circulars (search "GSM Framework — Securities under Stage")
- Updated weekly. Treat the list as moving — re-check periodically.

### 3. ASM (Additional Surveillance Measure) — Stage IV
- Long-term ASM: stock-specific risk flags (price band, narrow universe, etc.).
- Short-term ASM: trader-driven volatility flags.
- **Stage IV** has 100% margin requirement → no MIS leverage → Flattrade rejects MIS.
- Stage I–III: usually permitted, but confirm against your broker's circular.
- Source: https://www.nseindia.com/regulations/exchange-communique-circulars (search "Securities under Long Term ASM Framework Stage IV").

### 4. F&O Ban (Market-Wide Position Limit breached)
- When NSE puts an F&O scrip in ban for the day, intraday cash MIS gets restricted at most brokers.
- Source: https://www.nseindia.com/products-services/equity-derivatives-fno-banlist
- Daily list — too volatile to hard-code. Best handled by reading the daily ban list at engine start (future enhancement).

### 5. Stock-specific (broker overrides)
- Flattrade may withdraw MIS leverage on individual stocks for liquidity, slippage, or risk reasons even when NSE permits.
- Discovered empirically: log shows `Order Rejected — RMS:Insufficient funds` or `Margin shortfall` despite the local `mis_margin_pct` heuristic passing.
- Verification: try the same trade in CNC — if CNC works but MIS doesn't, it's broker-specific.

## Maintenance procedure

When you see `Order Rejected` for a symbol in the engine log:

1. Check the broker error message.
2. If it mentions GSM / ASM / surveillance / "MIS not allowed for this scrip" → add to the list.
3. If it mentions "Insufficient funds" only on a small position size → it's a margin issue, NOT a reject-list issue. Tune `mis_margin_pct` instead.
4. Append the uppercase symbol to `config.yaml`:

```yaml
broker_restrictions:
  flattrade:
    mis_rejected:
      - SYMBOL_X     # GSM Stage 2 since 2026-04-15
      - SYMBOL_Y     # ASM IV — confirmed broker rejection
```

5. Add a one-line comment with the reason and confirmation date so future review can decide when to remove it.
6. Restart the signal engine — the list is loaded once at startup.

## Removal procedure

Surveillance flags are temporary. Re-check NSE/BSE circulars monthly:

- If a symbol is no longer in GSM/ASM, remove it from the list to allow trades again.
- If unsure, leave it — the cost of being too cautious is missed trades, the cost of being too permissive is a rejected order plus noise.

## Current list

Maintained in `config.yaml`. As of **2026-05-04** the list is empty — populate it from your accumulated rejection log or paste the doc you mentioned and we'll seed it.

Today's three rejections (BELRISE, CUB, CESC, 2026-05-04) appeared to be **margin-shortage rejections** — the order value exceeded live capital after earlier positions were taken — not stock-specific reject-list rejections. They should NOT be added here unless they reject again on a clean morning with full capital available.

## Related

- `signal_engine/HOW-IT-WORKS.md` — overall pipeline
- `signal_engine/main.py:_handle_entry` — where the filter fires (step 2a)
- `signal_engine/config.py:_parse_broker_mis_rejected` — config parser
- `signal_engine/PRD.md` — T2T (BE series) handling for the related case
