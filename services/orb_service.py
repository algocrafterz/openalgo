"""ORB (Opening Range Breakout) preset service.

Fetches today's intraday candles + LTP to derive ORB levels,
then runs position sizing automatically.
"""
from dataclasses import dataclass
from datetime import date, datetime

from services.history_service import get_history
from services.quotes_service import get_quotes
from services.sizing_service import SizingInput, SizingResult, calculate_position_size
from utils.logging import get_logger

logger = get_logger(__name__)

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


def get_orb_preset(
    symbol: str,
    exchange: str,
    api_key: str,
    orb_minutes: int = 15,
    tp_rr: float = 2.0,
    capital: float | None = None,
) -> tuple[bool, ORBPresetResult | None, str | None]:
    """Fetch ORB levels, derive trade parameters, and calculate position size.

    Args:
        symbol: Trading symbol (e.g. SBIN)
        exchange: Exchange (e.g. NSE)
        api_key: OpenAlgo API key
        orb_minutes: Number of opening minutes to define the range (5/15/30)
        tp_rr: R-multiple for target price (e.g. 2.0 = 2R target)
        capital: Override capital; if None, live funds are used at caller level

    Returns:
        (success, ORBPresetResult | None, error_message | None)
    """
    today = date.today().strftime("%Y-%m-%d")

    # --- Fetch intraday 1-min history for today ---
    success, hist_data, status = get_history(
        symbol=symbol,
        exchange=exchange,
        interval="1m",
        start_date=today,
        end_date=today,
        api_key=api_key,
    )
    if not success:
        msg = hist_data.get("message", "Failed to fetch intraday history")
        return False, None, f"History fetch failed: {msg}"

    candles: list[dict] = hist_data.get("data", [])
    if not candles:
        return False, None, "No intraday data available — market may not be open yet"

    orb_candles = _parse_market_open_candles(candles, orb_minutes)
    if len(orb_candles) < orb_minutes:
        return (
            False,
            None,
            f"Only {len(orb_candles)} candles available — ORB{orb_minutes} not complete yet",
        )

    orb_high = max(float(c["high"]) for c in orb_candles)
    orb_low = min(float(c["low"]) for c in orb_candles)
    orb_range = orb_high - orb_low

    # --- Fetch LTP ---
    success2, quote_data, _ = get_quotes(symbol=symbol, exchange=exchange, api_key=api_key)
    if not success2:
        msg = quote_data.get("message", "Failed to fetch LTP")
        return False, None, f"Quotes fetch failed: {msg}"

    ltp = float(quote_data.get("data", {}).get("ltp", 0.0))
    if ltp <= 0:
        return False, None, "LTP is zero or unavailable"

    # --- Determine side and trade levels ---
    if ltp > orb_high:
        side = "BUY"
        sl = orb_low
    elif ltp < orb_low:
        side = "SELL"
        sl = orb_high
    else:
        # Price inside range — no clear breakout yet
        side = "INSIDE"
        sl = orb_low
        logger.info(f"ORB preset for {symbol}: price {ltp} is inside ORB ({orb_low}-{orb_high})")

    entry = ltp
    risk = abs(entry - sl)
    if side == "BUY":
        target = entry + risk * tp_rr
    elif side == "SELL":
        target = entry - risk * tp_rr
    else:
        target = entry + risk * tp_rr  # default for display

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
    )

    # --- Calculate position size ---
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
