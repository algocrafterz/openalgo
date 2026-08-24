"""Tests for signal parser — RED phase first."""


from signal_engine.parser import parse


class TestParseValidSignals:
    def test_long_signal_with_all_fields(self):
        text = "ORB LONG\nSymbol: RELIANCE\nEntry: 2500.50\nSL: 2480\nTP: 2540\nTime: 09:20"
        signal = parse(text)
        assert signal is not None
        assert signal.strategy == "ORB"
        assert signal.direction.value == "LONG"
        assert signal.symbol == "RELIANCE"
        assert signal.entry == 2500.50
        assert signal.sl == 2480.0
        assert signal.tp == 2540.0
        assert signal.time == "09:20"

    def test_short_signal(self):
        text = "VWAP SHORT\nSymbol: TCS\nEntry: 3800\nSL: 3830\nTP: 3750"
        signal = parse(text)
        assert signal is not None
        assert signal.direction.value == "SHORT"
        assert signal.symbol == "TCS"
        assert signal.entry == 3800.0

    def test_different_strategy_names(self):
        text = "BREAKOUT LONG\nSymbol: INFY\nEntry: 1500\nSL: 1480\nTP: 1550"
        signal = parse(text)
        assert signal is not None
        assert signal.strategy == "BREAKOUT"

    def test_case_insensitive_keys(self):
        text = "ORB LONG\nsymbol: SBIN\nENTRY: 600\nSl: 590\ntp: 620"
        signal = parse(text)
        assert signal is not None
        assert signal.symbol == "SBIN"
        assert signal.entry == 600.0

    def test_symbol_uppercased(self):
        text = "ORB LONG\nSymbol: reliance\nEntry: 2500\nSL: 2480\nTP: 2540"
        signal = parse(text)
        assert signal is not None
        assert signal.symbol == "RELIANCE"

    def test_optional_time_missing(self):
        text = "ORB LONG\nSymbol: HDFCBANK\nEntry: 1600\nSL: 1580\nTP: 1640"
        signal = parse(text)
        assert signal is not None
        assert signal.time is None

    def test_raw_message_captured(self):
        text = "ORB LONG\nSymbol: ITC\nEntry: 450\nSL: 440\nTP: 470"
        signal = parse(text)
        assert signal is not None
        assert signal.raw_message == text

    def test_received_at_set(self):
        text = "ORB LONG\nSymbol: ITC\nEntry: 450\nSL: 440\nTP: 470"
        signal = parse(text)
        assert signal is not None
        assert signal.received_at is not None

    def test_exchange_field_parsed(self):
        text = "ORB LONG\nSymbol: NIFTY24JAN24000CE\nExchange: NFO\nEntry: 200\nSL: 180\nTP: 240"
        signal = parse(text)
        assert signal is not None
        assert signal.exchange == "NFO"

    def test_product_field_parsed(self):
        text = "SWING LONG\nSymbol: RELIANCE\nProduct: CNC\nEntry: 2500\nSL: 2480\nTP: 2540"
        signal = parse(text)
        assert signal is not None
        assert signal.product == "CNC"

    def test_exit_direction_parsed(self):
        text = "RSI-TP-MR EXIT\nSymbol: RELIANCE\nProduct: CNC\nEntry: 2500\nSL: 2480\nTP: 2540"
        signal = parse(text)
        assert signal is not None
        assert signal.strategy == "RSI-TP-MR"
        assert signal.direction.value == "EXIT"
        assert signal.symbol == "RELIANCE"
        assert signal.product == "CNC"

    def test_exchange_and_product_both_parsed(self):
        text = "ORB LONG\nSymbol: NIFTY24JAN24000CE\nExchange: NFO\nProduct: NRML\nEntry: 200\nSL: 180\nTP: 240"
        signal = parse(text)
        assert signal is not None
        assert signal.exchange == "NFO"
        assert signal.product == "NRML"

    def test_exchange_defaults_to_none(self):
        text = "ORB LONG\nSymbol: SBIN\nEntry: 600\nSL: 590\nTP: 620"
        signal = parse(text)
        assert signal is not None
        assert signal.exchange is None

    def test_product_defaults_to_none(self):
        text = "ORB LONG\nSymbol: SBIN\nEntry: 600\nSL: 590\nTP: 620"
        signal = parse(text)
        assert signal is not None
        assert signal.product is None

    def test_exchange_uppercased(self):
        text = "ORB LONG\nSymbol: SBIN\nExchange: nse\nEntry: 600\nSL: 590\nTP: 620"
        signal = parse(text)
        assert signal is not None
        assert signal.exchange == "NSE"

    def test_product_uppercased(self):
        text = "ORB LONG\nSymbol: SBIN\nProduct: mis\nEntry: 600\nSL: 590\nTP: 620"
        signal = parse(text)
        assert signal is not None
        assert signal.product == "MIS"


class TestParseInvalidSignals:
    def test_missing_symbol(self):
        text = "ORB LONG\nEntry: 2500\nSL: 2480\nTP: 2540"
        assert parse(text) is None

    def test_missing_entry(self):
        text = "ORB LONG\nSymbol: RELIANCE\nSL: 2480\nTP: 2540"
        assert parse(text) is None

    def test_missing_sl(self):
        text = "ORB LONG\nSymbol: RELIANCE\nEntry: 2500\nTP: 2540"
        assert parse(text) is None

    def test_missing_tp(self):
        text = "ORB LONG\nSymbol: RELIANCE\nEntry: 2500\nSL: 2480"
        assert parse(text) is None

    def test_invalid_direction(self):
        text = "ORB UP\nSymbol: RELIANCE\nEntry: 2500\nSL: 2480\nTP: 2540"
        assert parse(text) is None

    def test_empty_message(self):
        assert parse("") is None

    def test_single_word(self):
        assert parse("hello") is None

    def test_no_key_value_pairs(self):
        text = "ORB LONG\njust some random text"
        assert parse(text) is None

    def test_non_numeric_entry(self):
        text = "ORB LONG\nSymbol: RELIANCE\nEntry: abc\nSL: 2480\nTP: 2540"
        assert parse(text) is None


class TestSignalContext:
    """Unrecognised Key: Value lines are kept verbatim so the PineScript can add entry-criteria
    fields without a matching engine change, and so win/loss attribution over a large sample
    reads from the trade log alone."""

    def _entry(self, extra: str = "") -> str:
        return (
            "BREAKOUT LONG\n"
            "Symbol: TATASTEEL\n"
            "Entry: 184.04\n"
            "SL: 183.41\n"
            "TP: 184.88\n" + extra
        )

    def test_unknown_fields_captured_in_context(self):
        sig = parse(self._entry("Score: 9\nRVOL: 1.5\nAdrUsed: 54\n"))
        assert sig.context == {"score": "9", "rvol": "1.5", "adrused": "54"}

    def test_context_empty_when_no_extra_fields(self):
        assert parse(self._entry()).context == {}

    def test_known_fields_not_duplicated_into_context(self):
        sig = parse(self._entry("Exchange: NSE\nProduct: MIS\nTime: 09:50\n"))
        assert sig.context == {}
        assert sig.exchange == "NSE"

    def test_context_preserves_multi_word_values(self):
        sig = parse(self._entry("Auction: Above VAH\nTrend: vwap- ema+ slope+\n"))
        assert sig.context["auction"] == "Above VAH"
        assert sig.context["trend"] == "vwap- ema+ slope+"

    def test_trigger_is_captured(self):
        """Trigger names the key level that fired the entry — the primary grouping key."""
        assert parse(self._entry("Trigger: VAH-RT\n")).context["trigger"] == "VAH-RT"

    def test_timestamp_line_not_captured(self):
        """"09:50 IST" matches the Key: Value shape but is a clock, not a field."""
        assert "09" not in parse(self._entry("09:50 IST\n")).context

    def test_chart_url_not_captured(self):
        """Every alert ends with a URL whose scheme would otherwise become a column."""
        ctx = parse(self._entry("https://www.tradingview.com/chart/?symbol=NSE:X&interval=5\n")).context
        assert "https" not in ctx
