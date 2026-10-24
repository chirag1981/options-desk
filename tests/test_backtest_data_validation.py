"""
tests/test_backtest_data_validation.py — Acceptance Tests for Historical Data Ingestion & Validation.
"""

import os
import tempfile
import pytest
import pandas as pd

from app.backtesting.data_validator import (
    HistoricalDataValidator,
    HISTORICAL_SCHEMA_COLUMNS,
)
from app.analysis import MarketAnalyzer
from app.strategies import AutoStrategySelector


def _generate_sample_dataframe(num_timestamps=3, strikes_count=9, with_delta_oi=True, crossed_quote=False):
    """Generates synthetic tabular test data conforming to the documented historical schema."""
    rows = []
    base_spot = 24500.0
    expiry = "27-OCT-2026"

    for t_idx in range(num_timestamps):
        ts_str = f"2026-10-24 09:{15 + t_idx * 5:02d}:00"
        spot = base_spot + (t_idx * 10.0)

        for i in range(-strikes_count // 2 + 1, strikes_count // 2 + 1):
            strike = 24500.0 + (i * 50.0)
            ce_ltp = max(1.0, 80.0 - (i * 12.0))
            pe_ltp = max(1.0, 80.0 + (i * 12.0))

            ce_bid = ce_ltp - 0.5
            ce_ask = ce_ltp + 0.5
            pe_bid = pe_ltp - 0.5
            pe_ask = pe_ltp + 0.5

            if crossed_quote and t_idx == 0 and i == 0:
                ce_bid = 100.0
                ce_ask = 90.0  # Crossed!

            ce_oi = 50000 + abs(i) * 1000
            pe_oi = 52000 + abs(i) * 1000
            ce_chg = 3000 if with_delta_oi else None
            pe_chg = 4500 if with_delta_oi else None

            # CE row
            rows.append({
                "timestamp": ts_str,
                "spot_price": spot,
                "spot_change_pct": 0.25,
                "expiry": expiry,
                "strike": strike,
                "option_type": "CE",
                "ltp": ce_ltp,
                "bid": ce_bid,
                "ask": ce_ask,
                "oi": ce_oi,
                "change_oi": ce_chg,
                "volume": 12000,
                "iv": 14.5,
                "symbol": f"NIFTY{int(strike)}CE",
            })
            # PE row
            rows.append({
                "timestamp": ts_str,
                "spot_price": spot,
                "spot_change_pct": 0.25,
                "expiry": expiry,
                "strike": strike,
                "option_type": "PE",
                "ltp": pe_ltp,
                "bid": pe_bid,
                "ask": pe_ask,
                "oi": pe_oi,
                "change_oi": pe_chg,
                "volume": 14000,
                "iv": 15.0,
                "symbol": f"NIFTY{int(strike)}PE",
            })

    return pd.DataFrame(rows)


def test_schema_definition_contains_mandatory_fields():
    required = HISTORICAL_SCHEMA_COLUMNS["required"]
    for col in ["timestamp", "spot_price", "expiry", "strike", "option_type", "ltp", "bid", "ask", "oi"]:
        assert col in required


def test_valid_csv_and_parquet_acceptance():
    df = _generate_sample_dataframe(num_timestamps=4, strikes_count=9)

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = os.path.join(tmpdir, "sample_options.csv")
        parquet_path = os.path.join(tmpdir, "sample_options.parquet")

        df.to_csv(csv_path, index=False)
        df.to_parquet(parquet_path, index=False)

        # 1. Test CSV validation
        ok_csv, report_csv, records_csv = HistoricalDataValidator.validate_file(csv_path)
        assert ok_csv is True
        assert report_csv.is_acceptable_for_backtest is True
        assert report_csv.valid_rows == len(df)
        assert report_csv.invalid_rows == 0
        assert report_csv.unique_timestamps == 4
        assert report_csv.reconstructable_snapshots == 4
        assert report_csv.executable_bid_ask_pct == 100.0

        # 2. Test Parquet validation
        ok_pq, report_pq, records_pq = HistoricalDataValidator.validate_file(parquet_path)
        assert ok_pq is True
        assert report_pq.is_acceptable_for_backtest is True
        assert report_pq.valid_rows == len(df)
        assert report_pq.reconstructable_snapshots == 4


def test_missing_required_column_rejected():
    df = _generate_sample_dataframe(num_timestamps=2)
    df_missing = df.drop(columns=["bid"])

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = os.path.join(tmpdir, "missing_bid.csv")
        df_missing.to_csv(csv_path, index=False)

        ok, report, records = HistoricalDataValidator.validate_file(csv_path)
        assert ok is False
        assert report.is_acceptable_for_backtest is False
        assert any("Missing required columns: bid" in r for r in report.rejection_reasons)


def test_crossed_quotes_rejected_and_reported():
    df = _generate_sample_dataframe(num_timestamps=2, crossed_quote=True)
    ok, report, records = HistoricalDataValidator.validate_dataframe(df)

    assert report.crossed_quotes_count > 0
    assert report.invalid_rows > 0
    assert any("crossed quotes" in w for w in report.warnings)


def test_missing_delta_oi_warned_and_supported():
    df = _generate_sample_dataframe(num_timestamps=2, with_delta_oi=False)
    ok, report, records = HistoricalDataValidator.validate_dataframe(df)

    assert ok is True
    assert report.change_oi_coverage_pct == 0.0
    assert any("ΔOI" in w for w in report.warnings)


def test_non_chronological_records_detected():
    df = _generate_sample_dataframe(num_timestamps=3)
    # Reverse rows to break chronological ordering
    df_reversed = df.iloc[::-1].reset_index(drop=True)

    ok, report, records = HistoricalDataValidator.validate_dataframe(df_reversed)
    assert report.non_chronological_records > 0
    assert any("chronological order" in w for w in report.warnings)


def test_snapshot_reconstruction_and_production_analysis_replay():
    df = _generate_sample_dataframe(num_timestamps=2, strikes_count=11)
    ok, report, records = HistoricalDataValidator.validate_dataframe(df)
    assert ok is True

    snapshots, incomplete_rejs, inconsistent_spots, fillable_count = HistoricalDataValidator.reconstruct_snapshots(records)
    assert len(snapshots) == 2
    assert incomplete_rejs == 0
    assert inconsistent_spots == 0
    assert fillable_count == 2

    # Feed reconstructed point-in-time snapshot directly into frozen production analyzer
    snap1 = snapshots[0]
    analysis = MarketAnalyzer.analyze_chain(snap1)
    assert analysis.spot_price == snap1["spot_price"]
    assert analysis.market_regime in ("BALANCED_RANGE", "TRENDING_BULLISH", "MILDLY_BULLISH", "MILDLY_BEARISH", "CHOPPY_OR_INSUFFICIENT")
    assert analysis.data_status == "HISTORICAL_REPLAY"

    # Evaluate strategy selector on reconstructed chain
    auto_res = AutoStrategySelector.evaluate_auto_selection(analysis, snap1["chain_by_strike"])
    assert auto_res.selection_type in ("SINGLE", "MULTIPLE", "NONE")
    assert auto_res.timestamp is not None


def test_missing_values_preserve_none_distinguishable_from_zero():
    # DataFrame where IV and Volume are None/empty
    df = _generate_sample_dataframe(num_timestamps=1, strikes_count=7)
    df["iv"] = None
    df["volume"] = None
    df["spot_change_pct"] = None

    ok, report, records = HistoricalDataValidator.validate_dataframe(df)
    assert ok is True
    assert records[0]["iv"] is None
    assert records[0]["volume"] is None
    assert records[0]["spot_change_pct"] is None

    snapshots, _, _, _ = HistoricalDataValidator.reconstruct_snapshots(records)
    opt_quote = snapshots[0]["chain_by_strike"][24500.0]["CE"]
    assert opt_quote["iv"] is None
    assert opt_quote["volume"] is None


def test_inconsistent_spot_price_rejected():
    df = _generate_sample_dataframe(num_timestamps=1, strikes_count=7)
    # Make one row have a different spot price at identical timestamp
    df.loc[0, "spot_price"] = 25000.0  # Conflicting spot!

    ok, report, records = HistoricalDataValidator.validate_dataframe(df)
    assert report.inconsistent_spot_snapshots == 1
    assert report.reconstructable_snapshots == 0
    assert any("conflicting spot" in w for w in report.warnings)


def test_incomplete_snapshot_rejected():
    # Only 2 strikes (lacks minimum 5 paired strikes and OTM wings)
    df = _generate_sample_dataframe(num_timestamps=1, strikes_count=2)
    ok, report, records = HistoricalDataValidator.validate_dataframe(df)

    assert report.incomplete_snapshots_rejected == 1
    assert report.reconstructable_snapshots == 0
    assert any("lacked complete ATM/wing coverage" in w for w in report.warnings)

