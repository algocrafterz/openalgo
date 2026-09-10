"""Per-strategy EOD summaries and the consolidated cross-strategy comparison (2026-09-11).

Each strategy sizes off its OWN cached day-start capital (RiskEngine's per-strategy
isolation), so a single blended win-rate/P&L number across strategies answers a question no
trader actually asks. These tests lock in: per-strategy grouping and stats, the comparison
table's ranking (best avg R first - the fair cross-strategy metric), and that each strategy's
own summary reaches its own channel independently of the others.
"""

from types import SimpleNamespace

import pytest

from signal_engine import notifier


def _rec(strategy, symbol="SYM", total_pnl=0.0, r_multiple=None, exit_types=None,
         direction="LONG", entry_price=100.0, exit_price=101.0):
    return SimpleNamespace(
        strategy=strategy, symbol=symbol, total_pnl=total_pnl, r_multiple=r_multiple,
        exit_types=exit_types or [], direction=direction,
        entry_price=entry_price, exit_price=exit_price,
    )


class TestGroupByStrategy:
    def test_groups_records_by_strategy(self):
        records = [_rec("ORB"), _rec("ORB"), _rec("BREAKOUT")]
        groups = notifier._group_by_strategy(records)
        assert len(groups["ORB"]) == 2
        assert len(groups["BREAKOUT"]) == 1

    def test_missing_strategy_falls_back_to_unknown(self):
        groups = notifier._group_by_strategy([_rec(None)])
        assert "UNKNOWN" in groups

    def test_empty_input_yields_empty_groups(self):
        assert notifier._group_by_strategy([]) == {}


class TestAggregateStrategyStats:
    def test_counts_wins_and_losses_by_pnl_sign(self):
        records = [_rec("ORB", total_pnl=100.0), _rec("ORB", total_pnl=-50.0)]
        stats = notifier._aggregate_strategy_stats(records)
        assert stats == {"trades": 2, "wins": 1, "losses": 1, "time_exits": 0}

    def test_time_exit_counted_as_a_trade_but_excluded_from_win_loss(self):
        """Matches tracker.py's own classification: a TIME exit is force-closed by the clock,
        not a decision the strategy's exit rule made."""
        records = [_rec("ORB", total_pnl=50.0, exit_types=["TIME"])]
        stats = notifier._aggregate_strategy_stats(records)
        assert stats == {"trades": 1, "wins": 0, "losses": 0, "time_exits": 1}

    def test_zero_pnl_counts_as_a_win(self):
        stats = notifier._aggregate_strategy_stats([_rec("ORB", total_pnl=0.0)])
        assert stats["wins"] == 1
        assert stats["losses"] == 0


class TestBestWorstLine:
    def test_reports_best_and_worst_by_r_multiple(self):
        records = [
            _rec("ORB", symbol="A", r_multiple=1.5),
            _rec("ORB", symbol="B", r_multiple=-0.8),
            _rec("ORB", symbol="C", r_multiple=0.2),
        ]
        line = notifier._best_worst_line(records)
        assert "Best: A (+1.5R)" in line
        assert "Worst: B (-0.8R)" in line

    def test_single_scored_trade_says_only_scored_trade(self):
        line = notifier._best_worst_line([_rec("ORB", symbol="A", r_multiple=1.0)])
        assert "Only scored trade: A" in line

    def test_no_scored_trades_returns_empty_string(self):
        assert notifier._best_worst_line([_rec("ORB", r_multiple=None)]) == ""


class TestComparisonRows:
    def test_ranked_best_to_worst_by_avg_r_not_by_pnl(self):
        """The whole point: a strategy with a smaller net ₹ but a better avg R ranks higher -
        avg R is capital/risk-% agnostic, net ₹ is not."""
        by_strategy = {
            "BREAKOUT": [_rec("BREAKOUT", total_pnl=1000.0, r_multiple=0.2)],
            "ORB": [_rec("ORB", total_pnl=100.0, r_multiple=1.5)],
        }
        rows = notifier._comparison_rows(by_strategy)
        assert rows[0].startswith("ORB")
        assert rows[1].startswith("BREAKOUT")

    def test_strategy_with_no_r_multiple_sorts_last(self):
        by_strategy = {
            "NOSCORE": [_rec("NOSCORE", r_multiple=None)],
            "ORB": [_rec("ORB", r_multiple=0.1)],
        }
        rows = notifier._comparison_rows(by_strategy)
        assert rows[0].startswith("ORB")
        assert rows[1].startswith("NOSCORE")


class TestChannelForStrategy:
    def _settings(self, channels):
        return SimpleNamespace(telegram_channels=tuple(channels), notify_channel={})

    def test_known_strategy_resolves_its_own_channel(self, monkeypatch):
        from signal_engine.config import TelegramChannel
        monkeypatch.setattr(notifier, "settings", self._settings(
            [TelegramChannel(name="intraday-orb-analyze", id=-100)]
        ))
        ch = notifier._channel_for_strategy("ORB", "analyze")
        assert ch.id == -100

    def test_unknown_strategy_returns_none(self, monkeypatch):
        monkeypatch.setattr(notifier, "settings", self._settings([]))
        assert notifier._channel_for_strategy("SOME-NEW-STRATEGY", "analyze") is None

    def test_known_strategy_but_unconfigured_phase_returns_none(self, monkeypatch):
        from signal_engine.config import TelegramChannel
        monkeypatch.setattr(notifier, "settings", self._settings(
            [TelegramChannel(name="intraday-orb-analyze", id=-100)]
        ))
        assert notifier._channel_for_strategy("ORB", "live") is None

    def test_lookup_is_case_insensitive(self, monkeypatch):
        from signal_engine.config import TelegramChannel
        monkeypatch.setattr(notifier, "settings", self._settings(
            [TelegramChannel(name="intraday-orb-analyze", id=-100)]
        ))
        assert notifier._channel_for_strategy("orb", "analyze").id == -100


class _FakeClient:
    def __init__(self, fail_for=()):
        self.sent = []
        self._fail_for = set(fail_for)

    async def send_message(self, chat_id, text):
        if chat_id in self._fail_for:
            raise RuntimeError("boom")
        self.sent.append((chat_id, text))


class TestSendPerStrategyDaySummaries:
    def _settings(self, channels):
        return SimpleNamespace(
            telegram_channels=tuple(channels), notify_channel={}, notify_level="quiet",
        )

    @pytest.mark.asyncio
    async def test_sends_one_message_per_strategy_to_its_own_channel(self, monkeypatch):
        from signal_engine.config import TelegramChannel
        monkeypatch.setattr(notifier, "settings", self._settings([
            TelegramChannel(name="intraday-orb-analyze", id=-100),
            TelegramChannel(name="intraday-breakout-analyze", id=-200),
        ]))
        client = _FakeClient()
        monkeypatch.setattr(notifier, "_client", client)

        by_strategy = notifier._group_by_strategy([
            _rec("ORB", symbol="SBIN", total_pnl=100.0, r_multiple=1.0),
            _rec("BREAKOUT", symbol="RELIANCE", total_pnl=-50.0, r_multiple=-0.5),
        ])
        await notifier._send_per_strategy_day_summaries(by_strategy, "11-Sep-2026", {}, "analyze")

        sent_channels = {chat_id for chat_id, _ in client.sent}
        assert sent_channels == {-100, -200}
        orb_msg = next(text for chat_id, text in client.sent if chat_id == -100)
        assert "ORB DAY SUMMARY" in orb_msg
        assert "SBIN" in orb_msg

    @pytest.mark.asyncio
    async def test_strategy_with_no_configured_channel_is_skipped(self, monkeypatch):
        monkeypatch.setattr(notifier, "settings", self._settings([]))
        client = _FakeClient()
        monkeypatch.setattr(notifier, "_client", client)

        by_strategy = notifier._group_by_strategy([_rec("ORB")])
        await notifier._send_per_strategy_day_summaries(by_strategy, "11-Sep-2026", {}, "analyze")

        assert client.sent == []

    @pytest.mark.asyncio
    async def test_one_strategys_send_failure_does_not_block_another(self, monkeypatch):
        from signal_engine.config import TelegramChannel
        monkeypatch.setattr(notifier, "settings", self._settings([
            TelegramChannel(name="intraday-orb-analyze", id=-100),
            TelegramChannel(name="intraday-breakout-analyze", id=-200),
        ]))
        client = _FakeClient(fail_for={-100})
        monkeypatch.setattr(notifier, "_client", client)

        by_strategy = notifier._group_by_strategy([_rec("ORB"), _rec("BREAKOUT")])
        await notifier._send_per_strategy_day_summaries(by_strategy, "11-Sep-2026", {}, "analyze")

        assert client.sent == [(-200, client.sent[0][1])]

    @pytest.mark.asyncio
    async def test_client_not_ready_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(notifier, "_client", None)
        by_strategy = notifier._group_by_strategy([_rec("ORB")])
        # Must not raise even though no channel/client setup was done for this test.
        await notifier._send_per_strategy_day_summaries(by_strategy, "11-Sep-2026", {}, "analyze")


class TestNotifyDaySummaryEndToEnd:
    """notify_day_summary() end to end: the consolidated message's comparison table only
    appears when there is something to compare, and per-strategy sends fire alongside it."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        from signal_engine.config import TelegramChannel

        monkeypatch.setattr(notifier, "settings", SimpleNamespace(
            telegram_channels=(
                TelegramChannel(name="intraday-orb-analyze", id=-100),
                TelegramChannel(name="intraday-breakout-analyze", id=-200),
            ),
            notify_channel={"analyze": TelegramChannel(name="signal-engine-analyze", id=-900)},
            notify_level="quiet",
        ))
        monkeypatch.setattr(notifier, "_current_phase", self._fake_phase)
        self.client = _FakeClient()
        monkeypatch.setattr(notifier, "_client", self.client)
        monkeypatch.setattr(
            notifier, "_DAY_SUMMARY_PIN_STATE_PATH", "/tmp/test_day_summary_pin_state_never_written.json"
        )
        monkeypatch.setattr(notifier, "_load_day_summary_pin_state", lambda: {})
        monkeypatch.setattr(notifier, "_save_day_summary_pin_state", lambda state: None)

    @staticmethod
    async def _fake_phase():
        return "analyze"

    @pytest.mark.asyncio
    async def test_single_strategy_gets_no_comparison_table(self):
        records = [_rec("ORB", symbol="SBIN", total_pnl=100.0, r_multiple=1.0)]
        delivered = await notifier.notify_day_summary(
            trades=1, wins=1, losses=0, net_pnl=100.0, capital=100000.0, trade_records=records,
        )
        assert delivered is True
        admin_msg = next(text for chat_id, text in self.client.sent if chat_id == -900)
        assert "By strategy" not in admin_msg
        # Still gets its own per-strategy send.
        assert any(chat_id == -100 for chat_id, _ in self.client.sent)

    @pytest.mark.asyncio
    async def test_multiple_strategies_get_a_comparison_table_and_their_own_sends(self):
        records = [
            _rec("ORB", symbol="SBIN", total_pnl=100.0, r_multiple=1.0),
            _rec("BREAKOUT", symbol="RELIANCE", total_pnl=-50.0, r_multiple=-0.5),
        ]
        delivered = await notifier.notify_day_summary(
            trades=2, wins=1, losses=1, net_pnl=50.0, capital=200000.0, trade_records=records,
        )
        assert delivered is True
        admin_msg = next(text for chat_id, text in self.client.sent if chat_id == -900)
        assert "By strategy" in admin_msg
        assert "ORB" in admin_msg
        assert "BREAKOUT" in admin_msg
        # Both strategies also got their own channel's summary.
        assert any(chat_id == -100 for chat_id, _ in self.client.sent)
        assert any(chat_id == -200 for chat_id, _ in self.client.sent)

    @pytest.mark.asyncio
    async def test_no_trades_sends_only_the_admin_no_trades_message(self):
        delivered = await notifier.notify_day_summary(
            trades=0, wins=0, losses=0, net_pnl=0.0, capital=100000.0,
        )
        assert delivered is True
        assert self.client.sent == [(-900, self.client.sent[0][1])]
        assert "No trades taken today" in self.client.sent[0][1]
