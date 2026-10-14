# Central Verification Manifest & Business Rules Registry

This registry documents the core business rules, expected behaviors, inputs, and the specific automated test files covering each rule for the Indian Index Options Desk.

---

## 1. Safety & Broker Isolation Rules

| Rule ID | Description | Enforced In | Test Coverage |
| :--- | :--- | :--- | :--- |
| **SAFE-01** | **Strict Paper-Trading:** Live broker order routing (`fyers.place_order`) must never be called. All executions are recorded as simulated paper trades in SQLite. | `options_signal_service.py` | `tests/test_execution_realism_and_costs.py`<br>`scripts/verify_project.py` |
| **SAFE-02** | **Risk Safeguards:** Max daily trades (3), trade cooldown (15m), and single active position limit (1) must never be weakened. | `options_signal_service.py` | `tests/test_single_active_signal_constraint.py`<br>`tests/test_nifty_risk_management.py` |

---

## 2. Open Interest (OI) & Strategy Rules

| Rule ID | Description | Enforced In | Test Coverage |
| :--- | :--- | :--- | :--- |
| **OI-01** | **OI-Led Directional Strategy:** Bullish OI + compatible price $\rightarrow$ BUY CE; Bearish OI + compatible price $\rightarrow$ BUY PE; Neutral/Conflicting/0 OI change $\rightarrow$ WAIT. Price is a sanity check, not a double-counted vote. | `options_engine.py` | `tests/test_oi_data_integrity_and_strategy.py`<br>`tests/test_simple_directional_strategy.py` |
| **OI-02** | **Dual Unwinding & Negative OI:** When both Call and Put OI changes are negative, system classifies as Unwinding/Neutral, preventing false Bullish/Bearish triggers. | `options_engine.py` | `tests/test_oi_data_integrity_and_strategy.py` |
| **OI-03** | **Interval Flow Integrity:** Interval change $\Delta\text{OI}$ requires two valid snapshots of identical underlying symbol, strike, contract type, and expiry separated by 3 to 15 minutes. Cross-outage comparisons (>15m) are rejected. | `options_signal_service.py` | `tests/test_oi_data_integrity_and_strategy.py`<br>`tests/test_time_window_and_oi_change.py` |
| **OI-04** | **Trap & Divergence Warning:** When Spot breaks Day Lows (`D.L.B.`) while Put OI is positive, system triggers `⚠️ DIVERGENCE: Put Writers Trapped below {strike}`. | `options_signal_service.py`<br>`options_desk.js` | `tests/test_trending_oi.py` |

---

## 3. Trade Execution & Price Realism Rules

| Rule ID | Description | Enforced In | Test Coverage |
| :--- | :--- | :--- | :--- |
| **EXEC-01** | **Bid/Ask Fill Realism:** Long entries execute at observed Ask; exits execute at observed Bid. LTP is never substituted for missing bids. | `options_signal_service.py`<br>`options_replay_backtest.py` | `tests/test_execution_realism_and_costs.py`<br>`tests/test_p0_p1_p2_p3_fixes.py` |
| **EXEC-02** | **Conservative Gap Fill:** Stop-loss breaches execute at `min(trailed_sl, current_bid)`. | `options_signal_service.py`<br>`options_replay_backtest.py` | `tests/test_execution_realism_and_costs.py` |
| **EXEC-03** | **Stale Quote Isolation:** Quotes with age > 120s or `data_status != 'LIVE'` are marked stale and barred from triggering price-based exits. | `options_signal_service.py` | `tests/test_oi_data_integrity_and_strategy.py`<br>`tests/test_options_data_integrity.py` |

---

## 4. Accounting, Fees & Reconciliation Rules

| Rule ID | Description | Enforced In | Test Coverage |
| :--- | :--- | :--- | :--- |
| **ACCT-01** | **Symbol-Specific Fee Models:** NIFTY uses NSE turnover fee (0.05%) and lot size 25. SENSEX uses BSE turnover fee (0.0325%) and lot size 20. | `options_replay_backtest.py`<br>`trade_analyzer_agent.py` | `tests/test_execution_realism_and_costs.py` |
| **ACCT-02** | **Partial Exit Multi-Leg Accounting:** Multi-lot trades book whole lots at T1 (e.g. 1 of 2 lots), moving remaining SL to breakeven. Dynamic columns `partial_exit_lots`, `partial_exit_price`, `partial_exit_time`, and `t1_hit_at` permanently preserve execution history. | `options_signal_service.py`<br>`trade_analyzer_agent.py` | `tests/test_p0_p1_p2_p3_fixes.py`<br>`tests/test_trade_analyzer_agent.py` |
