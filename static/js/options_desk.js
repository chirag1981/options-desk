/**
 * static/js/options_desk.js — Vanilla ES6 Client Controller for Fyers Option Desk
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

        const params = new URLSearchParams({
            symbol: state.symbol,
            expiry: state.expiry || "",
            refresh: isManual ? "true" : "false",
        });

        try {
            const resp = await fetch(`/api/options-desk/data?${params.toString()}`);
            const result = await resp.json();

            if (result.success && result.data) {
                state.lastData = result.data;
                renderDashboard(result.data);
                if (liveStatusText) liveStatusText.textContent = result.data.meta.data_status || "LIVE";
            } else {
                if (liveStatusText) liveStatusText.textContent = "ERROR";
                console.error("Options API Error:", result.error);
            }
        } catch (err) {
            if (liveStatusText) liveStatusText.textContent = "OFFLINE";
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
        if (levelR2) levelR2.textContent = formatIndianNumber(levels.resistance_2);
        if (levelR1) levelR1.textContent = formatIndianNumber(levels.resistance_1);
        if (levelAtm) levelAtm.textContent = formatIndianNumber(levels.atm_strike);
        if (levelS1) levelS1.textContent = formatIndianNumber(levels.support_1);
        if (levelS2) levelS2.textContent = formatIndianNumber(levels.support_2);

        if (levelAtmDist && bias.spot_price && levels.atm_strike) {
            const dist = (bias.spot_price - levels.atm_strike).toFixed(1);
            const distSign = dist >= 0 ? "+" : "";
            levelAtmDist.textContent = `Spot Distance: ${distSign}${dist} pts`;
        }

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
            if (dec === "CE") focusHeroBox.classList.add("bullish-hero");
            else if (dec === "PE") focusHeroBox.classList.add("bearish-hero");
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

        // 7. Section 3: OI Trend (ATM ± 8 strikes)
        renderOiTrendTable(data.oi_trend || [], levels.atm_strike, bias.spot_price);

        // 8. Section 4: Detailed Option Chain
        renderOptionChainTable(data.detailed_chain || [], levels.atm_strike, levels.resistance_1, levels.support_1, bias.spot_price);
    }

    /**
     * Renders Big OI Movements
     */
    function renderBigOiMovements(movements) {
        if (!bigOiList) return;
        if (movements.length === 0) {
            bigOiList.innerHTML = `<div class="empty-state">No large OI movements meeting threshold.</div>`;
            return;
        }

        bigOiList.innerHTML = "";
        movements.forEach((item) => {
            const row = document.createElement("div");
            row.className = "big-oi-item";

            const sideClass = item.side === "CE" ? "CE" : "PE";
            const deltaClass = item.change_oi >= 0 ? "pos" : "neg";

            row.innerHTML = `
                <div class="big-oi-strike-side">
                    <span class="side-pill ${sideClass}">${item.side}</span>
                    <span class="big-oi-strike">${formatIndianNumber(item.strike)}</span>
                </div>
                <div class="big-oi-delta ${deltaClass}">
                    ${item.change_oi_formatted} OI
                </div>
                <div class="big-oi-tag">
                    ${item.activity}
                </div>
            `;
            bigOiList.appendChild(row);
        });
    }

    /**
     * Renders OI Trend Table (ATM ± 8 strikes)
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

    if (btnExportCsv) {
        btnExportCsv.addEventListener("click", exportChainToCsv);
    }

    // Initial Load & Start Timer
    loadOptionsDeskData(false);
    startCountdown();
});
