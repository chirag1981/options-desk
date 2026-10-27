/**
 * app/static/js/analysis.js — Advanced OI Pulse Option Chain & Key Levels Controller
 * Features:
 * - Live broker feed vs. Historical session replay with exact point-in-time time-travel (e.g. 12:00:00).
 * - Dynamic Support (S1, S2) and Resistance (R1, R2) institutional walls.
 * - In-cell visual horizontal OI bars (red for Call, green for Put).
 * - Highlighting for maximum positive (green) and negative (red) OI change.
 * - ITM shading (soft gray), ATM highlight (golden-yellow), OTM clean contrast.
 * - Strike-wise PCR and total option chain PCR.
 * - Column customization with localStorage persistence.
 * - 7-Day historical OHLC and OI build-up modal on strike click.
 */

const STORAGE_KEY_COLS = "oipulse_option_chain_columns";

const DEFAULT_COLUMNS = {
    delta: true,
    interp: true,
    oip: true,
    oi: true,
    oichg: true,
    iv: true,
    ltp: true,
    ltpchg: true,
    pcr: true,
    ohlc: false,
    ivchg: false,
    ltpp: false,
};

let currentMode = "live";
let availableDates = [];
let activeChainData = null;
let visibleColumns = { ...DEFAULT_COLUMNS };

document.addEventListener("DOMContentLoaded", () => {
    loadColumnPreferences();
    initDateAndFilterOptions();
    fetchAnalysisData();

    // Auto-refresh live data every 3 minutes
    setInterval(() => {
        if (currentMode === "live") {
            fetchAnalysisData();
        }
    }, 180000);

    // Keyboard ESC to close modals
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            closeStrikeHistoryModal();
            closeColumnSettingsModal();
        }
    });
});

/* ---------------- 1. INITIALIZATION & COLUMN PREFERENCES ---------------- */

function loadColumnPreferences() {
    try {
        const saved = localStorage.getItem(STORAGE_KEY_COLS);
        if (saved) {
            const parsed = JSON.parse(saved);
            delete parsed.volume; // Explicitly purge volume
            visibleColumns = { ...DEFAULT_COLUMNS, ...parsed };
            delete visibleColumns.volume;
        }
    } catch (e) {
        console.warn("Could not load column preferences from localStorage:", e);
    }
}

async function initDateAndFilterOptions() {
    try {
        const res = await fetch("/api/option-chain/dates", { cache: "no-store" });
        const json = await res.json();
        if (json.available_dates && json.available_dates.length > 0) {
            availableDates = json.available_dates;
            const dateSel = document.getElementById("chain-date-select");
            if (dateSel) {
                dateSel.innerHTML = "";
                availableDates.forEach((d) => {
                    const opt = document.createElement("option");
                    opt.value = d;
                    opt.textContent = d;
                    dateSel.appendChild(opt);
                });
            }
        }
    } catch (e) {
        console.warn("Error loading historical dates:", e);
    }
}

/* ---------------- 2. MODE SWITCHING & FILTER ACTIONS ---------------- */

function setChainMode(mode) {
    currentMode = mode;
    const btnLive = document.getElementById("btn-mode-live");
    const btnHist = document.getElementById("btn-mode-historical");
    const dateGroup = document.getElementById("group-date-select");
    const modePill = document.getElementById("chain-mode-pill");
    const statusBadge = document.getElementById("ticker-status-badge");

    if (mode === "historical") {
        if (btnLive) btnLive.className = "btn-mode-toggle";
        if (btnHist) btnHist.className = "btn-mode-toggle active";
        if (dateGroup) dateGroup.style.display = "flex";
        if (modePill) {
            modePill.textContent = "HISTORICAL REPLAY";
            modePill.className = "status-chip chip-gold";
        }
        if (statusBadge) {
            statusBadge.textContent = "HISTORICAL SESSION";
            statusBadge.className = "status-chip chip-gold";
        }
        onHistoricalDateChange();
    } else {
        if (btnLive) btnLive.className = "btn-mode-toggle active";
        if (btnHist) btnHist.className = "btn-mode-toggle";
        if (dateGroup) dateGroup.style.display = "none";
        if (modePill) {
            modePill.textContent = "LIVE BROKER FEED";
            modePill.className = "status-chip chip-green";
        }
        if (statusBadge) {
            statusBadge.textContent = "LIVE BROKER STREAM";
            statusBadge.className = "status-chip chip-green";
        }
        fetchAnalysisData();
    }
}

function onHistoricalDateChange() {
    // When date changes, update available time slots
    const intervalSel = document.getElementById("chain-interval-select");
    if (intervalSel && intervalSel.value === "custom_time") {
        populateTimePicker();
    }
    applyChainFilter();
}

function onIntervalChange() {
    const intervalSel = document.getElementById("chain-interval-select");
    const customWrap = document.getElementById("group-custom-time");
    if (!intervalSel || !customWrap) return;

    if (intervalSel.value === "custom_time") {
        customWrap.style.display = "flex";
        populateTimePicker();
    } else {
        customWrap.style.display = "none";
        applyChainFilter();
    }
}

function populateTimePicker() {
    const timePicker = document.getElementById("chain-custom-time-picker");
    if (!timePicker) return;

    // Standard high-liquidity market snapshot timestamps
    const standardTimes = [
        { val: "09:30:00", label: "09:30 AM (Opening Flow)" },
        { val: "10:00:00", label: "10:00 AM (Morning Structure)" },
        { val: "11:00:00", label: "11:00 AM (Mid-Morning)" },
        { val: "12:00:00", label: "12:00 PM (Noon Snapshot)" },
        { val: "13:00:00", label: "01:00 PM (European Open)" },
        { val: "14:00:00", label: "02:00 PM (Afternoon Momentum)" },
        { val: "15:00:00", label: "03:00 PM (Closing Writers)" },
        { val: "15:30:00", label: "03:30 PM (Session Close)" },
    ];

    timePicker.innerHTML = "";
    standardTimes.forEach((t) => {
        const opt = document.createElement("option");
        opt.value = t.val;
        opt.textContent = t.label;
        if (t.val === "12:00:00") opt.selected = true;
        timePicker.appendChild(opt);
    });
}

function applyChainFilter() {
    fetchAnalysisData();
}

/* ---------------- 3. DATA FETCHING ---------------- */

async function fetchAnalysisData() {
    try {
        let url = `/api/option-chain?mode=${currentMode}`;

        if (currentMode === "historical") {
            const dateSel = document.getElementById("chain-date-select");
            const intervalSel = document.getElementById("chain-interval-select");
            const timePicker = document.getElementById("chain-custom-time-picker");

            const selectedDate = dateSel ? dateSel.value : "";
            if (selectedDate) {
                url += `&date=${encodeURIComponent(selectedDate)}`;
            }

            if (intervalSel && intervalSel.value === "custom_time" && timePicker) {
                url += `&time=${encodeURIComponent(timePicker.value)}`;
            } else if (intervalSel && intervalSel.value === "last_30") {
                url += `&time=15:00:00`;
            } else if (intervalSel && intervalSel.value === "last_15") {
                url += `&time=15:15:00`;
            } else if (intervalSel && intervalSel.value === "last_5") {
                url += `&time=15:25:00`;
            }
        }

        const expirySel = document.getElementById("chain-expiry-select");
        if (expirySel && expirySel.value) {
            url += `&expiry=${encodeURIComponent(expirySel.value)}`;
        }

        const res = await fetch(url, { cache: "no-store" });
        const json = await res.json();

        if (json.status !== "success" || !json.chain) {
            showChainMessage(json.message || "No option chain data available for this selection.");
            return;
        }

        activeChainData = json;

        // 1. Update Underlying Ticker Strip
        updateUnderlyingTicker(json);

        // 2. Update Top Summary Cards (Cards 01 and 02)
        updateSummaryCards(json);

        // 3. Render Advanced Option Chain Table
        renderAdvancedChainTable(json);

    } catch (err) {
        console.error("Option chain fetch error:", err);
        showChainMessage("Error fetching option chain. Please check network connection.");
    }
}

function showChainMessage(msg) {
    const tbody = document.getElementById("chain-tbody");
    if (tbody) {
        tbody.innerHTML = `<tr><td colspan="23" style="text-align:center; padding:2.5rem; color:var(--text-muted); font-size:0.85rem;">${msg}</td></tr>`;
    }
}

/* ---------------- 4. RENDER TOP TICKER & SUMMARY CARDS ---------------- */

function updateUnderlyingTicker(data) {
    const spotEl = document.getElementById("ticker-spot");
    const chgEl = document.getElementById("ticker-chg");
    const timeEl = document.getElementById("ticker-time");
    const expPill = document.getElementById("chain-active-expiry-pill");

    if (spotEl) spotEl.textContent = `₹${(data.spot_price || 0).toLocaleString("en-IN", { minimumFractionDigits: 1 })}`;

    if (chgEl) {
        const chg = data.spot_change || 0;
        const chgp = data.spot_change_pct || 0;
        const isPos = chg >= 0;
        chgEl.textContent = `${isPos ? "+" : ""}${chg.toFixed(1)} (${isPos ? "+" : ""}${chgp.toFixed(2)}%)`;
        chgEl.className = `ticker-chg ${isPos ? "chip-green text-green" : "chip-red text-red"}`;
    }

    if (timeEl) {
        timeEl.textContent = `${data.date || ""} ${data.display_time || data.timestamp || ""}`;
    }

    if (expPill) {
        expPill.textContent = `Expiry: ${data.expiry || "--"}`;
    }

    // Populate Expiry Dropdown with Monthly Expiries
    const expirySel = document.getElementById("chain-expiry-select");
    if (expirySel && data.available_expiries && data.available_expiries.length > 0) {
        const currentVal = expirySel.value || data.expiry;
        if (expirySel.options.length <= 1 || expirySel.options.length !== data.available_expiries.length) {
            expirySel.innerHTML = "";
            data.available_expiries.forEach((exp) => {
                const opt = document.createElement("option");
                opt.value = exp.date;
                opt.textContent = exp.label || `${exp.date} (Monthly)`;
                if (exp.date === (data.expiry || currentVal)) {
                    opt.selected = true;
                }
                expirySel.appendChild(opt);
            });
        }
    } else if (expirySel && data.expiry && expirySel.options.length <= 1) {
        expirySel.innerHTML = `<option value="${data.expiry}" selected>${data.expiry} (Monthly)</option>`;
    }
}

function updateSummaryCards(data) {
    // Card 01: Underlying Market Bias
    const anSpot = document.getElementById("an-spot");
    if (anSpot) anSpot.textContent = `₹${(data.spot_price || 0).toLocaleString("en-IN", { minimumFractionDigits: 1 })}`;

    const anSpotChg = document.getElementById("an-spot-chg");
    if (anSpotChg) {
        const isPos = (data.spot_change_pct || 0) >= 0;
        anSpotChg.textContent = `${isPos ? "+" : ""}${(data.spot_change_pct || 0).toFixed(2)}%`;
        anSpotChg.className = `spot-chg ${isPos ? "text-green" : "text-red"}`;
    }

    const pcr = data.total_pcr || 0;
    const anPcrBadge = document.getElementById("an-pcr-badge");
    if (anPcrBadge) anPcrBadge.textContent = `PCR: ${pcr.toFixed(2)}`;

    const anPcrVal = document.getElementById("an-pcr-val");
    if (anPcrVal) {
        const biasDesc = pcr >= 1.0 ? "Bullish Put Flow" : (pcr <= 0.8 ? "Bearish Call Flow" : "Neutral Balanced");
        anPcrVal.textContent = `${pcr.toFixed(2)} (${biasDesc})`;
    }

    const anBias = document.getElementById("an-bias");
    if (anBias) {
        const bias = pcr >= 1.0 ? "BULLISH" : (pcr <= 0.8 ? "BEARISH" : "NEUTRAL");
        anBias.textContent = bias;
        anBias.className = `bias-badge ${bias === "BULLISH" ? "chip-green" : (bias === "BEARISH" ? "chip-red" : "chip-gold")}`;
    }

    const anRegime = document.getElementById("an-regime");
    if (anRegime) anRegime.textContent = "BALANCED RANGE";

    const anMomentum = document.getElementById("an-momentum-text");
    if (anMomentum) {
        const mom = pcr >= 1.0 ? "BULLISH PUT WRITING" : (pcr <= 0.8 ? "BEARISH CALL WRITING" : "BALANCED FLOW");
        anMomentum.textContent = `Writer Momentum: ${mom}`;
    }

    const anOiTotals = document.getElementById("an-oi-totals");
    if (anOiTotals) {
        anOiTotals.textContent = `${(data.total_put_oi || 0).toLocaleString("en-IN")} PE / ${(data.total_call_oi || 0).toLocaleString("en-IN")} CE`;
    }

    const anNetDelta = document.getElementById("an-net-delta");
    if (anNetDelta) {
        const netD = (data.total_put_oi || 0) - (data.total_call_oi || 0);
        anNetDelta.textContent = `${netD >= 0 ? "+" : ""}${netD.toLocaleString("en-IN")} Net PE Shift`;
        anNetDelta.className = `dq-val ${netD >= 0 ? "text-green" : "text-red"}`;
    }

    // Card 02: Key Structural OI Boundaries
    const r1 = data.r1_resistance || 0;
    const s1 = data.s1_support || 0;
    const r2 = data.r2_resistance || 0;
    const s2 = data.s2_support || 0;
    const spot = data.spot_price || 0;

    const anR1Strike = document.getElementById("an-r1-strike");
    if (anR1Strike) anR1Strike.textContent = r1 > 0 ? Math.round(r1) : "--";

    const anR2Sub = document.getElementById("an-r2-sub");
    if (anR2Sub) anR2Sub.textContent = `R2: ${r2 > 0 ? Math.round(r2) : "--"}`;

    const anR1Dist = document.getElementById("an-r1-dist");
    if (anR1Dist) {
        const dist = Math.max(0, r1 - spot);
        anR1Dist.textContent = `${Math.round(dist)} pts away (${((dist / spot) * 100).toFixed(2)}%)`;
    }

    const anS1Strike = document.getElementById("an-s1-strike");
    if (anS1Strike) anS1Strike.textContent = s1 > 0 ? Math.round(s1) : "--";

    const anS2Sub = document.getElementById("an-s2-sub");
    if (anS2Sub) anS2Sub.textContent = `S2: ${s2 > 0 ? Math.round(s2) : "--"}`;

    const anS1Dist = document.getElementById("an-s1-dist");
    if (anS1Dist) {
        const dist = Math.max(0, spot - s1);
        anS1Dist.textContent = `${Math.round(dist)} pts away (${((dist / spot) * 100).toFixed(2)}%)`;
    }

    const anR1Cushion = document.getElementById("an-r1-cushion");
    if (anR1Cushion) anR1Cushion.textContent = `+${Math.round(Math.max(0, r1 - spot))} pts above Spot`;

    const anS1Cushion = document.getElementById("an-s1-cushion");
    if (anS1Cushion) anS1Cushion.textContent = `-${Math.round(Math.max(0, spot - s1))} pts below Spot`;

    const anConf = document.getElementById("an-confidence-badge");
    if (anConf && s1 > 0 && r1 > 0) {
        anConf.textContent = `Sellers betting range: ${Math.round(s1)} - ${Math.round(r1)}`;
    }
}

/* ---------------- 5. RENDER ADVANCED OPTION CHAIN TABLE ---------------- */

function renderAdvancedChainTable(data) {
    const thead = document.getElementById("chain-thead");
    const tbody = document.getElementById("chain-tbody");
    const tfoot = document.getElementById("chain-tfoot");

    if (!thead || !tbody || !tfoot) return;

    // Count active columns for Call and Put
    let activeCallCols = 0;
    let activePutCols = 0;

    const colDefs = [
        { key: "delta", label: "Delta", width: "4.8%" },
        { key: "ohlc", label: "O=L / O=H", width: "4.5%" },
        { key: "interp", label: "OI Int.", width: "4.5%" },
        { key: "oip", label: "OI %", width: "5.5%" },
        { key: "oi", label: "OI", width: "8.5%" },
        { key: "oichg", label: "OI Chg", width: "6.5%" },
        { key: "ivchg", label: "IV Chg", width: "4.5%" },
        { key: "iv", label: "IV", width: "4.5%" },
        { key: "ltp", label: "LTP", width: "5.5%" },
        { key: "ltpp", label: "LTP %", width: "5.5%" },
        { key: "ltpchg", label: "LTP Chg", width: "7.2%" },
    ];

    colDefs.forEach((c) => {
        if (visibleColumns[c.key]) activeCallCols++;
    });

    const putColDefs = [
        { key: "ltp", label: "LTP", width: "5.5%" },
        { key: "ltpchg", label: "LTP Chg", width: "7.2%" },
        { key: "ltpp", label: "LTP %", width: "5.5%" },
        { key: "iv", label: "IV", width: "4.5%" },
        { key: "ivchg", label: "IV Chg", width: "4.5%" },
        { key: "oichg", label: "OI Chg", width: "6.5%" },
        { key: "oi", label: "OI", width: "8.5%" },
        { key: "oip", label: "OI %", width: "5.5%" },
        { key: "interp", label: "OI Int.", width: "4.5%" },
        { key: "ohlc", label: "O=L / O=H", width: "4.5%" },
        { key: "delta", label: "Delta", width: "4.8%" },
    ];

    if (visibleColumns.pcr) putColDefs.push({ key: "pcr", label: "PCR", width: "5.0%" });

    putColDefs.forEach((c) => {
        if (visibleColumns[c.key]) activePutCols++;
    });

    // 1. Build Header
    let tier1Html = `
        <tr class="header-tier-1">
            <th colspan="${activeCallCols}" class="th-calls">CALLS (CE)</th>
            <th class="th-strike" style="width:6.0%;">STRIKE</th>
            <th colspan="${activePutCols}" class="th-puts">PUTS (PE)</th>
        </tr>
    `;

    let tier2Html = `<tr class="header-tier-2">`;
    colDefs.forEach((c) => {
        if (visibleColumns[c.key]) {
            tier2Html += `<th style="text-align:right; width:${c.width || 'auto'};">${c.label}</th>`;
        }
    });
    tier2Html += `<th class="th-strike" style="width:6.0%;">Strike Price</th>`;
    putColDefs.forEach((c) => {
        if (visibleColumns[c.key]) {
            tier2Html += `<th style="text-align:right; width:${c.width || 'auto'};">${c.label}</th>`;
        }
    });
    tier2Html += `</tr>`;

    thead.innerHTML = tier1Html + tier2Html;

    // 2. Build Body Rows
    const rows = data.chain || [];
    if (rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="${activeCallCols + activePutCols + 1}" style="text-align:center; padding:2rem; color:var(--text-muted);">No strikes found for current filter.</td></tr>`;
        return;
    }

    tbody.innerHTML = "";
    rows.forEach((r) => {
        const stk = r.strike;
        const ce = r.ce || {};
        const pe = r.pe || {};
        const isAtm = r.is_atm;
        const ceItm = r.ce_is_itm;
        const peItm = r.pe_is_itm;

        const tr = document.createElement("tr");
        if (isAtm) tr.className = "row-atm";

        let trHtml = "";

        // Render Call Columns
        colDefs.forEach((c) => {
            if (!visibleColumns[c.key]) return;
            trHtml += renderCellContent("ce", c.key, ce, ceItm, isAtm);
        });

        // Center Strike Column
        let wallBadge = "";
        if (ce.wall) {
            wallBadge = `<span class="wall-badge ${ce.wall === 'R1' ? 'wall-r1' : 'wall-r2'}">${ce.wall === 'R1' ? '🛡️ R1 WALL' : 'R2 WALL'}</span>`;
        } else if (pe.wall) {
            wallBadge = `<span class="wall-badge ${pe.wall === 'S1' ? 'wall-s1' : 'wall-s2'}">${pe.wall === 'S1' ? '🛡️ S1 WALL' : 'S2 WALL'}</span>`;
        }

        const atmLabel = isAtm ? `<span style="display:block; font-size:0.6rem; color:#8C6A00; font-weight:800;">⚡ ATM</span>` : "";

        trHtml += `
            <td class="strike-cell">
                <button type="button" class="strike-btn" onclick="openStrikeHistoryModal(${stk})" title="Click to view 7-Day Strategy Analysis for ${stk}">
                    ${Math.round(stk)}
                </button>
                ${atmLabel}
                ${wallBadge}
            </td>
        `;

        // Render Put Columns
        putColDefs.forEach((c) => {
            if (!visibleColumns[c.key]) return;
            if (c.key === "pcr") {
                const pcrVal = (r.pcr || 0).toFixed(2);
                trHtml += `<td class="${peItm ? 'itm-shaded' : 'otm-clear'}" style="text-align:right; font-weight:700;">${pcrVal}</td>`;
            } else {
                trHtml += renderCellContent("pe", c.key, pe, peItm, isAtm);
            }
        });

        tr.innerHTML = trHtml;
        tbody.appendChild(tr);
    });

    // 3. Build Footer Row
    let tfootHtml = `<tr>`;
    let ceColsCount = 0;
    colDefs.forEach((c) => {
        if (!visibleColumns[c.key]) return;
        ceColsCount++;
    });

    let peColsCount = 0;
    putColDefs.forEach((c) => {
        if (!visibleColumns[c.key]) return;
        peColsCount++;
    });

    tfootHtml += `<td colspan="${ceColsCount}" style="text-align:right; color:#922B22;">Total Call OI: <strong>${(data.total_call_oi || 0).toLocaleString("en-IN")}</strong></td>`;
    tfootHtml += `<td class="strike-cell" style="text-align:center;">PCR: <strong>${(data.total_pcr || 0).toFixed(2)}</strong></td>`;
    tfootHtml += `<td colspan="${peColsCount}" style="text-align:left; color:#166534;">Total Put OI: <strong>${(data.total_put_oi || 0).toLocaleString("en-IN")}</strong></td>`;
    tfootHtml += `</tr>`;

    tfoot.innerHTML = tfootHtml;
}

function renderCellContent(side, key, leg, isItm, isAtm) {
    const shadingClass = isItm ? "itm-shaded" : "otm-clear";

    switch (key) {
        case "delta":
            return `<td class="${shadingClass}">${(leg.delta || 0).toFixed(2)}</td>`;

        case "ohlc":
            if (leg.open_equals_low) return `<td class="${shadingClass} text-center"><span class="chip-ohlc chip-ol">O=L</span></td>`;
            if (leg.open_equals_high) return `<td class="${shadingClass} text-center"><span class="chip-ohlc chip-oh">O=H</span></td>`;
            return `<td class="${shadingClass} text-center text-muted">-</td>`;

        case "interp":
            const code = leg.oi_interpretation || "--";
            let chipClass = "chip-lb";
            if (code === "SB") chipClass = "chip-sb";
            if (code === "SC") chipClass = "chip-sc";
            if (code === "LU") chipClass = "chip-lu";
            return `<td class="${shadingClass} text-center">${code !== "--" ? `<span class="chip-interp ${chipClass}">${code}</span>` : `<span class="text-muted">-</span>`}</td>`;

        case "oip":
            const oip = leg.change_oi_pct || 0;
            const oipColor = oip >= 0 ? "text-green" : "text-red";
            return `<td class="${shadingClass} ${oipColor}">${oip >= 0 ? "+" : ""}${oip.toFixed(1)}%</td>`;

        case "oi":
            const oiVal = (leg.oi || 0).toLocaleString("en-IN");
            const barPct = leg.oi_pct_bar || 0;
            const barClass = side === "ce" ? "bar-fill-ce" : "bar-fill-pe";
            return `
                <td class="${shadingClass} cell-bar-${side}">
                    <div class="${barClass}" style="width:${barPct}%;"></div>
                    <span class="cell-content">${oiVal}</span>
                </td>
            `;

        case "oichg":
            const oichg = leg.change_oi || 0;
            let oichgClass = oichg >= 0 ? "text-green" : "text-red";
            if (leg.is_max_pos_chg) oichgClass = "badge-max-pos";
            if (leg.is_max_neg_chg) oichgClass = "badge-max-neg";
            return `<td class="${shadingClass}"><span class="${oichgClass}">${oichg >= 0 ? "+" : ""}${oichg.toLocaleString("en-IN")}</span></td>`;

        case "volume":
            return `<td class="${shadingClass} text-muted">${(leg.volume || 0).toLocaleString("en-IN")}</td>`;

        case "ivchg":
            const ivc = leg.iv_change || 0;
            return `<td class="${shadingClass} text-muted">${ivc !== 0 ? (ivc > 0 ? "+" : "") + ivc.toFixed(2) : "-"}</td>`;

        case "iv":
            return `<td class="${shadingClass}">${(leg.iv || 0).toFixed(1)}</td>`;

        case "ltp":
            return `<td class="${shadingClass}" style="font-weight:750;">₹${(leg.ltp || 0).toFixed(2)}</td>`;

        case "ltpp":
            const ltpp = leg.ltp_change_pct || 0;
            return `<td class="${shadingClass} ${ltpp >= 0 ? 'text-green' : 'text-red'}">${ltpp >= 0 ? "+" : ""}${ltpp.toFixed(1)}%</td>`;

        case "ltpchg":
            const ltpchg = leg.ltp_change || 0;
            const chgPct = leg.ltp_change_pct || 0;
            const isPos = ltpchg >= 0;
            return `<td class="${shadingClass}"><span class="${isPos ? 'text-green' : 'text-red'}" style="font-weight:750;">${isPos ? '+' : ''}${ltpchg.toFixed(1)} <span style="font-size:0.63rem; opacity:0.85;">(${isPos ? '+' : ''}${chgPct.toFixed(1)}%)</span></span></td>`;

        default:
            return `<td class="${shadingClass}">-</td>`;
    }
}

/* ---------------- 6. STRIKE 7-DAY HISTORY MODAL ---------------- */

async function openStrikeHistoryModal(strike) {
    const modal = document.getElementById("modal-strike-history");
    const titleEl = document.getElementById("modal-strike-title");
    const tbody = document.getElementById("strike-history-tbody");

    if (!modal || !titleEl || !tbody) return;

    titleEl.innerHTML = `
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg>
        <span>Strike ${Math.round(strike)} — Past 7 Sessions Expiry Strategy Analysis</span>
    `;

    tbody.innerHTML = `<tr><td colspan="15" style="text-align:center; padding:1.5rem; color:var(--text-muted);">Loading 7-day strike history for strike ${strike}...</td></tr>`;
    modal.classList.add("active");

    try {
        let url = `/api/strike-history?strike=${strike}`;
        if (activeChainData && activeChainData.date) {
            url += `&date=${encodeURIComponent(activeChainData.date)}`;
        }

        const res = await fetch(url, { cache: "no-store" });
        const json = await res.json();

        if (json.status !== "success" || !json.history || json.history.length === 0) {
            tbody.innerHTML = `<tr><td colspan="15" style="text-align:center; padding:1.5rem; color:var(--text-muted);">No historical strike records found for ${strike}.</td></tr>`;
            return;
        }

        tbody.innerHTML = "";
        json.history.forEach((d) => {
            const ce = d.ce || {};
            const pe = d.pe || {};
            const tr = document.createElement("tr");

            tr.innerHTML = `
                <td style="text-align:center; font-weight:750; background:#F8F7FD;">${d.date}</td>
                <td>₹${(ce.open || 0).toFixed(2)}</td>
                <td>₹${(ce.high || 0).toFixed(2)}</td>
                <td>₹${(ce.low || 0).toFixed(2)}</td>
                <td style="font-weight:750;">₹${(ce.close || 0).toFixed(2)}</td>
                <td class="text-muted">${(ce.volume || 0).toLocaleString("en-IN")}</td>
                <td style="font-weight:750;">${(ce.oi || 0).toLocaleString("en-IN")}</td>
                <td style="text-align:center;"><span class="chip-interp ${ce.interpretation === 'LB' ? 'chip-lb' : (ce.interpretation === 'SB' ? 'chip-sb' : (ce.interpretation === 'SC' ? 'chip-sc' : 'chip-lu'))}">${ce.interpretation || '--'}</span></td>
                <td>₹${(pe.open || 0).toFixed(2)}</td>
                <td>₹${(pe.high || 0).toFixed(2)}</td>
                <td>₹${(pe.low || 0).toFixed(2)}</td>
                <td style="font-weight:750;">₹${(pe.close || 0).toFixed(2)}</td>
                <td class="text-muted">${(pe.volume || 0).toLocaleString("en-IN")}</td>
                <td style="font-weight:750;">${(pe.oi || 0).toLocaleString("en-IN")}</td>
                <td style="text-align:center;"><span class="chip-interp ${pe.interpretation === 'LB' ? 'chip-lb' : (pe.interpretation === 'SB' ? 'chip-sb' : (pe.interpretation === 'SC' ? 'chip-sc' : 'chip-lu'))}">${pe.interpretation || '--'}</span></td>
            `;
            tbody.appendChild(tr);
        });

    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="15" style="text-align:center; padding:1.5rem; color:var(--color-red);">Failed loading strike history.</td></tr>`;
    }
}

function closeStrikeHistoryModal() {
    const modal = document.getElementById("modal-strike-history");
    if (modal) modal.classList.remove("active");
}

/* ---------------- 7. COLUMN SETTINGS MODAL ---------------- */

function openColumnSettingsModal() {
    const modal = document.getElementById("modal-col-settings");
    if (!modal) return;

    // Synchronize checkboxes with state
    Object.keys(DEFAULT_COLUMNS).forEach((col) => {
        const chk = document.getElementById(`chk-col-${col}`);
        if (chk && !chk.disabled) {
            chk.checked = !!visibleColumns[col];
        }
    });

    modal.classList.add("active");
}

function closeColumnSettingsModal() {
    const modal = document.getElementById("modal-col-settings");
    if (modal) modal.classList.remove("active");
}

function saveColumnSettings() {
    Object.keys(DEFAULT_COLUMNS).forEach((col) => {
        const chk = document.getElementById(`chk-col-${col}`);
        if (chk) {
            visibleColumns[col] = chk.checked;
        }
    });

    try {
        localStorage.setItem(STORAGE_KEY_COLS, JSON.stringify(visibleColumns));
    } catch (e) {
        console.warn("Could not save column preferences to localStorage:", e);
    }

    closeColumnSettingsModal();

    if (activeChainData) {
        renderAdvancedChainTable(activeChainData);
    }
}

function resetDefaultColumns() {
    visibleColumns = { ...DEFAULT_COLUMNS };
    saveColumnSettings();
}

function onModalOverlayClick(event, modalId) {
    if (event.target && event.target.id === modalId) {
        const modal = document.getElementById(modalId);
        if (modal) modal.classList.remove("active");
    }
}
