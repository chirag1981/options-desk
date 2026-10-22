# Strategy Rules &bull; NIFTY Options Selling Desk

## 1. Defined-Risk Invariant
All strategies must have 100% wing protection:
$$\text{Short Contracts} = \text{Long Contracts}$$
No naked or uncovered short options are allowed under any circumstances.

## 2. Four Supported Strategies

### A. Bull Put Credit Spread (`BULL_PUT`)
- **Regime Required:** `TRENDING_BULLISH` (PCR $\ge 1.15$, Put Writing dominant, Spot above S1).
- **Strike Selection:**
  - Sell OTM Put $\le$ S1 (Put Wall) at live Bid.
  - Buy OTM Put at (Short Strike - 50) at live Ask.
- **Payoff Metrics:**
  - Net Credit: $\text{Short Bid} - \text{Long Ask} \ge 10.0$ pts.
  - Max Loss: $(50 - \text{Net Credit}) \times 65$.
  - Lower Breakeven: $\text{Short Strike} - \text{Net Credit}$.

### B. Bear Call Credit Spread (`BEAR_CALL`)
- **Regime Required:** `TRENDING_BEARISH` (PCR $\le 0.85$, Call Writing dominant, Spot below R1).
- **Strike Selection:**
  - Sell OTM Call $\ge$ R1 (Call Wall) at live Bid.
  - Buy OTM Call at (Short Strike + 50) at live Ask.
- **Payoff Metrics:**
  - Net Credit: $\text{Short Bid} - \text{Long Ask} \ge 10.0$ pts.
  - Max Loss: $(50 - \text{Net Credit}) \times 65$.
  - Upper Breakeven: $\text{Short Strike} + \text{Net Credit}$.

### C. Iron Condor (`IRON_CONDOR`)
- **Regime Required:** `BALANCED_RANGE` ($0.90 \le \text{PCR} \le 1.10$, Spot centered safely between S1 and R1).
- **Strike Selection:**
  - Put Wing: Bull Put Spread at/below S1.
  - Call Wing: Bear Call Spread at/above R1.
- **Payoff Metrics:**
  - Net Credit: $\text{Put Wing Credit} + \text{Call Wing Credit}$.
  - Max Loss: $\text{Widest Wing Width} - \text{Combined Net Credit}$ (wings cannot lose simultaneously).

### D. Iron Butterfly (`IRON_BUTTERFLY`)
- **Structure:** 4-leg ATM short straddle with protective wings.
- **Status:** Evaluated and tested; active execution gated pending specific regime rule review.

## 3. Dual Trading Engines
- **Intraday Engine:** Trades 5m candle confirmation within session window (09:20 to 15:00 IST). Mandatory cutoff exit at 15:20 IST.
- **Positional Engine:** Trades daily/weekly trend structures. Minimum 75% confidence threshold. Pre-expiry exit enforced on expiry day.
