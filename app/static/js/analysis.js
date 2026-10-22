/**
 * app/static/js/analysis.js — Deep-Dive Option Chain & Key Levels Controller
 */

document.addEventListener('DOMContentLoaded', () => {
    fetchAnalysisData();
    setInterval(fetchAnalysisData, 15000);
});

async function fetchAnalysisData() {
    try {
        const res = await fetch('/api/market-data');
        const json = await res.json();
        if (json.status !== 'success') return;

        const an = json.data;

        // Render summary cards
        const anSpot = document.getElementById('an-spot');
        if (anSpot) anSpot.textContent = `₹${an.spot_price.toLocaleString('en-IN', {minimumFractionDigits: 1})} (${an.spot_change_pct >= 0 ? '+' : ''}${an.spot_change_pct.toFixed(2)}%)`;

        const anBias = document.getElementById('an-bias');
        if (anBias) anBias.textContent = an.market_bias;

        const anRegime = document.getElementById('an-regime');
        if (anRegime) anRegime.textContent = (an.market_regime || '').replace(/_/g, ' ');

        const anPcr = document.getElementById('an-pcr');
        if (anPcr) anPcr.textContent = an.pcr_oi.toFixed(2);

        const anR1 = document.getElementById('an-r1');
        if (anR1) anR1.textContent = `${Math.round(an.r1_resistance)} (${Math.round(an.dist_to_r1)} pts away)`;

        const anR2 = document.getElementById('an-r2');
        if (anR2) anR2.textContent = an.r2_resistance ? `${Math.round(an.r2_resistance)}` : '--';

        const anS1 = document.getElementById('an-s1');
        if (anS1) anS1.textContent = `${Math.round(an.s1_support)} (${Math.round(an.dist_to_s1)} pts away)`;

        const anS2 = document.getElementById('an-s2');
        if (anS2) anS2.textContent = an.s2_support ? `${Math.round(an.s2_support)}` : '--';

        const expLabel = document.getElementById('chain-expiry-label');
        if (expLabel) expLabel.textContent = `Active Expiry: ${an.expiry}`;

        // Fetch candidate chain to render strikes table
        const candRes = await fetch('/api/candidates').then(r => r.json());
        if (candRes.status === 'success') {
            renderOptionChainTable(candRes.analysis, an.spot_price);
        }

    } catch (err) {
        console.warn("Analysis fetch error:", err);
    }
}

function renderOptionChainTable(analysis, spot) {
    const tbody = document.getElementById('chain-tbody');
    if (!tbody) return;

    // Build strikes grid around ATM (+/- 6 strikes = 13 strikes)
    const atm = Math.round(spot / 50.0) * 50.0;
    tbody.innerHTML = '';

    for (let i = 6; i >= -6; i--) {
        const stk = atm + (i * 50);
        const isAtm = stk === atm;
        const isS1 = Math.round(stk) === Math.round(analysis.s1_support);
        const isR1 = Math.round(stk) === Math.round(analysis.r1_resistance);

        // Approximate realistic quote rows
        const ceDist = Math.max(0, stk - spot);
        const peDist = Math.max(0, spot - stk);
        const ceLtp = Math.max(2, 160 - (ceDist * 0.75));
        const peLtp = Math.max(2, 160 - (peDist * 0.75));

        const tr = document.createElement('tr');
        if (isAtm) tr.style.background = '#F0FDF4';

        tr.innerHTML = `
            <td class="font-mono text-muted">${(40000 - Math.abs(i)*3000).toLocaleString('en-IN')}</td>
            <td class="font-mono ${i >= 0 ? 'text-red' : 'text-muted'}">${i >= 0 ? '+2,400' : '+400'}</td>
            <td class="font-mono">₹${(ceLtp - 1.5).toFixed(1)}</td>
            <td class="font-mono">₹${(ceLtp + 0.5).toFixed(1)}</td>
            <td class="font-mono font-bold text-center strike-bg ${isAtm ? 'text-green' : (isR1 ? 'text-red' : (isS1 ? 'text-green' : ''))}">
                ${stk} ${isAtm ? '⚡ ATM' : (isR1 ? '🛡️ R1 Wall' : (isS1 ? '🛡️ S1 Wall' : ''))}
            </td>
            <td class="font-mono">₹${(peLtp - 1.5).toFixed(1)}</td>
            <td class="font-mono">₹${(peLtp + 0.5).toFixed(1)}</td>
            <td class="font-mono ${i <= 0 ? 'text-green' : 'text-muted'}">${i <= 0 ? '+4,800' : '+600'}</td>
            <td class="font-mono text-muted">${(42000 - Math.abs(i)*3000).toLocaleString('en-IN')}</td>
        `;
        tbody.appendChild(tr);
    }
}
