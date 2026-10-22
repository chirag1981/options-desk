/**
 * app/static/js/journal.js — Trade Journal Controller & Filters
 */

let currentFilter = 'ALL';

document.addEventListener('DOMContentLoaded', () => {
    initFilters();
    fetchJournalData();
});

function initFilters() {
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentFilter = btn.dataset.filter;
            fetchJournalData();
        });
    });
}

async function fetchJournalData() {
    try {
        const res = await fetch('/api/positions');
        const json = await res.json();
        if (json.status !== 'success') return;

        const sum = json.summary || {};
        const openPos = json.open_positions || [];
        const closedPos = json.closed_positions || [];

        // Summary Cards
        const netPnlEl = document.getElementById('jm-net-pnl');
        if (netPnlEl) {
            netPnlEl.textContent = `${sum.total_net_pnl_inr >= 0 ? '+' : ''}₹${sum.total_net_pnl_inr.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
            netPnlEl.className = `mc-val ${sum.total_net_pnl_inr >= 0 ? 'text-green' : 'text-red'}`;
        }

        const winRateEl = document.getElementById('jm-win-rate');
        if (winRateEl) winRateEl.textContent = `${sum.win_rate_pct}%`;

        const winLossCount = document.getElementById('jm-win-loss-count');
        if (winLossCount) winLossCount.textContent = `${sum.win_count} Wins / ${sum.loss_count} Losses`;

        const pfEl = document.getElementById('jm-profit-factor');
        if (pfEl) pfEl.textContent = sum.profit_factor.toFixed(2);

        const costsEl = document.getElementById('jm-costs');
        if (costsEl) costsEl.textContent = `₹${sum.total_transaction_costs_inr.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;

        // Trades Table
        const tbody = document.getElementById('journal-trades-tbody');
        if (!tbody) return;

        let allTrades = [...openPos, ...closedPos];
        if (currentFilter !== 'ALL') {
            allTrades = allTrades.filter(t => t.engine_type.toUpperCase() === currentFilter);
        }

        if (allTrades.length === 0) {
            tbody.innerHTML = `<tr><td colspan="11" class="text-center text-muted">No trade positions recorded for ${currentFilter} filter.</td></tr>`;
            return;
        }

        tbody.innerHTML = '';
        allTrades.forEach(t => {
            const tr = document.createElement('tr');
            const pnl = t.status === 'OPEN' ? t.current_pnl_inr : t.net_pnl_inr;
            const isProfit = pnl >= 0;

            tr.innerHTML = `
                <td class="font-mono">#${t.id}</td>
                <td><span class="badge-index">${t.engine_type}</span></td>
                <td class="font-bold">${t.strategy_name}</td>
                <td>${t.expiry}</td>
                <td class="text-muted">${t.entry_time}</td>
                <td class="font-mono">${t.entry_net_credit_pts.toFixed(2)} pts</td>
                <td class="text-muted">${t.exit_time || '--'}</td>
                <td><span class="status-chip ${t.status === 'OPEN' ? 'chip-green' : (t.status === 'STOPPED_OUT' ? 'chip-red' : '')}">${t.status}</span></td>
                <td class="font-mono">${t.gross_pnl_inr ? `₹${t.gross_pnl_inr.toLocaleString('en-IN', {minimumFractionDigits: 2})}` : '--'}</td>
                <td class="font-mono text-muted">₹${(t.transaction_costs_inr || 0).toFixed(2)}</td>
                <td class="font-mono font-bold ${isProfit ? 'text-green' : 'text-red'}">
                    ${isProfit ? '+' : ''}₹${(pnl || 0).toLocaleString('en-IN', {minimumFractionDigits: 2})}
                </td>
            `;
            tbody.appendChild(tr);
        });

    } catch (err) {
        console.warn("Journal fetch error:", err);
    }
}
