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


@main_bp.app_context_processor
def inject_global_market_quote():
    """Injects live NIFTY spot quote into all templates so navbar is always populated."""
    try:
        spot, spot_chg, status = MarketDataProvider.get_spot_price()
        return {
            "nav_spot_price": spot,
            "nav_spot_change_pct": spot_chg,
            "nav_spot_status": status,
        }
    except Exception as e:
        log.warning(f"Failed injecting nav spot quote: {e}")
        return {
            "nav_spot_price": 0.0,
            "nav_spot_change_pct": 0.0,
            "nav_spot_status": "DISCONNECTED",
        }


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
    spot, chg, status = MarketDataProvider.get_spot_price()
    return render_template("settings.html", config=Config, fyers_connected=(status == "FYERS_LIVE"))


# ---------------- REST API ENDPOINTS ----------------

@main_bp.route("/api/market-data")
def api_market_data():
    """Returns current spot price, OI walls, PCR, regime classification, and live strike chain."""
    chain_data = MarketDataProvider.get_option_chain()
    if chain_data.get("spot_price", 0.0) <= 0:
        return jsonify({
            "status": "warning",
            "message": "Live FYERS broker stream not connected. Please connect FYERS in Settings to stream live market data.",
            "data_status": chain_data.get("data_status", "DISCONNECTED"),
            "data": None,
            "chain_by_strike": {},
        })
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
    from app.trading import AutoTradeEngine
    chain_data = MarketDataProvider.get_option_chain()
    if chain_data.get("spot_price", 0.0) <= 0:
        return jsonify({
            "status": "warning",
            "message": "Live FYERS broker stream not connected. Please connect FYERS in Settings to stream live market data.",
            "data_status": chain_data.get("data_status", "DISCONNECTED"),
            "analysis": None,
            "intraday": [],
            "positional": [],
            "previews": [],
            "auto_selection": {
                "selected_strategies": [],
                "selected_strategy_id": None,
                "strategy_name": "AWAITING LIVE FEED",
                "mode": "DISCONNECTED",
                "rationale": "Live FYERS market data feed is disconnected. Connect FYERS in Settings to stream live market data."
            },
            "auto_trade_enabled": AutoTradeEngine.is_enabled(),
        })

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
    if chain_data.get("spot_price", 0.0) <= 0:
        return jsonify({
            "status": "warning",
            "message": "Auto-execution blocked: Live FYERS market data stream is disconnected. Please connect FYERS in Settings to execute paper trades."
        })
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
        TradePosition.status != "OPEN"
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
    try:
        lots = int(payload.get("lots", 1))
        if lots < 1:
            return jsonify({"status": "error", "message": "Lots must be a positive integer."}), 400
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Invalid lots parameter."}), 400

    if engine_type not in ("INTRADAY", "POSITIONAL"):
        return jsonify({"status": "error", "message": f"Invalid engine_type: {engine_type}"}), 400

    SUPPORTED_STRATEGIES = {"BULL_PUT", "BEAR_CALL", "IRON_CONDOR"}
    if not strategy_id or str(strategy_id).upper() not in SUPPORTED_STRATEGIES:
        return jsonify({
            "status": "error",
            "message": f"Unsupported or disabled strategy '{strategy_id}'. Supported strategies are: {', '.join(sorted(SUPPORTED_STRATEGIES))}."
        }), 400
    strategy_id = str(strategy_id).upper()

    # Duplicate check: prevent duplicate open position for same strategy and engine
    existing = TradePosition.query.filter_by(strategy_id=strategy_id, engine_type=engine_type, status="OPEN").first()
    if existing:
        return jsonify({
            "status": "error",
            "message": f"An open position for {strategy_id} ({engine_type}) already exists (ID: #{existing.id}). Duplicate entry blocked.",
        }), 400

    chain_data = MarketDataProvider.get_option_chain()
    if chain_data.get("spot_price", 0.0) <= 0:
        return jsonify({
            "status": "error",
            "message": "Execution blocked: Live FYERS market data stream is disconnected. Please connect FYERS in Settings to execute paper trades.",
        }), 400
    analysis = MarketAnalyzer.analyze_chain(chain_data)
    chain = chain_data.get("chain_by_strike", {})

    # Evaluate via appropriate engine to respect session guards, regime, and confidence thresholds
    engine = IntradayEngine if engine_type == "INTRADAY" else PositionalEngine
    engine_candidates = engine.evaluate(analysis, chain)

    matched_candidate = None
    for cand in engine_candidates:
        if cand.strategy_id == strategy_id:
            matched_candidate = cand
            break

    if not matched_candidate:
        reasons = [cand.rationale for cand in engine_candidates if not cand.is_eligible]
        msg = f"{engine_type} engine did not generate an eligible candidate for {strategy_id}."
        if reasons:
            msg += f" Reasons: {'; '.join(reasons)}"
        return jsonify({"status": "error", "message": msg}), 400

    if not matched_candidate.is_eligible:
        return jsonify({
            "status": "error",
            "message": f"Strategy {strategy_id} is not eligible: {matched_candidate.rationale}",
        }), 400

    # Re-build candidate with requested lots if lots > 1
    if lots != matched_candidate.lots:
        if strategy_id == "BULL_PUT":
            candidate = StrategyBuilder.build_bull_put_spread(analysis, chain, engine_type=engine_type, lots=lots)
        elif strategy_id == "BEAR_CALL":
            candidate = StrategyBuilder.build_bear_call_spread(analysis, chain, engine_type=engine_type, lots=lots)
        elif strategy_id == "IRON_CONDOR":
            candidate = StrategyBuilder.build_iron_condor(analysis, chain, engine_type=engine_type, lots=lots)
        else:
            candidate = matched_candidate
    else:
        candidate = matched_candidate

    # Authoritative Execution Safety Gate
    from app.safety import ExecutionSafetyGate
    is_safe, safety_msg = ExecutionSafetyGate.validate_execution(candidate, analysis, chain)
    if not is_safe:
        return jsonify({
            "status": "blocked_by_safety_gate",
            "message": f"Execution safety gate rejected trade: {safety_msg}",
        }), 400

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
    """Explicitly saves and activates a FYERS access token."""
    from app.market_data import MarketDataProvider

    payload = request.get_json() or {}
    token = payload.get("access_token", "").strip()
    if not token:
        return jsonify({"status": "error", "message": "No access_token provided"}), 400

    saved = MarketDataProvider.set_access_token(token)
    if not saved:
        return jsonify({"status": "error", "message": "Failed saving token to disk cache."}), 500

    spot, chg, status = MarketDataProvider.get_spot_price()
    if status == "FYERS_LIVE":
        return jsonify({
            "status": "success",
            "message": f"FYERS Access Token verified! Live feed active (NIFTY Spot: ₹{spot:,.1f}).",
            "data_status": "FYERS_LIVE",
            "spot_price": spot,
        })
    else:
        return jsonify({
            "status": "warning",
            "message": "Token saved to cache, but FYERS returned invalid token or market closed. Please verify token.",
            "data_status": status,
        })


@main_bp.route("/api/fyers/connect", methods=["POST"])
def api_fyers_connect():
    """Performs automated zero-click authentication with FYERS."""
    from app.market_data import MarketDataProvider
    token = MarketDataProvider._authenticate_headless()
    if token:
        MarketDataProvider.set_access_token(token)
        spot, chg, status = MarketDataProvider.get_spot_price()
        return jsonify({
            "status": "success",
            "message": f"FYERS Zero-Click Login Successful! Live stream connected (NIFTY Spot: ₹{spot:,.1f}).",
            "data_status": status,
            "spot_price": spot,
        })
    else:
        return jsonify({
            "status": "error",
            "message": "Automated FYERS login failed or rate-limited. You can paste an access token directly below.",
        }), 400


@main_bp.route("/api/fyers/status")
def api_fyers_status():
    """Returns current live broker stream status."""
    from app.market_data import MarketDataProvider
    spot, chg, status = MarketDataProvider.get_spot_price()
    return jsonify({
        "status": "success",
        "data_status": status,
        "is_live": status == "FYERS_LIVE",
        "spot_price": spot,
        "spot_change_pct": chg,
        "token_date": MarketDataProvider._token_date,
        "has_token": bool(MarketDataProvider._access_token),
    })


@main_bp.after_request
def add_no_cache_headers(response):
    """Ensures API responses are never cached by browsers so polls always receive fresh data."""
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


