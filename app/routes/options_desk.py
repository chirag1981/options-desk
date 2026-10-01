"""
app/routes/options_desk.py — Flask Blueprint for Options Desk Dashboard & APIs
Provides user-facing UI routes and JSON endpoints for live option chain and market bias data.
"""

from flask import Blueprint, render_template, request, jsonify
from app.services.fyers_options_service import (
    fetch_option_chain_data,
    get_supported_indices,
    get_upcoming_expiries,
    INDEX_CONFIGS
)
from app.services.options_engine import analyze_option_desk

options_desk_bp = Blueprint("options_desk", __name__)


@options_desk_bp.route("/")
@options_desk_bp.route("/options-desk")
def index():
    """Renders the main Options Desk dashboard."""
    indices = get_supported_indices()
    default_symbol = "NIFTY"
    expiries = get_upcoming_expiries(default_symbol)
    return render_template(
        "options_desk.html",
        indices=indices,
        default_symbol=default_symbol,
        expiries=expiries,
    )


@options_desk_bp.route("/api/options-desk/data", methods=["GET"])
def get_options_desk_data():
    """
    Returns full processed options analysis payload for specified symbol & expiry.
    Query params:
      - symbol: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX (default NIFTY)
      - expiry: DD-MMM-YYYY (optional)
      - refresh: 'true' or '1' to force fresh fetch
    """
    symbol = request.args.get("symbol", "NIFTY").upper()
    expiry = request.args.get("expiry", None)
    force = request.args.get("refresh", "false").lower() in ["true", "1", "yes"]

    if symbol not in INDEX_CONFIGS:
        symbol = "NIFTY"

    try:
        raw_chain = fetch_option_chain_data(symbol=symbol, expiry=expiry, force_refresh=force)
        analysis = analyze_option_desk(raw_chain)
        return jsonify({"success": True, "data": analysis})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@options_desk_bp.route("/api/options-desk/expiries", methods=["GET"])
def get_expiries():
    """Returns available expiries for requested symbol."""
    symbol = request.args.get("symbol", "NIFTY").upper()
    expiries = get_upcoming_expiries(symbol)
    return jsonify({"success": True, "symbol": symbol, "expiries": expiries})


@options_desk_bp.route("/api/options-desk/symbols", methods=["GET"])
def get_symbols():
    """Returns list of supported underlying indices."""
    return jsonify({"success": True, "indices": get_supported_indices()})


@options_desk_bp.route("/api/options-desk/status", methods=["GET"])
def get_desk_status():
    """Returns health and safe connection status without disclosing secrets."""
    from app.services.fyers_auth import load_cached_token
    has_token = bool(load_cached_token())
    return jsonify({
        "success": True,
        "fyers_authenticated": has_token,
        "engine_status": "OPERATIONAL",
        "auto_refresh_interval_sec": 180,
    })
