# Risk Management Policy &bull; NIFTY Options Selling Desk

## 1. Non-Negotiable Hard Risk Ceilings

### Single-Trade Risk Budget (0.5%)
- Maximum capital at risk for any individual trade is strictly capped at **0.5% of total account equity** (₹5,000 on a ₹10,00,000 paper portfolio).
- Calculated as $\text{Max Loss INR} = (\text{Spread Width} - \text{Net Credit}) \times \text{Lot Size} \times \text{Lots}$.
- Any candidate with maximum loss exceeding ₹5,000 is rejected immediately.

### Combined Portfolio Risk Ceiling (2.0%)
- The sum of all active open position risks across both Intraday and Positional engines cannot exceed **2.0% of total equity** (₹20,000 on ₹10,00,000).
- If an engine proposes an entry that would cause total open risk to exceed 2.0%, the trade is blocked.

### Daily Drawdown Circuit Breaker (1.5%)
- If total realized losses in a single trading day reach **1.5% of account equity** (₹15,000), trading is halted for the remainder of the session.
- Fresh entries are rejected with status `CIRCUIT_BREAKER_ACTIVE`.

### Concurrent Position Caps
- **Intraday Engine:** Maximum 2 concurrent open positions.
- **Positional Engine:** Maximum 2 concurrent open positions.

### Consecutive Loss Cooldown (15 Minutes)
- Upon realizing a stop-loss exit, a 15-minute cooling-off lockout is initiated to prevent emotional revenge trading during volatile market whipsaws.

## 2. Paper Trading Isolation
- Real live broker order placement (`fyers.place_order`) is strictly disabled in the source code.
- All trade executions and position fills are simulated using real-time conservative bid/ask market quotes.
