"""
tests/test_signal_deduplication.py — Comprehensive Unit & Regression Tests for Signal Logging Deduplication
Verifies:
1. Identical candidates inside the heartbeat interval do not create duplicate rows.
2. A changed expiry or contract leg creates a new record.
3. ELIGIBLE to WAIT status transition creates a new record.
4. WAIT to ELIGIBLE status transition creates a new record.
5. A meaningful net-credit change (> 0.5 pts tolerance) creates a new record.
6. An insignificant net-credit change (<= 0.5 pts) inside documented tolerance does not create a duplicate.
7. Different engines (INTRADAY vs. POSITIONAL) remain completely independent.
8. A heartbeat creates a fresh record after its configured interval (15 minutes).
9. Application restart behavior recovers from DB without generating duplicate records.
10. Deduplication of signal logging never blocks explicit paper-execution requests.
11. Rejection reason changes in WAIT status trigger persistence.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import SignalCandidate, TradePosition
from app.strategies import SpreadCandidate, OptionLegSpec
from app.scheduler import SignalDeduplicator, DeskScheduler
from app.trading import PaperTradingManager

IST = ZoneInfo("Asia/Kolkata")


class DedupTestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SIGNAL_HEARTBEAT_MINS = 15
    SIGNAL_NET_CREDIT_TOLERANCE_PTS = 0.5


@pytest.fixture
def app_ctx():
    """Provides isolated in-memory test database and application context."""
    app = create_app(DedupTestConfig)
    with app.app_context():
        db.create_all()
        SignalDeduplicator.reset_cache()
        yield app
        db.session.remove()
        db.drop_all()
        SignalDeduplicator.reset_cache()


def make_candidate(
    strategy_id="IRON_CONDOR",
    engine_type="INTRADAY",
    expiry="27-OCT-2026",
    is_eligible=True,
    net_credit=20.0,
    short_strike=24350.0,
    long_strike=24300.0,
    short_strike_2=24650.0,
    long_strike_2=24700.0,
    rationale="Iron Condor 4-Leg Setup",
) -> SpreadCandidate:
    legs = []
    if short_strike is not None:
        legs.append(OptionLegSpec(symbol=f"NIFTY{int(short_strike)}PE", strike=short_strike, option_type="PE", action="SELL", bid=30.0, ask=31.0, ltp=30.5, quantity=65))
    if long_strike is not None:
        legs.append(OptionLegSpec(symbol=f"NIFTY{int(long_strike)}PE", strike=long_strike, option_type="PE", action="BUY", bid=10.0, ask=11.0, ltp=10.5, quantity=65))
    if short_strike_2 is not None:
        legs.append(OptionLegSpec(symbol=f"NIFTY{int(short_strike_2)}CE", strike=short_strike_2, option_type="CE", action="SELL", bid=25.0, ask=26.0, ltp=25.5, quantity=65))
    if long_strike_2 is not None:
        legs.append(OptionLegSpec(symbol=f"NIFTY{int(long_strike_2)}CE", strike=long_strike_2, option_type="CE", action="BUY", bid=5.6, ask=6.0, ltp=5.8, quantity=65))

    return SpreadCandidate(
        strategy_id=strategy_id,
        strategy_name="Iron Condor" if strategy_id == "IRON_CONDOR" else strategy_id,
        engine_type=engine_type,
        symbol="NIFTY",
        expiry=expiry,
        lots=1,
        quantity=65,
        legs=legs,
        net_credit_pts=net_credit,
        strike_width=50.0,
        max_profit_inr=net_credit * 65,
        max_loss_pts=30.0,
        max_loss_inr=30.0 * 65,
        risk_reward_ratio=0.67,
        breakevens=[short_strike - net_credit, short_strike_2 + net_credit] if short_strike and short_strike_2 else [],
        is_eligible=is_eligible,
        rejection_reasons=[] if is_eligible else [rationale],
        rationale=rationale,
        spot_price=24500.0,
    )


def test_identical_candidates_inside_heartbeat_do_not_duplicate(app_ctx):
    """Verifies that identical candidate evaluations within heartbeat interval are deduplicated."""
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=IST)
    cand = make_candidate()

    # Cycle 1: First evaluation -> Must persist
    should_persist, reason = SignalDeduplicator.should_persist_signal(cand, now_dt=t0)
    assert should_persist is True
    assert "INITIAL_CANDIDATE" in reason

    # Simulate DeskScheduler saving to DB & updating dedup cache
    sc = SignalCandidate(
        engine_type=cand.engine_type,
        strategy_id=cand.strategy_id,
        strategy_name=cand.strategy_name,
        status="ELIGIBLE",
        symbol=cand.symbol,
        expiry=cand.expiry,
        spot_at_eval=cand.spot_price,
        short_strike=cand.legs[0].strike,
        long_strike=cand.legs[1].strike,
        short_strike_2=cand.legs[2].strike,
        long_strike_2=cand.legs[3].strike,
        net_credit_pts=cand.net_credit_pts,
        created_at=t0,
    )
    db.session.add(sc)
    db.session.commit()
    SignalDeduplicator.record_persisted_signal(cand, now_dt=t0)

    # Cycle 2: 20 seconds later with exact same market data -> Must be deduplicated
    t1 = t0 + timedelta(seconds=20)
    should_persist_2, reason_2 = SignalDeduplicator.should_persist_signal(cand, now_dt=t1)
    assert should_persist_2 is False
    assert reason_2 == "DEDUPLICATED_UNCHANGED"

    # Cycle 3: 2 minutes later with exact same market data -> Must be deduplicated
    t2 = t0 + timedelta(minutes=2)
    should_persist_3, reason_3 = SignalDeduplicator.should_persist_signal(cand, now_dt=t2)
    assert should_persist_3 is False
    assert reason_3 == "DEDUPLICATED_UNCHANGED"

    # Assert database still contains exactly 1 row
    assert SignalCandidate.query.count() == 1


def test_changed_expiry_or_contract_legs_creates_new_record(app_ctx):
    """Verifies that changing expiry date or option strikes triggers a new signal log."""
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=IST)
    cand1 = make_candidate(expiry="27-OCT-2026", short_strike=24350.0)
    SignalDeduplicator.record_persisted_signal(cand1, now_dt=t0)

    # 1. Changed expiry -> Must persist
    cand_diff_exp = make_candidate(expiry="03-NOV-2026", short_strike=24350.0)
    should_p_exp, _ = SignalDeduplicator.should_persist_signal(cand_diff_exp, now_dt=t0 + timedelta(seconds=20))
    assert should_p_exp is True

    # 2. Changed strike (e.g. market moved 50 pts) -> Must persist
    cand_diff_strike = make_candidate(expiry="27-OCT-2026", short_strike=24400.0)
    should_p_stk, reason_stk = SignalDeduplicator.should_persist_signal(cand_diff_strike, now_dt=t0 + timedelta(seconds=20))
    assert should_p_stk is True
    assert "STRIKES_CHANGED" in reason_stk


def test_status_transitions_between_eligible_and_wait(app_ctx):
    """Verifies that status changes between ELIGIBLE and WAIT force immediate persistence."""
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=IST)
    cand_eligible = make_candidate(is_eligible=True)
    SignalDeduplicator.record_persisted_signal(cand_eligible, now_dt=t0)

    # 1. ELIGIBLE -> WAIT (e.g. regime became directional or cushion breached)
    cand_wait = make_candidate(is_eligible=False, rationale="Credit below minimum")
    should_p1, r1 = SignalDeduplicator.should_persist_signal(cand_wait, now_dt=t0 + timedelta(seconds=20))
    assert should_p1 is True
    assert "STATUS_CHANGED: ELIGIBLE -> WAIT" in r1

    # Record the WAIT state
    SignalDeduplicator.record_persisted_signal(cand_wait, now_dt=t0 + timedelta(seconds=20))

    # 2. WAIT -> ELIGIBLE (recovery)
    should_p2, r2 = SignalDeduplicator.should_persist_signal(cand_eligible, now_dt=t0 + timedelta(seconds=40))
    assert should_p2 is True
    assert "STATUS_CHANGED: WAIT -> ELIGIBLE" in r2


def test_meaningful_vs_insignificant_net_credit_change(app_ctx):
    """Verifies net credit tolerance: > 0.5 pts persists, <= 0.5 pts deduplicates."""
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=IST)
    cand_base = make_candidate(net_credit=20.0)
    SignalDeduplicator.record_persisted_signal(cand_base, now_dt=t0)

    # Insignificant change: 20.0 -> 20.25 (delta = 0.25 <= 0.50 pts) -> DEDUPLICATED
    cand_small_diff = make_candidate(net_credit=20.25)
    should_p1, r1 = SignalDeduplicator.should_persist_signal(cand_small_diff, now_dt=t0 + timedelta(seconds=20))
    assert should_p1 is False
    assert r1 == "DEDUPLICATED_UNCHANGED"

    # Material change: 20.0 -> 20.75 (delta = 0.75 > 0.50 pts) -> PERSIST
    cand_material_diff = make_candidate(net_credit=20.75)
    should_p2, r2 = SignalDeduplicator.should_persist_signal(cand_material_diff, now_dt=t0 + timedelta(seconds=40))
    assert should_p2 is True
    assert "NET_CREDIT_SHIFT" in r2


def test_rejection_reason_change_in_wait_status_persists(app_ctx):
    """Verifies that when a strategy remains in WAIT, a change in rejection reason creates a record."""
    t0 = datetime(2026, 10, 24, 9, 15, 0, tzinfo=IST)
    cand_wait1 = make_candidate(is_eligible=False, rationale="Outside intraday trading window")
    SignalDeduplicator.record_persisted_signal(cand_wait1, now_dt=t0)

    # Same reason 20s later -> Deduplicated
    cand_wait_same = make_candidate(is_eligible=False, rationale="Outside intraday trading window")
    should_p_same, _ = SignalDeduplicator.should_persist_signal(cand_wait_same, now_dt=t0 + timedelta(seconds=20))
    assert should_p_same is False

    # Shifted to new reason (e.g. session opened, but net credit is inadequate) -> Must persist
    cand_wait2 = make_candidate(is_eligible=False, rationale="Net credit below minimum 10.00 pts")
    should_p2, r2 = SignalDeduplicator.should_persist_signal(cand_wait2, now_dt=t0 + timedelta(minutes=6))
    assert should_p2 is True
    assert r2 == "REJECTION_REASON_CHANGED"


def test_different_engines_remain_independent(app_ctx):
    """Verifies that INTRADAY and POSITIONAL engines are tracked and logged independently."""
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=IST)
    cand_intraday = make_candidate(engine_type="INTRADAY")
    cand_positional = make_candidate(engine_type="POSITIONAL")

    # Cycle 1: Both engines must persist their first candidate
    p_intra, _ = SignalDeduplicator.should_persist_signal(cand_intraday, now_dt=t0)
    assert p_intra is True
    SignalDeduplicator.record_persisted_signal(cand_intraday, now_dt=t0)

    p_pos, _ = SignalDeduplicator.should_persist_signal(cand_positional, now_dt=t0)
    assert p_pos is True
    SignalDeduplicator.record_persisted_signal(cand_positional, now_dt=t0)

    # Cycle 2: Both engines evaluated 20s later -> Both deduplicated
    t1 = t0 + timedelta(seconds=20)
    p_intra_2, _ = SignalDeduplicator.should_persist_signal(cand_intraday, now_dt=t1)
    assert p_intra_2 is False

    p_pos_2, _ = SignalDeduplicator.should_persist_signal(cand_positional, now_dt=t1)
    assert p_pos_2 is False


def test_heartbeat_creates_fresh_record_after_interval(app_ctx):
    """Verifies that an unchanged candidate triggers a fresh log after the heartbeat (15m)."""
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=IST)
    cand = make_candidate()
    SignalDeduplicator.record_persisted_signal(cand, now_dt=t0)

    # At 14 minutes: inside heartbeat -> Deduplicated
    t_14m = t0 + timedelta(minutes=14)
    should_p_14, _ = SignalDeduplicator.should_persist_signal(cand, now_dt=t_14m)
    assert should_p_14 is False

    # At 15 minutes + 1 second: heartbeat expired -> Must persist
    t_15m = t0 + timedelta(minutes=15, seconds=1)
    should_p_15, r_15 = SignalDeduplicator.should_persist_signal(cand, now_dt=t_15m)
    assert should_p_15 is True
    assert "HEARTBEAT_EXPIRED" in r_15


def test_restart_recovery_from_database_prevents_duplicate(app_ctx):
    """Verifies that restarting the app (empty in-memory cache) loads the DB record and does not duplicate."""
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=IST)
    cand = make_candidate()

    # Pre-populate database as if saved before server shutdown
    sc = SignalCandidate(
        engine_type=cand.engine_type,
        strategy_id=cand.strategy_id,
        strategy_name=cand.strategy_name,
        status="ELIGIBLE",
        symbol=cand.symbol,
        expiry=cand.expiry,
        spot_at_eval=cand.spot_price,
        short_strike=cand.legs[0].strike,
        long_strike=cand.legs[1].strike,
        short_strike_2=cand.legs[2].strike,
        long_strike_2=cand.legs[3].strike,
        net_credit_pts=cand.net_credit_pts,
        rationale=cand.rationale,
        created_at=t0,
    )
    db.session.add(sc)
    db.session.commit()

    # Simulate restart by clearing in-memory cache
    SignalDeduplicator.reset_cache()

    # Evaluation 30 seconds after restart with identical candidate -> Must be deduplicated via DB lookup
    t1 = t0 + timedelta(seconds=30)
    should_persist, reason = SignalDeduplicator.should_persist_signal(cand, now_dt=t1)
    assert should_persist is False
    assert reason == "DEDUPLICATED_UNCHANGED"


def test_deduplication_never_blocks_paper_execution(app_ctx):
    """Verifies that signal deduplication does not impede paper trade execution."""
    cand = make_candidate(net_credit=20.0)

    # Candidate was deduplicated in scheduler logging
    SignalDeduplicator.record_persisted_signal(cand)
    should_persist, _ = SignalDeduplicator.should_persist_signal(cand)
    assert should_persist is False

    # Paper execution must succeed normally regardless of logging deduplication
    ok, msg, pos = PaperTradingManager.execute_candidate(cand)
    assert ok is True
    assert pos.status == "OPEN"
    assert pos.strategy_id == "IRON_CONDOR"
    assert TradePosition.query.count() == 1


def test_scheduler_run_cycle_deduplication_cadence(app_ctx, monkeypatch):
    """
    Verifies that calling DeskScheduler.run_cycle repeatedly across 5 cycles
    persists candidate signals only on Cycle 1 (2 records), deduplicating Cycles 2-5,
    demonstrating the exact fix for the 700-row inflation.
    """
    from app.market_data import MarketDataProvider

    # Freeze trading window boundaries so test runs deterministically regardless of time of day
    monkeypatch.setattr(Config, "INTRADAY_ENTRY_START", "00:00:00")
    monkeypatch.setattr(Config, "INTRADAY_ENTRY_CUTOFF", "23:59:59")

    # Execute Cycle 1 -> 2 signals persisted (1 Intraday + 1 Positional)
    DeskScheduler.run_cycle()
    count_cycle1 = SignalCandidate.query.count()
    assert count_cycle1 == 2

    # Execute Cycles 2, 3, 4, 5 with identical market chain -> Zero additional signals created
    DeskScheduler.run_cycle()
    DeskScheduler.run_cycle()
    DeskScheduler.run_cycle()
    DeskScheduler.run_cycle()
    assert SignalCandidate.query.count() == 2

    # Now simulate a market shift (e.g. spot moves by 100 pts)
    original_get_chain = MarketDataProvider.get_option_chain
    def shifted_chain(expiry=None):
        data = original_get_chain(expiry)
        data["spot_price"] = 24650.0
        data["chain_by_strike"] = MarketDataProvider._generate_mock_chain(24650.0)
        return data

    monkeypatch.setattr(MarketDataProvider, "get_option_chain", shifted_chain)

    # Execute Cycle 6 with shifted market data -> Strikes change, so 2 new signals are persisted
    DeskScheduler.run_cycle()
    assert SignalCandidate.query.count() == 4


def test_repeated_small_credit_changes_accumulate_and_trigger_persistence(app_ctx):
    """
    Verifies that small credit increments (+0.2 pts) cannot drift indefinitely:
    Once cumulative drift exceeds the 0.50 pt tolerance from the last persisted baseline,
    a new record is triggered.
    """
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=IST)
    cand_base = make_candidate(net_credit=20.0)
    SignalDeduplicator.record_persisted_signal(cand_base, now_dt=t0)

    # Tick 1: +0.20 pts (20.20) -> Delta 0.20 <= 0.50 -> Suppressed, baseline remains 20.0
    cand_1 = make_candidate(net_credit=20.20)
    p1, r1 = SignalDeduplicator.should_persist_signal(cand_1, now_dt=t0 + timedelta(seconds=20))
    assert p1 is False

    # Tick 2: +0.20 pts (20.40) -> Delta 0.40 <= 0.50 -> Suppressed, baseline remains 20.0
    cand_2 = make_candidate(net_credit=20.40)
    p2, r2 = SignalDeduplicator.should_persist_signal(cand_2, now_dt=t0 + timedelta(seconds=40))
    assert p2 is False

    # Tick 3: +0.20 pts (20.60) -> Cumulative delta from baseline is 0.60 > 0.50 -> MUST PERSIST!
    cand_3 = make_candidate(net_credit=20.60)
    p3, r3 = SignalDeduplicator.should_persist_signal(cand_3, now_dt=t0 + timedelta(seconds=60))
    assert p3 is True
    assert "NET_CREDIT_SHIFT" in r3


def test_malformed_historical_record_fails_safely(app_ctx, monkeypatch):
    """Verifies that an unreadable/malformed DB query or exception falls back safely to logging the candidate."""
    cand = make_candidate()
    SignalDeduplicator.reset_cache()

    # Simulate database error on query
    def broken_filter_by(**kwargs):
        raise RuntimeError("Simulated database read failure")

    monkeypatch.setattr(SignalCandidate.query, "filter_by", broken_filter_by)

    # Should safely catch error, log warning, and allow persistence without crashing
    should_persist, reason = SignalDeduplicator.should_persist_signal(cand)
    assert should_persist is True
    assert reason == "INITIAL_CANDIDATE"


