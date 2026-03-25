"""Tests for signal normalizer — preprocesses noisy messages into canonical format."""

import pytest

from signal_engine.normalizer import normalize
from signal_engine.parser import parse


class TestNormalizeBasic:
    def test_empty_input(self):
        assert normalize("") == ""

    def test_whitespace_only(self):
        assert normalize("   \n  \n  ") == ""

    def test_none_input(self):
        assert normalize(None) == ""

    def test_already_canonical_passes_through(self):
        text = "ORB LONG\nSymbol: RELIANCE\nEntry: 2500.50\nSL: 2480\nTP: 2540"
        assert normalize(text) == text


class TestEmojiStripping:
    def test_strips_green_circle_prefix(self):
        result = normalize("\U0001f7e2 ORB LONG\nSymbol: SBIN\nEntry: 600\nSL: 590\nTP: 620")
        assert result.startswith("ORB LONG")

    def test_strips_red_circle_prefix(self):
        result = normalize("\U0001f534 SMI SHORT\nSymbol: TCS\nEntry: 3800\nSL: 3830\nTP: 3750")
        assert result.startswith("SMI SHORT")

    def test_strips_multiple_emoji_types(self):
        result = normalize("\u2705\u27a1\ufe0f ORB LONG\nSymbol: INFY\nEntry: 1500\nSL: 1480\nTP: 1550")
        assert result.startswith("ORB LONG")

    def test_preserves_key_value_lines(self):
        result = normalize("ORB LONG\nEntry: 394.2\nSL: 390\nTP: 402")
        assert "Entry: 394.2" in result
        assert "SL: 390" in result
        assert "TP: 402" in result


class TestSeparatorRemoval:
    def test_removes_dash_separator(self):
        text = "ORB LONG\n------------------------\nSymbol: SBIN\nEntry: 600\nSL: 590\nTP: 620"
        result = normalize(text)
        assert "---" not in result
        assert "Symbol: SBIN" in result

    def test_removes_equals_separator(self):
        text = "ORB LONG\n========================\nSymbol: SBIN\nEntry: 600\nSL: 590\nTP: 620"
        result = normalize(text)
        assert "===" not in result

    def test_removes_underscore_separator(self):
        text = "ORB LONG\n________________________\nSymbol: SBIN\nEntry: 600\nSL: 590\nTP: 620"
        result = normalize(text)
        assert "___" not in result


class TestPipeDelimitedFirstLine:
    def test_pipe_splits_into_strategy_and_symbol(self):
        text = "ORB LONG | NATIONALUM\nEntry: 394.2\nSL: 390\nTP: 402.56"
        result = normalize(text)
        lines = result.splitlines()
        assert lines[0] == "ORB LONG"
        assert "Symbol: NATIONALUM" in result

    def test_pipe_with_emoji_prefix(self):
        text = "\U0001f7e2 ORB LONG | NATIONALUM\n------------------------\nEntry: 394.2\nTP: 402.56\nSL: 390.5"
        result = normalize(text)
        lines = result.splitlines()
        assert lines[0] == "ORB LONG"
        assert "Symbol: NATIONALUM" in result

    def test_pipe_short_signal(self):
        text = "SMI SHORT | HDFCBANK\nEntry: 1600\nSL: 1620\nTP: 1560"
        result = normalize(text)
        lines = result.splitlines()
        assert lines[0] == "SMI SHORT"
        assert "Symbol: HDFCBANK" in result

    def test_pipe_not_applied_to_non_first_lines(self):
        text = "ORB LONG\nSymbol: SBIN\nEntry: 600\nNotes: use | for exit\nSL: 590\nTP: 620"
        result = normalize(text)
        assert "Notes: use | for exit" in result


class TestWhitespaceHandling:
    def test_strips_leading_trailing_whitespace_on_lines(self):
        text = "  ORB LONG  \n  Symbol: SBIN  \n  Entry: 600  \n  SL: 590  \n  TP: 620  "
        result = normalize(text)
        for line in result.splitlines():
            assert line == line.strip()

    def test_removes_blank_lines(self):
        text = "ORB LONG\n\nSymbol: SBIN\n\nEntry: 600\nSL: 590\nTP: 620"
        result = normalize(text)
        assert "\n\n" not in result


class TestTargetToTPAlias:
    """Backward compatibility: 'Target:' in old messages becomes 'TP:'."""

    def test_target_renamed_to_tp(self):
        text = "ORB LONG\nSymbol: SBIN\nEntry: 600\nSL: 590\nTarget: 620"
        result = normalize(text)
        assert "TP: 620" in result
        assert "Target" not in result

    def test_target_case_insensitive(self):
        text = "ORB LONG\nSymbol: SBIN\nEntry: 600\nSL: 590\ntarget: 620"
        result = normalize(text)
        assert "TP: 620" in result

    def test_tp_stays_as_tp(self):
        text = "ORB LONG\nSymbol: SBIN\nEntry: 600\nSL: 590\nTP: 620"
        result = normalize(text)
        assert "TP: 620" in result

    def test_target_alias_full_pipeline(self):
        """Old format with Target: should parse correctly after normalize."""
        text = "ORB LONG\nSymbol: SBIN\nEntry: 600\nSL: 590\nTarget: 620"
        signal = parse(normalize(text))
        assert signal is not None
        assert signal.tp == 620.0


class TestRealWorldSmidestn:
    """End-to-end tests with the actual smidestn channel format."""

    def test_smidestn_long_signal(self):
        text = (
            "\U0001f7e2 ORB LONG | NATIONALUM\n"
            "------------------------\n"
            "Entry: 394.2\n"
            "TP: 402.56\n"
            "SL: 390.5"
        )
        result = normalize(text)
        assert "ORB LONG" in result
        assert "Symbol: NATIONALUM" in result
        assert "Entry: 394.2" in result
        assert "TP: 402.56" in result
        assert "SL: 390.5" in result

    def test_smidestn_short_signal(self):
        text = (
            "\U0001f534 SMI SHORT | TCS\n"
            "------------------------\n"
            "Entry: 3800\n"
            "TP: 3750\n"
            "SL: 3830"
        )
        result = normalize(text)
        assert "SMI SHORT" in result
        assert "Symbol: TCS" in result


class TestNormalizeThenParse:
    """Integration tests: normalize() -> parse() produces valid Signal objects."""

    def test_smidestn_long_parses_successfully(self):
        text = (
            "\U0001f7e2 ORB LONG | NATIONALUM\n"
            "------------------------\n"
            "Entry: 394.2\n"
            "TP: 402.56\n"
            "SL: 390.5"
        )
        signal = parse(normalize(text))
        assert signal is not None
        assert signal.strategy == "ORB"
        assert signal.direction.value == "LONG"
        assert signal.symbol == "NATIONALUM"
        assert signal.entry == 394.2
        assert signal.tp == 402.56
        assert signal.sl == 390.5

    def test_smidestn_short_parses_successfully(self):
        text = (
            "\U0001f534 SMI SHORT | HDFCBANK\n"
            "------------------------\n"
            "Entry: 1600\n"
            "TP: 1560\n"
            "SL: 1620"
        )
        signal = parse(normalize(text))
        assert signal is not None
        assert signal.strategy == "SMI"
        assert signal.direction.value == "SHORT"
        assert signal.symbol == "HDFCBANK"

    def test_canonical_format_parses_identically(self):
        text = "ORB LONG\nSymbol: RELIANCE\nEntry: 2500.50\nSL: 2480\nTP: 2540"
        signal_direct = parse(text)
        signal_normalized = parse(normalize(text))
        assert signal_direct is not None
        assert signal_normalized is not None
        assert signal_direct.strategy == signal_normalized.strategy
        assert signal_direct.symbol == signal_normalized.symbol
        assert signal_direct.entry == signal_normalized.entry

    def test_tradingview_canonical_format(self):
        text = "ORB LONG\nSymbol: SBIN\nEntry: 600\nSL: 590\nTP: 620"
        signal = parse(normalize(text))
        assert signal is not None
        assert signal.symbol == "SBIN"


class TestSwingExitFormat:
    """Tests for RSI-TP-MR alert format (RSI(2) strategy entry/exit signals)."""

    def test_exit_pipe_format_normalizes(self):
        text = "RSI-TP-MR EXIT | RELIANCE\n------------------------\nEntry: 1500\nSL: 1420\nTP: 1545\nProduct: CNC"
        result = normalize(text)
        lines = result.splitlines()
        assert lines[0] == "RSI-TP-MR EXIT"
        assert "Symbol: RELIANCE" in result

    def test_exit_pipe_format_parses(self):
        text = "RSI-TP-MR EXIT | RELIANCE\n------------------------\nEntry: 1500\nSL: 1420\nTP: 1545\nProduct: CNC"
        signal = parse(normalize(text))
        assert signal is not None
        assert signal.strategy == "RSI-TP-MR"
        assert signal.direction.value == "EXIT"
        assert signal.symbol == "RELIANCE"
        assert signal.product == "CNC"

    def test_rsi_tp_mr_long_entry_format(self):
        """Entry alert from RSI(2) PineScript v2."""
        text = (
            "RSI-TP-MR LONG | HDFCBANK\n"
            "------------------------\n"
            "Entry: 1542.30\n"
            "SL: 1420.00\n"
            "TP: 1575.50\n"
            "Product: CNC\n"
            "------------------------\n"
            "Risk: 122.30 | Reward: 33.20\n"
            "R:R 1:0.3\n"
            "RSI(2): 3.8\n"
            "Exit: close > 5 SMA (CNC delivery)\n"
            "------------------------\n"
            "15:25 IST\n"
            "https://www.tradingview.com/chart/?symbol=NSE:HDFCBANK&interval=D"
        )
        signal = parse(normalize(text))
        assert signal is not None
        assert signal.strategy == "RSI-TP-MR"
        assert signal.direction.value == "LONG"
        assert signal.symbol == "HDFCBANK"
        assert signal.entry == 1542.30
        assert signal.sl == 1420.00
        assert signal.tp == 1575.50
        assert signal.product == "CNC"

    def test_rsi_tp_mr_exit_with_extras(self):
        """Exit alert from RSI(2) PineScript v2 — extra lines ignored by parser."""
        text = (
            "RSI-TP-MR EXIT | HDFCBANK\n"
            "------------------------\n"
            "Entry: 1542.30\n"
            "SL: 1420.00\n"
            "TP: 1575.50\n"
            "Product: CNC\n"
            "------------------------\n"
            "Exit: 1578.50\n"
            "Reason: Close > 5 SMA\n"
            "P&L: +36.20 (+2.3%)\n"
            "Bars held: 4\n"
            "------------------------\n"
            "15:25 IST"
        )
        signal = parse(normalize(text))
        assert signal is not None
        assert signal.strategy == "RSI-TP-MR"
        assert signal.direction.value == "EXIT"
        assert signal.symbol == "HDFCBANK"
        assert signal.entry == 1542.30
        assert signal.sl == 1420.00
        assert signal.tp == 1575.50
        assert signal.product == "CNC"
