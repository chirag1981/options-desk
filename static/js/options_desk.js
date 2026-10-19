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
        refreshIntervalSec: 60, // 60s auto-refresh for real-time barrier & level updates
        timerRemaining: 60,
        timerId: null,
        isFetching: false,
        lastData: null,
        trendingData: null,
        trendingLastFetchTime: 0,
        trendingTimerId: null,
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
    const biasStrikePill = document.getElementById("bias-strike-pill");
    const confidencePill = document.getElementById("confidence-pill");
    const scorePill = document.getElementById("score-pill");
    const scorePillVal = document.getElementById("score-pill-val");
    const biasLastChanged = document.getElementById("bias-last-changed");
    const whyBiasList = document.getElementById("why-bias-list");
    const tomorrowBiasPill = document.getElementById("tomorrow-bias-pill");
    const tomorrowBiasDetail = document.getElementById("tomorrow-bias-detail");

    // Key Levels Elements (OI walls - Compact)
    const levelsSpotVal = document.getElementById("levels-spot-val");
    const levelsStatusChip = document.getElementById("levels-status-chip");
    const levelsStatusText = document.getElementById("levels-status-text");
    const levelsRoomVal = document.getElementById("levels-room-val");
    const oiWallsUnavailable = document.getElementById("oi-walls-unavailable");
    const oiWallsBarContainer = document.getElementById("oi-walls-bar-container");
    const barS1Val = document.getElementById("bar-s1-val");
    const barR1Val = document.getElementById("bar-r1-val");
    const oiBarTrack = document.getElementById("oi-bar-track");
    const oiBarAtmTick = document.getElementById("oi-bar-atm-tick");
    const oiBarSpotMarker = document.getElementById("oi-bar-spot-marker");
    const oiBarSpotLabel = document.getElementById("oi-bar-spot-label");
    const distS1Val = document.getElementById("dist-s1-val");
    const distR1Val = document.getElementById("dist-r1-val");
    const levelS2Val = document.getElementById("level-s2-val");
    const levelR2Val = document.getElementById("level-r2-val");
    const levelsOuterTargets = document.getElementById("levels-outer-targets");

    // Nearest S & R Barriers Elements (Card 4)
    const barrierResStrike = document.getElementById("barrier-res-strike");
    const barrierResDist = document.getElementById("barrier-res-dist");
    const barrierResArrowBadge = document.getElementById("barrier-res-arrow-badge");
    const barrierResArrow = document.getElementById("barrier-res-arrow");
    const barrierResStatus = document.getElementById("barrier-res-status");
    const barrierResChg = document.getElementById("barrier-res-chg");
    const barrierSpotVal = document.getElementById("barrier-spot-val");
    const barrierSupStrike = document.getElementById("barrier-sup-strike");
    const barrierSupDist = document.getElementById("barrier-sup-dist");
    const barrierSupArrowBadge = document.getElementById("barrier-sup-arrow-badge");
    const barrierSupArrow = document.getElementById("barrier-sup-arrow");
    const barrierSupStatus = document.getElementById("barrier-sup-status");
    const barrierSupChg = document.getElementById("barrier-sup-chg");

    // Top Ticker / OI Totals Elements
    const totalCeOi = document.getElementById("total-ce-oi");
    const totalCeChg = document.getElementById("total-ce-chg");
    const totalPeOi = document.getElementById("total-pe-oi");
    const totalPeChg = document.getElementById("total-pe-chg");

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

    // Helper function for sentiment badge class
    function getSentimentBadgeClass(sentimentStr) {
        const s = (sentimentStr || "").toLowerCase();
        if (s.includes("strong bullish")) return "badge-sentiment-strong-bullish";
        if (s.includes("strong bearish")) return "badge-sentiment-strong-bearish";
        if (s.includes("bullish")) return "badge-sentiment-bullish";
        if (s.includes("bearish")) return "badge-sentiment-bearish";
        return "badge-sentiment-neutral";
    }

    // Trending OI Elements
    const trendingIntervalSelect = document.getElementById("trending-interval-select");
    const trendingSelectedStrikesContainer = document.getElementById("trending-selected-strikes-container");
    const trendingLiveCapsuleSummary = document.getElementById("trending-live-capsule-summary");
    const trendingOiTimeseriesContainer = document.getElementById("trending-oi-timeseries-container");
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
            const result = await resp.json();

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
        if (x === undefined || x === null || x === "None" || x === "" || isNaN(Number(x))) return "--";
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
     * Formats OI flow values in Professional Clean Terminal style:
     * - legType: 'CE'   (Call: + is Bearish/Red, - is Bullish/Green)
     *            'PE'   (Put: + is Bullish/Green, - is Bearish/Red)
     *            'DIFF' (Net: + is Bullish/Green, - is Bearish/Red)
     *            'DIR'  (Shift: + is Bullish/Green, - is Bearish/Red)
     */
    function formatOiFlowCell(x, legType = 'DIFF') {
        if (x === undefined || x === null || isNaN(Number(x))) {
            return `<span class="oi-val val-zero">--</span>`;
        }
        const num = Number(x);
        if (num === 0) {
            return `<span class="oi-val val-zero">0</span>`;
        }
        const absVal = Math.abs(num);
        const sign = num > 0 ? "+" : "-";
        const compactText = formatCompactOi(num);
        const fullText = formatIndianNumber(absVal);

        let isBullish = false;
        if (legType === 'CE') {
            isBullish = num < 0; // Call covering is green, Call writing is red
        } else {
            isBullish = num >= 0; // Put writing is green, Put unwinding is red
        }

        const cls = isBullish ? "val-bullish" : "val-bearish";
        return `<span class="oi-val ${cls}" title="${sign}${fullText} contracts">${compactText}</span>`;
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
        const morning = data.morning_bias || {};
        const biasType = (morning.badge || morning.direction || bias.bias || "NEUTRAL").toUpperCase();
        const badgeClass = morning.badge_class || (bias.bias || "neutral").toLowerCase();
        
        if (biasText) biasText.textContent = morning.badge || biasType;
        
        if (cardMarketBias) {
            cardMarketBias.className = `desk-card card-bias bias-${badgeClass}`;
        }
        if (biasBadge) {
            biasBadge.className = `bias-badge-large ${badgeClass}`;
        }
        if (biasStrikePill) {
            const activeContract = (morning.suggested_contract && morning.suggested_contract !== "--")
                ? morning.suggested_contract
                : ((data.option_buying && data.option_buying.selected_option && data.option_buying.selected_option !== "--") ? data.option_buying.selected_option : "--");
            biasStrikePill.textContent = activeContract;
        }
        if (confidencePill) {
            confidencePill.textContent = `Confidence: ${morning.confidence || bias.confidence || "MEDIUM"}`;
        }
        if (scorePillVal) {
            scorePillVal.textContent = morning.score !== undefined ? morning.score : (bias.bullish_score || 0);
        }
        if (biasLastChanged) {
            biasLastChanged.textContent = bias.last_changed || "--:--";
        }

        // Render Explainability Reasons
        if (whyBiasList) {
            whyBiasList.innerHTML = "";
            const reasons = (morning.why_reasons && morning.why_reasons.length > 0) ? morning.why_reasons : (bias.why_reasons || []);
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

        // Render Tomorrow Bias Strip (Card 1 Footer - Option 1)
        const tomorrow = data.tomorrow_bias || {};
        if (tomorrowBiasPill) {
            tomorrowBiasPill.textContent = tomorrow.badge || "NEUTRAL";
            tomorrowBiasPill.className = `tomorrow-bias-pill font-mono ${tomorrow.badge_class || "neutral"}`;
        }
        if (tomorrowBiasDetail) {
            if (tomorrow.detail) {
                tomorrowBiasDetail.textContent = tomorrow.detail;
            } else if (tomorrow.pivot) {
                tomorrowBiasDetail.textContent = `Next Pivot: ${formatIndianNumber(tomorrow.pivot)} | CPR: ${tomorrow.cpr_type || 'NORMAL'} | Resist: ${tomorrow.key_wall || '--'} | Base: ${tomorrow.key_support || '--'}`;
            } else {
                tomorrowBiasDetail.textContent = `Next Pivot: ${formatIndianNumber(levels.atm_strike || 0)} | Resistance: ${levels.resistance_1 || '--'} | Base: ${levels.support_1 || '--'}`;
            }
        }

        // 3. Card 2: Key Levels (OI walls - Compact)
        const spot = bias.spot_price || 0;
        const s1 = levels.support_1;
        const r1 = levels.resistance_1;
        const s2 = levels.support_2;
        const r2 = levels.resistance_2;
        const isAvailable = levels.is_available === true;

        if (levelsSpotVal) {
            levelsSpotVal.textContent = spot > 0 ? `₹${formatIndianNumber(spot)}` : "₹--";
        }

        if (levelS2Val) levelS2Val.textContent = s2 ? formatIndianNumber(s2) : "--";
        if (levelR2Val) levelR2Val.textContent = r2 ? formatIndianNumber(r2) : "--";

        if (!isAvailable) {
            if (oiWallsUnavailable) oiWallsUnavailable.style.display = "flex";
            if (oiWallsBarContainer) oiWallsBarContainer.style.display = "none";
            if (levelsOuterTargets) levelsOuterTargets.style.display = "none";
            if (levelsStatusChip) {
                levelsStatusChip.className = "levels-status-chip font-mono chip-unavailable";
            }
            if (levelsStatusText) levelsStatusText.textContent = "OI WALLS UNAVAILABLE";
            if (levelsRoomVal) levelsRoomVal.textContent = "--";
        } else {
            if (oiWallsUnavailable) oiWallsUnavailable.style.display = "none";
            if (oiWallsBarContainer) oiWallsBarContainer.style.display = "flex";
            if (levelsOuterTargets) levelsOuterTargets.style.display = "flex";

            // Strikes
            if (barS1Val) barS1Val.textContent = formatIndianNumber(s1);
            if (barR1Val) barR1Val.textContent = formatIndianNumber(r1);

            // Status Chip & Class
            const chipStatus = levels.status_chip || "MID-RANGE";
            if (levelsStatusText) levelsStatusText.textContent = chipStatus;
            if (levelsStatusChip) {
                let chipCls = "chip-mid-range";
                if (chipStatus === "NEAR SUPPORT") chipCls = "chip-near-support";
                else if (chipStatus === "NEAR RESISTANCE") chipCls = "chip-near-resistance";
                else if (chipStatus === "BREAKOUT") chipCls = "chip-breakout";
                else if (chipStatus === "BREAKDOWN") chipCls = "chip-breakdown";
                levelsStatusChip.className = `levels-status-chip font-mono ${chipCls}`;
            }

            // Room / Risk metric
            if (levelsRoomVal) {
                const ratio = levels.room_ratio;
                levelsRoomVal.textContent = (ratio !== undefined && ratio !== null && !isNaN(ratio)) ? `${Number(ratio).toFixed(1)}x` : "--";
            }

            // Proportional position & clamping
            const posPct = levels.position_pct !== undefined && levels.position_pct !== null
                ? Math.max(0, Math.min(100, Number(levels.position_pct)))
                : 50;

            if (oiBarSpotMarker) {
                oiBarSpotMarker.style.left = `${posPct}%`;
            }
            if (oiBarSpotLabel) {
                oiBarSpotLabel.textContent = spot > 0 ? formatIndianNumber(spot) : "--";
                if (posPct > 80) {
                    const shift = 50 + (posPct - 80) * 2.2;
                    oiBarSpotLabel.style.transform = `translateX(-${shift}%)`;
                } else if (posPct < 20) {
                    const shift = Math.max(0, 50 - (20 - posPct) * 2.2);
                    oiBarSpotLabel.style.transform = `translateX(-${shift}%)`;
                } else {
                    oiBarSpotLabel.style.transform = "translateX(-50%)";
                }
            }

            // ATM Tick on bar (only when it differs from S1/R1)
            if (oiBarAtmTick) {
                if (levels.atm_tick_pct !== undefined && levels.atm_tick_pct !== null) {
                    const atmPct = Math.max(0, Math.min(100, Number(levels.atm_tick_pct)));
                    oiBarAtmTick.style.left = `${atmPct}%`;
                    oiBarAtmTick.style.display = "block";
                } else {
                    oiBarAtmTick.style.display = "none";
                }
            }

            // Distances in points under each end
            if (distS1Val) {
                const dS1 = levels.dist_to_s1;
                distS1Val.textContent = (dS1 !== undefined && dS1 !== null)
                    ? `${dS1 >= 0 ? "+" : ""}${dS1} pts`
                    : "-- pts";
            }
            if (distR1Val) {
                const dR1 = levels.dist_to_r1;
                distR1Val.textContent = (dR1 !== undefined && dR1 !== null)
                    ? `${dR1 >= 0 ? "+" : ""}${dR1} pts`
                    : "-- pts";
            }
        }

        // Market Vector & Seller Action (Card 2)
        const mv = data.market_vector || (levels && levels.market_vector) || {};
        const vectorBox = document.getElementById("market-vector-box");
        const vectorHeadText = document.getElementById("vector-head-text");
        const vectorModePill = document.getElementById("vector-mode-pill");
        const vectorSellerAction = document.getElementById("vector-seller-action");
        const vectorPlaybookText = document.getElementById("vector-playbook-text");

        if (vectorBox) {
            const modeCls = mv.mode_class || "neutral";
            vectorBox.className = `market-vector-box mode-${modeCls}`;
        }
        if (vectorHeadText) {
            vectorHeadText.textContent = mv.title || "ANALYZING FLOW...";
        }
        if (vectorModePill) {
            vectorModePill.textContent = mv.mode_pill || "--";
        }
        if (vectorSellerAction) {
            vectorSellerAction.textContent = mv.seller_action || "Evaluating institutional writing flow...";
        }
        if (vectorPlaybookText) {
            vectorPlaybookText.textContent = mv.playbook || "Awaiting boundary confirmation.";
        }

        // 4. Card 4: Nearest S & R Barriers (Writer Positioning & Flow)
        const barriers = data.nearest_barriers || (levels && levels.nearest_barriers) || {};
        const sup = barriers.support || {};
        const res = barriers.resistance || {};

        if (barrierSpotVal) {
            const spotVal = barriers.spot_price || spot || (bias && bias.spot_price) || 0;
            barrierSpotVal.textContent = spotVal > 0 ? `₹${formatIndianNumber(spotVal)}` : "₹--";
        }

        // Resistance (Call Writers)
        if (barrierResStrike) {
            const rStrike = res.strike || levels.resistance_1;
            barrierResStrike.textContent = rStrike ? formatIndianNumber(Math.round(Number(rStrike))) : "--";
        }
        if (barrierResDist) {
            const rDist = res.dist_pts !== undefined ? res.dist_pts : (levels.dist_to_r1 !== undefined ? Math.abs(levels.dist_to_r1) : null);
            barrierResDist.textContent = (rDist !== null && rDist !== undefined) ? `+${rDist} pts` : "+-- pts";
        }
        if (barrierResStatus) {
            barrierResStatus.textContent = res.status || "Call Writers Adding";
        }
        if (barrierResChg) {
            barrierResChg.textContent = res.change_formatted || (res.change_oi !== undefined ? formatCompactOi(res.change_oi) : "--");
        }
        if (barrierResArrow) {
            barrierResArrow.textContent = res.arrow || "―";
        }
        if (barrierResArrowBadge) {
            barrierResArrowBadge.className = `barrier-arrow-badge ${res.arrow_class || "arrow-neutral"}`;
        }

        // Support (Put Writers)
        if (barrierSupStrike) {
            const sStrike = sup.strike || levels.support_1;
            barrierSupStrike.textContent = sStrike ? formatIndianNumber(Math.round(Number(sStrike))) : "--";
        }
        if (barrierSupDist) {
            const sDist = sup.dist_pts !== undefined ? sup.dist_pts : (levels.dist_to_s1 !== undefined ? Math.abs(levels.dist_to_s1) : null);
            barrierSupDist.textContent = (sDist !== null && sDist !== undefined) ? `-${sDist} pts` : "--- pts";
        }
        if (barrierSupStatus) {
            barrierSupStatus.textContent = sup.status || "Put Writers Adding";
        }
        if (barrierSupChg) {
            barrierSupChg.textContent = sup.change_formatted || (sup.change_oi !== undefined ? formatCompactOi(sup.change_oi) : "--");
        }
        if (barrierSupArrow) {
            barrierSupArrow.textContent = sup.arrow || "―";
        }
        if (barrierSupArrowBadge) {
            barrierSupArrowBadge.className = `barrier-arrow-badge ${sup.arrow_class || "arrow-neutral"}`;
        }

        // 5. Card 3: Option Buying Focus & Execution Plan
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
        if (tradeRr) {
            const rawRr = tradePlan.risk_reward || "1:2.0";
            tradeRr.textContent = rawRr.replace(/\s*:\s*/g, ":");
        }

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

        // 5. Total OI Totals (Top Ticker)
        if (totalCeOi) totalCeOi.textContent = summary.total_ce_oi || "--";
        if (totalCeChg) totalCeChg.textContent = `(${summary.total_ce_change_oi || "--"})`;
        if (totalPeOi) totalPeOi.textContent = summary.total_pe_oi || "--";
        if (totalPeChg) totalPeChg.textContent = `(${summary.total_pe_change_oi || "--"})`;

        // 6. Section 3: Trending OI (ATM ± 5 strikes)
        // Automatically update Trending OI whenever fresh desk data arrives
        if (data.trending_oi_timeseries && data.trending_oi_timeseries.rows) {
            state.trendingData = data.trending_oi_timeseries;
            state.trendingLastFetchTime = Date.now();
        }
        if (state.trendingData) {
            renderTrendingOiTable(state.trendingData, levels.atm_strike, bias.spot_price);
        }


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
            const pctSign = latest.strength_pct > 0 ? "+" : "";
            const dots = latest.strength_dots || 0;
            const absPct = Math.abs(latest.strength_pct || 0);
            const isGrayed = absPct < 30 || latest.strength_class === "strength-weak";

            let dotPillHtml = "";
            if (dots >= 2) {
                dotPillHtml = `<span class="strength-dots-pill">${'<span class="strength-dot"></span>'.repeat(dots)}</span>`;
            } else if (dots === 1) {
                dotPillHtml = `<span class="strength-dots-pill pill-weak"><span class="strength-dot"></span></span>`;
            }

            const convictionBadge = dots >= 2 ?
                `<span class="badge-conviction-status status-confirmed" title="Oi Pulse Conviction Rule met: >=40% diff with 2+ dots">✓ Confirmed (&ge;40%, ${dots} dots)</span>` :
                (absPct >= 30 ?
                    `<span class="badge-conviction-status status-weak" title="Weak sentiment (<40% or <2 dots) — Avoid trading action">⚠️ Weak Trend (&lt;40%)</span>` :
                    `<span class="badge-conviction-status status-grayed" title="Grayed out (<30%) — Indecisive / noise zone">○ Grayed Out (&lt;30%)</span>`
                );

            // Handle Divergence Alert Banner
            const divAlertEl = document.getElementById("trending-divergence-alert-container");
            if (divAlertEl) {
                if (latest && latest.divergence_badge) {
                    const trapClass = latest.divergence === "BULL_TRAP" ? "divergence-bull-trap" : "divergence-bear-trap";
                    divAlertEl.className = `trending-oi-divergence-banner ${trapClass}`;
                    divAlertEl.innerHTML = `<span><strong>${latest.divergence_badge}</strong> &mdash; ${latest.divergence_detail || 'Price breaking range in opposite direction of cumulative OI.'}</span>`;
                    divAlertEl.style.display = "flex";
                } else {
                    divAlertEl.style.display = "none";
                }
            }

            trendingLiveCapsuleSummary.innerHTML = `
                <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;" title="Oi Pulse Conviction Rule: >=40% diff with 2+ dots indicates trading conviction. Below 30% is grayed out noise.">
                    <span style="font-size:0.75rem;color:var(--text-muted,#94a3b8);font-weight:600;">Live Strength:</span>
                    <div class="strength-capsule ${latest.strength_class || 'strength-weak'} ${isGrayed ? 'is-grayed' : ''}">
                        <span class="strength-pct">${pctSign}${latest.strength_pct}%</span>
                        ${dotPillHtml}
                    </div>
                    ${convictionBadge}
                    <span class="badge ${getSentimentBadgeClass(latest.sentiment)}" style="margin-left:0.2rem;">
                        ${latest.sentiment}
                    </span>
                    ${latest.divergence_badge ? `
                        <span class="badge-divergence-pill ${latest.divergence === 'BULL_TRAP' ? 'pill-bull-trap' : 'pill-bear-trap'}" style="margin-left:0.3rem;" title="${latest.divergence_detail || ''}">
                            ${latest.divergence_badge}
                        </span>
                    ` : ''}
                </div>
            `;
        }

        // Render table rows (Strictly within market hours 09:10:00 - 15:30:00)
        trendingPulseTbody.innerHTML = "";
        const rowsSelect = document.getElementById("trending-rows-select");
        const rowLimit = rowsSelect && rowsSelect.value !== "all" ? parseInt(rowsSelect.value, 10) : Infinity;
        let displayedCount = 0;
        trendingTimeseries.rows.forEach(r => {
            if (displayedCount >= rowLimit) {
                return;
            }
            if (r.time) {
                const timeNorm = r.time.length === 5 ? r.time + ":00" : r.time;
                if (timeNorm < "09:10:00" || timeNorm > "15:30:00") {
                    return;
                }
            }
            displayedCount++;
            const tr = document.createElement("tr");

            // 1. Date & Time
            const dateStr = r.date || "--";
            const timeStr = r.time || "--";

            // 2. LTP
            const ltpVal = typeof r.ltp === "number" ? r.ltp.toFixed(2) : (r.ltp || "--");

            // 3. Format Day H/L Break
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

            // 4. Compact & formatted values
            const ceChgHtml = formatOiFlowCell(r.ce_change_oi, 'CE');
            const peChgHtml = formatOiFlowCell(r.pe_change_oi, 'PE');
            const diffOiHtml = formatOiFlowCell(r.diff_oi, 'DIFF');

            // 5. Strength capsule matching Oi Pulse specification
            const pctSign = r.strength_pct > 0 ? "+" : "";
            const dots = r.strength_dots || 0;
            const absPct = Math.abs(r.strength_pct || 0);
            const isGrayed = absPct < 30 || r.strength_class === "strength-weak";

            let dotPillHtml = "";
            if (dots >= 2) {
                dotPillHtml = `<span class="strength-dots-pill">${'<span class="strength-dot"></span>'.repeat(dots)}</span>`;
            } else if (dots === 1) {
                dotPillHtml = `<span class="strength-dots-pill pill-weak"><span class="strength-dot"></span></span>`;
            }

            const strengthHtml = `
                <div class="strength-capsule ${r.strength_class || 'strength-weak'} ${isGrayed ? 'is-grayed' : ''}" title="${r.strength_pct}% (${dots} dots) - ${dots >= 2 ? 'Confirmed trend conviction' : (dots === 1 ? 'Weak sentiment' : 'Grayed out noise zone')}">
                    <span class="strength-pct">${pctSign}${r.strength_pct}%</span>
                    ${dotPillHtml}
                </div>
            `;

            // 6. Direction of chng. (Square button with Up/Down arrow)
            const isUp = r.direction_arrow === "↑" || r.direction_color === "green" || (r.chng_in_direction >= 0);
            const dirBtnHtml = isUp
                ? `<span class="direction-btn dir-up" title="Direction: Up">↑</span>`
                : `<span class="direction-btn dir-down" title="Direction: Down">↓</span>`;

            // 7. Chng. In Direction & %
            const chngDirHtml = formatOiFlowCell(r.chng_in_direction, 'DIR');
            const dirPctVal = typeof r.direction_chng_pct === "number" ? r.direction_chng_pct : Number(r.direction_chng_pct || 0);
            const dirPctSign = dirPctVal > 0 ? "+" : "";
            const dirPctClass = dirPctVal > 0 ? "val-bullish" : (dirPctVal < 0 ? "val-bearish" : "val-zero");
            const dirPctHtml = `<span class="oi-val ${dirPctClass} font-mono">${dirPctSign}${dirPctVal.toFixed(2)}%</span>`;

            // 8. Net PCR
            const netPcrVal = r.net_pcr != null ? Number(r.net_pcr).toFixed(2) : "--";

            // 9. Day High/Low Diff in OI badge
            let dayHlDiffHtml = "-";
            if (r.day_hl_diff_oi === "Day Low Break") {
                dayHlDiffHtml = `<span class="badge-diff-break-low">Day Low Break</span>`;
            } else if (r.day_hl_diff_oi === "Day High Break") {
                dayHlDiffHtml = `<span class="badge-diff-break-high">Day High Break</span>`;
            }

            // 10. Build-up Badge & Sub-activity
            let buildupClass = r.buildup_class || "buildup-neutral";
            const buildupLabel = r.buildup_label || "Neutral";
            if (buildupLabel === "Consolidation" || (r.buildup_type && r.buildup_type.includes("CONSOLIDATION"))) {
                buildupClass = "buildup-consolidation";
            }
            const subActText = r.sub_activity || "";
            const sentClass = getSentimentBadgeClass(r.sentiment);

            // Divergence badge in sentiment cell
            const divBadgeHtml = r.divergence_badge ? `
                <span class="badge-divergence-pill ${r.divergence === 'BULL_TRAP' ? 'pill-bull-trap' : 'pill-bear-trap'}" title="${r.divergence_detail || ''}" style="margin-top:2px;">
                    ${r.divergence_badge}
                </span>
            ` : '';

            tr.innerHTML = `
                <td class="text-center font-mono" style="font-size:0.75rem;color:var(--text-secondary,#64748b);">${dateStr}</td>
                <td class="text-center font-mono"><strong style="font-size:0.75rem;">${timeStr}</strong></td>
                <td class="text-right font-mono font-semibold">₹${ltpVal}</td>
                <td class="text-center">${dlbHtml}</td>
                <td class="text-right font-mono">${ceChgHtml}</td>
                <td class="text-right font-mono">${peChgHtml}</td>
                <td class="text-right font-mono font-bold">${diffOiHtml}</td>
                <td class="text-center">${strengthHtml}</td>
                <td class="text-center">${dirBtnHtml}</td>
                <td class="text-right font-mono">${chngDirHtml}</td>
                <td class="text-right font-mono">${dirPctHtml}</td>
                <td class="text-center">${dayHlDiffHtml}</td>
                <td class="text-center">
                    <div style="display:inline-flex;flex-direction:column;align-items:center;gap:2px;">
                        <span class="${sentClass}">${r.sentiment || "--"}</span>
                        ${buildupLabel !== "Neutral" ? `<span class="badge-buildup ${buildupClass}" title="${subActText}" style="font-size:0.62rem;padding:1px 5px;">${buildupLabel}</span>` : ''}
                        ${divBadgeHtml}
                    </div>
                </td>
            `;

            trendingPulseTbody.appendChild(tr);
        });

        if (displayedCount === 0) {
            trendingPulseTbody.innerHTML = `<tr><td colspan="13" class="text-center" style="padding:1.5rem;color:var(--text-muted,#94a3b8);">No Trending OI data available during market hours (09:10 - 15:30).</td></tr>`;
        }
    }





    // Event Listeners
    if (symbolSelect) {
        symbolSelect.addEventListener("change", async (e) => {
            state.symbol = e.target.value;
            state.trendingData = null;
            state.trendingLastFetchTime = 0;
            await updateExpiriesForSymbol(state.symbol);
            loadOptionsDeskData(true);
        });
    }

    if (expirySelect) {
        expirySelect.addEventListener("change", (e) => {
            state.expiry = e.target.value;
            state.trendingData = null;
            state.trendingLastFetchTime = 0;
            loadOptionsDeskData(true);
        });
    }

    if (btnRefresh) {
        btnRefresh.addEventListener("click", () => {
            loadOptionsDeskData(true);
            loadSignalsData();
            loadTradeAnalysis();
            startCountdown();
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
                const reachedT2 = (sig.highest_price || 0) >= (sig.target_2 || Infinity);
                const reachedT1 = (sig.highest_price || 0) >= (sig.target_1 || Infinity) || Boolean(sig.t1_hit_at);
                if (reachedT2) {
                    outcomeText = "🎯 T2 ACHIEVED & TSL";
                } else if (reachedT1) {
                    outcomeText = "🛡️ T1 ACHIEVED & TSL";
                } else if ((sig.points_pnl || 0) >= 0) {
                    outcomeText = "🛡️ TRAILED BE / TSL";
                } else {
                    outcomeText = "🛡️ TRAILING SL HIT";
                }
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

            let entryPrice = parseFloat(tradePlan.entry_price || tradePlan.ask_at_entry || optBuying.entry_price || 0);
            let strike = tradePlan.strike;
            let contractName = tradePlan.contract_name || optBuying.selected_option || "";
            let tradeType = tradePlan.type || (contractName.includes("PE") || (optBuying.decision && optBuying.decision.includes("PE")) ? "PE" : "CE");
            let expiry = tradePlan.expiry || (state.lastData.meta ? state.lastData.meta.expiry : "");

            // Fallback: If tradePlan is missing, pull ATM contract from chain
            if (entryPrice <= 0 || !strike) {
                const chain = state.lastData.detailed_chain || state.lastData.chain || [];
                const atmStrike = state.lastData.key_levels ? state.lastData.key_levels.atm_strike : 0;
                const row = chain.find(r => Math.abs(r.strike - atmStrike) < 1) || chain[0];
                if (row) {
                    strike = row.strike;
                    const isPe = tradeType === "PE";
                    entryPrice = parseFloat((isPe ? row.pe_ask || row.pe_ltp : row.ce_ask || row.ce_ltp) || 0);
                    contractName = `${strike} ${tradeType}`;
                }
            }

            if (entryPrice <= 0) {
                alert("No active option contract price available to trade.");
                return;
            }

            const lots = parseInt(paperLotsInput ? paperLotsInput.value : "1", 10) || 1;
            const bidPrice = parseFloat(tradePlan.bid_at_entry !== undefined && tradePlan.bid_at_entry > 0 ? tradePlan.bid_at_entry : Math.max(0.05, entryPrice - (tradePlan.spread_paid || 0.5)));
            const askPrice = parseFloat(tradePlan.ask_at_entry !== undefined && tradePlan.ask_at_entry > 0 ? tradePlan.ask_at_entry : entryPrice);

            // Automatically execute with SL, T1, and T2
            const riskPts = parseFloat(tradePlan.risk_pts) || Math.max(3.0, Math.min(8.0, Math.round(entryPrice * 0.15 * 10) / 10));
            const stopLoss = parseFloat(tradePlan.stop_loss) > 0 ? parseFloat(tradePlan.stop_loss) : Math.max(0.05, Math.round((entryPrice - riskPts) * 100) / 100);
            const target1 = parseFloat(tradePlan.target_1) > 0 ? parseFloat(tradePlan.target_1) : Math.round((entryPrice + (riskPts * 1.5)) * 100) / 100;
            const target2 = parseFloat(tradePlan.target_2) > 0 ? parseFloat(tradePlan.target_2) : Math.round((entryPrice + (riskPts * 2.5)) * 100) / 100;

            const payload = {
                symbol: state.symbol,
                type: tradeType,
                contract_name: contractName,
                strike: strike,
                expiry: expiry,
                spot_price: bias.spot_price,
                entry_price: entryPrice,
                bid_at_entry: bidPrice,
                ask_at_entry: askPrice,
                spread_paid: tradePlan.spread_paid || Math.round((askPrice - bidPrice) * 100) / 100,
                stop_loss: stopLoss,
                target_1: target1,
                target_2: target2,
                risk_reward: tradePlan.risk_reward || "1:2.0",
                setup_score: optBuying.setup_score || 0,
                reason: optBuying.reason || "Manual Paper Trade Setup",
                lots: lots,
                pcr: bias.pcr || (state.lastData && state.lastData.pcr) || 1.0,
                bias: bias.bias || "NEUTRAL",
                atm_iv: (state.lastData && state.lastData.atm_iv) || 0.0,
                moneyness: tradePlan.moneyness || "ATM",
                pillar_flags: optBuying.pillar_flags || {},
                raw_values: optBuying.raw_values || {},
                data_quality: optBuying.data_quality || {},
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
                        paperTradeMsg.textContent = `✓ Logged ${contractName} (${lots} Lot)!`;
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
        const totalTrades = samples.closed_trades || samples.valid_closed_trades || samples.closed_trades_raw || 0;

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
            const pts = perf.total_net_pts !== undefined ? perf.total_net_pts : (perf.total_pts || 0.0);
            const sign = pts >= 0 ? '+' : '';
            diagOvPts.textContent = `${sign}${pts.toFixed(1)} pts`;
            diagOvPts.className = `d-val font-mono ${pts >= 0 ? 'val-win' : 'val-loss'}`;
        }

        if (diagOvAvgWinLoss) {
            const avgW = perf.avg_win_pts !== undefined ? `+${perf.avg_win_pts} pts` : `+${perf.avg_win_r || 0}R`;
            const avgL = perf.avg_loss_pts !== undefined ? `-${perf.avg_loss_pts} pts` : `-${perf.avg_loss_r || 0}R`;
            diagOvAvgWinLoss.textContent = `Avg W: ${avgW} | Avg L: ${avgL}`;
        }

        if (diagOvInr) {
            const inr = perf.total_net_inr !== undefined ? perf.total_net_inr : (perf.total_inr || 0.0);
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
                    const rawPts = m.total_net_pts !== undefined ? m.total_net_pts : m.total_pts;
                    const mPts = typeof rawPts === 'number' ? rawPts : (parseFloat(rawPts) || 0);
                    const ptsClass = mPts >= 0 ? "val-pos" : "val-neg";
                    const ptsSign = mPts >= 0 ? "+" : "";
                    tr.innerHTML = `
                        <td class="text-left font-mono"><strong>${m.moneyness || '--'}</strong></td>
                        <td class="text-center font-mono">${m.count || 0}</td>
                        <td class="text-right font-mono ${(m.win_rate_pct || 0) >= 50 ? 'val-pos' : 'val-neg'}">${m.win_rate_pct || 0}%</td>
                        <td class="text-right font-mono">${m.profit_factor || 0}</td>
                        <td class="text-right font-mono ${ptsClass}">${ptsSign}${mPts.toFixed(1)} pts</td>
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
                    const rawPts = w.total_net_pts !== undefined ? w.total_net_pts : w.total_pts;
                    const wPts = typeof rawPts === 'number' ? rawPts : (parseFloat(rawPts) || 0);
                    const ptsClass = wPts >= 0 ? "val-pos" : "val-neg";
                    const ptsSign = wPts >= 0 ? "+" : "";
                    tr.innerHTML = `
                        <td class="text-left font-mono">${w.window || w.label || '--'}</td>
                        <td class="text-center font-mono">${w.count || 0}</td>
                        <td class="text-right font-mono ${(w.win_rate_pct || 0) >= 50 ? 'val-pos' : 'val-neg'}">${w.win_rate_pct || 0}%</td>
                        <td class="text-right font-mono ${ptsClass}">${ptsSign}${wPts.toFixed(1)} pts</td>
                    `;
                    diagTimewindowTbody.appendChild(tr);
                });
            }
        }
    }





    // Dedicated Trending OI Interval Fetcher & Timer
    async function fetchTrendingOiData(force = false) {
        const intervalVal = parseInt(trendingIntervalSelect ? trendingIntervalSelect.value : "5", 10) || 5;
        const now = Date.now();

        // Prevent rapid repeated calls within 5 seconds unless forced
        if (!force && state.trendingData && (now - (state.trendingLastFetchTime || 0)) < 5000) {
            return;
        }

        try {
            const expParam = state.expiry ? `&expiry=${encodeURIComponent(state.expiry)}` : "";
            const res = await fetch(`/api/options-desk/trending-oi?symbol=${state.symbol}${expParam}&interval=${intervalVal}`);
            const json = await res.json();
            if (json.success && json.data) {
                state.trendingData = json.data;
                state.trendingLastFetchTime = Date.now();
                const spot = state.lastData?.market_bias?.spot_price || parseFloat(document.getElementById("spot-price-display")?.textContent?.replace(/,/g, "") || "0");
                const atm = state.trendingData.atm_strike || state.lastData?.key_levels?.atm_strike || null;
                renderTrendingOiTable(json.data, atm, spot);
            }
        } catch (err) {
            console.error("Error updating trending OI:", err);
        }
    }

    function setupTrendingOiTimer() {
        if (state.trendingTimerId) {
            clearInterval(state.trendingTimerId);
            state.trendingTimerId = null;
        }
        // Poll dedicated Trending OI endpoint every 60 seconds for live interval flow updates
        state.trendingTimerId = setInterval(() => {
            fetchTrendingOiData(true);
        }, 60000);
    }

    // Trending OI Interval Selection (Dedicated to Trending OI table)
    if (trendingIntervalSelect) {
        trendingIntervalSelect.addEventListener("change", () => {
            state.trendingData = null;
            state.trendingLastFetchTime = 0;
            fetchTrendingOiData(true);
            setupTrendingOiTimer();
        });
    }

    // Trending OI Rows Selection (Instant no-scroll compact view)
    const trendingRowsSelect = document.getElementById("trending-rows-select");
    if (trendingRowsSelect) {
        const savedRows = localStorage.getItem("options_desk_trending_rows");
        if (savedRows) {
            trendingRowsSelect.value = savedRows;
        }
        trendingRowsSelect.addEventListener("change", () => {
            localStorage.setItem("options_desk_trending_rows", trendingRowsSelect.value);
            if (state.trendingData) {
                const spot = state.lastData?.market_bias?.spot_price || parseFloat(document.getElementById("spot-price-display")?.textContent?.replace(/,/g, "") || "0");
                const atm = state.trendingData.atm_strike || state.lastData?.key_levels?.atm_strike || null;
                renderTrendingOiTable(
                    state.trendingData,
                    atm,
                    spot
                );
            }
        });
    }

    // Initial Load & Start Timers (Desk auto-refresh is strictly 3 minutes / 180s)
    state.refreshIntervalSec = 180;
    state.timerRemaining = 180;

    setupTrendingOiTimer();
    loadOptionsDeskData(false);
    loadSignalsData();
    loadTradeAnalysis();
    startCountdown();

    // Dedicated 30-second auto-refresh for Signal Tracker & Journal live prices, trades & analytics
    setInterval(() => {
        loadSignalsData();
        loadTradeAnalysis();
    }, 30000);
});


