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
from app.services.options_signal_service import get_trending_oi_timeseries

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

        # Attach Trending OI timeseries (ATM ± 5 strikes, default 5m intervals)
        try:
            interval_m = int(request.args.get("interval", 5))
        except (TypeError, ValueError):
            interval_m = 5

        timeseries = get_trending_oi_timeseries(
            symbol=symbol,
            interval_minutes=interval_m,
            current_chain=analysis.get("detailed_chain", []),
            spot_price=float(analysis.get("market_bias", {}).get("spot_price", 0.0)),
            strike_step=float(analysis.get("key_levels", {}).get("strike_step", 50.0)),
        )
        analysis["trending_oi_timeseries"] = timeseries

        return jsonify({"success": True, "data": analysis})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@options_desk_bp.route("/api/options-desk/trending-oi", methods=["GET"])
def get_trending_oi():
    """
    Dedicated endpoint for Oi Pulse style Trending OI time-series table.
    Query params:
      - symbol: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX
      - interval: 3, 5, 15 (minutes, default 5)
      - date: YYYY-MM-DD (optional, defaults to today)
    """
    symbol = request.args.get("symbol", "NIFTY").upper()
    if symbol not in INDEX_CONFIGS:
        symbol = "NIFTY"
    try:
        interval_m = int(request.args.get("interval", 5))
    except (TypeError, ValueError):
        interval_m = 5

    trade_date = request.args.get("date", None)

    try:
        raw_chain = fetch_option_chain_data(symbol=symbol)
        analysis = analyze_option_desk(raw_chain)
        timeseries = get_trending_oi_timeseries(
            symbol=symbol,
            trade_date=trade_date,
            interval_minutes=interval_m,
            current_chain=analysis.get("detailed_chain", []),
            spot_price=float(analysis.get("market_bias", {}).get("spot_price", 0.0)),
            strike_step=float(analysis.get("key_levels", {}).get("strike_step", 50.0)),
        )
        return jsonify({"success": True, "data": timeseries})
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
    """Returns health and safe FYERS connection status without disclosing secrets."""
    from app.services.fyers_auth import get_fyers_token
    fy_token = bool(get_fyers_token())
    return jsonify({
        "success": True,
        "broker": "FYERS" if fy_token else "NONE",
        "fyers_authenticated": fy_token,
        "engine_status": "OPERATIONAL",
        "auto_refresh_interval_sec": 180,
    })


@options_desk_bp.route("/api/options-desk/signals", methods=["GET"])
def get_signals():
    """Returns active signals, trade history, and performance metrics."""
    try:
        from app.services.options_signal_service import get_signals_summary, update_active_signals
        # Trigger quick live price refresh on open signals
        update_active_signals()
        summary = get_signals_summary()
        return jsonify({"success": True, "data": summary})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@options_desk_bp.route("/api/options-desk/signals/record", methods=["POST"])
def record_paper_trade():
    """Records a manual paper trade or custom signal into the journal."""
    try:
        from app.services.options_signal_service import record_signal
        data = request.get_json() or {}
        lots = int(data.get("lots", 1))
        sig = record_signal(data, is_paper_trade=True, lots=lots)
        if not sig:
            return jsonify({"success": False, "error": "Invalid trade details or price missing"}), 400
        return jsonify({"success": True, "signal": sig})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@options_desk_bp.route("/api/options-desk/signals/<int:signal_id>/close", methods=["POST"])
def close_trade(signal_id):
    """Manually closes/squares off an active trade."""
    try:
        from app.services.options_signal_service import close_signal_manually
        sig = close_signal_manually(signal_id)
        if not sig:
            return jsonify({"success": False, "error": "Signal not found"}), 404
        return jsonify({"success": True, "signal": sig})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@options_desk_bp.route("/api/options-desk/signals/<int:signal_id>", methods=["DELETE"])
def remove_signal(signal_id):
    """Deletes a signal record."""
    try:
        from app.services.options_signal_service import delete_signal
        delete_signal(signal_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@options_desk_bp.route("/api/options-desk/signals/clear-history", methods=["POST"])
def clear_trade_history():
    """Clears closed signal history."""
    try:
        from app.services.options_signal_service import clear_history
        clear_history()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@options_desk_bp.route("/api/options-desk/signals/export-csv", methods=["GET"])
def export_journal_csv():
    """Exports full trade journal with rich telemetry to CSV."""
    from flask import Response
    from app.services.options_signal_service import export_signals_csv
    try:
        csv_data = export_signals_csv()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=options_trade_journal.csv"}
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@options_desk_bp.route("/api/options-desk/trade-analysis", methods=["GET"])
def get_trade_analysis():
    """
    Returns AI/Statistical Strategy Diagnostic & Parameter Optimization Report
    evaluating paper trading history, profit leakages, moneyness, MFE/MAE, and tuning actions.
    """
    try:
        from app.services.trade_analyzer_agent import analyze_paper_trading_logs
        report = analyze_paper_trading_logs()
        return jsonify({"success": True, "report": report})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



