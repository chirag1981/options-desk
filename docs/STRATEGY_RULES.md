# Strategy Rules &bull; NIFTY Options Selling Desk

## 1. Defined-Risk Invariant
All strategies must have 100% wing protection:
$$\text{Short Contracts} = \text{Long Contracts}$$
No naked or uncovered short options are allowed under any circumstances.

## 2. Supported Strategy

### A. Iron Condor (`IRON_CONDOR`)
- **Regime Required:** `BALANCED_RANGE` ($0.90 \le \text{PCR} \le 1.10$, Spot centered safely between S1 and R1).
- **Strike Selection:**
  - Put Wing: Protective put spread at/below S1.
  - Call Wing: Protective call spread at/above R1.
- **Payoff Metrics:**
  - Net Credit: $\text{Put Wing Credit} + \text{Call Wing Credit}$.
  - Max Loss: $\text{Widest Wing Width} - \text{Combined Net Credit}$ (wings cannot lose simultaneously).

### B. Iron Butterfly (`IRON_BUTTERFLY`)
- **Structure:** 4-leg ATM short straddle with protective wings.
- **Status:** Evaluated and tested; active execution gated pending specific regime rule review.

## 3. Dual Trading Engines
- **Intraday Engine:** Trades 5m candle confirmation within session window (09:20 to 15:00 IST). Mandatory cutoff exit at 15:20 IST.
- **Positional Engine:** Trades daily/weekly trend structures. Minimum 75% confidence threshold. Pre-expiry exit enforced on expiry day.
