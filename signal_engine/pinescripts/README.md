# PineScripts

TradingView sources for the signal engine. Scripts here emit the Telegram alerts
that `signal_engine/` parses, sizes and executes — see `../PRD.md` for the
pipeline and `../HOW-IT-WORKS.md` for the live ORB path end to end.

## Layout

```
pinescripts/
├── intraday/<strategy>/
└── swing/<strategy>/
```

Each strategy folder holds its `.pine` sources plus a `STRATEGY-ANALYSIS.md`
describing the edge and its measured behaviour. Deeper material (performance
reports, tooling, exports) lives in a subfolder — see ORB below.

## Conventions

- One folder per strategy, named after the strategy.
- `STRATEGY-ANALYSIS.md` — the analysis doc. Exactly this name, every folder.
  It used to appear as three different casings, which made it ungreppable.
- `strategy("<name>", ...)` — the Pine title is the alert identity the engine
  matches on (`intraday-orb`, `intraday-breakout`, `swing-dividend-growth`),
  so it must stay in sync with `config.yaml` `strategy_profiles`.
- Run artifacts (`result.json`, `charts/`) are gitignored — they are
  Strategy-Tester exports and screenshots, regenerated per run, not source.

## Scripts

### intraday/orb — Opening Range Breakout
| File | Pine title | Role |
| --- | --- | --- |
| `orb.pine` | `intraday-orb` | **Frozen and live.** The production ORB strategy. |
| `breakout.pine` | `intraday-breakout` | **In development.** Key-level engine; changelog in `breakout.md`. |
| `orb-luxy-big-beautiful-dynamic-orb.pine` | `Luxy Big Beautiful ORB` | Third-party reference indicator, not wired to the engine. |

`trade-analysis/` holds the performance reports, the Telegram export and
`analyze_orb.py`, which globs `orb-telegram-export-*.json` next to itself.

### intraday/orderflow
| File | Pine title | Role |
| --- | --- | --- |
| `initiative_drive_detector_v6.pine` | `Initiative Drive Detector v6` | Flags expansion candles as *candidates*. Does not confirm order flow — every flag is a prompt to check the footprint. |
| `keylevel-candles.pine` | `Key-Level Candles (breakout companion)` | Companion to `breakout.pine`. Marks whether the candle **at a key level** confirmed the break (BRK/ENG) or rejected it (REJ/FAIL). Level set and ATR band mirror `breakout.pine`. |
| `candlestick-patterns.pine` | third-party (repo32, MPL-2.0) | Untracked source material that `keylevel-candles.pine` was reduced from. Not wired to anything. |

### intraday/volume-profile
| File | Pine title | Role |
| --- | --- | --- |
| `volume-profile-decision-assist.pine` | `Volume Profile Decision Assist` | Key-level engine feeding the breakout work. |
| `volume-heatmap.pine` | `Volcano Heatmap Volume v2` | Volume heatmap. |
| `volume-suite.pine` | `Volume Suite - By Leviathan` | Third-party volume toolkit. |
| `smart-money-concepts-luxalgo.pine` | `anand-Smart Money Concepts [LuxAlgo]` | Third-party SMC indicator. |

### intraday/ib-extension — Initial Balance extension
| File | Pine title | Role |
| --- | --- | --- |
| `ib-extension.pine` | strategy | IB extension strategy. |

### swing/dividend-growth
| File | Pine title | Role |
| --- | --- | --- |
| `dividend-growth.pine` | `swing-dividend-growth` | Swing strategy on dividend growers. |
