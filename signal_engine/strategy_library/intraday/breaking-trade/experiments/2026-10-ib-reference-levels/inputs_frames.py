"""Build frames.pkl for sample_a.py (unrecorded IDEA-034 driver, copied verbatim).

PYTHONPATH=. uv run python <this dir>/inputs_frames.py <out dir>/frames.pkl
"""

import dataclasses
import pickle
import sys
from pathlib import Path

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.data_collect import DataCollector
from signal_engine.backtest.data_readiness import preflight as pf
from signal_engine.backtest.types import RunConfig
from signal_engine.research.intake import freeze as F

folder = Path("signal_engine/research/ideas/idea-034-multi-day-balance-breakout")
frozen = F.freeze(folder, idea_id="IDEA-034", register_version=False)
res = pf.prepare_data(folder, frozen, dataset_version="nse-intraday-5m-20261021-002")
print("dataset", res.version_id, res.report.state, len(res.eligible))
cfg = rr.frozen_settings(frozen)
print({k: v for k, v in cfg.items() if k not in ("parameters",)})
col = DataCollector()
with res.open() as src:
    frames = src.intraday(res.eligible, "5m", None, col)
    pf_ = src.session_price_factors(sorted(frames), None)
layer = rr._build_data_layer(
    frames,
    col,
    cfg,
    price_factors=pf_,
    expected_bars_for=rr.intraday_session_shape(),
    official=res.calendar,
    out_of_scope=[
        __import__("datetime").date.fromisoformat(d)
        for d in res.manifest.get("out_of_scope_sessions") or []
    ],
)
fr = layer.frames
print(len(fr), sum(len(v) for v in fr.values()))
pickle.dump({"frames": fr, "cfg": cfg}, open(sys.argv[1], "wb"), protocol=4)
