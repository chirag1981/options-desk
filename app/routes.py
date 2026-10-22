"""
app/routes.py — Dashboard Views & REST API Endpoints
Handles page rendering and lightweight HTTP endpoints, delegating all domain
calculations to the respective service modules.
"""

import os
import logging
from flask import Blueprint, render_template, jsonify, request, Response

from app.market_data import MarketDataProvider
from app.analysis import MarketAnalyzer
from app.strategies import StrategyBuilder, IntradayEngine, PositionalEngine
from app.risk import PortfolioRiskManager
from app.trading import PaperTradingManager
from app.performance import PerformanceAnalytics, SystemDiagnostics
from app.models import TradePosition, OISnapshot, SignalCandidate
from app.config import Config
from app.extensions import db

log = logging.getLogger("routes")
main_bp = Blueprint("main", __name__)


# ---------------- PAGE ROUTES ----------------

@main_bp.route("/")
@main_bp.route("/dashboard")
def dashboard():
    """Renders the main 6-panel compact options-selling dashboard."""
    return render_template("dashboard.html", symbol=Config.SYMBOL, lot_size=Config.LOT_SIZE)


@main_bp.route("/journal")
def journal():
    """Renders the paper trade journal and P&L history page."""
    return render_template("journal.html")


@main_bp.route("/analysis")
def analysis_page():
    """Renders the deep-dive Open Interest and strike breakdown page."""
    return render_template("analysis.html")


@main_bp.route("/diagnostics")
def diagnostics_page():
    """Renders the read-only system diagnostics and health page."""
    return render_template("diagnostics.html")


@main_bp.route("/settings")
def settings_page():
    """Renders engine settings and risk configuration page."""
    return render_template("settings.html", config=Config)


# ---------------- REST API ENDPOINTS ----------------

@main_bp.route("/api/market-data")
def api_market_data():
    """Returns current spot price, OI walls, PCR, regime classification, and live strike chain."""
    chain_data = MarketDataProvider.get_option_chain()
    analysis = MarketAnalyzer.analyze_chain(chain_data)
    chain = chain_data.get("chain_by_strike", {})
    return jsonify({
        "status": "success",
        "data": analysis.to_dict(),
        "chain_by_strike": chain,
    })


@main_bp.route("/api/candidates")
def api_candidates():
    """Returns generated strategy candidates for Intraday and Positional engines."""
    chain_data = MarketDataProvider.get_option_chain()
    analysis = MarketAnalyzer.analyze_chain(chain_data)
    chain = chain_data.get("chain_by_strike", {})

    intraday_cands = [c.to_dict() for c in IntradayEngine.evaluate(analysis, chain)]
    positional_cands = [c.to_dict() for c in PositionalEngine.evaluate(analysis, chain)]

    # Also build standalone 4 strategy previews for UI selector
    bp_cand = StrategyBuilder.build_bull_put_spread(analysis, chain, engine_type="INTRADAY")
    bc_cand = StrategyBuilder.build_bear_call_spread(analysis, chain, engine_type="INTRADAY")
    ic_cand = StrategyBuilder.build_iron_condor(analysis, chain, engine_type="INTRADAY")
    ib_cand = StrategyBuilder.build_iron_butterfly(analysis, chain, engine_type="INTRADAY")

    from app.strategies import AutoStrategySelector
    from app.trading import AutoTradeEngine
    auto_sel = AutoStrategySelector.evaluate_auto_selection(analysis, chain)

    all_previews = [bp_cand.to_dict(), bc_cand.to_dict(), ic_cand.to_dict(), ib_cand.to_dict()]

    return jsonify({
        "status": "success",
        "analysis": analysis.to_dict(),
        "intraday": intraday_cands,
        "positional": positional_cands,
        "previews": all_previews,
        "auto_selection": auto_sel.to_dict(),
        "auto_trade_enabled": AutoTradeEngine.is_enabled(),
    })


@main_bp.route("/api/auto-trade/toggle", methods=["POST"])
def api_toggle_auto_trade():
    """Toggles automatic paper trade execution on/off."""
    from app.trading import AutoTradeEngine
    payload = request.get_json() or {}
    enabled = payload.get("enabled")
    if enabled is None:
        new_state = not AutoTradeEngine.is_enabled()
    else:
        new_state = bool(enabled)

    AutoTradeEngine.set_enabled(new_state)
    return jsonify({
        "status": "success",
        "auto_trade_enabled": new_state,
        "message": f"Auto-trading {'ACTIVATED' if new_state else 'PAUSED'}"
    })


@main_bp.route("/api/auto-trade/execute-now", methods=["POST"])
def api_execute_auto_now():
    """Immediately executes all currently auto-selected strategies."""
    from app.trading import AutoTradeEngine
    chain_data = MarketDataProvider.get_option_chain()
    analysis = MarketAnalyzer.analyze_chain(chain_data)
    chain = chain_data.get("chain_by_strike", {})

    executed, msg = AutoTradeEngine.execute_all_selected(analysis, chain)
    if not executed:
        return jsonify({
            "status": "warning",
            "message": msg
        })

    return jsonify({
        "status": "success",
        "message": msg,
        "positions": executed
    })


@main_bp.route("/api/positions")
def api_positions():
    """Returns active open positions and recent closed positions."""
    try:
        from app.trading import PaperTradingManager
        from app.market_data import MarketDataProvider
        open_count = TradePosition.query.filter_by(status="OPEN").count()
        if open_count > 0:
            chain_data = MarketDataProvider.get_option_chain()
            chain = chain_data.get("chain_by_strike", {})
            spot = chain_data.get("spot_price", 0.0)
            PaperTradingManager.update_open_positions_mtm(chain, spot_price=spot)
    except Exception as e:
        log.warning(f"Error during positions MTM refresh: {e}")

    open_positions = TradePosition.query.filter_by(status="OPEN").order_by(TradePosition.id.desc()).all()
    closed_positions = TradePosition.query.filter(
        TradePosition.status.in_(["CLOSED", "STOPPED_OUT", "TARGET_HIT", "SESSION_EXIT"])
    ).order_by(TradePosition.id.desc()).limit(20).all()

    summary = PerformanceAnalytics.get_portfolio_summary()

    return jsonify({
        "status": "success",
        "open_positions": [p.to_dict() for p in open_positions],
        "closed_positions": [p.to_dict() for p in closed_positions],
        "summary": summary,
    })


@main_bp.route("/api/execute-candidate", methods=["POST"])
def api_execute_candidate():
    """Validates risk and executes an eligible candidate spread in paper trading."""
    payload = request.get_json() or {}
    strategy_id = payload.get("strategy_id")
    engine_type = payload.get("engine_type", "INTRADAY").upper()
    lots = int(payload.get("lots", 1))

    chain_data = MarketDataProvider.get_option_chain()
    analysis = MarketAnalyzer.analyze_chain(chain_data)
    chain = chain_data.get("chain_by_strike", {})

    # Build candidate
    if strategy_id == "BULL_PUT":
        candidate = StrategyBuilder.build_bull_put_spread(analysis, chain, engine_type=engine_type, lots=lots)
    elif strategy_id == "BEAR_CALL":
        candidate = StrategyBuilder.build_bear_call_spread(analysis, chain, engine_type=engine_type, lots=lots)
    elif strategy_id == "IRON_CONDOR":
        candidate = StrategyBuilder.build_iron_condor(analysis, chain, engine_type=engine_type, lots=lots)
    else:
        return jsonify({"status": "error", "message": f"Strategy {strategy_id} is not eligible for execution."}), 400

    # Risk Check
    risk_res = PortfolioRiskManager.evaluate_candidate_risk(candidate, engine_type=engine_type)
    if not risk_res.approved:
        return jsonify({
            "status": "blocked_by_risk",
            "message": risk_res.summary,
            "risk_details": risk_res.to_dict(),
        }), 400

    # Execute Paper Fill
    ok, msg, position = PaperTradingManager.execute_candidate(candidate)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400

    return jsonify({
        "status": "success",
        "message": msg,
        "position": position.to_dict() if position else None,
    })


@main_bp.route("/api/close-position/<int:position_id>", methods=["POST"])
def api_close_position(position_id):
    """Manually closes an open paper position."""
    spot, _, _ = MarketDataProvider.get_spot_price()
    ok, msg = PaperTradingManager.close_position(position_id, exit_spot=spot, reason="MANUAL_CLOSE")
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400
    return jsonify({"status": "success", "message": msg})


@main_bp.route("/api/export-csv")
def api_export_csv():
    """Generates and downloads the Trade Journal as an RFC 4180 CSV file."""
    csv_data = PerformanceAnalytics.export_trades_csv()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=nifty_options_journal.csv"},
    )


@main_bp.route("/api/diagnostics")
def api_diagnostics():
    """Returns read-only system diagnostic checks."""
    return jsonify(SystemDiagnostics.run_diagnostics())


@main_bp.route("/api/fyers/set-token", methods=["POST"])
def api_fyers_set_token():
    """Allows updating the daily FYERS access token."""
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.market_data import MarketDataProvider, TOKEN_CACHE_FILE
    from app.config import Config

    payload = request.get_json() or {}
    token = payload.get("access_token", "").strip()
    if not token:
        return jsonify({"status": "error", "message": "No access_token provided"}), 400

    IST = ZoneInfo("Asia/Kolkata")
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    MarketDataProvider._access_token = token
    MarketDataProvider._token_date = today_str
    try:
        os.makedirs(Config.INSTANCE_DIR, exist_ok=True)
        with open(TOKEN_CACHE_FILE, "w") as f:
            json.dump({"date": today_str, "access_token": token}, f)
    except Exception as e:
        log.error(f"Failed writing token cache: {e}")

    return jsonify({
        "status": "success",
        "message": "FYERS access token updated successfully! Live data activated."
    })

