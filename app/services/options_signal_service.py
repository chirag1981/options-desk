"""
app/services/options_signal_service.py — Automated Signal Lifecycle & Paper Trading Journal
Persists generated options signals, evaluates live P&L on background refresh cycles,
tracks Target 1, Target 2, Stop Loss, and EOD exits, and provides comprehensive performance metrics.
"""

import os
import sqlite3
import logging
from datetime import datetime, time as dtime
from app.services.fyers_options_service import fetch_option_chain_data, INDEX_CONFIGS

log = logging.getLogger("options_signal_service")

# Database Path
DB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../instance"))
DB_PATH = os.path.join(DB_DIR, "options_signals.db")


def _get_db():
    """Returns a SQLite connection with dict-like row factory."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_signal_db():
    """Initializes SQLite schema for options signals and paper trades."""
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                contract_name TEXT NOT NULL,
                strike REAL NOT NULL,
                expiry TEXT,
                spot_at_entry REAL NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                target_1 REAL NOT NULL,
                target_2 REAL NOT NULL,
                risk_reward TEXT DEFAULT '1:2.0',
                setup_score INTEGER DEFAULT 0,
                trigger_reason TEXT,
                status TEXT DEFAULT 'ACTIVE',
                current_price REAL NOT NULL,
                highest_price REAL NOT NULL,
                lowest_price REAL NOT NULL,
                exit_price REAL,
                exit_time TEXT,
                points_pnl REAL DEFAULT 0.0,
                pnl_pct REAL DEFAULT 0.0,
                lots INTEGER DEFAULT 1,
                lot_size INTEGER DEFAULT 25,
                net_pnl_inr REAL DEFAULT 0.0,
                is_paper_trade INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)")
        conn.commit()


# Initialize table on module import
try:
    init_signal_db()
except Exception as e:
    log.error(f"Failed to initialize signals database: {e}")


def record_signal(signal_data: dict, is_paper_trade: bool = False, lots: int = 1) -> dict | None:
    """
    Records a new signal into the journal.
    Prevents duplicates for the same symbol & contract within 15 minutes.
    """
    symbol = signal_data.get("symbol", "NIFTY").upper()
    signal_type = signal_data.get("type") or signal_data.get("signal_type", "CE")
    strike = float(signal_data.get("strike", 0.0))
    contract_name = signal_data.get("contract_name") or f"{int(strike)} {signal_type}"
    expiry = signal_data.get("expiry", "")
    spot_at_entry = float(signal_data.get("spot_price") or signal_data.get("spot_at_entry", 0.0))
    entry_price = float(signal_data.get("entry_price", 0.0))
    stop_loss = float(signal_data.get("stop_loss", 0.0))
    target_1 = float(signal_data.get("target_1", 0.0))
    target_2 = float(signal_data.get("target_2", 0.0))
    risk_reward = signal_data.get("risk_reward", "1:2.0")
    setup_score = int(signal_data.get("setup_score", 0))
    trigger_reason = signal_data.get("reason") or signal_data.get("trigger_reason", "")

    if entry_price <= 0:
        return None

    cfg = INDEX_CONFIGS.get(symbol, INDEX_CONFIGS["NIFTY"])
    lot_size = cfg.get("lot_size", 25)

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Anti-duplicate and Cooldown check:
    # 1. Reject if same symbol & contract is currently ACTIVE / TARGET_1_HIT
    # 2. Reject if same symbol & contract was closed within the last 15 minutes (cooldown window)
    with _get_db() as conn:
        recent = conn.execute("""
            SELECT id, created_at, status FROM signals 
            WHERE symbol = ? AND contract_name = ?
            ORDER BY id DESC LIMIT 1
        """, (symbol, contract_name)).fetchone()

        if recent and not is_paper_trade:
            # Check if active
            if recent["status"] in ('ACTIVE', 'TARGET_1_HIT'):
                return None
            
            # Check 15-minute cooldown on closed trades
            try:
                prev_created = datetime.strptime(recent["created_at"], "%Y-%m-%d %H:%M:%S")
                if (now - prev_created).total_seconds() < 900:  # 15 minutes
                    log.info(f"Signal suppressed for {symbol} {contract_name}: 15-minute cooldown active.")
                    return None
            except Exception:
                pass

        cursor = conn.execute("""
            INSERT INTO signals (
                symbol, signal_type, contract_name, strike, expiry, spot_at_entry,
                entry_price, stop_loss, target_1, target_2, risk_reward, setup_score,
                trigger_reason, status, current_price, highest_price, lowest_price,
                points_pnl, pnl_pct, lots, lot_size, net_pnl_inr, is_paper_trade,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, 0.0, 0.0, ?, ?, 0.0, ?, ?, ?)
        """, (
            symbol, signal_type, contract_name, strike, expiry, spot_at_entry,
            entry_price, stop_loss, target_1, target_2, risk_reward, setup_score,
            trigger_reason, entry_price, entry_price, entry_price,
            lots, lot_size, 1 if is_paper_trade else 0,
            now_str, now_str
        ))
        conn.commit()
        new_id = cursor.lastrowid

    log.info(f"Recorded new signal #{new_id}: {symbol} {contract_name} @ {entry_price}")
    return get_signal_by_id(new_id)


def get_signal_by_id(signal_id: int) -> dict | None:
    """Retrieves a single signal by its primary key ID."""
    with _get_db() as conn:
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return dict(row) if row else None


def update_active_signals(market_data_cache: dict | None = None) -> list[dict]:
    """
    Background Evaluator:
    Iterates through all open signals (status IN ('ACTIVE', 'TARGET_1_HIT')),
    fetches current option LTP, updates high/low watermarks, computes live P&L,
    and checks if Target 1, Target 2, Stop Loss, or EOD exit triggered.
    """
    with _get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM signals 
            WHERE status IN ('ACTIVE', 'TARGET_1_HIT')
        """).fetchall()

    if not rows:
        return []

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_now = now.time()
    is_eod = (time_now >= dtime(15, 20)) # 03:20 PM IST session close square-off

    updated_signals = []

    for r in rows:
        sig = dict(r)
        sig_id = sig["id"]
        sym = sig["symbol"]
        strike = float(sig["strike"])
        sig_type = sig["signal_type"]
        entry = float(sig["entry_price"])
        t1 = float(sig["target_1"])
        t2 = float(sig["target_2"])
        sl = float(sig["stop_loss"])
        lot_size = int(sig["lot_size"])
        lots = int(sig["lots"])
        curr_status = sig["status"]

        # 1. Fetch current LTP for this specific contract
        current_ltp = None
        if market_data_cache and sym in market_data_cache:
            chain = market_data_cache[sym].get("chain", [])
            for item in chain:
                if abs(item["strike"] - strike) < 1.0:
                    current_ltp = item.get("ce_ltp" if sig_type == "CE" else "pe_ltp")
                    break
        
        if current_ltp is None or current_ltp <= 0:
            try:
                raw = fetch_option_chain_data(symbol=sym, expiry=sig.get("expiry"), force_refresh=False)
                for item in raw.get("chain", []):
                    if abs(item["strike"] - strike) < 1.0:
                        current_ltp = item.get("ce_ltp" if sig_type == "CE" else "pe_ltp")
                        break
            except Exception as e:
                log.warning(f"Could not fetch LTP for signal #{sig_id}: {e}")

        if current_ltp is None or current_ltp <= 0:
            current_ltp = sig["current_price"]

        highest = max(float(sig["highest_price"]), current_ltp)
        lowest = min(float(sig["lowest_price"]), current_ltp)

        new_status = curr_status
        exit_price = sig["exit_price"]
        exit_time = sig["exit_time"]
        trailed_sl = sl

        # Calculate time elapsed for Stagnation Stop (P3)
        try:
            created_dt = datetime.strptime(sig["created_at"], "%Y-%m-%d %H:%M:%S")
            elapsed_mins = (now - created_dt).total_seconds() / 60.0
        except Exception:
            elapsed_mins = 0.0

        # P1 & P4: Handle Target 1 reached
        if current_ltp >= t1 and curr_status == "ACTIVE":
            new_status = "TARGET_1_HIT"
            curr_status = "TARGET_1_HIT"
            # P1: Trail Stop Loss to Entry + Buffer (Cost Protection)
            trailed_sl = round(max(sl, entry + 1.0), 2)

        # 2. Check Exits (Target 2, Trailing SL, Standard SL, Stagnation Time Stop, EOD)
        if current_ltp >= t2:
            new_status = "TARGET_2_HIT"
            raw_exit = round(max(t2, current_ltp), 2)
            # P4: 50% booked at T1 + 50% booked at T2
            points_pnl = round(0.5 * (t1 - entry) + 0.5 * (raw_exit - entry), 2)
            exit_price = round((t1 + raw_exit) / 2.0, 2)
            exit_time = now_str
        elif curr_status == "TARGET_1_HIT" and current_ltp <= trailed_sl:
            # Trailed Stop Loss hit after T1 -> Locks in guaranteed 50% profit from T1
            new_status = "TSL_HIT"
            points_pnl = round(0.5 * (t1 - entry), 2)
            exit_price = round((t1 + entry) / 2.0, 2)
            exit_time = now_str
        elif curr_status == "ACTIVE" and current_ltp <= sl:
            new_status = "SL_HIT"
            slippage = round(sl * 0.02, 2)
            exit_price = round(max(sl - slippage, min(sl, current_ltp)), 2)
            points_pnl = round(exit_price - entry, 2)
            exit_time = now_str
        elif curr_status == "ACTIVE":
            # P3: Dynamic Stagnation / Time Stop
            # Morning (before 13:00): 25 mins max stagnation window
            # Afternoon (post 13:00): 12 mins accelerated window to protect from afternoon theta crush
            max_allowed_stagnation = 12.0 if time_now >= dtime(13, 0) else 25.0
            if elapsed_mins >= max_allowed_stagnation and (current_ltp - entry) < ((t1 - entry) * 0.25):
                new_status = "TIME_STOP_EXIT"
                exit_price = current_ltp
                points_pnl = round(current_ltp - entry, 2)
                exit_time = now_str
        elif is_eod and curr_status in ("ACTIVE", "TARGET_1_HIT"):
            new_status = "EOD_CLOSED"
            exit_price = current_ltp
            if curr_status == "TARGET_1_HIT":
                points_pnl = round(0.5 * (t1 - entry) + 0.5 * (current_ltp - entry), 2)
            else:
                points_pnl = round(current_ltp - entry, 2)
            exit_time = now_str
        else:
            # Live open position P&L
            if curr_status == "TARGET_1_HIT":
                # P4: 50% realized from T1 + 50% live on current LTP
                points_pnl = round(0.5 * (t1 - entry) + 0.5 * (current_ltp - entry), 2)
                trailed_sl = round(max(sl, entry + 1.0), 2)
            else:
                points_pnl = round(current_ltp - entry, 2)

        pnl_pct = round((points_pnl / entry) * 100, 2) if entry > 0 else 0.0
        net_inr = round(points_pnl * lot_size * lots, 2)

        # 3. Persist update (including updated trailed stop_loss)
        with _get_db() as conn:
            conn.execute("""
                UPDATE signals
                SET current_price = ?, highest_price = ?, lowest_price = ?,
                    stop_loss = ?, points_pnl = ?, pnl_pct = ?, net_pnl_inr = ?,
                    status = ?, exit_price = ?, exit_time = ?, updated_at = ?
                WHERE id = ?
            """, (
                current_ltp, highest, lowest,
                trailed_sl, points_pnl, pnl_pct, net_inr,
                new_status, exit_price, exit_time, now_str,
                sig_id
            ))
            conn.commit()

        sig.update({
            "current_price": current_ltp,
            "highest_price": highest,
            "lowest_price": lowest,
            "stop_loss": trailed_sl,
            "points_pnl": points_pnl,
            "pnl_pct": pnl_pct,
            "net_pnl_inr": net_inr,
            "status": new_status,
            "exit_price": exit_price,
            "exit_time": exit_time,
            "updated_at": now_str
        })
        updated_signals.append(sig)

    return updated_signals


def close_signal_manually(signal_id: int) -> dict | None:
    """Manually squares off an active signal at its current price."""
    sig = get_signal_by_id(signal_id)
    if not sig or sig["status"] not in ("ACTIVE", "TARGET_1_HIT"):
        return sig

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = float(sig["entry_price"])
    t1 = float(sig["target_1"])
    current_ltp = float(sig["current_price"])

    if sig["status"] == "TARGET_1_HIT":
        # 50% was booked at T1 + 50% manually squared off at current LTP
        points_pnl = round(0.5 * (t1 - entry) + 0.5 * (current_ltp - entry), 2)
        exit_price = round((t1 + current_ltp) / 2.0, 2)
    else:
        points_pnl = round(current_ltp - entry, 2)
        exit_price = current_ltp

    pnl_pct = round((points_pnl / entry) * 100, 2) if entry > 0 else 0.0
    net_inr = round(points_pnl * sig["lot_size"] * sig["lots"], 2)

    with _get_db() as conn:
        conn.execute("""
            UPDATE signals
            SET status = 'MANUALLY_CLOSED', exit_price = ?, exit_time = ?,
                points_pnl = ?, pnl_pct = ?, net_pnl_inr = ?, updated_at = ?
            WHERE id = ?
        """, (exit_price, now_str, points_pnl, pnl_pct, net_inr, now_str, signal_id))
        conn.commit()

    return get_signal_by_id(signal_id)


def delete_signal(signal_id: int) -> bool:
    """Deletes a signal record from SQLite."""
    with _get_db() as conn:
        conn.execute("DELETE FROM signals WHERE id = ?", (signal_id,))
        conn.commit()
    return True


def clear_history() -> bool:
    """Deletes all completed/closed signals from database."""
    with _get_db() as conn:
        conn.execute("DELETE FROM signals WHERE status NOT IN ('ACTIVE', 'TARGET_1_HIT')")
        conn.commit()
    return True


def get_signals_summary() -> dict:
    """
    Returns active signals, closed signals, and comprehensive performance metrics:
    - Total Trades, Win Rate %, Profit Factor, Total Points P&L, Net INR Returns.
    """
    with _get_db() as conn:
        all_rows = conn.execute("SELECT * FROM signals ORDER BY id DESC").fetchall()

    active_signals = []
    closed_signals = []

    win_count = 0
    loss_count = 0
    be_count = 0
    total_points = 0.0
    total_inr = 0.0
    gross_profit_pts = 0.0
    gross_loss_pts = 0.0

    active_points = 0.0
    active_inr = 0.0

    for row in all_rows:
        sig = dict(row)
        if sig["status"] in ("ACTIVE", "TARGET_1_HIT"):
            active_signals.append(sig)
            active_points += float(sig["points_pnl"] or 0.0)
            active_inr += float(sig["net_pnl_inr"] or 0.0)
        else:
            closed_signals.append(sig)
            pts = float(sig["points_pnl"] or 0.0)
            inr = float(sig["net_pnl_inr"] or 0.0)
            total_points += pts
            total_inr += inr
            if pts > 0:
                win_count += 1
                gross_profit_pts += pts
            elif pts < 0:
                loss_count += 1
                gross_loss_pts += abs(pts)
            else:
                be_count += 1

    closed_count = len(closed_signals)
    win_rate = round((win_count / closed_count * 100), 1) if closed_count > 0 else None
    
    if gross_loss_pts > 0:
        profit_factor = round(gross_profit_pts / gross_loss_pts, 2)
    elif gross_profit_pts > 0:
        profit_factor = round(gross_profit_pts, 2)
    elif closed_count == 0 and len(active_signals) > 0:
        profit_factor = None
    else:
        profit_factor = 1.0

    avg_win = round(gross_profit_pts / win_count, 1) if win_count > 0 else 0.0
    avg_loss = round(gross_loss_pts / loss_count, 1) if loss_count > 0 else 0.0

    return {
        "metrics": {
            "total_signals": len(all_rows),
            "active_count": len(active_signals),
            "closed_count": closed_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "be_count": be_count,
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "realized_pnl_points": round(total_points, 1),
            "realized_pnl_inr": round(total_inr, 2),
            "unrealized_pnl_points": round(active_points, 1),
            "unrealized_pnl_inr": round(active_inr, 2),
            "total_pnl_points": round(total_points + active_points, 1),
            "total_pnl_inr": round(total_inr + active_inr, 2),
            "avg_win_pts": avg_win,
            "avg_loss_pts": avg_loss,
            "gross_profit_pts": round(gross_profit_pts, 1),
            "gross_loss_pts": round(gross_loss_pts, 1),
        },
        "active_signals": active_signals,
        "history_signals": closed_signals[:100],  # Most recent 100 closed
    }

