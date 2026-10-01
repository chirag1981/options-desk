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
                loadSignalsData();
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
            const isAddition = item.change_oi >= 0;
            const deltaClass = isAddition ? "pos" : "neg";
            const deltaSign = isAddition ? "▲ +" : "▼ ";

            // Format directional impact tag
            let tagClass = "tag-neutral";
            let tagHtml = item.activity;
            let rowTitle = "";

            if (item.activity.includes("CALL WRITING") || (item.side === "CE" && item.change_oi > 0)) {
                tagClass = "tag-bearish";
                tagHtml = `<span class="dir-icon">🔻</span> Resistance Build`;
                rowTitle = "Call Writing: Institutional resistance building (Bearish Ceiling)";
            } else if (item.activity.includes("PUT WRITING") || (item.side === "PE" && item.change_oi > 0)) {
                tagClass = "tag-bullish";
                tagHtml = `<span class="dir-icon">🔺</span> Support Build`;
                rowTitle = "Put Writing: Institutional support building (Bullish Floor)";
            } else if (item.activity.includes("CALL UNWINDING") || (item.side === "CE" && item.change_oi < 0)) {
                tagClass = "tag-bullish";
                tagHtml = `<span class="dir-icon">↗️</span> Short Covering`;
                rowTitle = "Call Unwinding: Call sellers exiting (Bulls pushing higher)";
            } else if (item.activity.includes("PUT UNWINDING") || (item.side === "PE" && item.change_oi < 0)) {
                tagClass = "tag-bearish";
                tagHtml = `<span class="dir-icon">↘️</span> Support Cracking`;
                rowTitle = "Put Unwinding: Put sellers exiting (Downside risk increasing)";
            }

            row.setAttribute("title", rowTitle);
            row.innerHTML = `
                <div class="big-oi-strike-side">
                    <span class="side-pill ${sideClass}">${item.side}</span>
                    <span class="big-oi-strike font-mono">${formatIndianNumber(item.strike)}</span>
                </div>
                <div class="big-oi-delta ${deltaClass} font-mono">
                    ${deltaSign}${item.change_oi_formatted.replace("+", "").replace("-", "")} OI
                </div>
                <div class="big-oi-tag ${tagClass}">
                    ${tagHtml}
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

    // ==========================================================================
    // Signal Tracker & Paper Trading Journal Controller
    // ==========================================================================
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";

    // Journal DOM Elements
    const tabBtnActive = document.getElementById("tab-btn-active-signals");
    const tabBtnHistory = document.getElementById("tab-btn-history-signals");
    const paneActive = document.getElementById("pane-active-signals");
    const paneHistory = document.getElementById("pane-history-signals");
    const activeCountBadge = document.getElementById("active-signals-count-badge");
    const historyCountBadge = document.getElementById("history-signals-count-badge");

    const metricWinRate = document.getElementById("metric-win-rate");
    const metricWinRatio = document.getElementById("metric-win-ratio");
    const metricTotalPts = document.getElementById("metric-total-pts");
    const metricPtsSub = document.getElementById("metric-pts-sub");
    const metricNetInr = document.getElementById("metric-net-inr");
    const metricProfitFactor = document.getElementById("metric-profit-factor");
    const metricActiveCount = document.getElementById("metric-active-count");

    const activeSignalsTbody = document.getElementById("active-signals-tbody");
    const historySignalsTbody = document.getElementById("history-signals-tbody");
    const btnLogPaperTrade = document.getElementById("btn-log-paper-trade");
    const paperLotsInput = document.getElementById("paper-lots-input");
    const paperTradeMsg = document.getElementById("paper-trade-msg");
    const btnExportJournalCsv = document.getElementById("btn-export-journal-csv");
    const btnClearJournal = document.getElementById("btn-clear-journal");

    let lastSignalsSummary = null;

    /**
     * Tab Switcher (Active Trades vs Trade Journal)
     */
    if (tabBtnActive && tabBtnHistory && paneActive && paneHistory) {
        tabBtnActive.addEventListener("click", () => {
            tabBtnActive.classList.add("active");
            tabBtnHistory.classList.remove("active");
            paneActive.classList.add("active");
            paneHistory.classList.remove("active");
        });

        tabBtnHistory.addEventListener("click", () => {
            tabBtnHistory.classList.add("active");
            tabBtnActive.classList.remove("active");
            paneHistory.classList.add("active");
            paneActive.classList.remove("active");
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
        const history = data.history_signals || [];

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
        const winRate = closedCount > 0 ? ((winCount / closedCount) * 100).toFixed(1) : null;
        
        let profitFactor = null;
        if (grossLossPts > 0) {
            profitFactor = (grossWinPts / grossLossPts).toFixed(2);
        } else if (grossWinPts > 0) {
            profitFactor = grossWinPts.toFixed(2);
        } else if (closedCount > 0) {
            profitFactor = "1.00";
        }

        // 2. Metrics Cards
        if (metricWinRate) {
            if (winRate !== null) {
                metricWinRate.textContent = `${winRate}%`;
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
            if (profitFactor !== null) {
                metricProfitFactor.textContent = profitFactor;
            } else {
                metricProfitFactor.textContent = "--";
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
                    ${sig.is_paper_trade ? '<small style="color:var(--cyan-accent);font-size:0.65rem;">(Paper)</small>' : ''}
                </td>
                <td class="text-right font-mono">₹${sig.entry_price.toFixed(2)}</td>
                <td class="text-right font-mono" style="font-weight:700;">₹${sig.current_price.toFixed(2)}</td>
                <td class="text-right font-mono" style="font-size:0.75rem;color:var(--text-muted);">
                    L: ₹${sig.lowest_price.toFixed(1)} &bull; H: ₹${sig.highest_price.toFixed(1)}
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

            tr.innerHTML = `
                <td class="text-left font-mono" style="font-size:0.75rem;">
                    <div>${sig.created_at.split(" ")[0]}</div>
                    <div style="color:var(--text-muted);">${sig.created_at.split(" ")[1] || ""}</div>
                </td>
                <td class="text-left">
                    <span class="side-pill ${sig.signal_type}">${sig.symbol}</span>
                    <strong class="font-mono" style="margin-left:0.35rem;">${sig.contract_name}</strong>
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
            };

            try {
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
            }
        });
    }

    /**
     * Clear Closed Journal History Action
     */
    if (btnClearJournal) {
        btnClearJournal.addEventListener("click", async () => {
            if (confirm("Are you sure you want to clear all closed trade history from the journal?")) {
                try {
                    const resp = await fetch("/api/options-desk/signals/clear-history", {
                        method: "POST",
                        headers: { "X-CSRFToken": csrfToken },
                    });
                    const res = await resp.json();
                    if (res.success) {
                        await loadSignalsData();
                    }
                } catch (e) {
                    console.error("Failed to clear journal history:", e);
                }
            }
        });
    }

    /**
     * Export Trade Journal to CSV
     */
    if (btnExportJournalCsv) {
        btnExportJournalCsv.addEventListener("click", () => {
            if (!lastSignalsSummary || !lastSignalsSummary.history_signals) {
                alert("No trade history available to export.");
                return;
            }
            const history = lastSignalsSummary.history_signals;
            const headers = [
                "ID", "SYMBOL", "TYPE", "CONTRACT", "STRIKE", "EXPIRY",
                "ENTRY_PRICE", "EXIT_PRICE", "STOP_LOSS", "TARGET_1", "TARGET_2",
                "POINTS_PNL", "PNL_PCT", "LOTS", "LOT_SIZE", "NET_PNL_INR",
                "STATUS", "TRIGGER_REASON", "CREATED_AT", "EXIT_TIME"
            ];

            const rows = history.map((s) => [
                s.id, s.symbol, s.signal_type, `"${s.contract_name}"`, s.strike, s.expiry,
                s.entry_price, s.exit_price || s.current_price, s.stop_loss, s.target_1, s.target_2,
                s.points_pnl, s.pnl_pct, s.lots, s.lot_size, s.net_pnl_inr,
                `"${s.status}"`, `"${(s.trigger_reason || "").replace(/"/g, '""')}"`,
                `"${s.created_at}"`, `"${s.exit_time || ""}"`
            ]);

            let csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows.map((e) => e.join(","))].join("\n");
            const encodedUri = encodeURI(csvContent);
            const link = document.createElement("a");
            link.setAttribute("href", encodedUri);
            link.setAttribute("download", `Option_Desk_Trade_Journal_${new Date().toISOString().slice(0,10)}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        });
    }

    // Export Chain CSV Handler
    if (btnExportCsv) {
        btnExportCsv.addEventListener("click", exportChainToCsv);
    }

    // Initial Load & Start Timer
    loadOptionsDeskData(false);
    loadSignalsData();
    startCountdown();
});

