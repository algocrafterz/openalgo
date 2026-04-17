import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError
from werkzeug.exceptions import BadRequest

from database.apilog_db import async_log_order
from database.apilog_db import executor as log_executor
from database.auth_db import get_auth_token_broker
from services.funds_service import get_funds
from limiter import limiter
from restx_api.schemas import SizingCalculatorSchema
from services.sizing_service import calculate_position_size, validate_sizing_input
from utils.logging import get_logger

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "50 per second")
api = Namespace("sizing", description="Position Sizing Calculator API")

logger = get_logger(__name__)

sizing_schema = SizingCalculatorSchema()


def _extract_capital_from_funds(funds_data: dict) -> float | None:
    """Extract available cash from a funds API response dict."""
    data = funds_data.get("data") or {}
    for key in ("availablecash", "available_cash", "net", "equity", "cash"):
        val = data.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


@api.route("/", strict_slashes=False)
class SizingCalculator(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Calculate position size based on capital, risk, and entry/SL parameters."""
        data = None
        try:
            try:
                data = request.json
            except BadRequest:
                error_response = {"status": "error", "message": "Invalid JSON in request body"}
                return make_response(jsonify(error_response), 400)

            # Validate and deserialize input using Marshmallow schema
            try:
                validated_data = sizing_schema.load(data or {})
            except ValidationError as err:
                error_message = str(err.messages)
                error_response = {"status": "error", "message": error_message}
                log_executor.submit(async_log_order, "sizing", data, error_response)
                return make_response(jsonify(error_response), 400)

            api_key = validated_data.get("apikey")

            # Authenticate
            AUTH_TOKEN, broker_name = get_auth_token_broker(api_key)
            if AUTH_TOKEN is None:
                error_response = {"status": "error", "message": "Invalid openalgo apikey"}
                return make_response(jsonify(error_response), 403)

            # Resolve capital: use provided value or fetch live funds
            capital = validated_data.get("capital")
            if capital is None:
                success, funds_resp, status_code = get_funds(api_key=api_key)
                if not success:
                    error_response = {
                        "status": "error",
                        "message": funds_resp.get("message", "Failed to fetch live capital"),
                    }
                    log_executor.submit(async_log_order, "sizing", validated_data, error_response)
                    return make_response(jsonify(error_response), status_code)

                capital = _extract_capital_from_funds(funds_resp)
                if capital is None:
                    error_response = {
                        "status": "error",
                        "message": "Could not parse available capital from funds response",
                    }
                    log_executor.submit(async_log_order, "sizing", validated_data, error_response)
                    return make_response(jsonify(error_response), 400)

            # Build service input dict
            service_data = {
                "capital": capital,
                "entry_price": validated_data["entry_price"],
                "stop_loss": validated_data["stop_loss"],
                "sizing_mode": validated_data["sizing_mode"],
                "risk_per_trade": validated_data.get("risk_per_trade", 0.01),
                "pct_of_capital": validated_data.get("pct_of_capital"),
                "slippage_factor": validated_data.get("slippage_factor", 0.0),
                "max_sl_pct_for_sizing": validated_data.get("max_sl_pct_for_sizing", 0.0),
                "min_entry_price": validated_data.get("min_entry_price", 0.0),
                "max_entry_price": validated_data.get("max_entry_price", 0.0),
                "target": validated_data.get("target", 0.0),
                "side": validated_data.get("side", "BUY"),
            }

            is_valid, sizing_input, err_msg = validate_sizing_input(service_data)
            if not is_valid:
                error_response = {"status": "error", "message": err_msg}
                log_executor.submit(async_log_order, "sizing", validated_data, error_response)
                return make_response(jsonify(error_response), 400)

            result = calculate_position_size(sizing_input)

            response_data = {
                "status": "success",
                "data": {
                    "quantity": result.quantity,
                    "raw_quantity": result.raw_quantity,
                    "risk_amount": round(result.risk_amount, 4),
                    "risk_pct_of_capital": round(result.risk_pct_of_capital, 6),
                    "reward_amount": round(result.reward_amount, 4),
                    "position_value": round(result.position_value, 4),
                    "rr_ratio": round(result.rr_ratio, 4),
                    "sl_distance_pct": round(result.sl_distance_pct, 6),
                    "skip_reason": result.skip_reason,
                    "warnings": list(result.warnings),
                },
            }
            log_executor.submit(async_log_order, "sizing", validated_data, response_data)
            return make_response(jsonify(response_data), 200)

        except Exception:
            logger.exception("Unexpected error in SizingCalculator endpoint.")
            error_response = {
                "status": "error",
                "message": "An unexpected error occurred in the API endpoint",
            }
            try:
                log_executor.submit(
                    async_log_order, "sizing", data if data is not None else {}, error_response
                )
            except Exception as e:
                logger.exception(f"Failed to log sizing error: {e}")
            return make_response(jsonify(error_response), 500)
