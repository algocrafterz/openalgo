"""Session volume factor and per-slot bar baseline, ported from klVolFactor (pine 1098-1143).

Quirk kept on purpose: the pine ring buffer holds `days` rows INCLUDING the current session,
so `days=14` averages 13 prior sessions, and the reading needs min(3, days-1) of them.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.features.volume import session_volume_factor

IST = "Asia/Kolkata"


def session(day: str, per_bar_volume: float, bars: int = 3) -> pd.DataFrame:
    idx = pd.date_range(f"{day} 09:15", periods=bars, freq="5min", tz=IST)
    c = pd.Series(100.0, index=idx)
    return pd.DataFrame(
        {"Open": c, "High": c, "Low": c, "Close": c, "Volume": float(per_bar_volume)}, index=idx
    )


def four_sessions() -> pd.DataFrame:
    return pd.concat(
        [
            session("2026-09-24", 100),
            session("2026-09-25", 200),
            session("2026-09-28", 300),
            session("2026-09-29", 400),
        ]
    )


class TestSessionVolumeFactor:
    def test_needs_three_prior_sessions(self):
        out = session_volume_factor(four_sessions())
        first_three = out.iloc[:9]
        assert first_three["vf"].isna().all()
        assert first_three["bar_baseline"].isna().all()

    def test_hand_computed_fourth_session(self):
        out = session_volume_factor(four_sessions()).iloc[9:]
        # slot 0: prior cum 100/200/300 -> mean 200; today 400 -> vf 2.0; bar baseline 200
        # slot 1: prior cum 200/400/600 -> mean 400; today 800 -> vf 2.0; baseline (1200-600)/3
        # slot 2: prior cum 300/600/900 -> mean 600; today 1200 -> vf 2.0; baseline 200
        assert list(out["vf"]) == pytest.approx([2.0, 2.0, 2.0])
        assert list(out["bar_baseline"]) == pytest.approx([200.0, 200.0, 200.0])

    def test_rvol_is_volume_over_baseline(self):
        df = four_sessions()
        out = session_volume_factor(df)
        rvol = df["Volume"] / out["bar_baseline"]
        assert rvol.iloc[9] == pytest.approx(2.0)

    def test_ring_buffer_holds_days_rows_including_today(self):
        # days=3 -> today + 2 prior sessions (300, 200 on 09-29): mean cum slot0 = 250
        out = session_volume_factor(four_sessions(), days=3)
        assert out["vf"].iloc[9] == pytest.approx(400 / 250)

    def test_min_history_is_capped_by_window(self):
        # days=2 -> one prior session allowed (min(3, 1) = 1)
        out = session_volume_factor(four_sessions(), days=2)
        assert out["vf"].iloc[3] == pytest.approx(200 / 100)  # second session vs first
        assert out["vf"].iloc[:3].isna().all()

    def test_zero_volume_prior_session_is_ignored(self):
        df = pd.concat([session("2026-09-23", 0), four_sessions()])
        out = session_volume_factor(df).iloc[12:]  # 09-29 has 3 real priors + a dead one
        assert list(out["vf"]) == pytest.approx([2.0, 2.0, 2.0])

    def test_output_aligned_to_input(self):
        df = four_sessions()
        assert session_volume_factor(df).index.equals(df.index)

    @pytest.mark.parametrize("cut", [1, 2, 4])
    def test_causal_truncation_does_not_change_history(self, cut):
        rng = np.random.default_rng(7)
        df = pd.concat(
            [
                session(f"2026-09-{d:02d}", float(rng.integers(50, 500)), bars=5)
                for d in range(15, 30)
            ]
        )
        full = session_volume_factor(df)
        part = session_volume_factor(df.iloc[: len(df) - cut])
        pd.testing.assert_frame_equal(part, full.iloc[: len(part)])
