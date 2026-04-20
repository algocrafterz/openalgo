"""ORB (Opening Range Breakout) preset service.

Fetches today's intraday candles + LTP to derive ORB levels,
then runs position sizing automatically.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from services.history_service import get_history
from services.quotes_service import get_quotes
from services.sizing_service import SizingInput, SizingResult, calculate_position_size
from utils.logging import get_logger

logger = get_logger(__name__)

_MAX_LOOKBACK_DAYS = 5


def _prev_trading_dates(from_date: date, n: int) -> list[date]:
    """Return up to n calendar dates before from_date (weekdays only)."""
    results = []
    d = from_date - timedelta(days=1)
    while len(results) < n:
        if d.weekday() < 5:  # Mon–Fri
            results.append(d)
        d -= timedelta(days=1)
    return results

# ORB preset defaults (mirror signal_engine/config.yaml)
ORB_DEFAULTS = {
    "sizing_mode": "fixed_fractional",
    "risk_per_trade": 0.01,
    "slippage_factor": 0.10,
    "product": "MIS",
}


@dataclass(frozen=True)
class ORBLevels:
    orb_high: float
    orb_low: float
    orb_range: float
    ltp: float
    side: str       # "BUY" | "SELL" | "INSIDE"
    entry: float
    sl: float
    target: float
    orb_minutes: int
    tp_rr: float
    candles_used: int
    data_date: str  # YYYY-MM-DD of the session used (may differ from today when market is closed)


@dataclass(frozen=True)
class ORBPresetResult:
    orb: ORBLevels
    sizing: SizingResult
    preset_inputs: dict


def _parse_market_open_candles(candles: list[dict], orb_minutes: int) -> list[dict]:
    """Return the first orb_minutes candles at or after 09:15 IST."""
    result = []
    for c in candles:
        ts = c.get("timestamp")
        if ts is None:
            continue
        try:
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts)
            else:
                dt = datetime.fromisoformat(str(ts))
        except (ValueError, OSError):
            continue

        # Only candles from 09:15 onwards
        if dt.hour > 9 or (dt.hour == 9 and dt.minute >= 15):
            result.append(c)
            if len(result) >= orb_minutes:
                break
    return result


def _fetch_orb_candles(
    symbol: str,
    exchange: str,
    api_key: str,
    session_date: str,
    orb_minutes: int,
) -> tuple[list[dict], str | None]:
    """Fetch and parse ORB candles for a given session date.

    Returns (orb_candles, error_msg). orb_candles is empty on failure.
    """
    success, hist_data, _ = get_history(
        symbol=symbol,
        exchange=exchange,
        interval="1m",
        start_date=session_date,
        end_date=session_date,
        api_key=api_key,
    )
    if not success:
        msg = hist_data.get("message", "Failed to fetch intraday history")
        return [], msg

    candles: list[dict] = hist_data.get("data", [])
    if not candles:
        return [], "No intraday data"

    orb_candles = _parse_market_open_candles(candles, orb_minutes)
    if len(orb_candles) < orb_minutes:
        return [], f"Only {len(orb_candles)} candles — ORB{orb_minutes} not complete"

    return orb_candles, None


def get_orb_preset(
    symbol: str,
    exchange: str,
    api_key: str,
    orb_minutes: int = 15,
    tp_rr: float = 2.0,
    capital: float | None = None,
) -> tuple[bool, ORBPresetResult | None, str | None]:
    """Fetch ORB levels, derive trade parameters, and calculate position size.

    Falls back to the most recent trading day (up to _MAX_LOOKBACK_DAYS back) when
    today has no data (market closed, weekend, pre-open). Returns data_date so the
    caller can surface a warning when showing historical data.

    Returns:
        (success, ORBPresetResult | None, error_message | None)
    """
    today = date.today()
    candidate_dates = [today] + _prev_trading_dates(today, _MAX_LOOKBACK_DAYS)

    orb_candles: list[dict] = []
    data_date: date | None = None
    last_error = "No intraday data available for the last 5 trading days"

    for candidate in candidate_dates:
        date_str = candidate.strftime("%Y-%m-%d")
        candles, err = _fetch_orb_candles(symbol, exchange, api_key, date_str, orb_minutes)
        if candles:
            orb_candles = candles
            data_date = candidate
            break
        last_error = err or last_error

    if not orb_candles or data_date is None:
        return False, None, last_error

    orb_high = max(float(c["high"]) for c in orb_candles)
    orb_low = min(float(c["low"]) for c in orb_candles)
    orb_range = orb_high - orb_low

    # --- Fetch LTP; fall back to last candle close if market is closed ---
    ltp: float = 0.0
    success2, quote_data, _ = get_quotes(symbol=symbol, exchange=exchange, api_key=api_key)
    if success2:
        ltp = float(quote_data.get("data", {}).get("ltp", 0.0))

    if ltp <= 0:
        # Use last candle close as a reference price (market closed)
        ltp = float(orb_candles[-1].get("close", 0.0))

    if ltp <= 0:
        return False, None, "Price unavailable — LTP and last-candle close are both zero"

    # --- Determine side and trade levels ---
    if ltp > orb_high:
        side = "BUY"
        sl = orb_low
    elif ltp < orb_low:
        side = "SELL"
        sl = orb_high
    else:
        side = "INSIDE"
        sl = orb_low
        logger.info(f"ORB preset for {symbol}: price {ltp} inside ORB ({orb_low}-{orb_high})")

    entry = ltp
    risk = abs(entry - sl)
    if side == "BUY":
        target = entry + risk * tp_rr
    elif side == "SELL":
        target = entry - risk * tp_rr
    else:
        target = entry + risk * tp_rr

    orb_levels = ORBLevels(
        orb_high=round(orb_high, 2),
        orb_low=round(orb_low, 2),
        orb_range=round(orb_range, 2),
        ltp=round(ltp, 2),
        side=side,
        entry=round(entry, 2),
        sl=round(sl, 2),
        target=round(target, 2),
        orb_minutes=orb_minutes,
        tp_rr=tp_rr,
        candles_used=len(orb_candles),
        data_date=data_date.strftime("%Y-%m-%d"),
    )

    effective_capital = capital if (capital and capital > 0) else 0.0

    sizing_inp = SizingInput(
        capital=effective_capital,
        entry_price=entry,
        stop_loss=sl,
        target=target,
        sizing_mode=ORB_DEFAULTS["sizing_mode"],
        risk_per_trade=ORB_DEFAULTS["risk_per_trade"],
        slippage_factor=ORB_DEFAULTS["slippage_factor"],
        side=side,
    )

    sizing_result = calculate_position_size(sizing_inp)

    preset_inputs = {
        "symbol": symbol,
        "exchange": exchange,
        "side": side,
        "product": ORB_DEFAULTS["product"],
        "entry_price": round(entry, 2),
        "stop_loss": round(sl, 2),
        "target": round(target, 2),
        "sizing_mode": ORB_DEFAULTS["sizing_mode"],
        "risk_per_trade": ORB_DEFAULTS["risk_per_trade"],
        "slippage_factor": ORB_DEFAULTS["slippage_factor"],
    }

    return True, ORBPresetResult(orb=orb_levels, sizing=sizing_result, preset_inputs=preset_inputs), None
