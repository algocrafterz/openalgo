"""Stop-trigger rounding follows NSE's tiered tick sizes, not a fixed 0.05.

NSE/CMTR/67133: tick is 0.01 below Rs 250, 0.05 to Rs 1,000, 0.10 to Rs 5,000, 0.50 to Rs 10,000,
1.00 to Rs 20,000, 5.00 above. The tier comes from the previous month's last close, which the
executor cannot see, so it rounds to the coarsest tick that could apply (every tier's tick is a
multiple of the finer ones, so that price is valid under any of them).
"""

from signal_engine.executor import _trigger_matches, round_to_tick


class TestRoundToTick:
    def test_long_stop_above_rs_1000_lands_on_a_ten_paise_tick(self):
        # the live TCS stop that used to go out as 2079.75 (not a valid 0.10 tick)
        assert round_to_tick(2079.74, "up") == 2079.8

    def test_short_stop_rounds_down_on_a_ten_paise_tick(self):
        assert round_to_tick(2090.26, "down") == 2090.2

    def test_mid_priced_stock_gets_a_price_valid_under_either_neighbouring_tier(self):
        # Rs 894.78 can sit in the 0.05 tier or, after a rise, the 0.10 tier: 0.10 is valid in both
        assert round_to_tick(894.78, "down") == 894.7

    def test_cheap_stock_keeps_a_fine_tick(self):
        assert round_to_tick(123.456, "nearest") == 123.46

    def test_high_priced_stock_uses_the_coarse_tick(self):
        assert round_to_tick(24987.0, "up") == 24990.0

    def test_an_already_valid_price_is_left_alone_despite_float_noise(self):
        assert round_to_tick(2079.7, "up") == 2079.7
        assert round_to_tick(2079.7, "down") == 2079.7
        assert round_to_tick(2079.7, "nearest") == 2079.7

    def test_rounding_direction_is_respected(self):
        assert round_to_tick(2079.71, "up") == 2079.8
        assert round_to_tick(2079.79, "down") == 2079.7

    def test_every_result_is_a_multiple_of_each_finer_tier_tick(self):
        for price in (180.37, 640.11, 999.93, 1004.27, 2079.74, 5210.6, 12345.67):
            out = round_to_tick(price, "up")
            assert abs(out / 0.05 - round(out / 0.05)) < 1e-6, price
            assert abs(out / 0.01 - round(out / 0.01)) < 1e-6, price


class TestTriggerMatching:
    def test_match_tolerance_is_one_tick_at_the_stocks_price_level(self):
        assert _trigger_matches(2079.8, 2079.8)
        assert _trigger_matches(2079.9, 2079.8)
        assert not _trigger_matches(2080.0, 2079.8)

    def test_cheap_stock_tolerance_is_tight(self):
        assert _trigger_matches(123.46, 123.45)
        assert not _trigger_matches(123.55, 123.45)
