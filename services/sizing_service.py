"""Position Sizing Calculator service.

Pure, stateless computation — no DB calls, no logging side effects.
"""
import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SizingInput:
    """Validated inputs for position sizing calculation."""

    capital: float
    entry_price: float
    stop_loss: float
    sizing_mode: str  # "fixed_fractional" | "pct_of_capital"
    risk_per_trade: float = 0.01
    pct_of_capital: float = 0.0
    slippage_factor: float = 0.0
    max_sl_pct_for_sizing: float = 0.0
    min_entry_price: float = 0.0
    max_entry_price: float = 0.0
    target: float = 0.0
    side: str = "BUY"


@dataclass(frozen=True)
class SizingResult:
    """Output of a position sizing calculation."""

    quantity: int
    raw_quantity: int
    risk_amount: float
    risk_pct_of_capital: float
    reward_amount: float
    position_value: float
    rr_ratio: float
    sl_distance_pct: float
    skip_reason: str | None
    warnings: list[str] = field(default_factory=list)


VALID_SIZING_MODES = ("fixed_fractional", "pct_of_capital")


def _skip_result(skip_reason: str, entry_price: float = 0.0, stop_loss: float = 0.0, capital: float = 0.0) -> SizingResult:
    """Helper to build a zero-quantity result with a skip reason."""
    sl_distance_pct = abs(entry_price - stop_loss) / entry_price if entry_price > 0 else 0.0
    return SizingResult(
        quantity=0,
        raw_quantity=0,
        risk_amount=0.0,
        risk_pct_of_capital=0.0,
        reward_amount=0.0,
        position_value=0.0,
        rr_ratio=0.0,
        sl_distance_pct=sl_distance_pct,
        skip_reason=skip_reason,
        warnings=[],
    )


def _compute_metrics(qty: int, inp: SizingInput, risk_amount: float) -> SizingResult:
    """Build a full SizingResult from a computed quantity."""
    entry = inp.entry_price
    sl = inp.stop_loss
    target = inp.target
    capital = inp.capital

    sl_distance = abs(entry - sl)
    sl_distance_pct = sl_distance / entry if entry > 0 else 0.0
    position_value = qty * entry

    # Reward amount: distance from entry to target * qty
    if target > 0.0 and qty > 0:
        reward_amount = abs(target - entry) * qty
    else:
        reward_amount = 0.0

    # R:R ratio
    if sl_distance > 0 and target > 0.0:
        rr_ratio = abs(target - entry) / sl_distance
    else:
        rr_ratio = 0.0

    # risk_pct_of_capital: actual risk as fraction of capital
    risk_pct_of_capital = risk_amount / capital if capital > 0 else 0.0

    return SizingResult(
        quantity=qty,
        raw_quantity=qty,
        risk_amount=risk_amount,
        risk_pct_of_capital=risk_pct_of_capital,
        reward_amount=reward_amount,
        position_value=position_value,
        rr_ratio=rr_ratio,
        sl_distance_pct=sl_distance_pct,
        skip_reason=None,
        warnings=[],
    )


def calculate_position_size(inp: SizingInput) -> SizingResult:
    """Calculate position size using the specified sizing mode.

    Args:
        inp: Validated SizingInput dataclass.

    Returns:
        SizingResult with quantity, risk/reward metrics, and any warnings.

    Raises:
        ValueError: If sizing_mode is not one of the supported values.
    """
    if inp.sizing_mode not in VALID_SIZING_MODES:
        raise ValueError(
            f"Invalid sizing_mode '{inp.sizing_mode}'. Must be one of: {VALID_SIZING_MODES}"
        )

    entry = inp.entry_price
    sl = inp.stop_loss
    capital = inp.capital

    # Price filter: min_entry_price (0 = disabled)
    if inp.min_entry_price > 0.0 and entry < inp.min_entry_price:
        return _skip_result(
            f"Entry price {entry} is below min_entry_price {inp.min_entry_price}",
            entry_price=entry, stop_loss=sl, capital=capital,
        )

    # Price filter: max_entry_price (0 = disabled)
    if inp.max_entry_price > 0.0 and entry > inp.max_entry_price:
        return _skip_result(
            f"Entry price {entry} exceeds max_entry_price {inp.max_entry_price}",
            entry_price=entry, stop_loss=sl, capital=capital,
        )

    if inp.sizing_mode == "fixed_fractional":
        return _fixed_fractional(inp)
    else:
        return _pct_of_capital(inp)


def _fixed_fractional(inp: SizingInput) -> SizingResult:
    """Risk a fixed % of capital, sized by distance to stop-loss."""
    entry = inp.entry_price
    sl = inp.stop_loss
    capital = inp.capital

    if capital <= 0:
        return _skip_result("Capital must be positive", entry_price=entry, stop_loss=sl, capital=capital)

    risk_per_share = abs(entry - sl)
    if risk_per_share <= 0:
        return _skip_result(
            "Entry price equals stop-loss — zero SL distance, cannot size position",
            entry_price=entry, stop_loss=sl, capital=capital,
        )

    # Apply SL cap for sizing if configured and actual SL is wider than the cap
    if inp.max_sl_pct_for_sizing > 0 and entry > 0:
        max_sl_distance = entry * inp.max_sl_pct_for_sizing
        if risk_per_share > max_sl_distance:
            risk_per_share = max_sl_distance

    risk_per_share *= (1.0 + inp.slippage_factor)
    risk_amount = capital * inp.risk_per_trade
    qty = math.floor(risk_amount / risk_per_share)

    if qty <= 0:
        return _skip_result(
            f"Position size is zero — stock price {entry} exceeds capital allocation for {inp.sizing_mode}",
            entry_price=entry, stop_loss=sl, capital=capital,
        )

    # Use actual sl_distance (pre-cap) for risk_amount in result
    actual_sl_distance = abs(entry - sl)
    actual_risk_amount = qty * actual_sl_distance

    return _compute_metrics(qty, inp, actual_risk_amount)


def _pct_of_capital(inp: SizingInput) -> SizingResult:
    """Allocate a fixed % of capital to the position."""
    entry = inp.entry_price
    sl = inp.stop_loss
    capital = inp.capital

    if entry <= 0:
        return _skip_result(
            "Entry price must be positive for pct_of_capital mode",
            entry_price=entry, stop_loss=sl, capital=capital,
        )

    allocation = capital * inp.pct_of_capital
    qty = math.floor(allocation / entry)

    if qty <= 0:
        return _skip_result(
            f"Position size is zero — insufficient allocation for entry price {entry}",
            entry_price=entry, stop_loss=sl, capital=capital,
        )

    # Risk amount: qty * sl_distance
    sl_distance = abs(entry - sl)
    risk_amount = qty * sl_distance

    return _compute_metrics(qty, inp, risk_amount)


def validate_sizing_input(data: dict) -> tuple[bool, SizingInput | None, str | None]:
    """Validate raw request dict and return a SizingInput or an error message.

    Args:
        data: Raw request dict.

    Returns:
        (is_valid, SizingInput | None, error_message | None)
    """
    if data is None:
        return False, None, "Request data is required"

    # Required fields
    if "entry_price" not in data:
        return False, None, "Missing required field: entry_price"
    if "stop_loss" not in data:
        return False, None, "Missing required field: stop_loss"
    if "sizing_mode" not in data:
        return False, None, "Missing required field: sizing_mode"

    # Validate sizing_mode
    sizing_mode = data.get("sizing_mode")
    if sizing_mode not in VALID_SIZING_MODES:
        return False, None, (
            f"Invalid sizing_mode '{sizing_mode}'. Must be one of: {list(VALID_SIZING_MODES)}"
        )

    # Validate entry_price
    try:
        entry_price = float(data["entry_price"])
    except (TypeError, ValueError):
        return False, None, "entry_price must be a number"
    if entry_price < 0:
        return False, None, "entry_price must be non-negative"

    # Validate stop_loss
    try:
        stop_loss = float(data["stop_loss"])
    except (TypeError, ValueError):
        return False, None, "stop_loss must be a number"

    # Validate capital (optional — if absent, set to 0.0 for live fetch at API layer)
    if "capital" in data and data["capital"] is not None:
        try:
            capital = float(data["capital"])
        except (TypeError, ValueError):
            return False, None, "capital must be a number"
        if capital < 0:
            return False, None, "capital must be non-negative"
    else:
        capital = 0.0  # signals the API layer to fetch live funds

    # Optional fields with defaults
    def _float_or_default(key: str, default: float) -> float:
        v = data.get(key)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    risk_per_trade = _float_or_default("risk_per_trade", 0.01)
    pct_of_capital = _float_or_default("pct_of_capital", 0.0)
    slippage_factor = _float_or_default("slippage_factor", 0.0)
    max_sl_pct_for_sizing = _float_or_default("max_sl_pct_for_sizing", 0.0)
    min_entry_price = _float_or_default("min_entry_price", 0.0)
    max_entry_price = _float_or_default("max_entry_price", 0.0)
    target = _float_or_default("target", 0.0)

    side = data.get("side") or "BUY"

    inp = SizingInput(
        capital=capital,
        entry_price=entry_price,
        stop_loss=stop_loss,
        sizing_mode=sizing_mode,
        risk_per_trade=risk_per_trade,
        pct_of_capital=pct_of_capital,
        slippage_factor=slippage_factor,
        max_sl_pct_for_sizing=max_sl_pct_for_sizing,
        min_entry_price=min_entry_price,
        max_entry_price=max_entry_price,
        target=target,
        side=side,
    )
    return True, inp, None
