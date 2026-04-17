import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import Schema, ValidationError, fields, validate

from database.apilog_db import async_log_order
from database.apilog_db import executor as log_executor
from database.auth_db import get_auth_token_broker
from limiter import limiter
from services.funds_service import get_funds
from services.orb_service import get_orb_preset
from utils.logging import get_logger

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")
api = Namespace("sizing_orb", description="ORB Preset Position Sizing API")
logger = get_logger(__name__)


class ORBPresetSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    symbol = fields.Str(required=True, validate=validate.Length(min=1, max=50))
    exchange = fields.Str(required=True, validate=validate.Length(min=1, max=10))
    orb_minutes = fields.Int(
        load_default=15,
        validate=validate.OneOf([5, 15, 30, 60], error="orb_minutes must be one of: 5, 15, 30, 60"),
    )
    tp_rr = fields.Float(
        load_default=2.0,
        validate=validate.Range(min=0.5, max=10.0, error="tp_rr must be between 0.5 and 10.0"),
    )
    capital = fields.Float(
        load_default=None,
        allow_none=True,
        validate=validate.Range(min=0, error="capital must be non-negative"),
    )


_schema = ORBPresetSchema()


@api.route("/", strict_slashes=False)
class ORBPreset(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Calculate position size using ORB preset — auto-derives entry, SL, and TP from live data"""
        data = {}
        try:
            data = request.json or {}

            try:
                validated = _schema.load(data)
            except ValidationError as err:
                error_resp = {"status": "error", "message": str(err.messages)}
                log_executor.submit(async_log_order, "sizing_orb", data, error_resp)
                return make_response(jsonify(error_resp), 400)

            api_key = validated["apikey"]
            symbol = validated["symbol"]
            exchange = validated["exchange"]
            orb_minutes = validated["orb_minutes"]
            tp_rr = validated["tp_rr"]
            capital = validated.get("capital")

            # Validate API key
            auth_token, _, _ = get_auth_token_broker(api_key, include_feed_token=True)
            if auth_token is None:
                error_resp = {"status": "error", "message": "Invalid openalgo apikey"}
                log_executor.submit(async_log_order, "sizing_orb", data, error_resp)
                return make_response(jsonify(error_resp), 403)

            # Live capital fetch if not provided
            if not capital:
                ok, funds_data, _ = get_funds(api_key=api_key)
                if ok:
                    capital = float(funds_data.get("data", {}).get("availablecash", 0.0))

            success, result, error_msg = get_orb_preset(
                symbol=symbol,
                exchange=exchange,
                api_key=api_key,
                orb_minutes=orb_minutes,
                tp_rr=tp_rr,
                capital=capital,
            )

            if not success:
                error_resp = {"status": "error", "message": error_msg}
                log_executor.submit(async_log_order, "sizing_orb", data, error_resp)
                return make_response(jsonify(error_resp), 422)

            orb = result.orb
            sz = result.sizing

            response_data = {
                "status": "success",
                "data": {
                    "orb": {
                        "orb_high": orb.orb_high,
                        "orb_low": orb.orb_low,
                        "orb_range": orb.orb_range,
                        "orb_minutes": orb.orb_minutes,
                        "ltp": orb.ltp,
                        "side": orb.side,
                        "candles_used": orb.candles_used,
                    },
                    "inputs": result.preset_inputs,
                    "sizing": {
                        "quantity": sz.quantity,
                        "raw_quantity": sz.raw_quantity,
                        "risk_amount": round(sz.risk_amount, 2),
                        "risk_pct_of_capital": round(sz.risk_pct_of_capital, 4),
                        "reward_amount": round(sz.reward_amount, 2),
                        "position_value": round(sz.position_value, 2),
                        "rr_ratio": round(sz.rr_ratio, 2),
                        "sl_distance_pct": round(sz.sl_distance_pct, 6),
                        "skip_reason": sz.skip_reason,
                        "warnings": sz.warnings,
                    },
                },
            }

            log_executor.submit(async_log_order, "sizing_orb", data, response_data)
            return make_response(jsonify(response_data), 200)

        except Exception:
            logger.exception("Unexpected error in ORB preset endpoint")
            error_resp = {"status": "error", "message": "An unexpected error occurred"}
            try:
                log_executor.submit(async_log_order, "sizing_orb", data, error_resp)
            except Exception:
                pass
            return make_response(jsonify(error_resp), 500)
