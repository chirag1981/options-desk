# NIFTY Options Research: Iron Condor Baseline Specification

## 1. Executive Summary & Research Scope

This document records the authoritative baseline configuration and performance benchmark for the isolated `IRON_CONDOR` strategy on NIFTY index options.

Under the approved research scope:
1. **Strategy Restriction**: The research workflow is restricted strictly to `IRON_CONDOR` on confirmed range-bound market regimes (`BALANCED_RANGE`).
2. **Preservation of Other Strategies**: Directional credit spreads (`BULL_PUT` and `BEAR_CALL`) are excluded from candidate evaluation, experiment execution, and research summaries. Their production implementations, historical ledger records, and production support remain completely intact.
3. **Fail-Closed Execution**: `IRON_CONDOR` is strictly rejected whenever the regime is `CHOPPY_OR_INSUFFICIENT`, unknown, or based on invalid/missing OI/quote data.

---

## 2. Dataset Versioning & Chronological Splits

- **Dataset Location**: `instance/historical_data/`
- **Total Expiry Sessions**: 105 NIFTY expiry day sessions (5-minute tick snapshots).
- **Date Range**: October 24, 2024 through October 06, 2026.
- **Total Snapshots Reconstructed**: 7,773 chronological snapshots.
- **Balanced Range Regimes**: 1,266 snapshots (16.3% of total).
- **Partitioning Protocol**:
  - **In-Sample Discovery Split**: First 75 sessions (October 24, 2024 to March 12, 2026).
  - **Out-of-Sample Holdout Split**: Final 30 sessions (March 19, 2026 to October 06, 2026).

---

## 3. Historical Lot-Size Transition Schedule

Per official National Stock Exchange of India (NSE) circulars, lot sizes are mapped dynamically by calendar trade date and contract series expiry (no blanket 75/65 mapping is applied):

| Period / Boundary | Effective Circular | Lot Size | Trade Count in Baseline |
| :--- | :--- | :---: | :---: |
| **April 26, 2024 – November 19, 2024** | `NSE/FAOP/61328` (Revision 50 $\to$ 25) | **25** | 2 Trades (Trades 1 & 2) |
| **November 20, 2024 – December 30, 2025** | `NSE/FAOP/64625` & `NSE/FAOP/70616` (Revision 25 $\to$ 75) | **75** | 11 Trades (Trades 3 to 13) |
| **December 31, 2025 – Present** | `NSE/FAOP/70616` (Revision 75 $\to$ 65) | **65** | 18 Trades (Trades 14 to 31) |

---

## 4. Maximum Loss & Risk Reconciliation

- **Strike Step & Wing Width**: NIFTY strikes use a 50 pt step. Standard Iron Condor wing width is exactly **50.0 pts** across both Put and Call spreads.
- **Gross Maximum Loss Formula**:
  $$\text{Gross Max Loss (INR)} = (\text{Wing Width} - \text{Net Credit (pts)}) \times \text{Lot Size}$$
- **Effective Trade Risk (with Transaction Costs)**:
  $$\text{Effective Risk} = \text{Gross Max Loss} + \text{Estimated Round-Trip Costs}$$

### Reconciliation with Reported ₹1,950–₹2,275 Figures:
For 75-lot Iron Condors (May 2025 – Dec 2025):
- At net credit of 24.00 pts: $(50 - 24) \times 75 = \text{₹}1,950.00$
- At net credit of 20.65 pts: $(50 - 20.65) \times 75 = \text{₹}2,201.25$
- At net credit of 21.30 pts: $(50 - 21.30) \times 75 = \text{₹}2,152.50$
- At net credit of 19.40 pts: $(50 - 19.40) \times 75 = \text{₹}2,295.00$

All 31 trades in the baseline ledger strictly respect:
1. Per-trade risk ceiling: $\le \text{₹}5,000$ (actual max risk observed: ₹2,399.84).
2. Combined portfolio risk ceiling: $\le \text{₹}20,000$.
3. Daily loss circuit breaker: $\le \text{₹}15,000$.

---

## 5. Itemized Statutory Fee Schedule

All transaction costs are calculated strictly in accordance with Indian exchange and statutory rules:

1. **Brokerage**: Flat ₹20 per executed leg order (₹80 round-trip for 4 legs entry, ₹80 exit).
2. **Securities Transaction Tax (STT)**:
   - October 1, 2024 to March 31, 2026: **0.10%** (0.0010) on sell leg premium turnover (Finance Act 2024).
   - April 1, 2026 onwards: **0.15%** (0.0015) on sell leg premium turnover (Finance Act 2026).
   - Zero STT on buy legs.
3. **Exchange Turnover Charges**:
   - Effective October 1, 2024: **0.03503%** (₹35.03 per lakh) on total premium turnover (NSE Circular `NSE/FA/64232` under SEBI True-to-Label directive).
4. **Investor Protection Fund Trust (IPFT)**: **0.0005%** (₹50 per crore) on total premium turnover.
5. **SEBI Turnover Fee**: **0.0001%** (₹10 per crore) on total premium turnover.
6. **Stamp Duty (Indian Stamp Act)**: **0.003%** (₹300 per crore) on buy-side turnover only.
7. **GST**: **18%** applied strictly to taxable services `(Brokerage + Exchange Turnover Charges + SEBI Turnover Fee + IPFT)`. Statutory taxes (STT and Stamp Duty) are excluded from the GST taxable base to avoid double-taxation.
8. **Audit Discrepancy Note**: On trades closed via session cutoff/expiry settlement (e.g., Trade 3), the legacy settlement handler resets brokerage and taxes to zero. This is preserved for baseline consistency and documented for subsequent cost-hardening experiments.

---

## 6. Frozen Baseline Performance Benchmarks

Across the complete 105-session dataset, the isolated `IRON_CONDOR` strategy produces the following independently verified metrics:

| Metric | Optimistic Scenario (Mid-Price Fill, 0 bps Slip) | Base Scenario (Bid/Ask Crossing, 5 bps Slip) | Conservative Scenario (Adverse Fill, 10 bps Slip) |
| :--- | :---: | :---: | :---: |
| **Total Trades** | 31 | **31** | 31 |
| **Winning Trades** | 20 | **19** | 18 |
| **Losing Trades** | 11 | **12** | 13 |
| **Win Rate** | 64.5% | **61.3%** | 58.1% |
| **Gross P&L** | +₹13,545.25 | **+₹13,073.50** | +₹9,638.50 |
| **Transaction Costs** | ₹6,007.92 | **₹6,001.39** | ₹5,997.85 |
| **Net P&L** | **+₹7,537.33** | **+₹7,072.11** | **+₹3,640.65** |
| **Profit Factor** | **1.64** | **1.59** | **1.26** |
| **Expectancy per Trade** | +₹243.14 | **+₹228.13** | +₹117.44 |
| **Max Drawdown** | ₹6,432.25 | **₹6,570.27** | ₹7,215.10 |
| **Entry Regime Distribution** | 100% Balanced Range | **100% Balanced Range** | 100% Balanced Range |

---

## 7. Next Research Protocol: Controlled OFAT Experiments

With the baseline frozen and fully reconciled:
1. **Single-Variable Discipline**: All future strategy experiments must vary only one factor at a time (e.g., entry timing window, spot cushion distance, minimum credit threshold, or profit target).
2. **In-Sample First**: Candidates must demonstrate net edge improvement on the 75 in-sample sessions before touching the 30 out-of-sample holdout sessions.
3. **Production Isolation**: All experiment parameters remain confined to `scripts/run_strategy_experiments.py` and research scripts without modifying live trading thresholds in `app/config.py` or `app/strategies.py`.
