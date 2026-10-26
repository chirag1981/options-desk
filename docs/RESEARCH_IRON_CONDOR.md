# NIFTY Options Research: Iron Condor Baseline Specification

## 1. Executive Summary & Research Scope

This document records the authoritative baseline configuration and performance benchmark for the isolated `IRON_CONDOR` strategy on NIFTY index options.

Under the approved research scope:
1. **Strategy Restriction**: The research workflow is restricted strictly to `IRON_CONDOR` on confirmed range-bound market regimes (`BALANCED_RANGE`).
2. **Removal of Directional Strategies**: Directional credit spreads (`BULL_PUT` and `BEAR_CALL`) have been completely removed from the project; the desk is 100% focused on Iron Condor.
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

## 7. Stage A — Settlement Accounting Defect Fix & Versioned Corrected Baseline

### Investigation of Settlement Defect (Trade 3)
- **Trade ID**: `BT-BAS-0003` (Executed May 15, 2025; 75 qty; entry 10:50:00).
- **Exit Event**: Parquet data ended at 13:20:00 (midday session cutoff).
- **Root Cause**: In `HistoricalBacktestEngine._close_trade_at_settlement`, lines 245–247 assigned `trade.total_brokerage = 0.0` and `trade.total_taxes = 0.0`. This inadvertently wiped out the ₹80.00 entry brokerage and ₹29.80 entry statutory taxes that were incurred at trade inception, and failed to compute exit transaction costs for market square-off at session cutoff.
- **Resolution**:
  - `_close_trade_at_settlement` was updated to reconstruct closing spread legs and compute full itemized statutory fees (`TransactionCostEngine.calculate_order_costs_breakdown`).
  - Added exit costs to `trade.total_brokerage` and `trade.total_taxes` instead of overwriting them.
  - Frozen baseline benchmark (`4ca238a`) is preserved as the immutable control record.

### Quantitative Comparison: Frozen vs. Corrected Baseline (Full 105 Sessions)

| Execution Scenario | Frozen Gross P&L | Frozen Costs | Frozen Net P&L | Corrected Gross P&L | Corrected Costs | Corrected Net P&L | Delta Costs | Delta Net P&L | Corrected PF |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **OPTIMISTIC** | +₹13,545.25 | ₹6,007.92 | +₹7,537.33 | +₹13,545.25 | ₹6,229.98 | **+₹7,315.27** | +₹222.06 | -₹222.06 | **1.61** |
| **BASE** | +₹13,073.50 | ₹6,001.39 | +₹7,072.11 | +₹13,073.50 | ₹6,223.40 | **+₹6,850.10** | +₹222.01 | -₹222.01 | **1.56** |
| **CONSERVATIVE** | +₹9,638.50 | ₹5,997.85 | +₹3,640.65 | +₹9,638.50 | ₹6,219.80 | **+₹3,418.70** | +₹221.95 | -₹221.95 | **1.24** |

*Note: All delta amounts correspond with exact paisa precision to Trade 3 exit square-off and entry fee restoration (₹160.00 brokerage + ₹62.01 statutory taxes).*

---

## 8. Stage B — Independent Holdout Validation (Discovery vs. Holdout)

Using the corrected trade ledger, the dataset was strictly separated into Discovery (first 75 sessions, Oct 24, 2024 to Mar 24, 2026) and Holdout (final 30 sessions, Mar 30, 2026 to Oct 19, 2026) without inferring holdout metrics from aggregate totals.

### Period Performance Metrics

| Metric | Discovery Split (75 Sessions: Oct 2024 – Mar 2026) | Holdout Split (30 Sessions: Mar 2026 – Oct 2026) | Full Period Combined (105 Sessions) |
| :--- | :---: | :---: | :---: |
| **Total Trades** | 17 | 14 | 31 |
| **Distinct Sessions Traded** | 17 | 14 | 31 |
| **Win / Loss Record (Base)** | 11 Wins / 6 Losses | 8 Wins / 6 Losses | 19 Wins / 12 Losses |
| **Win Rate (Base)** | **64.7%** | **57.1%** | **61.3%** |
| **Gross P&L (Base)** | +₹8,835.50 | +₹4,238.00 | +₹13,073.50 |
| **Modeled Costs (Base)** | ₹3,400.60 | ₹2,822.80 | ₹6,223.40 |
| **Net P&L (Base)** | **+₹5,434.90** | **+₹1,415.20** | **+₹6,850.10** |
| **Expectancy / Trade (Base)** | **+₹319.70** | **+₹101.09** | **+₹220.97** |
| **Profit Factor (Base)** | **1.92** | **1.23** | **1.56** |
| **Max Drawdown (Base)** | ₹1,677.01 | ₹5,221.39 | ₹6,570.27 |
| **Max Losing Streak (Base)** | 2 consecutive losses | 4 consecutive losses | 6 consecutive losses |

### Execution Scenario Sensitivity by Period

#### 1. Discovery Split (75 Sessions)
- **OPTIMISTIC**: 17 Trades | 70.6% Win Rate | Gross: +₹8,969.25 | Costs: ₹3,403.93 | **Net: +₹5,565.32 | PF: 1.94** | Exp: +₹327.37 | Max DD: ₹1,692.28
- **BASE**: 17 Trades | 64.7% Win Rate | Gross: +₹8,835.50 | Costs: ₹3,400.60 | **Net: +₹5,434.90 | PF: 1.92** | Exp: +₹319.70 | Max DD: ₹1,677.01
- **CONSERVATIVE**: 17 Trades | 64.7% Win Rate | Gross: +₹8,540.00 | Costs: ₹3,394.67 | **Net: +₹5,145.33 | PF: 1.84** | Exp: +₹302.67 | Max DD: ₹1,729.45

#### 2. Holdout Split (30 Sessions)
- **OPTIMISTIC**: 14 Trades | 57.1% Win Rate | Gross: +₹4,576.00 | Costs: ₹2,826.05 | **Net: +₹1,749.95 | PF: 1.29** | Exp: +₹125.00 | Max DD: ₹5,165.64
- **BASE**: 14 Trades | 57.1% Win Rate | Gross: +₹4,238.00 | Costs: ₹2,822.80 | **Net: +₹1,415.20 | PF: 1.23** | Exp: +₹101.09 | Max DD: ₹5,221.39
- **CONSERVATIVE**: 14 Trades | 50.0% Win Rate | Gross: +₹1,098.50 | Costs: ₹2,825.13 | **Net: -₹1,726.63 | PF: 0.79** | Exp: -₹123.33 | Max DD: ₹5,503.98

### Key Critical Findings for Stage C Optimization
1. **Edge Decay in Holdout**: Net expectancy drops from **+₹319.70** in Discovery to **+₹101.09** in Holdout (Base scenario).
2. **Fragility to Execution Friction**: In Holdout, under Conservative assumptions (10 bps adverse fill per leg), the strategy net P&L turns negative (-₹1,726.63, 0.79 PF) due to ₹2,825.13 in transaction costs and slippage consuming 100% of the gross profit (+₹1,098.50).
3. **Core Vulnerability**: Iron Condors entering with low net credit (< 24 pts) do not generate sufficient profit cushion relative to the fixed ₹160 brokerage + statutory taxes + 4-leg crossing costs.

---

## 9. Phase 2 Stage 2 — Trade-Level Execution Analysis

Across all 31 baseline trades:
- **Strikes & Wing Width**: All trades deployed 50.0 pt wings on both Put and Call sides.
- **Entry Regime Evidence**: 100% (31/31) trades entered with confirmed `BALANCED_RANGE` regime evidence. Entry spot cushion ranged between 21.05 and 48.55 pts (median 33.45 pts).
- **Cost Impact**:
  - Total Base Costs: ₹6,223.40 (Brokerage: ₹4,800.00, Statutory Taxes: ₹1,423.40).
  - Total Conservative Costs: ₹6,219.80 (Brokerage: ₹4,800.00, Statutory Taxes: ₹1,419.80).
  - Brokerage and taxes are virtually invariant between scenarios (governed by trade count and contract quantity).
- **Flipped Trades (Winner $\to$ Loser)**:
  - Exactly **1 trade** flipped across scenarios: `BT-BAS-0024` / `BT-CON-0024` (June 30, 2026; 65 qty; entered 11:30:00).
  - In Base: Exit debit reached 7.40 pts at 14:50:00, successfully triggering the 70% profit target for **+₹1,020.94 Net P&L**.
  - In Conservative: Due to adverse bid/ask crossing, the spread cost at 14:50:00 remained above the target threshold, leaving the position open until the 15:00:00 intraday cutoff where spread cost widened to 44.25 pts, closing for **-₹1,406.70 Net Loss** (a net delta of -₹2,427.64 on a single trade).
- **Summary**: The gross P&L delta between Base (+₹13,073.50) and Conservative (+₹9,638.50) is -₹3,435.00, of which 70.6% (-₹2,424.50) is explained by Trade 24 missing its profit target exit before the intraday cutoff.

---

## 10. Phase 2 Stage 3 — One-Factor Credit Quality Experiments

Tested a predeclared set of 5 credit quality configurations, keeping timing, cushion, wing width, and exit rules identical:

| Experiment | Description | In-Sample Base Net (PF) | In-Sample Cons Net (PF) | Holdout Base Net (PF) | Holdout Cons Net (PF) |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **CQ-0 (Control)** | Baseline Control (Credit $\ge 10$, Cost/Profit $\le 30\%$) | **+₹5,434.90 (1.92)** | **+₹5,145.33 (1.84)** | **+₹1,415.20 (1.23)** | **-₹1,726.63 (0.79)** |
| **CQ-1** | Min Credit $\ge 23.0$ pts | +₹2,630.38 (1.44) | +₹2,364.77 (1.38) | +₹1,415.20 (1.23) | -₹1,726.63 (0.79) |
| **CQ-2** | Min Credit $\ge 25.0$ pts (50% Wing Rule) | +₹732.99 (1.13) | +₹551.01 (1.12) | +₹1,218.36 (1.23) | -₹1,894.55 (0.73) |
| **CQ-3** | Cost/Max Profit Ratio $\le 12.5\%$ | +₹1,094.05 (1.18) | +₹720.55 (1.12) | +₹1,218.36 (1.23) | -₹1,894.55 (0.73) |
| **CQ-4** | Min Credit $\ge 27.0$ pts (Upper Median) | +₹2,208.70 (1.74) | +₹2,200.57 (1.75) | -₹1,008.92 (0.81) | -₹3,114.52 (0.44) |

### Analysis of Credit Quality Experiments
1. **Trade Quality vs. Trade Elimination**: Raising the credit threshold did not improve the quality of remaining trades; it merely eliminated winning trades in 2024/2025.
2. **Adverse Selection via Cushion Compression**: Requiring higher net credits ($\ge 25$ or $\ge 27$ pts) on a 50 pt wing forces short strikes closer to ATM (compressing spot cushion). In the 30-session holdout period, this cushion compression exacerbated losses when intraday breakouts occurred, resulting in severe negative net P&L (-₹1,008.92 in Base, -₹3,114.52 in Conservative for CQ-4).
3. **No Solution to Friction Sensitivity**: None of the credit quality thresholds prevented the strategy from turning negative under conservative execution friction in the holdout split.

---

## 11. Final Research Stop Rule & Definitive Decision

**Decision**: **NOT PROVEN** (Fails Acceptance Criteria for Paper-Trading / Live Activation)

### Justification:
1. **Failure of Out-of-Sample Conservative Robustness**: While the strategy demonstrates positive expectancy under Base assumptions (+₹6,850.10 overall; +₹1,415.20 in holdout), it suffers negative net expectancy (-₹123.33/trade) and negative net P&L (-₹1,726.63, 0.79 PF) under Conservative execution friction in the untouched 30-session holdout.
2. **Limited Out-of-Sample Sample Size**: The holdout period contains only 14 trades across 30 sessions, which is statistically insufficient to confirm an edge after factoring in bid/ask crossing friction.
3. **Credit Quality Optimization Ineffective**: One-factor parameter tuning on credit thresholds did not resolve holdout fragility and degraded performance further.
4. **Safety Mandate**: Per the research stop rule, live order routing remains strictly disabled, production code and allowlists remain unmodified, and no strategy modification is promoted to production. All checkpoints (`4ca238a`, `b42976e`) are preserved.


