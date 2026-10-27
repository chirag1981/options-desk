/**
 * app/static/js/journal.js — Trade Journal Controller & Filters
 */

let currentFilter = 'ALL';

document.addEventListener('DOMContentLoaded', () => {
    initFilters();
    fetchJournalData();
    fetchPlanAdherenceReport();
    // Auto-sync journal every 10 seconds
    setInterval(() => {
        fetchJournalData();
        fetchPlanAdherenceReport();
    }, 10000);
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

        const pnlSubEl = document.getElementById('jm-pnl-sub');
        if (pnlSubEl) {
            const real = sum.realized_net_pnl_inr || 0;
            const mtm = sum.unrealized_pnl_inr || 0;
            pnlSubEl.textContent = `Realized: ${real >= 0 ? '+' : ''}₹${real.toLocaleString('en-IN', {minimumFractionDigits: 2})} | MTM: ${mtm >= 0 ? '+' : ''}₹${mtm.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
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

async function fetchPlanAdherenceReport() {
    try {
        const res = await fetch('/api/discipline/report');
        const json = await res.json();
        if (json.status !== 'success' || !json.report) return;

        const rep = json.report;

        // Header chip
        const chip = document.getElementById('plan-adherence-rate-chip');
        if (chip) {
            chip.textContent = `Adherence: ${rep.plan_adherence_rate_pct.toFixed(1)}%`;
            chip.className = `status-chip ${rep.plan_adherence_rate_pct >= 95 ? 'chip-green' : 'chip-red'}`;
        }

        // Summary counters
        const plansEl = document.getElementById('adherence-total-plans');
        if (plansEl) plansEl.textContent = rep.total_plans_created || 0;

        const violEl = document.getElementById('adherence-blocked-violations');
        if (violEl) violEl.textContent = rep.total_violations_blocked || 0;

        const violBreakEl = document.getElementById('adherence-violation-breakdown');
        if (violBreakEl && rep.violations_by_rule) {
            const rules = Object.entries(rep.violations_by_rule);
            if (rules.length > 0) {
                violBreakEl.textContent = rules.map(([r, c]) => `${r}: ${c}`).join(', ');
            } else {
                violBreakEl.textContent = "Zero blocked violations";
            }
        }

        const excEl = document.getElementById('adherence-exceeded-count');
        if (excEl) {
            const count = (rep.trades_exceeding_plan || []).length;
            excEl.textContent = count;
            excEl.style.color = count > 0 ? '#ef4444' : '#10b981';
        }

        // Audit table
        const tbody = document.getElementById('adherence-records-tbody');
        if (!tbody) return;

        const records = rep.trade_adherence_records || [];
        if (records.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted">No trade plans recorded yet.</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        records.forEach(rec => {
            const tr = document.createElement('tr');
            const pnl = rec.actual_pnl_inr;
            const hasExceeded = rec.exceeded_planned_loss;

            tr.innerHTML = `
                <td class="font-mono">#${rec.position_id}</td>
                <td>${rec.expiry}</td>
                <td class="font-mono">${rec.net_credit_pts.toFixed(2)} pts</td>
                <td class="font-mono text-muted">₹${rec.planned_loss_exit_inr.toLocaleString('en-IN', {minimumFractionDigits: 2})}</td>
                <td class="font-mono text-muted">₹${rec.max_loss_inr.toLocaleString('en-IN', {minimumFractionDigits: 2})}</td>
                <td class="font-mono font-bold ${pnl !== null ? (pnl >= 0 ? 'text-green' : 'text-red') : 'text-muted'}">
                    ${pnl !== null ? `${pnl >= 0 ? '+' : ''}₹${pnl.toLocaleString('en-IN', {minimumFractionDigits: 2})}` : 'Active'}
                </td>
                <td>
                    <span class="status-chip ${hasExceeded ? 'chip-red' : 'chip-green'}">
                        ${hasExceeded ? 'EXCEEDED PLAN' : 'ADHERED'}
                    </span>
                </td>
                <td style="font-size:0.8rem; color:${hasExceeded ? '#ef4444' : '#888'};">
                    ${rec.exceeded_cause || rec.exit_reason || 'In Progress'}
                </td>
            `;
            tbody.appendChild(tr);
        });

    } catch (err) {
        console.warn("Plan adherence fetch error:", err);
    }
}

