/**
 * app/static/js/analysis.js — Deep-Dive Live Option Chain & Key Levels Controller
 * Auto-refreshes every 3 minutes in sync with project data lifecycle standards.
 */

document.addEventListener('DOMContentLoaded', () => {
    fetchAnalysisData();
    // Refresh market data every 3 minutes (180,000 ms) in accordance with project data sync standard
    setInterval(fetchAnalysisData, 180000);
});

async function fetchAnalysisData() {
    try {
        const res = await fetch('/api/market-data', { cache: 'no-store' });
        const json = await res.json();
        if (json.status !== 'success') return;

        const an = json.data;
        const chainByStrike = json.chain_by_strike || {};

        // 1. Render Underlying Market Bias Card (Card 01)
        const anSpot = document.getElementById('an-spot');
        if (anSpot) anSpot.textContent = `₹${an.spot_price.toLocaleString('en-IN', { minimumFractionDigits: 1 })}`;

        const anSpotChg = document.getElementById('an-spot-chg');
        if (anSpotChg) {
            const isPos = (an.spot_change_pct || 0) >= 0;
            anSpotChg.textContent = `${isPos ? '+' : ''}${(an.spot_change_pct || 0).toFixed(2)}%`;
            anSpotChg.className = `spot-chg ${isPos ? 'text-green' : 'text-red'}`;
        }

        const anBias = document.getElementById('an-bias');
        if (anBias) {
            anBias.textContent = an.market_bias || 'NEUTRAL';
            anBias.className = `bias-badge ${an.market_bias === 'BULLISH' ? 'chip-green' : (an.market_bias === 'BEARISH' ? 'chip-red' : 'chip-gold')}`;
        }

        const anRegime = document.getElementById('an-regime');
        if (anRegime) {
            anRegime.textContent = (an.market_regime || 'BALANCED').replace(/_/g, ' ');
        }

        const anMomentum = document.getElementById('an-momentum-text');
        if (anMomentum) {
            const momClean = (an.writer_momentum || 'BALANCED').replace(/_/g, ' ');
            anMomentum.textContent = `Writer Momentum: ${momClean}`;
        }

        const anPcrBadge = document.getElementById('an-pcr-badge');
        if (anPcrBadge) anPcrBadge.textContent = `PCR: ${an.pcr_oi.toFixed(2)}`;

        const anPcrVal = document.getElementById('an-pcr-val');
        if (anPcrVal) {
            const biasDesc = an.pcr_oi >= 1.0 ? 'Bullish Put Flow' : (an.pcr_oi <= 0.8 ? 'Bearish Call Flow' : 'Neutral Balanced');
            anPcrVal.textContent = `${an.pcr_oi.toFixed(2)} (${biasDesc})`;
        }

        const anOiTotals = document.getElementById('an-oi-totals');
        if (anOiTotals) {
            anOiTotals.textContent = `${(an.total_pe_oi || 0).toLocaleString('en-IN')} PE / ${(an.total_ce_oi || 0).toLocaleString('en-IN')} CE`;
        }

        const anNetDelta = document.getElementById('an-net-delta');
        if (anNetDelta) {
            const netD = an.net_delta_pe_ce_oi || 0;
            anNetDelta.textContent = `${netD >= 0 ? '+' : ''}${netD.toLocaleString('en-IN')} Net PE Shift`;
            anNetDelta.className = `dq-val ${netD >= 0 ? 'text-green' : 'text-red'}`;
        }

        const anAtmIv = document.getElementById('an-atm-iv');
        if (anAtmIv) anAtmIv.textContent = `${(an.atm_iv || 0).toFixed(1)}%`;

        // 2. Render Key Structural OI Boundaries Card (Card 02)
        const anR1Dist = document.getElementById('an-r1-dist');
        if (anR1Dist) anR1Dist.textContent = `${Math.round(an.dist_to_r1)} pts away (${(an.dist_to_r1_pct || 0).toFixed(2)}%)`;

        const anR1Strike = document.getElementById('an-r1-strike');
        if (anR1Strike) anR1Strike.textContent = Math.round(an.r1_resistance);

        const anR2Sub = document.getElementById('an-r2-sub');
        if (anR2Sub) anR2Sub.textContent = `R2: ${an.r2_resistance ? Math.round(an.r2_resistance) : '--'}`;

        const anS1Dist = document.getElementById('an-s1-dist');
        if (anS1Dist) anS1Dist.textContent = `${Math.round(an.dist_to_s1)} pts away (${(an.dist_to_s1_pct || 0).toFixed(2)}%)`;

        const anS1Strike = document.getElementById('an-s1-strike');
        if (anS1Strike) anS1Strike.textContent = Math.round(an.s1_support);

        const anS2Sub = document.getElementById('an-s2-sub');
        if (anS2Sub) anS2Sub.textContent = `S2: ${an.s2_support ? Math.round(an.s2_support) : '--'}`;

        const anR1Cushion = document.getElementById('an-r1-cushion');
        if (anR1Cushion) anR1Cushion.textContent = `+${Math.round(an.dist_to_r1)} pts above Spot`;

        const anS1Cushion = document.getElementById('an-s1-cushion');
        if (anS1Cushion) anS1Cushion.textContent = `-${Math.round(an.dist_to_s1)} pts below Spot`;

        const anConfidence = document.getElementById('an-confidence-badge');
        if (anConfidence) anConfidence.textContent = `${(an.confidence_score || 0).toFixed(0)}% Structural Confidence`;

        // 3. Render Header Pills
        const expLabel = document.getElementById('chain-expiry-label');
        if (expLabel) expLabel.textContent = `Active Expiry: ${an.expiry}`;

        const chainStatus = document.getElementById('chain-status-chip');
        if (chainStatus) {
            chainStatus.textContent = an.data_status === 'FYERS_LIVE' ? 'LIVE BROKER DATA' : 'SIMULATED DATA';
            chainStatus.className = `status-chip ${an.data_status === 'FYERS_LIVE' ? 'chip-green' : 'chip-gold'}`;
        }

        // 4. Render Live Option Chain Table directly from live chain_by_strike
        renderLiveOptionChain(chainByStrike, an, an.spot_price);

    } catch (err) {
        console.warn("Analysis fetch error:", err);
    }
}

/**
 * Builds and renders the live option chain table directly from live chain_by_strike data.
 */
function renderLiveOptionChain(chainByStrike, analysis, spot) {
    const tbody = document.getElementById('chain-tbody');
    if (!tbody) return;

    const strikes = Object.keys(chainByStrike).map(s => parseFloat(s)).sort((a, b) => b - a);
    if (strikes.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" class="text-center text-muted">No live option strikes available.</td></tr>`;
        return;
    }

    const atm = Math.round(spot / 50.0) * 50.0;
    const s1 = Math.round(analysis.s1_support);
    const r1 = Math.round(analysis.r1_resistance);

    // Focus on +/- 400 pts around ATM (17 strikes)
    const visibleStrikes = strikes.filter(s => Math.abs(s - atm) <= 400);

    tbody.innerHTML = '';
    visibleStrikes.forEach(stk => {
        const row = chainByStrike[stk] || chainByStrike[stk.toFixed(1)] || {};
        const ce = row.CE || {};
        const pe = row.PE || {};

        const isAtm = Math.round(stk) === atm;
        const isS1 = Math.round(stk) === s1;
        const isR1 = Math.round(stk) === r1;

        const tr = document.createElement('tr');
        if (isAtm) tr.style.background = '#F7F4EA';

        const ceOiChg = ce.change_oi || 0;
        const peOiChg = pe.change_oi || 0;

        tr.innerHTML = `
            <td class="font-mono text-muted">${(ce.oi || 0).toLocaleString('en-IN')}</td>
            <td class="font-mono ${ceOiChg >= 0 ? 'text-red' : 'text-green'}">${ceOiChg >= 0 ? '+' : ''}${ceOiChg.toLocaleString('en-IN')}</td>
            <td class="font-mono">₹${(ce.bid || 0).toFixed(2)}</td>
            <td class="font-mono">₹${(ce.ask || 0).toFixed(2)}</td>
            <td class="font-mono font-bold text-center strike-bg ${isAtm ? 'text-green' : (isR1 ? 'text-red' : (isS1 ? 'text-green' : ''))}">
                ${Math.round(stk)} ${isAtm ? '⚡ ATM' : (isR1 ? '🛡️ R1 Wall' : (isS1 ? '🛡️ S1 Wall' : ''))}
            </td>
            <td class="font-mono">₹${(pe.bid || 0).toFixed(2)}</td>
            <td class="font-mono">₹${(pe.ask || 0).toFixed(2)}</td>
            <td class="font-mono ${peOiChg >= 0 ? 'text-green' : 'text-red'}">${peOiChg >= 0 ? '+' : ''}${peOiChg.toLocaleString('en-IN')}</td>
            <td class="font-mono text-muted">${(pe.oi || 0).toLocaleString('en-IN')}</td>
        `;
        tbody.appendChild(tr);
    });
}
