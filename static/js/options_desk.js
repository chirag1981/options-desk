/**
 * static/js/options_desk.js — Vanilla ES6 Client Controller for FYERS Options Desk Terminal
 * Handles 3-minute auto-refresh cycle, symbol/expiry changes, data rendering,
 * and CSV export without UI flickering.
 */

document.addEventListener("DOMContentLoaded", () => {
    // State
    const state = {
        symbol: "NIFTY",
        expiry: "",
        strikeRange: 5, // Default ATM ± 5 strikes (5 up, 5 down)
        refreshIntervalSec: 180, // 3 mins
        timerRemaining: 180,
        timerId: null,
        isFetching: false,
        lastData: null,
    };

    // DOM Elements
    const symbolSelect = document.getElementById("symbol-select");
    const expirySelect = document.getElementById("expiry-select");
    const btnRefresh = document.getElementById("btn-manual-refresh");
    const countdownTimer = document.getElementById("countdown-timer");
    const liveStatusPill = document.getElementById("live-status-pill");
    const liveStatusText = document.getElementById("live-status-text");

    // Ticker Elements
    const tickerSymName = document.getElementById("ticker-sym-name");
    const spotPriceDisplay = document.getElementById("spot-price-display");
    const spotChangeDisplay = document.getElementById("spot-change-display");
    const atmStrikeDisplay = document.getElementById("atm-strike-display");
    const pcrDisplay = document.getElementById("pcr-display");
    const lastUpdatedDisplay = document.getElementById("last-updated-display");

    // Market Bias Elements
    const cardMarketBias = document.getElementById("card-market-bias");
    const biasBadge = document.getElementById("bias-badge");
    const biasText = document.getElementById("bias-text");
    const confidencePill = document.getElementById("confidence-pill");
    const scorePill = document.getElementById("score-pill");
    const biasLastChanged = document.getElementById("bias-last-changed");
    const whyBiasList = document.getElementById("why-bias-list");

    // Key Levels Elements
    const levelR2 = document.getElementById("level-r2");
    const levelR1 = document.getElementById("level-r1");
    const levelAtm = document.getElementById("level-atm");
    const levelAtmDist = document.getElementById("level-atm-dist");
    const levelS1 = document.getElementById("level-s1");
    const levelS2 = document.getElementById("level-s2");
    const badgeExpectedRange = document.getElementById("badge-expected-range");
    const ladderSLbl = document.getElementById("ladder-s-lbl");
    const ladderSpotLbl = document.getElementById("ladder-spot-lbl");
    const ladderRLbl = document.getElementById("ladder-r-lbl");
    const ladderSpotPin = document.getElementById("ladder-spot-pin");
    const levelsSummaryText = document.getElementById("levels-summary-text");

    // Institutional Stance Dominance Elements
    const instDomTitle = document.getElementById("inst-dom-title");
    const instDomPct = document.getElementById("inst-dom-pct");
    const domBarCe = document.getElementById("dom-bar-ce");
    const domBarPe = document.getElementById("dom-bar-pe");

    // Option Buying & Setup Score Elements
    const cardOptionFocus = document.getElementById("card-option-focus");
    const setupScoreVal = document.getElementById("setup-score-val");
    const focusHeroBox = document.getElementById("focus-hero-box");
    const focusTitle = document.getElementById("focus-title");
    const focusReason = document.getElementById("focus-reason");
    const preferredContractPill = document.getElementById("preferred-contract-pill");
    const tradeEntry = document.getElementById("trade-entry");
    const tradeSl = document.getElementById("trade-sl");
    const tradeT1 = document.getElementById("trade-t1");
    const tradeT2 = document.getElementById("trade-t2");
    const tradeRr = document.getElementById("trade-rr");
    const checklistSummary = document.getElementById("checklist-summary");
    const checklistGrid = document.getElementById("checklist-grid");

    // OI Activity Elements
    const actCallWriting = document.getElementById("act-call-writing");
    const actPutWriting = document.getElementById("act-put-writing");
    const actCallUnwinding = document.getElementById("act-call-unwinding");
    const actPutUnwinding = document.getElementById("act-put-unwinding");
    const totalCeOi = document.getElementById("total-ce-oi");
    const totalCeChg = document.getElementById("total-ce-chg");
    const totalPeOi = document.getElementById("total-pe-oi");
    const totalPeChg = document.getElementById("total-pe-chg");

    // Lists & Tables
    const bigOiList = document.getElementById("big-oi-list");
    const oiTrendTbody = document.getElementById("oi-trend-tbody");
    const optionChainTbody = document.getElementById("option-chain-tbody");
    const btnExportCsv = document.getElementById("btn-export-csv");

    // Trending OI Elements
    const btnTabTrendingOi = document.getElementById("btn-tab-trending-oi");
    const btnTabStrikeBreakdown = document.getElementById("btn-tab-strike-breakdown");
    const trendingIntervalSelect = document.getElementById("trending-interval-select");
    const trendingSelectedStrikesContainer = document.getElementById("trending-selected-strikes-container");
    const trendingLiveCapsuleSummary = document.getElementById("trending-live-capsule-summary");
    const trendingOiTimeseriesContainer = document.getElementById("trending-oi-timeseries-container");
    const trendingStrikeBreakdownContainer = document.getElementById("trending-strike-breakdown-container");
    const trendingPulseTbody = document.getElementById("trending-pulse-tbody");

    // Initial state setup
    if (symbolSelect) state.symbol = symbolSelect.value;
    if (expirySelect) state.expiry = expirySelect.value;

    /**
     * Formats integer seconds into mm:ss
     */
    function formatTime(seconds) {
        const m = Math.floor(seconds / 60);
        const s = seconds % 60;
        return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }

    /**
     * Starts the 3-minute auto-refresh countdown timer
     */
    function startCountdown() {
        if (state.timerId) clearInterval(state.timerId);
        state.timerRemaining = state.refreshIntervalSec;
        countdownTimer.textContent = formatTime(state.timerRemaining);

        state.timerId = setInterval(() => {
            state.timerRemaining--;
            if (state.timerRemaining <= 0) {
                state.timerRemaining = state.refreshIntervalSec;
                loadOptionsDeskData(false);
            }
            countdownTimer.textContent = formatTime(state.timerRemaining);
        }, 1000);
    }

    /**
     * Fetches options data from API
     */
    async function loadOptionsDeskData(isManual = false) {
        if (state.isFetching) return;
        state.isFetching = true;

        if (btnRefresh) btnRefresh.classList.add("spinning");
        if (liveStatusText) liveStatusText.textContent = "UPDATING...";

        const selectedInterval = trendingIntervalSelect ? trendingIntervalSelect.value : "5";
        const params = new URLSearchParams({
            symbol: state.symbol,
            expiry: state.expiry || "",
            interval: selectedInterval || "5",
            refresh: isManual ? "true" : "false",
        });

        try {
            const resp = await fetch(`/api/options-desk/data?${params.toString()}`);
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
            }
            result = await resp.json();

            if (result && result.success && result.data) {
                state.lastData = result.data;
                try {
                    renderDashboard(result.data);
                } catch (renderErr) {
                    console.error("Error rendering dashboard UI:", renderErr);
                }
                try {
                    loadSignalsData();
                } catch (sigErr) {
                    console.error("Error loading signal tracker data:", sigErr);
                }
                const statusLabel = (result.data.meta && result.data.meta.data_status) || "LIVE";
                if (liveStatusText) liveStatusText.textContent = statusLabel;
                if (liveStatusPill) {
                    liveStatusPill.className = `status-pill ${statusLabel.toLowerCase()}`;
                }
            } else {
                if (liveStatusText) liveStatusText.textContent = "ERROR";
                if (liveStatusPill) liveStatusPill.className = "status-pill error";
                console.error("Options API Error:", result ? result.error : "Empty response");
            }
        } catch (err) {
            if (liveStatusText) liveStatusText.textContent = "OFFLINE";
            if (liveStatusPill) liveStatusPill.className = "status-pill offline";
            console.error("Network Error fetching options data:", err);
        } finally {
            state.isFetching = false;
            if (btnRefresh) btnRefresh.classList.remove("spinning");
            if (isManual) {
                // Reset countdown
                startCountdown();
            }
        }
    }

    /**
     * Updates expiry dropdown options for active symbol
     */
    async function updateExpiriesForSymbol(symbol) {
        try {
            const resp = await fetch(`/api/options-desk/expiries?symbol=${encodeURIComponent(symbol)}`);
            const json = await resp.json();
            if (json.success && json.expiries && expirySelect) {
                expirySelect.innerHTML = "";
                json.expiries.forEach((exp) => {
                    const opt = document.createElement("option");
                    opt.value = exp;
                    opt.textContent = exp;
                    expirySelect.appendChild(opt);
                });
                state.expiry = json.expiries[0] || "";
            }
        } catch (e) {
            console.error("Error updating expiries:", e);
        }
    }

    /**
     * Formats number with Indian comma grouping
     */
    function formatIndianNumber(x) {
        if (x === undefined || x === null) return "--";
        const parts = x.toString().split(".");
        let lastThree = parts[0].substring(parts[0].length - 3);
        const otherNumbers = parts[0].substring(0, parts[0].length - 3);
        if (otherNumbers !== "") lastThree = "," + lastThree;
        const res = otherNumbers.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + lastThree;
        return parts.length > 1 ? res + "." + parts[1] : res;
    }

    /**
     * Formats numbers compactly into Lakhs/Crores (e.g. +1.04Cr, -2.92L)
     */
    function formatCompactOi(x) {
        if (x === undefined || x === null || isNaN(x)) return "--";
        const num = Number(x);
        const sign = num > 0 ? "+" : (num < 0 ? "-" : "");
        const abs = Math.abs(num);
        if (abs >= 10000000) {
            return `${sign}${(abs / 10000000).toFixed(2)}Cr`;
        } else if (abs >= 100000) {
            return `${sign}${(abs / 100000).toFixed(2)}L`;
        } else if (abs >= 1000) {
            return `${sign}${(abs / 1000).toFixed(1)}k`;
        }
        return `${sign}${abs}`;
    }

    /**
     * Renders all dashboard sections from analytical payload
     */
    function renderDashboard(data) {
        const bias = data.market_bias || {};
        const levels = data.key_levels || {};
        const focus = data.option_focus || {};
        const summary = data.oi_activity_summary || {};
        const meta = data.meta || {};

        // 1. Ticker Bar
        if (tickerSymName) tickerSymName.textContent = meta.name || state.symbol;
        if (spotPriceDisplay) spotPriceDisplay.textContent = formatIndianNumber(bias.spot_price);
        
        if (spotChangeDisplay) {
            const chg = bias.spot_change_pct || 0;
            const sign = chg >= 0 ? "+" : "";
            spotChangeDisplay.textContent = `${sign}${chg.toFixed(2)}%`;
            spotChangeDisplay.className = `spot-change ${chg >= 0 ? "up" : "down"}`;
        }

        if (atmStrikeDisplay) atmStrikeDisplay.textContent = formatIndianNumber(levels.atm_strike);
        if (pcrDisplay) pcrDisplay.textContent = summary.pcr ? summary.pcr.toFixed(2) : "--";
        if (lastUpdatedDisplay) lastUpdatedDisplay.textContent = meta.last_updated || "--:--:--";

        // 2. Card 1: Market Bias
        const biasType = (bias.bias || "NEUTRAL").toUpperCase();
        if (biasText) biasText.textContent = biasType;
        
        if (cardMarketBias) {
            cardMarketBias.className = `desk-card card-bias bias-${biasType.toLowerCase()}`;
        }
        if (biasBadge) {
            biasBadge.className = `bias-badge-large ${biasType.toLowerCase()}`;
        }
        if (confidencePill) {
            confidencePill.textContent = `Confidence: ${bias.confidence || "MEDIUM"}`;
        }
        if (scorePill) {
            scorePill.textContent = `Bull: ${bias.bullish_score}% | Bear: ${bias.bearish_score}%`;
        }
        if (biasLastChanged) {
            biasLastChanged.textContent = bias.last_changed || "--:--";
        }

        // Render Explainability Reasons
        if (whyBiasList) {
            whyBiasList.innerHTML = "";
            const reasons = bias.why_reasons || [];
            if (reasons.length === 0) {
                whyBiasList.innerHTML = `<li>Balanced option chain without dominant skew.</li>`;
            } else {
                reasons.forEach((r) => {
                    const li = document.createElement("li");
                    li.textContent = r;
                    whyBiasList.appendChild(li);
                });
            }
        }

        // 3. Card 2: Key Levels
        const r1 = levels.resistance_1 || 0;
        const r2 = levels.resistance_2 || 0;
        const s1 = levels.support_1 || 0;
        const s2 = levels.support_2 || 0;
        const atm = levels.atm_strike || 0;
        const spot = bias.spot_price || 0;

        if (levelR2) levelR2.textContent = formatIndianNumber(r2);
        if (levelR1) levelR1.textContent = formatIndianNumber(r1);
        if (levelAtm) levelAtm.textContent = formatIndianNumber(atm);
        if (levelS1) levelS1.textContent = formatIndianNumber(s1);
        if (levelS2) levelS2.textContent = formatIndianNumber(s2);

        if (levelAtmDist && spot && atm) {
            const dist = (spot - atm).toFixed(1);
            const distSign = dist >= 0 ? "+" : "";
            levelAtmDist.textContent = `Spot Distance: ${distSign}${dist} pts`;
        }

        // Visual Price Range Ladder & Expected Range
        const rangePts = Math.abs(r1 - s1);
        if (badgeExpectedRange) {
            badgeExpectedRange.textContent = `Range: ${formatIndianNumber(s1)} - ${formatIndianNumber(r1)} (${rangePts} pts)`;
        }
        if (ladderSLbl) ladderSLbl.textContent = `S1: ${formatIndianNumber(s1)}`;
        if (ladderRLbl) ladderRLbl.textContent = `R1: ${formatIndianNumber(r1)}`;
        if (ladderSpotLbl) ladderSpotLbl.textContent = `📍 Spot: ${formatIndianNumber(spot)}`;

        if (ladderSpotPin && r1 > s1 && spot > 0) {
            let pinPct = ((spot - s1) / (r1 - s1)) * 100;
            pinPct = Math.max(0, Math.min(100, pinPct));
            ladderSpotPin.style.left = `${pinPct}%`;
        }

        // Plain English 1-Liner Takeaway
        if (levelsSummaryText) {
            if (spot >= r1 && r1 > 0) {
                levelsSummaryText.innerHTML = `<strong>Breakout Alert:</strong> Spot (${formatIndianNumber(spot)}) is crossing R1 (${formatIndianNumber(r1)}). Upside target is R2 (${formatIndianNumber(r2)}).`;
            } else if (spot <= s1 && s1 > 0) {
                levelsSummaryText.innerHTML = `<strong>Breakdown Alert:</strong> Spot (${formatIndianNumber(spot)}) is breaching S1 (${formatIndianNumber(s1)}). Downside slide towards S2 (${formatIndianNumber(s2)}).`;
            } else if (r1 > 0 && s1 > 0) {
                const distR = (r1 - spot).toFixed(1);
                const distS = (spot - s1).toFixed(1);
                levelsSummaryText.innerHTML = `<strong>Channel:</strong> Spot is ${distS} pts above S1 Support (${formatIndianNumber(s1)}) and ${distR} pts below R1 Ceiling (${formatIndianNumber(r1)}).`;
            } else {
                levelsSummaryText.textContent = "Analyzing key level breakout & support zones...";
            }
        }

        // Directional Conviction Correlation Radar
        updateDirectionalRadar(data);

        // 4. Card 3: Option Buying Focus & Execution Plan
        const optBuying = data.option_buying || data.option_focus || {};
        const tradePlan = optBuying.trade_plan || {};
        const checklist = optBuying.checklist || [];

        if (setupScoreVal) {
            setupScoreVal.textContent = optBuying.setup_score !== undefined ? optBuying.setup_score : "0";
        }
        if (focusTitle) {
            focusTitle.textContent = optBuying.title || (optBuying.decision ? `${optBuying.decision} SETUP` : "WAIT / NO TRADE");
        }
        if (focusReason) {
            focusReason.textContent = optBuying.reason || "Analyzing market structure and level confirmation...";
        }
        if (preferredContractPill) {
            if (tradePlan.contract_name) {
                preferredContractPill.textContent = `${tradePlan.contract_name} @ ₹${tradePlan.entry_price || "--"}`;
            } else {
                preferredContractPill.textContent = "--";
            }
        }

        if (focusHeroBox) {
            focusHeroBox.classList.remove("bullish-hero", "bearish-hero", "neutral-hero");
            const dec = (optBuying.decision || optBuying.type || "WAIT").toUpperCase();
            if (dec.includes("CE")) focusHeroBox.classList.add("bullish-hero");
            else if (dec.includes("PE")) focusHeroBox.classList.add("bearish-hero");
            else focusHeroBox.classList.add("neutral-hero");
        }

        // Trade Levels
        if (tradeEntry) tradeEntry.textContent = tradePlan.entry_price ? `₹${tradePlan.entry_price}` : "₹--";
        if (tradeSl) tradeSl.textContent = tradePlan.stop_loss ? `₹${tradePlan.stop_loss}` : "₹--";
        if (tradeT1) tradeT1.textContent = tradePlan.target_1 ? `₹${tradePlan.target_1}` : "₹--";
        if (tradeT2) tradeT2.textContent = tradePlan.target_2 ? `₹${tradePlan.target_2}` : "₹--";
        if (tradeRr) tradeRr.textContent = tradePlan.risk_reward || "1 : 2.0";

        // Confirmation Checklist
        if (checklistGrid && checklist.length > 0) {
            checklistGrid.innerHTML = "";
            let passedCount = 0;
            checklist.forEach((item) => {
                if (item.passed) passedCount++;
                const itemDiv = document.createElement("div");
                itemDiv.className = `check-item ${item.passed ? "check-pass" : "check-fail"}`;
                itemDiv.title = item.desc || "";
                itemDiv.innerHTML = `
                    <span class="chk-icon">${item.passed ? "✓" : "✗"}</span>
                    <span class="chk-text">${item.name}</span>
                `;
                checklistGrid.appendChild(itemDiv);
            });
            if (checklistSummary) {
                checklistSummary.textContent = `${passedCount}/${checklist.length} Confirmed`;
            }
        }

        // 5. Card 4: OI Activity Summary
        if (actCallWriting) actCallWriting.textContent = formatIndianNumber(summary.call_writing_strike);
        if (actPutWriting) actPutWriting.textContent = formatIndianNumber(summary.put_writing_strike);
        if (actCallUnwinding) actCallUnwinding.textContent = formatIndianNumber(summary.call_unwinding_strike);
        if (actPutUnwinding) actPutUnwinding.textContent = formatIndianNumber(summary.put_unwinding_strike);

        if (totalCeOi) totalCeOi.textContent = summary.total_ce_oi || "--";
        if (totalCeChg) totalCeChg.textContent = `(${summary.total_ce_change_oi || "--"})`;
        if (totalPeOi) totalPeOi.textContent = summary.total_pe_oi || "--";
        if (totalPeChg) totalPeChg.textContent = `(${summary.total_pe_change_oi || "--"})`;

        // 6. Card 5: Big OI Movements
        renderBigOiMovements(data.big_oi_movements || []);

        // 7. Section 3: Trending OI & Strike Breakdown (ATM ± 5 strikes)
        renderTrendingOiTable(data.trending_oi_timeseries || {}, levels.atm_strike, bias.spot_price);
        renderOiTrendTable(data.oi_trend || [], levels.atm_strike, bias.spot_price);

        // 8. Section 4: Detailed Option Chain
        renderOptionChainTable(data.detailed_chain || [], levels.atm_strike, levels.resistance_1, levels.support_1, bias.spot_price);
    }

    /**
     * Correlates ATM price, Key Levels (S1/R1), and Big OI Flow into pure Directional Vector
     */
    function updateDirectionalRadar(data) {
        const radarBox = document.getElementById("directional-radar-box");
        const radarHeadText = document.getElementById("radar-head-text");
        const radarConvictionPill = document.getElementById("radar-conviction-pill");
        const radarThesis = document.getElementById("radar-thesis");
        const radarPlaybookText = document.getElementById("radar-playbook-text");

        if (!radarBox) return;

        const bias = data.market_bias || {};
        const levels = data.key_levels || {};
        const movements = data.big_oi_movements || [];
        const spot = bias.spot_price || 0;
        const atm = levels.atm_strike || 0;
        const r1 = levels.resistance_1 || 0;
        const r2 = levels.resistance_2 || 0;
        const s1 = levels.support_1 || 0;
        const s2 = levels.support_2 || 0;

        let ceAdds = 0;
        let peAdds = 0;
        movements.forEach((m) => {
            const chg = m.change_oi || 0;
            if (m.side === "CE" || (m.activity && m.activity.includes("CALL"))) {
                if (chg > 0) ceAdds += chg;
            } else {
                if (chg > 0) peAdds += chg;
            }
        });

        const isCeHeavy = ceAdds > (peAdds * 1.25);
        const isPeHeavy = peAdds > (ceAdds * 1.25);
        const isBelowAtm = spot < atm;
        const isAboveAtm = spot > atm;
        const distToR1 = r1 > 0 ? (r1 - spot) : 999;
        const distToS1 = s1 > 0 ? (spot - s1) : 999;

        let mode = "WAIT";
        let convictionPct = 50;
        let head = "CHOP / TWO-WAY TRAP ZONE";
        let thesis = "";
        let playbook = "";

        // BEARISH DIRECTIONAL CORRELATION (PE PLAY)
        if ((isCeHeavy || bias.bias === "BEARISH") && isBelowAtm) {
            mode = "PE";
            convictionPct = Math.min(95, Math.max(65, Math.round(bias.bearish_score || 78)));
            head = `BEARISH VECTOR (${convictionPct}% Conviction)`;
            
            const ceRatio = peAdds > 0 ? (ceAdds / peAdds).toFixed(1) : "Heavy";
            thesis = `Institutional Call Writing (${ceRatio}x vs Put Support) capping upside. Spot (${formatIndianNumber(spot)}) pinned below ATM (${formatIndianNumber(atm)}).`;
            
            if (distToS1 <= (atm * 0.003)) {
                playbook = `Breakdown Zone: Spot testing S1 (${formatIndianNumber(s1)}). Clean break below ${formatIndianNumber(s1)} opens fast slide to S2 (${formatIndianNumber(s2)}). Invalidation: Reclaiming ATM (${formatIndianNumber(atm)}).`;
            } else {
                playbook = `Sell-on-Rise: Rallies towards ${formatIndianNumber(atm)} or ${formatIndianNumber(r1)} face heavy institutional supply. Downside target: ${formatIndianNumber(s1)}.`;
            }
        }
        // BULLISH DIRECTIONAL CORRELATION (CE PLAY)
        else if ((isPeHeavy || bias.bias === "BULLISH") && isAboveAtm) {
            mode = "CE";
            convictionPct = Math.min(95, Math.max(65, Math.round(bias.bullish_score || 78)));
            head = `BULLISH VECTOR (${convictionPct}% Conviction)`;
            
            const peRatio = ceAdds > 0 ? (peAdds / ceAdds).toFixed(1) : "Heavy";
            thesis = `Institutional Put Writing (${peRatio}x vs Call Resistance) building strong floor. Spot (${formatIndianNumber(spot)}) holding above ATM (${formatIndianNumber(atm)}).`;
            
            if (distToR1 <= (atm * 0.003)) {
                playbook = `Breakout Zone: Spot testing R1 (${formatIndianNumber(r1)}). Clean break above ${formatIndianNumber(r1)} opens squeeze to R2 (${formatIndianNumber(r2)}). Invalidation: Falling below ATM (${formatIndianNumber(atm)}).`;
            } else {
                playbook = `Buy-on-Dip: Pullbacks towards ${formatIndianNumber(atm)} or ${formatIndianNumber(s1)} offer support. Upside target: ${formatIndianNumber(r1)}.`;
            }
        }
        // NEUTRAL / CONSOLIDATION CHOP
        else {
            mode = "WAIT";
            convictionPct = 50;
            head = `CHOP / TWO-WAY TRAP ZONE`;
            thesis = `Both Call & Put writing active around ATM (${formatIndianNumber(atm)}). Range bounded between S1 (${formatIndianNumber(s1)}) and R1 (${formatIndianNumber(r1)}).`;
            playbook = `Preserve Capital: Avoid fresh naked option buying in middle of channel. Wait for breakout above ${formatIndianNumber(r1)} (CE) or breakdown below ${formatIndianNumber(s1)} (PE).`;
        }

        radarBox.className = `directional-radar-box radar-mode-${mode.toLowerCase()}`;
        if (radarHeadText) radarHeadText.textContent = head;
        if (radarConvictionPill) {
            radarConvictionPill.textContent = mode === "PE" ? "⚡ PURE PE PLAY" : (mode === "CE" ? "⚡ PURE CE PLAY" : "⏸ WAIT / TRAP");
        }
        if (radarThesis) radarThesis.textContent = thesis;
        if (radarPlaybookText) radarPlaybookText.textContent = playbook;
    }

    /**
     * Renders Big OI Movements
     */
    function renderBigOiMovements(movements) {
        if (!bigOiList) return;
        if (movements.length === 0) {
            bigOiList.innerHTML = `<div class="empty-state">No large OI movements meeting threshold.</div>`;
            if (instDomPct) instDomPct.textContent = "No large movements";
            return;
        }

        // Calculate Institutional Dominance (% CE vs % PE)
        let ceOiSum = 0;
        let peOiSum = 0;
        movements.forEach((item) => {
            const chg = Math.abs(item.change_oi || 0);
            if (item.side === "CE" || (item.activity && item.activity.includes("CALL"))) {
                ceOiSum += chg;
            } else {
                peOiSum += chg;
            }
        });

        const totalMove = ceOiSum + peOiSum;
        const cePct = totalMove > 0 ? Math.round((ceOiSum / totalMove) * 100) : 50;
        const pePct = 100 - cePct;

        if (domBarCe) domBarCe.style.width = `${cePct}%`;
        if (domBarPe) domBarPe.style.width = `${pePct}%`;

        if (instDomPct) {
            if (cePct >= 65) {
                instDomPct.textContent = `🔴 Bears Dominating (${cePct}% Call Resistance)`;
                instDomPct.className = "inst-dom-pct val-loss";
            } else if (pePct >= 65) {
                instDomPct.textContent = `🟢 Bulls Dominating (${pePct}% Put Support)`;
                instDomPct.className = "inst-dom-pct val-win";
            } else {
                instDomPct.textContent = `🟡 Balanced Stance (${cePct}% CE / ${pePct}% PE)`;
                instDomPct.className = "inst-dom-pct";
            }
        }

        const maxOi = Math.max(...movements.map((m) => Math.abs(m.change_oi || 1)), 1);

        bigOiList.innerHTML = "";
        movements.forEach((item) => {
            const row = document.createElement("div");
            row.className = "big-oi-item";

            const sideClass = (item.side || (item.activity && item.activity.includes("CALL") ? "CE" : "PE"));
            const chgVal = item.change_oi !== undefined ? item.change_oi : (item.ce_change_oi || item.pe_change_oi || 0);
            const isAddition = chgVal >= 0;
            const deltaClass = isAddition ? "pos" : "neg";
            const deltaSign = isAddition ? "▲ +" : "▼ ";
            const widthPct = Math.min(100, Math.max(10, Math.round((Math.abs(chgVal) / maxOi) * 100)));
            row.style.setProperty("--bar-width", `${widthPct}%`);

            // Format directional impact tag
            let tagClass = "tag-neutral";
            const act = item.activity || "";
            let tagHtml = act;
            let rowTitle = "";

            if (act.includes("CALL WRITING") || (sideClass === "CE" && chgVal > 0)) {
                tagClass = "tag-bearish";
                tagHtml = `<span class="dir-icon">🔻</span> Resistance Build`;
                rowTitle = "Call Writing: Institutional resistance building (Bearish Ceiling)";
            } else if (act.includes("PUT WRITING") || (sideClass === "PE" && chgVal > 0)) {
                tagClass = "tag-bullish";
                tagHtml = `<span class="dir-icon">🔺</span> Support Build`;
                rowTitle = "Put Writing: Institutional support building (Bullish Floor)";
            } else if (act.includes("CALL UNWINDING") || (sideClass === "CE" && chgVal < 0)) {
                tagClass = "tag-bullish";
                tagHtml = `<span class="dir-icon">↗️</span> Short Covering`;
                rowTitle = "Call Unwinding: Call sellers exiting (Bulls pushing higher)";
            } else if (act.includes("PUT UNWINDING") || (sideClass === "PE" && chgVal < 0)) {
                tagClass = "tag-bearish";
                tagHtml = `<span class="dir-icon">↘️</span> Support Cracking`;
                rowTitle = "Put Unwinding: Put sellers exiting (Downside risk increasing)";
            }

            const formattedChg = item.change_oi_formatted ? item.change_oi_formatted.replace("+", "").replace("-", "") : formatIndianNumber(Math.abs(chgVal));

            row.setAttribute("title", rowTitle);
            row.innerHTML = `
                <div class="big-oi-strike-side">
                    <span class="side-pill ${sideClass}">${sideClass}</span>
                    <span class="big-oi-strike font-mono">${formatIndianNumber(item.strike)}</span>
                </div>
                <div class="big-oi-delta ${deltaClass} font-mono">
                    ${deltaSign}${formattedChg} OI
                </div>
                <div class="big-oi-tag ${tagClass}">
                    ${tagHtml}
                </div>
            `;
            bigOiList.appendChild(row);
        });
    }

    /**
     * Renders Trending OI Oi Pulse Table (ATM ± 5 strikes)
     */
    function renderTrendingOiTable(trendingTimeseries, atmStrike, spotPrice) {
        if (!trendingPulseTbody) return;
        if (!trendingTimeseries || !trendingTimeseries.rows || trendingTimeseries.rows.length === 0) {
            trendingPulseTbody.innerHTML = `<tr><td colspan="13" class="text-center" style="padding:1.5rem;color:var(--text-muted,#94a3b8);">No Trending OI data available yet.</td></tr>`;
            return;
        }

        // Render selected strike chips
        if (trendingSelectedStrikesContainer) {
            const strikes = trendingTimeseries.selected_strikes || [];
            const effectiveAtm = trendingTimeseries.atm_strike || atmStrike;
            if (strikes.length > 0) {
                trendingSelectedStrikesContainer.innerHTML = strikes.map(s => {
                    const isAtm = Math.abs(s - effectiveAtm) < 1.0;
                    return `<span class="strike-chip ${isAtm ? 'is-atm' : ''}" title="${isAtm ? 'ATM Strike' : 'Selected Strike'}">${formatIndianNumber(s)}${isAtm ? ' [ATM]' : ''}</span>`;
                }).join("");
            }
        }

        // Render live capsule in header strip
        if (trendingLiveCapsuleSummary && trendingTimeseries.latest_summary) {
            const latest = trendingTimeseries.latest_summary;
            const pctSign = latest.strength_pct >= 0 ? "+" : "";
            const dotChar = "●";
            const dotsStr = dotChar.repeat(latest.strength_dots || 0);

            trendingLiveCapsuleSummary.innerHTML = `
                <div style="display:flex;align-items:center;gap:0.4rem;" title="Oi Pulse Conviction Rule: >=40% diff with 2+ dots indicates trading conviction. <30% is noise/chop.">
                    <span style="font-size:0.75rem;color:var(--text-muted,#94a3b8);">Live Strength:</span>
                    <span class="strength-capsule ${latest.strength_class || 'strength-weak'}">
                        <span>${pctSign}${latest.strength_pct}%</span>
                        ${dotsStr ? `<span class="strength-dots">${dotsStr}</span>` : ''}
                    </span>
                    <span class="badge ${latest.sentiment === 'Bullish' ? 'badge-sentiment-bullish' : (latest.sentiment === 'Bearish' ? 'badge-sentiment-bearish' : 'badge-sentiment-neutral')}" style="margin-left:0.3rem;">
                        ${latest.sentiment}
                    </span>
                    <span style="font-size:0.72rem;color:var(--text-muted,#94a3b8);font-family:var(--font-mono);margin-left:0.5rem;">Net PCR: <strong>${latest.net_pcr}</strong></span>
                </div>
            `;
        }

        // Render table rows
        trendingPulseTbody.innerHTML = "";
        trendingTimeseries.rows.forEach(r => {
            const tr = document.createElement("tr");

            // Format Day H/L Break
            let dlbHtml = "-";
            if (r.day_hl_break && r.day_hl_break !== "-") {
                if (r.day_hl_break.startsWith("D.L.B.")) {
                    dlbHtml = `<span class="badge-dlb">${r.day_hl_break}</span>`;
                } else if (r.day_hl_break.startsWith("D.H.B.")) {
                    dlbHtml = `<span class="badge-dhb">${r.day_hl_break}</span>`;
                } else {
                    dlbHtml = r.day_hl_break;
                }
            }

            // Diff in OI class
            const diffClass = r.diff_oi >= 0 ? "val-pos" : "val-neg";
            const diffSign = r.diff_oi >= 0 ? "+" : "";

            // Strength capsule
            const pctSign = r.strength_pct >= 0 ? "+" : "";
            const dotChar = "●";
            const dotsStr = dotChar.repeat(r.strength_dots || 0);
            const strengthHtml = `
                <span class="strength-capsule ${r.strength_class}">
                    <span>${pctSign}${r.strength_pct}%</span>
                    ${dotsStr ? `<span class="strength-dots">${dotsStr}</span>` : ''}
                </span>
            `;

            // Direction arrow pill
            const dirClass = r.direction_color === "green" ? "dir-green" : "dir-red";
            const dirArrowHtml = `<span class="dir-arrow-pill ${dirClass}">${r.direction_arrow}</span>`;

            // Chng In Direction
            const chngDirClass = r.chng_in_direction >= 0 ? "val-pos" : "val-neg";
            const chngDirSign = r.chng_in_direction >= 0 ? "+" : "";

            // Direction of Chng %
            const dirPctClass = r.direction_chng_pct >= 0 ? "val-pos" : "val-neg";
            const dirPctSign = r.direction_chng_pct >= 0 ? "+" : "";

            // Day High/Low Diff in OI badge
            let dayHlDiffHtml = "-";
            if (r.day_hl_diff_oi === "Day Low Break") {
                dayHlDiffHtml = `<span class="badge-diff-break-low">Day Low Break</span>`;
            } else if (r.day_hl_diff_oi === "Day High Break") {
                dayHlDiffHtml = `<span class="badge-diff-break-high">Day High Break</span>`;
            }

            // Sentiment badge
            let sentClass = "badge-sentiment-neutral";
            if (r.sentiment === "Bullish") sentClass = "badge-sentiment-bullish";
            else if (r.sentiment === "Bearish") sentClass = "badge-sentiment-bearish";

            const fullCeChg = formatIndianNumber(r.ce_change_oi);
            const fullPeChg = formatIndianNumber(r.pe_change_oi);
            const fullDiffOi = formatIndianNumber(r.diff_oi);
            const fullChngDir = formatIndianNumber(r.chng_in_direction);

            const compactCeChg = formatCompactOi(r.ce_change_oi);
            const compactPeChg = formatCompactOi(r.pe_change_oi);
            const compactDiffOi = formatCompactOi(r.diff_oi);
            const compactChngDir = formatCompactOi(r.chng_in_direction);

            tr.innerHTML = `
                <td class="text-center font-mono">
                    <strong style="font-size:0.75rem;">${r.time}</strong>
                    <span class="sub-date">${r.date}</span>
                </td>
                <td class="text-right font-mono">₹${r.ltp.toFixed(2)}</td>
                <td class="text-center">${dlbHtml}</td>
                <td class="text-right font-mono" title="${fullCeChg} contracts">${compactCeChg}</td>
                <td class="text-right font-mono" title="${fullPeChg} contracts">${compactPeChg}</td>
                <td class="text-right font-mono ${diffClass}" title="${diffSign}${fullDiffOi} contracts"><strong>${compactDiffOi}</strong></td>
                <td class="text-center">${strengthHtml}</td>
                <td class="text-center">${dirArrowHtml}</td>
                <td class="text-right font-mono ${chngDirClass}" title="${chngDirSign}${fullChngDir} contracts">${compactChngDir}</td>
                <td class="text-right font-mono ${dirPctClass}">${dirPctSign}${r.direction_chng_pct.toFixed(2)}%</td>
                <td class="text-center font-mono"><strong>${r.net_pcr.toFixed(2)}</strong></td>
                <td class="text-center">${dayHlDiffHtml}</td>
                <td class="text-center"><span class="${sentClass}">${r.sentiment}</span></td>
            `;

            trendingPulseTbody.appendChild(tr);
        });
    }

    /**
     * Renders OI Trend Table (ATM ± 5 strikes)
     */
    function renderOiTrendTable(rows, atmStrike, spotPrice) {
        if (!oiTrendTbody) return;
        if (rows.length === 0) {
            oiTrendTbody.innerHTML = `<tr><td colspan="8" class="text-center">No strike trend available.</td></tr>`;
            return;
        }

        oiTrendTbody.innerHTML = "";
        rows.forEach((r) => {
            const tr = document.createElement("tr");
            const isAtm = Math.abs(r.strike - atmStrike) < 1.0;
            const isItmCe = spotPrice ? r.strike < spotPrice : false;
            const isItmPe = spotPrice ? r.strike > spotPrice : false;

            if (isAtm) tr.classList.add("row-atm");

            const ceChgClass = r.ce_change_oi >= 0 ? "val-pos" : "val-neg";
            const peChgClass = r.pe_change_oi >= 0 ? "val-pos" : "val-neg";

            let actBadgeClass = "badge-neutral";
            let actText = r.ce_activity !== "NO CLEAR SIGNAL" ? r.ce_activity : r.pe_activity;
            if (actText === "CALL WRITING") actBadgeClass = "badge-call-writing";
            else if (actText === "PUT WRITING") actBadgeClass = "badge-put-writing";
            else if (actText === "CALL UNWINDING") actBadgeClass = "badge-call-unwinding";
            else if (actText === "PUT UNWINDING") actBadgeClass = "badge-put-unwinding";

            const atmTag = isAtm ? `<span class="atm-marker-tag">ATM</span>` : "";

            tr.innerHTML = `
                <td class="text-right ${isItmCe ? 'cell-itm' : ''}">${r.ce_oi_formatted || "--"}</td>
                <td class="text-right ${ceChgClass} ${isItmCe ? 'cell-itm' : ''}">${r.ce_change_oi_formatted || "--"}</td>
                <td class="text-right ${isItmCe ? 'cell-itm' : ''}">₹${r.ce_ltp ? r.ce_ltp.toFixed(2) : "0.00"}</td>
                <td class="text-center highlight-col strike-cell-container">
                    <span class="strike-pill ${isAtm ? 'strike-pill-atm' : ''}">${formatIndianNumber(r.strike)}</span>
                    ${atmTag}
                </td>
                <td class="text-left ${isItmPe ? 'cell-itm' : ''}">₹${r.pe_ltp ? r.pe_ltp.toFixed(2) : "0.00"}</td>
                <td class="text-left ${peChgClass} ${isItmPe ? 'cell-itm' : ''}">${r.pe_change_oi_formatted || "--"}</td>
                <td class="text-left ${isItmPe ? 'cell-itm' : ''}">${r.pe_oi_formatted || "--"}</td>
                <td class="text-center">
                    <span class="act-badge ${actBadgeClass}">${actText}</span>
                </td>
            `;
            oiTrendTbody.appendChild(tr);
        });
    }

    /**
     * Renders Full Detailed Option Chain (Filtered to ATM ± 5 strikes by default)
     */
    function renderOptionChainTable(chain, atmStrike, r1, s1, spotPrice, strikeStep = 50) {
        if (!optionChainTbody) return;
        if (chain.length === 0) {
            optionChainTbody.innerHTML = `<tr><td colspan="9" class="text-center">No option chain strikes loaded.</td></tr>`;
            return;
        }

        let filteredChain = chain;
        if (state.strikeRange > 0 && atmStrike) {
            const maxDiff = (strikeStep || 50) * state.strikeRange;
            filteredChain = chain.filter((r) => Math.abs(r.strike - atmStrike) <= maxDiff + 0.1);
        }

        optionChainTbody.innerHTML = "";
        filteredChain.forEach((r) => {
            const tr = document.createElement("tr");
            const isAtm = Math.abs(r.strike - atmStrike) < 1.0;
            const isItmCe = spotPrice ? r.strike < spotPrice : false;
            const isItmPe = spotPrice ? r.strike > spotPrice : false;

            if (isAtm) tr.classList.add("row-atm");

            const isR1 = Math.abs(r.strike - r1) < 1.0;
            const isS1 = Math.abs(r.strike - s1) < 1.0;

            const ceChgClass = r.ce_change_oi >= 0 ? "val-pos" : "val-neg";
            const peChgClass = r.pe_change_oi >= 0 ? "val-pos" : "val-neg";

            let strikeTag = "";
            if (isAtm) strikeTag = ` <span class="atm-badge-tag-lg">★ ATM ★</span>`;
            else if (isR1) strikeTag = ` <span class="res-badge" style="font-size:0.6rem;padding:0.1rem 0.3rem;">R1</span>`;
            else if (isS1) strikeTag = ` <span class="sup-badge" style="font-size:0.6rem;padding:0.1rem 0.3rem;">S1</span>`;

            tr.innerHTML = `
                <td class="text-right ${isItmCe ? 'cell-itm' : ''}">${r.ce_oi_formatted || formatIndianNumber(r.ce_oi)}</td>
                <td class="text-right ${ceChgClass} ${isItmCe ? 'cell-itm' : ''}">${r.ce_change_oi_formatted || formatIndianNumber(r.ce_change_oi)}</td>
                <td class="text-right ${isItmCe ? 'cell-itm' : ''}">${formatIndianNumber(r.ce_volume)}</td>
                <td class="text-right ${isItmCe ? 'cell-itm' : ''}">₹${r.ce_ltp ? r.ce_ltp.toFixed(2) : "0.00"}</td>
                <td class="text-center highlight-col strike-cell-container">
                    <span class="strike-pill ${isAtm ? 'strike-pill-atm' : ''}">${formatIndianNumber(r.strike)}</span>${strikeTag}
                </td>
                <td class="text-left ${isItmPe ? 'cell-itm' : ''}">₹${r.pe_ltp ? r.pe_ltp.toFixed(2) : "0.00"}</td>
                <td class="text-left ${isItmPe ? 'cell-itm' : ''}">${formatIndianNumber(r.pe_volume)}</td>
                <td class="text-left ${peChgClass} ${isItmPe ? 'cell-itm' : ''}">${r.pe_change_oi_formatted || formatIndianNumber(r.pe_change_oi)}</td>
                <td class="text-left ${isItmPe ? 'cell-itm' : ''}">${r.pe_oi_formatted || formatIndianNumber(r.pe_oi)}</td>
            `;
            optionChainTbody.appendChild(tr);
        });
    }

    /**
     * Exports current option chain table to CSV file
     */
    function exportChainToCsv() {
        if (!state.lastData || !state.lastData.detailed_chain) return;
        const chain = state.lastData.detailed_chain;

        const headers = ["CE_OI", "CE_CHG_OI", "CE_VOLUME", "CE_LTP", "STRIKE", "PE_LTP", "PE_VOLUME", "PE_CHG_OI", "PE_OI"];
        const rows = chain.map((r) => [
            r.ce_oi,
            r.ce_change_oi,
            r.ce_volume,
            r.ce_ltp,
            r.strike,
            r.pe_ltp,
            r.pe_volume,
            r.pe_change_oi,
            r.pe_oi,
        ]);

        let csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows.map((e) => e.join(","))].join("\n");
        const encodedUri = encodeURI(csvContent);
        const link = document.createElement("a");
        link.setAttribute("href", encodedUri);
        link.setAttribute("download", `Option_Desk_${state.symbol}_${state.expiry || "Chain"}.csv`);
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }

    // Event Listeners
    const strikeRangeSelect = document.getElementById("strike-range-select");
    if (strikeRangeSelect) {
        strikeRangeSelect.addEventListener("change", (e) => {
            state.strikeRange = parseInt(e.target.value, 10);
            if (state.lastData) {
                renderOptionChainTable(
                    state.lastData.detailed_chain || [],
                    state.lastData.key_levels?.atm_strike,
                    state.lastData.key_levels?.resistance_1,
                    state.lastData.key_levels?.support_1,
                    state.lastData.market_bias?.spot_price,
                    state.lastData.meta?.strike_step || 50
                );
            }
        });
    }

    // Event Listeners
    if (symbolSelect) {
        symbolSelect.addEventListener("change", async (e) => {
            state.symbol = e.target.value;
            await updateExpiriesForSymbol(state.symbol);
            loadOptionsDeskData(true);
        });
    }

    if (expirySelect) {
        expirySelect.addEventListener("change", (e) => {
            state.expiry = e.target.value;
            loadOptionsDeskData(true);
        });
    }

    if (btnRefresh) {
        btnRefresh.addEventListener("click", () => {
            loadOptionsDeskData(true);
        });
    }

    // ==========================================================================
    // Signal Tracker & Paper Trading Journal Controller
    // ==========================================================================
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";

    // Journal DOM Elements
    const tabBtnActive = document.getElementById("tab-btn-active-signals");
    const tabBtnHistory = document.getElementById("tab-btn-history-signals");
    const tabBtnAnalysis = document.getElementById("tab-btn-analysis");
    const paneActive = document.getElementById("pane-active-signals");
    const paneHistory = document.getElementById("pane-history-signals");
    const paneAnalysis = document.getElementById("pane-analysis");
    const activeCountBadge = document.getElementById("active-signals-count-badge");
    const historyCountBadge = document.getElementById("history-signals-count-badge");
    const strategyScoreBadge = document.getElementById("strategy-score-badge");

    const metricWinRate = document.getElementById("metric-win-rate");
    const metricWinRatio = document.getElementById("metric-win-ratio");
    const metricTotalPts = document.getElementById("metric-total-pts");
    const metricPtsSub = document.getElementById("metric-pts-sub");
    const metricNetInr = document.getElementById("metric-net-inr");
    const metricProfitFactor = document.getElementById("metric-profit-factor");
    const metricRrSub = document.getElementById("metric-rr-sub");
    const metricActiveCount = document.getElementById("metric-active-count");

    const activeSignalsTbody = document.getElementById("active-signals-tbody");
    const historySignalsTbody = document.getElementById("history-signals-tbody");
    const btnLogPaperTrade = document.getElementById("btn-log-paper-trade");
    const paperLotsInput = document.getElementById("paper-lots-input");
    const paperTradeMsg = document.getElementById("paper-trade-msg");
    const btnExportJournalCsv = document.getElementById("btn-export-journal-csv");

    // Strategy Diagnostics DOM Elements
    const diagHealthScore = document.getElementById("diag-health-score");
    const diagHealthGrade = document.getElementById("diag-health-grade");
    const diagMfeEff = document.getElementById("diag-mfe-eff");
    const diagOvWinrate = document.getElementById("diag-ov-winrate");
    const diagOvRatio = document.getElementById("diag-ov-ratio");
    const diagOvPf = document.getElementById("diag-ov-pf");
    const diagOvPts = document.getElementById("diag-ov-pts");
    const diagOvAvgWinLoss = document.getElementById("diag-ov-avg-winloss");
    const diagOvInr = document.getElementById("diag-ov-inr");
    const diagLastAnalyzed = document.getElementById("diag-last-analyzed");
    const diagBestList = document.getElementById("diag-best-list");
    const diagWorstList = document.getElementById("diag-worst-list");
    const btnRunDiagnostics = document.getElementById("btn-run-diagnostics");
    const analysisIssuesList = document.getElementById("analysis-issues-list");
    const analysisTuningList = document.getElementById("analysis-tuning-list");
    const diagMoneynessTbody = document.getElementById("diag-moneyness-tbody");
    const diagTimewindowTbody = document.getElementById("diag-timewindow-tbody");

    let lastSignalsSummary = null;

    /**
     * Tab Switcher (Active Trades vs Trade Journal vs AI Diagnostics)
     */
    function switchJournalTab(targetTab) {
        if (tabBtnActive) tabBtnActive.classList.toggle("active", targetTab === "active");
        if (tabBtnHistory) tabBtnHistory.classList.toggle("active", targetTab === "history");
        if (tabBtnAnalysis) tabBtnAnalysis.classList.toggle("active", targetTab === "analysis");

        if (paneActive) paneActive.classList.toggle("active", targetTab === "active");
        if (paneHistory) paneHistory.classList.toggle("active", targetTab === "history");
        if (paneAnalysis) paneAnalysis.classList.toggle("active", targetTab === "analysis");

        if (targetTab === "analysis") {
            loadTradeAnalysis();
        }
    }

    const btnTopTradeAnalysis = document.getElementById("btn-top-trade-analysis");

    if (tabBtnActive) tabBtnActive.addEventListener("click", () => switchJournalTab("active"));
    if (tabBtnHistory) tabBtnHistory.addEventListener("click", () => switchJournalTab("history"));
    if (tabBtnAnalysis) tabBtnAnalysis.addEventListener("click", () => switchJournalTab("analysis"));
    if (btnRunDiagnostics) btnRunDiagnostics.addEventListener("click", () => loadTradeAnalysis());

    if (btnTopTradeAnalysis) {
        btnTopTradeAnalysis.addEventListener("click", () => {
            switchJournalTab("analysis");
            const journalSec = document.getElementById("signal-journal-section");
            if (journalSec) {
                journalSec.scrollIntoView({ behavior: "smooth", block: "start" });
            }
        });
    }

    /**
     * Fetches Signal Summary and Trade History from API
     */
    async function loadSignalsData() {
        try {
            const resp = await fetch("/api/options-desk/signals");
            const result = await resp.json();
            if (result.success && result.data) {
                lastSignalsSummary = result.data;
                renderSignalsDashboard(result.data);
            }
        } catch (e) {
            console.error("Error loading signals data:", e);
        }
    }

    /**
     * Renders KPI cards and tables
     */
    function renderSignalsDashboard(data) {
        const active = data.active_signals || [];
        const history = data.history_signals || data.history || [];
        const metrics = data.metrics || {};

        // 1. Badges
        if (activeCountBadge) activeCountBadge.textContent = active.length;
        if (historyCountBadge) historyCountBadge.textContent = history.length;

        // Compute Live & Realized P&L directly from signals
        let openPts = 0;
        let openInr = 0;
        active.forEach((s) => {
            openPts += parseFloat(s.points_pnl || 0);
            openInr += parseFloat(s.net_pnl_inr || 0);
        });

        let closedPts = 0;
        let closedInr = 0;
        let winCount = 0;
        let lossCount = 0;
        let beCount = 0;
        let grossWinPts = 0;
        let grossLossPts = 0;

        history.forEach((s) => {
            const pts = parseFloat(s.points_pnl || 0);
            const inr = parseFloat(s.net_pnl_inr || 0);
            closedPts += pts;
            closedInr += inr;
            if (pts > 0) {
                winCount++;
                grossWinPts += pts;
            } else if (pts < 0) {
                lossCount++;
                grossLossPts += Math.abs(pts);
            } else {
                beCount++;
            }
        });

        const totalCombinedPts = closedPts + openPts;
        const totalCombinedInr = closedInr + openInr;
        const closedCount = history.length;

        const winRate = (metrics.win_rate_pct !== undefined && metrics.win_rate_pct !== null)
            ? metrics.win_rate_pct
            : (closedCount > 0 ? ((winCount / closedCount) * 100).toFixed(1) : null);

        let profitFactor = (metrics.profit_factor !== undefined && metrics.profit_factor !== null)
            ? metrics.profit_factor
            : null;

        if (profitFactor === null) {
            if (grossLossPts > 0) {
                profitFactor = (grossWinPts / grossLossPts).toFixed(2);
            } else if (grossWinPts > 0) {
                profitFactor = grossWinPts.toFixed(2);
            } else if (closedCount > 0) {
                profitFactor = "1.00";
            }
        }

        // 2. Metrics Cards
        if (metricWinRate) {
            if (winRate !== null && winRate !== undefined) {
                metricWinRate.textContent = `${parseFloat(winRate).toFixed(1)}%`;
                metricWinRate.className = `metric-val font-mono ${parseFloat(winRate) >= 50 ? "val-win" : "val-loss"}`;
            } else {
                metricWinRate.textContent = "--";
                metricWinRate.className = "metric-val font-mono";
            }
        }
        if (metricWinRatio) {
            if (closedCount > 0) {
                metricWinRatio.textContent = beCount > 0 
                    ? `(${winCount}W / ${lossCount}L / ${beCount}BE)`
                    : `(${winCount}W / ${lossCount}L)`;
            } else {
                metricWinRatio.textContent = `(${active.length} Open / 0 Closed)`;
            }
        }

        if (metricTotalPts) {
            const sign = totalCombinedPts >= 0 ? "+" : "";
            metricTotalPts.textContent = `${sign}${totalCombinedPts.toFixed(1)} pts`;
            metricTotalPts.className = `metric-val font-mono ${totalCombinedPts > 0 ? "val-win" : (totalCombinedPts < 0 ? "val-loss" : "")}`;
        }
        if (metricPtsSub) {
            const openSign = openPts >= 0 ? "+" : "";
            const closedSign = closedPts >= 0 ? "+" : "";
            if (active.length > 0 && closedCount > 0) {
                metricPtsSub.textContent = `Open: ${openSign}${openPts.toFixed(1)} | Closed: ${closedSign}${closedPts.toFixed(1)} pts`;
            } else if (active.length > 0) {
                metricPtsSub.textContent = `Open: ${openSign}${openPts.toFixed(1)} pts (Live)`;
            } else {
                metricPtsSub.textContent = `Realized: ${closedSign}${closedPts.toFixed(1)} pts`;
            }
        }

        if (metricNetInr) {
            const sign = totalCombinedInr >= 0 ? "+₹" : "-₹";
            metricNetInr.textContent = `${sign}${formatIndianNumber(Math.abs(totalCombinedInr).toFixed(2))}`;
            metricNetInr.className = `metric-val font-mono ${totalCombinedInr > 0 ? "val-win" : (totalCombinedInr < 0 ? "val-loss" : "")}`;
        }

        if (metricProfitFactor) {
            if (profitFactor !== null && profitFactor !== undefined) {
                metricProfitFactor.textContent = typeof profitFactor === "number" ? profitFactor.toFixed(2) : profitFactor;
                metricProfitFactor.className = `metric-val font-mono ${parseFloat(profitFactor) >= 1.5 ? "val-win" : (parseFloat(profitFactor) < 1.0 ? "val-loss" : "")}`;
            } else {
                metricProfitFactor.textContent = "--";
                metricProfitFactor.className = "metric-val font-mono";
            }
        }

        if (metricRrSub) {
            if (closedCount > 0) {
                if (grossLossPts > 0 && winCount > 0 && lossCount > 0) {
                    metricRrSub.textContent = `Avg W: +${(grossWinPts/winCount).toFixed(1)} | L: -${(grossLossPts/lossCount).toFixed(1)}`;
                } else if (grossWinPts > 0) {
                    metricRrSub.textContent = `Gross Win: +${grossWinPts.toFixed(1)} pts`;
                } else {
                    metricRrSub.textContent = `Gross Loss: -${grossLossPts.toFixed(1)} pts`;
                }
            } else {
                metricRrSub.textContent = "Awaiting Closed Trades";
            }
        }

        if (metricActiveCount) {
            metricActiveCount.textContent = active.length;
        }

        // 3. Render Active Signals Table
        renderActiveSignalsTable(active);

        // 4. Render History Signals Table
        renderHistorySignalsTable(history);
    }

    /**
     * Renders Active Signals Table
     */
    function renderActiveSignalsTable(activeList) {
        if (!activeSignalsTbody) return;
        if (activeList.length === 0) {
            activeSignalsTbody.innerHTML = `<tr><td colspan="10" class="text-center" style="padding:1.5rem;color:var(--text-muted);">No active signals currently open. Triggered setups will appear here in realtime.</td></tr>`;
            return;
        }

        activeSignalsTbody.innerHTML = "";
        activeList.forEach((sig) => {
            const tr = document.createElement("tr");
            const isProfit = (sig.points_pnl || 0) >= 0;
            const pnlPtsClass = isProfit ? "val-pos" : "val-neg";
            const ptsSign = isProfit ? "+" : "";
            const inrSign = (sig.net_pnl_inr || 0) >= 0 ? "+₹" : "-₹";

            let statusTagClass = "status-tag-active";
            let statusLabel = "ACTIVE";
            if (sig.status === "TARGET_1_HIT") {
                statusTagClass = "status-tag-t1";
                statusLabel = "TARGET 1 HIT";
            }

            tr.innerHTML = `
                <td class="text-left font-mono">
                    <strong>#${sig.id}</strong>
                    <div style="font-size:0.7rem;color:var(--text-muted);">${sig.created_at.split(" ")[1] || sig.created_at}</div>
                </td>
                <td class="text-left">
                    <span class="side-pill ${sig.signal_type}">${sig.symbol}</span>
                    <strong class="font-mono" style="margin-left:0.35rem;">${sig.contract_name}</strong>
                    <span class="chip-moneyness" style="font-size:0.65rem;padding:0.15rem 0.35rem;background:rgba(255,255,255,0.06);border-radius:4px;color:var(--text-secondary);margin-left:0.25rem;">${sig.moneyness || 'ATM'}</span>
                    ${sig.is_paper_trade ? '<small style="color:var(--cyan-accent);font-size:0.65rem;margin-left:0.25rem;">(Paper)</small>' : ''}
                    <div style="font-size:0.68rem;color:var(--text-muted);margin-top:2px;">PCR: ${sig.pcr_at_entry ? sig.pcr_at_entry.toFixed(2) : '1.0'} &bull; ${sig.duration_mins || 0}m</div>
                </td>
                <td class="text-right font-mono">₹${sig.entry_price.toFixed(2)}</td>
                <td class="text-right font-mono" style="font-weight:700;">₹${sig.current_price.toFixed(2)}</td>
                <td class="text-right font-mono" style="font-size:0.75rem;color:var(--text-muted);">
                    L: ₹${sig.lowest_price.toFixed(1)} &bull; H: ₹${sig.highest_price.toFixed(1)}
                    <div style="color:var(--emerald-accent);font-size:0.68rem;">MFE: +${sig.mfe_points ? sig.mfe_points.toFixed(1) : (sig.highest_price - sig.entry_price).toFixed(1)} pts</div>
                </td>
                <td class="text-center">
                    <div class="targets-chip-wrap font-mono">
                        <span class="chip-sl" title="Stop Loss">SL: ${sig.stop_loss}</span>
                        <span class="chip-t1" title="Target 1">T1: ${sig.target_1}</span>
                        <span class="chip-t2" title="Target 2">T2: ${sig.target_2}</span>
                    </div>
                </td>
                <td class="text-right font-mono ${pnlPtsClass}" style="font-weight:700;">
                    ${ptsSign}${sig.points_pnl.toFixed(2)} (${ptsSign}${sig.pnl_pct.toFixed(1)}%)
                </td>
                <td class="text-right font-mono ${pnlPtsClass}" style="font-weight:700;">
                    ${inrSign}${formatIndianNumber(Math.abs(sig.net_pnl_inr).toFixed(2))}
                    <div style="font-size:0.65rem;color:var(--text-muted);">${sig.lots} Lot (${sig.lot_size * sig.lots} Qty)</div>
                </td>
                <td class="text-center">
                    <span class="status-tag ${statusTagClass}">${statusLabel}</span>
                </td>
                <td class="text-center">
                    <button type="button" class="btn-square-off" data-id="${sig.id}" title="Square off at current LTP">
                        Square Off
                    </button>
                </td>
            `;
            activeSignalsTbody.appendChild(tr);
        });

        // Attach square off events
        activeSignalsTbody.querySelectorAll(".btn-square-off").forEach((btn) => {
            btn.addEventListener("click", async (e) => {
                const id = e.target.getAttribute("data-id");
                if (confirm(`Square off Signal #${id} at current market price?`)) {
                    await closeTrade(id);
                }
            });
        });
    }

    /**
     * Renders Trade History Table
     */
    function renderHistorySignalsTable(historyList) {
        if (!historySignalsTbody) return;
        if (historyList.length === 0) {
            historySignalsTbody.innerHTML = `<tr><td colspan="10" class="text-center" style="padding:1.5rem;color:var(--text-muted);">No completed trades in journal history.</td></tr>`;
            return;
        }

        historySignalsTbody.innerHTML = "";
        historyList.forEach((sig) => {
            const tr = document.createElement("tr");
            const isProfit = (sig.points_pnl || 0) >= 0;
            const pnlPtsClass = isProfit ? "val-pos" : "val-neg";
            const ptsSign = isProfit ? "+" : "";
            const inrSign = (sig.net_pnl_inr || 0) >= 0 ? "+₹" : "-₹";

            let outcomeClass = "status-tag-manual";
            let outcomeText = sig.status.replace("_", " ");
            if (sig.status === "TARGET_2_HIT") {
                outcomeClass = "status-tag-t2";
                outcomeText = "🎯 TARGET 2 HIT";
            } else if (sig.status === "TSL_HIT") {
                outcomeClass = "status-tag-tsl";
                outcomeText = "🛡️ T1 BOOKED & TSL";
            } else if (sig.status === "TARGET_1_HIT") {
                outcomeClass = "status-tag-t1";
                outcomeText = "🎯 TARGET 1 HIT";
            } else if (sig.status === "SL_HIT") {
                outcomeClass = "status-tag-sl";
                outcomeText = "🛑 SL HIT";
            } else if (sig.status === "TIME_STOP_EXIT") {
                outcomeClass = "status-tag-timestop";
                outcomeText = "⏱ TIME STOP";
            } else if (sig.status === "EOD_CLOSED") {
                outcomeClass = "status-tag-eod";
                outcomeText = "⏱ EOD CLOSED";
            } else if (sig.status === "MANUALLY_CLOSED") {
                outcomeClass = "status-tag-manual";
                outcomeText = "✋ SQUARED OFF";
            }

            const mfeDisplay = sig.mfe_points ? `+${sig.mfe_points.toFixed(1)} pts` : `${(sig.highest_price - sig.entry_price).toFixed(1)} pts`;

            tr.innerHTML = `
                <td class="text-left font-mono" style="font-size:0.75rem;">
                    <div>${sig.created_at.split(" ")[0]}</div>
                    <div style="color:var(--text-muted);">${sig.created_at.split(" ")[1] || ""} (${sig.duration_mins || 0}m)</div>
                </td>
                <td class="text-left">
                    <span class="side-pill ${sig.signal_type}">${sig.symbol}</span>
                    <strong class="font-mono" style="margin-left:0.35rem;">${sig.contract_name}</strong>
                    <span class="chip-moneyness" style="font-size:0.65rem;padding:0.15rem 0.35rem;background:rgba(255,255,255,0.06);border-radius:4px;color:var(--text-secondary);margin-left:0.25rem;">${sig.moneyness || 'ATM'}</span>
                    <div style="font-size:0.68rem;color:var(--text-muted);margin-top:2px;">Peak: ${mfeDisplay} &bull; PCR: ${sig.pcr_at_entry ? sig.pcr_at_entry.toFixed(2) : '1.0'}</div>
                </td>
                <td class="text-right font-mono">₹${sig.entry_price.toFixed(2)}</td>
                <td class="text-right font-mono" style="font-weight:600;">₹${sig.exit_price ? sig.exit_price.toFixed(2) : sig.current_price.toFixed(2)}</td>
                <td class="text-right font-mono ${pnlPtsClass}" style="font-weight:700;">
                    ${ptsSign}${sig.points_pnl.toFixed(2)} pts
                </td>
                <td class="text-right font-mono ${pnlPtsClass}" style="font-weight:700;">
                    ${ptsSign}${sig.pnl_pct.toFixed(1)}%
                </td>
                <td class="text-right font-mono ${pnlPtsClass}" style="font-weight:700;">
                    ${inrSign}${formatIndianNumber(Math.abs(sig.net_pnl_inr).toFixed(2))}
                </td>
                <td class="text-center">
                    <span class="status-tag ${outcomeClass}">${outcomeText}</span>
                </td>
                <td class="text-left" style="font-size:0.75rem;color:var(--text-secondary);max-width:240px;">
                    ${sig.trigger_reason || "Multi-factor option setup"}
                </td>
                <td class="text-center">
                    <button type="button" class="btn-del-record" data-id="${sig.id}" title="Delete trade record">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                </td>
            `;
            historySignalsTbody.appendChild(tr);
        });

        // Attach delete events
        historySignalsTbody.querySelectorAll(".btn-del-record").forEach((btn) => {
            btn.addEventListener("click", async (e) => {
                const id = btn.getAttribute("data-id");
                if (confirm(`Delete Signal #${id} from journal history?`)) {
                    await deleteSignal(id);
                }
            });
        });
    }

    /**
     * Manually close trade
     */
    async function closeTrade(id) {
        try {
            const resp = await fetch(`/api/options-desk/signals/${id}/close`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken,
                },
            });
            const res = await resp.json();
            if (res.success) {
                await loadSignalsData();
            } else {
                alert(`Error closing trade: ${res.error}`);
            }
        } catch (e) {
            console.error("Failed to close trade:", e);
        }
    }

    /**
     * Delete trade record
     */
    async function deleteSignal(id) {
        try {
            const resp = await fetch(`/api/options-desk/signals/${id}`, {
                method: "DELETE",
                headers: {
                    "X-CSRFToken": csrfToken,
                },
            });
            const res = await resp.json();
            if (res.success) {
                await loadSignalsData();
            }
        } catch (e) {
            console.error("Failed to delete signal:", e);
        }
    }

    /**
     * Log Paper Trade Button Action
     */
    if (btnLogPaperTrade) {
        btnLogPaperTrade.addEventListener("click", async () => {
            if (!state.lastData) {
                alert("Please wait for market data to load.");
                return;
            }

            const optBuying = state.lastData.option_buying || state.lastData.option_focus || {};
            const tradePlan = optBuying.trade_plan || {};
            const bias = state.lastData.market_bias || {};

            if (!tradePlan.entry_price || tradePlan.entry_price <= 0) {
                alert("No active option contract price available to trade.");
                return;
            }

            const lots = parseInt(paperLotsInput ? paperLotsInput.value : "1", 10) || 1;
            const payload = {
                symbol: state.symbol,
                type: tradePlan.type || optBuying.decision || "CE",
                contract_name: tradePlan.contract_name,
                strike: tradePlan.strike,
                expiry: tradePlan.expiry,
                spot_price: bias.spot_price,
                entry_price: tradePlan.entry_price,
                stop_loss: tradePlan.stop_loss,
                target_1: tradePlan.target_1,
                target_2: tradePlan.target_2,
                risk_reward: tradePlan.risk_reward || "1:2.0",
                setup_score: optBuying.setup_score || 0,
                reason: optBuying.reason || "Manual Paper Trade Setup",
                lots: lots,
                pcr: bias.pcr || state.lastData.pcr || 1.0,
                bias: bias.bias || "NEUTRAL",
                atm_iv: state.lastData.atm_iv || 0.0,
                moneyness: tradePlan.moneyness || "ATM",
            };

            try {
                btnLogPaperTrade.disabled = true;
                const resp = await fetch("/api/options-desk/signals/record", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": csrfToken,
                    },
                    body: JSON.stringify(payload),
                });
                const res = await resp.json();
                if (res.success) {
                    if (paperTradeMsg) {
                        paperTradeMsg.textContent = `✓ Logged ${tradePlan.contract_name} (${lots} Lot)!`;
                        setTimeout(() => { paperTradeMsg.textContent = ""; }, 4000);
                    }
                    await loadSignalsData();
                } else {
                    alert(`Error logging trade: ${res.error}`);
                }
            } catch (err) {
                console.error("Failed to log paper trade:", err);
            } finally {
                btnLogPaperTrade.disabled = false;
            }
        });
    }


    /**
     * Export Trade Journal to CSV
     */
    if (btnExportJournalCsv) {
        btnExportJournalCsv.addEventListener("click", () => {
            window.location.href = "/api/options-desk/signals/export-csv";
        });
    }

    /**
     * Fetches AI Strategy Diagnostic & Parameter Optimization Report
     */
    async function loadTradeAnalysis() {
        try {
            const resp = await fetch("/api/options-desk/trade-analysis");
            const result = await resp.json();
            if (result.success && result.report) {
                renderTradeAnalysis(result.report);
            }
        } catch (err) {
            console.error("Error loading trade analysis:", err);
        }
    }

    /**
     * Renders Strategy Diagnostics & Optimization Blueprint (5 Required Dashboard Sections)
     */
    function renderTradeAnalysis(rep) {
        const perf = rep.overall_performance || rep.performance || {};
        const exc = (rep.dimensions && rep.dimensions.excursion) || rep.excursion_analysis || {};
        const bestConds = rep.best_conditions || [];
        const worstConds = rep.worst_conditions || [];
        const leaks = rep.top_profit_leaks || rep.diagnostic_issues || [];
        const recs = rep.recommended_changes || rep.tuning_recommendations || [];
        const dims = rep.dimensions || {};
        const segs = rep.segmentations || {};
        const samples = rep.sample_size || {};

        const score = perf.strategy_health_score !== undefined ? perf.strategy_health_score : 50;
        const totalTrades = samples.closed_trades || 0;

        // Badge in Tab Header
        if (strategyScoreBadge) {
            strategyScoreBadge.textContent = `${score}`;
        }

        // Section 1: Overall Performance Summary
        if (diagLastAnalyzed) {
            diagLastAnalyzed.textContent = `Last Audited: ${rep.timestamp || '--'}`;
        }

        if (diagOvWinrate) {
            const wr = samples.win_rate_pct !== undefined ? samples.win_rate_pct : 0.0;
            diagOvWinrate.textContent = `${wr.toFixed(1)}%`;
            diagOvWinrate.className = `d-val font-mono ${wr >= 50 ? 'val-win' : 'val-loss'}`;
        }

        if (diagOvRatio) {
            diagOvRatio.textContent = totalTrades > 0
                ? `${samples.win_count}W / ${samples.loss_count}L / ${samples.be_count || 0}BE`
                : '0 Closed Trades';
        }

        if (diagOvPf) {
            diagOvPf.textContent = perf.profit_factor !== undefined ? perf.profit_factor : '1.00';
        }

        if (diagOvPts) {
            const pts = perf.total_pts || 0.0;
            const sign = pts >= 0 ? '+' : '';
            diagOvPts.textContent = `${sign}${pts.toFixed(1)} pts`;
            diagOvPts.className = `d-val font-mono ${pts >= 0 ? 'val-win' : 'val-loss'}`;
        }

        if (diagOvAvgWinLoss) {
            diagOvAvgWinLoss.textContent = `Avg W: +${perf.avg_win_pts || 0} | Avg L: -${perf.avg_loss_pts || 0} pts`;
        }

        if (diagOvInr) {
            const inr = perf.total_inr || 0.0;
            const sign = inr >= 0 ? '+₹' : '-₹';
            diagOvInr.textContent = `${sign}${formatIndianNumber(Math.abs(inr).toFixed(2))}`;
            diagOvInr.className = `d-val font-mono ${inr >= 0 ? 'val-win' : 'val-loss'}`;
        }

        if (diagMfeEff) {
            diagMfeEff.textContent = `${exc.mfe_capture_efficiency_pct || 0}%`;
        }

        if (diagHealthScore) {
            diagHealthScore.textContent = `${score}/100`;
            if (score >= 75) {
                diagHealthScore.style.color = "var(--bullish-green)";
            } else if (score >= 50) {
                diagHealthScore.style.color = "var(--gold-light)";
            } else {
                diagHealthScore.style.color = "var(--bearish-red)";
            }
        }

        if (diagHealthGrade) {
            if (totalTrades === 0) {
                diagHealthGrade.textContent = "Awaiting Paper Trade Logs";
            } else if (score >= 80) {
                diagHealthGrade.textContent = "★ Robust Execution";
            } else if (score >= 65) {
                diagHealthGrade.textContent = "✓ Moderate Quality";
            } else {
                diagHealthGrade.textContent = "⚠ High Leakage Drag";
            }
        }

        // Section 2: Best Conditions
        if (diagBestList) {
            if (bestConds.length === 0) {
                diagBestList.innerHTML = `<div class="empty-state" style="padding:1rem;color:var(--text-muted);">Awaiting winning trade samples to detect optimal conditions.</div>`;
            } else {
                diagBestList.innerHTML = "";
                bestConds.forEach((c) => {
                    const el = document.createElement("div");
                    el.className = "condition-item condition-best";
                    el.innerHTML = `
                        <div class="condition-header">
                            <span class="condition-cat font-mono">${c.category}</span>
                            <span class="condition-name font-mono"><strong>${c.condition}</strong></span>
                        </div>
                        <div class="condition-metric font-mono text-bullish">${c.metric}</div>
                    `;
                    diagBestList.appendChild(el);
                });
            }
        }

        // Section 3: Worst Conditions
        if (diagWorstList) {
            if (worstConds.length === 0) {
                diagWorstList.innerHTML = `<div class="empty-state" style="padding:1rem;color:var(--text-muted);">No persistent underperforming conditions detected.</div>`;
            } else {
                diagWorstList.innerHTML = "";
                worstConds.forEach((c) => {
                    const el = document.createElement("div");
                    el.className = "condition-item condition-worst";
                    el.innerHTML = `
                        <div class="condition-header">
                            <span class="condition-cat font-mono">${c.category}</span>
                            <span class="condition-name font-mono"><strong>${c.condition}</strong></span>
                        </div>
                        <div class="condition-metric font-mono text-bearish">${c.metric}</div>
                    `;
                    diagWorstList.appendChild(el);
                });
            }
        }

        // Section 4: Top 3 Actual Profit Leaks
        if (analysisIssuesList) {
            if (leaks.length === 0) {
                analysisIssuesList.innerHTML = `<div class="empty-state" style="padding:1rem;color:var(--text-muted);">No major profit leakages detected in recorded journal trades.</div>`;
            } else {
                analysisIssuesList.innerHTML = "";
                leaks.forEach((item, idx) => {
                    const sev = item.severity || "WARNING";
                    const el = document.createElement("div");
                    el.className = `issue-item severity-${sev}`;
                    el.innerHTML = `
                        <div class="issue-header">
                            <span class="issue-title"><span class="leak-rank">#${idx + 1}</span> ${item.title}</span>
                            <span class="issue-badge badge-${sev}">${sev}</span>
                        </div>
                        <div class="issue-desc"><strong>Evidence:</strong> ${item.evidence || item.message}</div>
                        ${item.likely_cause ? `<div class="issue-root-cause"><strong>Likely Cause:</strong> ${item.likely_cause}</div>` : (item.root_cause ? `<div class="issue-root-cause"><strong>Likely Cause:</strong> ${item.root_cause}</div>` : "")}
                        ${item.impact ? `<div class="issue-impact font-mono text-amber">Impact: ${item.impact}</div>` : ""}
                    `;
                    analysisIssuesList.appendChild(el);
                });
            }
        }

        // Section 5: Recommended Changes (Advisory Only)
        if (analysisTuningList) {
            if (recs.length === 0) {
                analysisTuningList.innerHTML = `<div class="empty-state" style="padding:1rem;color:var(--text-muted);">No parameter adjustments currently required.</div>`;
            } else {
                analysisTuningList.innerHTML = "";
                recs.forEach((r) => {
                    const el = document.createElement("div");
                    el.className = "tuning-item";
                    el.innerHTML = `
                        <div class="tuning-header">
                            <span class="tuning-param">${r.rule || r.parameter}</span>
                            <span class="tuning-cat font-mono">${r.target_file ? r.target_file.split('/').pop() : 'Advisory'}</span>
                        </div>
                        <div class="tuning-obs"><strong>Observation:</strong> ${r.observation || r.impact || ''}</div>
                        <div class="tuning-prop"><strong>Proposed Change:</strong> <span class="text-bullish font-mono">${r.proposal || r.recommended}</span></div>
                        ${r.expected_benefit ? `<div class="tuning-benefit"><strong>Expected Benefit:</strong> ${r.expected_benefit}</div>` : ""}
                        <div class="tuning-target-tag">Target: ${r.target_file || r.code_target || 'Strategy Rules'} (Manual Approval Required)</div>
                    `;
                    analysisTuningList.appendChild(el);
                });
            }
        }

        // Section 6: Breakdown Tables (Moneyness & Time)
        if (diagMoneynessTbody) {
            const mList = (dims.moneyness) || (segs.by_moneyness) || [];
            if (mList.length === 0) {
                diagMoneynessTbody.innerHTML = `<tr><td colspan="5" class="text-center" style="padding:1rem;color:var(--text-muted);">No moneyness data recorded yet.</td></tr>`;
            } else {
                diagMoneynessTbody.innerHTML = "";
                mList.forEach((m) => {
                    const tr = document.createElement("tr");
                    const ptsClass = m.total_pts >= 0 ? "val-pos" : "val-neg";
                    const ptsSign = m.total_pts >= 0 ? "+" : "";
                    tr.innerHTML = `
                        <td class="text-left font-mono"><strong>${m.moneyness}</strong></td>
                        <td class="text-center font-mono">${m.count}</td>
                        <td class="text-right font-mono ${m.win_rate_pct >= 50 ? 'val-pos' : 'val-neg'}">${m.win_rate_pct}%</td>
                        <td class="text-right font-mono">${m.profit_factor}</td>
                        <td class="text-right font-mono ${ptsClass}">${ptsSign}${m.total_pts.toFixed(1)} pts</td>
                    `;
                    diagMoneynessTbody.appendChild(tr);
                });
            }
        }

        if (diagTimewindowTbody) {
            const wList = (dims.time_of_day) || (segs.by_time_window) || [];
            if (wList.length === 0) {
                diagTimewindowTbody.innerHTML = `<tr><td colspan="4" class="text-center" style="padding:1rem;color:var(--text-muted);">No session time data recorded yet.</td></tr>`;
            } else {
                diagTimewindowTbody.innerHTML = "";
                wList.forEach((w) => {
                    const tr = document.createElement("tr");
                    const ptsClass = w.total_pts >= 0 ? "val-pos" : "val-neg";
                    const ptsSign = w.total_pts >= 0 ? "+" : "";
                    tr.innerHTML = `
                        <td class="text-left font-mono">${w.window || w.label}</td>
                        <td class="text-center font-mono">${w.count}</td>
                        <td class="text-right font-mono ${w.win_rate_pct >= 50 ? 'val-pos' : 'val-neg'}">${w.win_rate_pct}%</td>
                        <td class="text-right font-mono ${ptsClass}">${ptsSign}${w.total_pts.toFixed(1)} pts</td>
                    `;
                    diagTimewindowTbody.appendChild(tr);
                });
            }
        }
    }

    // Export Chain CSV Handler
    if (btnExportCsv) {
        btnExportCsv.addEventListener("click", exportChainToCsv);
    }

    // Trending OI View Tab Switching
    if (btnTabTrendingOi && btnTabStrikeBreakdown) {
        btnTabTrendingOi.addEventListener("click", () => {
            btnTabTrendingOi.classList.add("active");
            btnTabTrendingOi.style.background = "var(--accent-color, #3b82f6)";
            btnTabTrendingOi.style.color = "#fff";
            btnTabStrikeBreakdown.classList.remove("active");
            btnTabStrikeBreakdown.style.background = "transparent";
            btnTabStrikeBreakdown.style.color = "var(--text-muted, #94a3b8)";
            if (trendingOiTimeseriesContainer) trendingOiTimeseriesContainer.style.display = "block";
            if (trendingStrikeBreakdownContainer) trendingStrikeBreakdownContainer.style.display = "none";
        });

        btnTabStrikeBreakdown.addEventListener("click", () => {
            btnTabStrikeBreakdown.classList.add("active");
            btnTabStrikeBreakdown.style.background = "var(--accent-color, #3b82f6)";
            btnTabStrikeBreakdown.style.color = "#fff";
            btnTabTrendingOi.classList.remove("active");
            btnTabTrendingOi.style.background = "transparent";
            btnTabTrendingOi.style.color = "var(--text-muted, #94a3b8)";
            if (trendingOiTimeseriesContainer) trendingOiTimeseriesContainer.style.display = "none";
            if (trendingStrikeBreakdownContainer) trendingStrikeBreakdownContainer.style.display = "block";
        });
    }

    // Trending OI Interval Selection
    if (trendingIntervalSelect) {
        trendingIntervalSelect.addEventListener("change", () => {
            const intervalVal = trendingIntervalSelect.value;
            fetch(`/api/options-desk/trending-oi?symbol=${state.symbol}&interval=${intervalVal}`)
                .then(res => res.json())
                .then(res => {
                    if (res.success && res.data) {
                        const spot = parseFloat(document.getElementById("spot-price")?.textContent?.replace(/,/g, "") || "0");
                        renderTrendingOiTable(res.data, null, spot);
                    }
                })
                .catch(err => console.error("Error updating trending OI interval:", err));
        });
    }

    // Initial Load & Start Timers
    loadOptionsDeskData(false);
    loadSignalsData();
    loadTradeAnalysis();
    startCountdown();

    // Dedicated 5-second auto-refresh for Signal Tracker & Journal live prices & P&L
    setInterval(() => {
        loadSignalsData();
    }, 5000);
});


