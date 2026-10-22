/**
 * app/static/js/dashboard.js — Interactive Client Controller for 6 Dashboard Panels
 */

let currentEngine = 'INTRADAY';
let currentStrategy = 'BULL_PUT';
let currentCandidates = {};

document.addEventListener('DOMContentLoaded', () => {
    initControls();
    fetchDashboardData();

    // 1. Active Open Positions auto-refreshed every 1 Minute (60,000 ms)
    setInterval(fetchPositionsData, 60000);

    // 2. Entire project data (Market Regime, OI Structure, Strategy Candidates, Setup) auto-refreshed every 5 Minutes (300,000 ms)
    setInterval(fetchProjectData, 300000);
});

function initControls() {
    // Engine Tab Switching (INTRADAY vs POSITIONAL)
    document.querySelectorAll('.engine-tab').forEach(tab => {
        tab.addEventListener('click', (e) => {
            document.querySelectorAll('.engine-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentEngine = tab.dataset.engine;
            renderStrategySetup();
        });
    });

    // Strategy Selector Pills
    document.querySelectorAll('.strat-pill').forEach(pill => {
        pill.addEventListener('click', (e) => {
            if (pill.classList.contains('disabled')) return;
            document.querySelectorAll('.strat-pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            currentStrategy = pill.dataset.strat;
            renderStrategySetup();
        });
    });

    // Execute Paper Trade Button
    const btnExecute = document.getElementById('btn-execute-paper');
    if (btnExecute) {
        btnExecute.addEventListener('click', executeSelectedCandidate);
    }

    // Auto-Trade Toggle Button
    const btnToggleAuto = document.getElementById('btn-toggle-auto-trade');
    if (btnToggleAuto) {
        btnToggleAuto.addEventListener('click', toggleAutoTrade);
    }

    // Auto-Trade Execute Now Button
    const btnExecuteAuto = document.getElementById('btn-auto-execute-now');
    if (btnExecuteAuto) {
        btnExecuteAuto.addEventListener('click', executeAutoNow);
    }

    // Dynamic Strategy Cost & Lots Recalculation
    const lotsSelect = document.getElementById('trade-lots-select');
    if (lotsSelect) {
        lotsSelect.addEventListener('change', () => {
            renderStrategySetup();
        });
    }
}

/**
 * Refreshes Entire Project Data (5-Minute Cycle)
 * Fetches market regime, spot quote, OI structure & walls, auto-selection intelligence, and candidate payoffs.
 */
async function fetchProjectData() {
    try {
        const candRes = await fetch('/api/candidates').then(r => r.json());
        if (candRes.status === 'success') {
            currentCandidates = candRes;
            renderMarketRegime(candRes.analysis);
            renderOIStructure(candRes.analysis);
            renderAutoSelection(candRes.auto_selection, candRes.auto_trade_enabled);
            renderStrategySetup();
        }
    } catch (err) {
        console.warn("Project data polling warning (5m):", err);
    }
}

/**
 * Refreshes Active Open Positions (1-Minute Cycle)
 * Fetches live MTM P&L, position legs, portfolio risk meter, and performance summary.
 */
async function fetchPositionsData() {
    try {
        const posRes = await fetch('/api/positions').then(r => r.json());
        if (posRes.status === 'success') {
            renderRiskAndPositions(posRes);
            renderPerformance(posRes.summary);
        }
    } catch (err) {
        console.warn("Active open positions polling warning (1m):", err);
    }
}

/**
 * Combined initial and on-demand synchronization.
 */
async function fetchDashboardData() {
    await Promise.all([
        fetchProjectData(),
        fetchPositionsData()
    ]);
}

function renderMarketRegime(an) {
    if (!an) return;

    // Header Spot Pill
    const headerSpotVal = document.getElementById('header-spot-val');
    if (headerSpotVal) {
        headerSpotVal.textContent = `NIFTY: ₹${an.spot_price.toLocaleString('en-IN', { minimumFractionDigits: 1 })} (${an.spot_change_pct >= 0 ? '+' : ''}${an.spot_change_pct.toFixed(2)}%)`;
    }

    // Panel 1 Elements
    const spotEl = document.getElementById('spot-price');
    if (spotEl) spotEl.textContent = `₹${an.spot_price.toLocaleString('en-IN', { minimumFractionDigits: 1 })}`;

    const spotChgEl = document.getElementById('spot-chg');
    if (spotChgEl) {
        spotChgEl.textContent = `${an.spot_change_pct >= 0 ? '+' : ''}${an.spot_change_pct.toFixed(2)}%`;
        spotChgEl.className = `spot-chg ${an.spot_change_pct >= 0 ? 'text-green' : 'text-red'}`;
    }

    const regimeBadge = document.getElementById('market-regime-badge');
    if (regimeBadge) {
        regimeBadge.textContent = (an.market_regime || 'CHOPPY').replace(/_/g, ' ');
    }

    const biasText = document.getElementById('market-bias-text');
    if (biasText) biasText.textContent = `Bias: ${an.market_bias || 'NEUTRAL'}`;

    const dqProvider = document.getElementById('dq-provider');
    if (dqProvider) dqProvider.textContent = an.data_status || 'LIVE';

    const dqIv = document.getElementById('dq-iv');
    if (dqIv) dqIv.textContent = `${an.atm_iv || 0}%`;

    const dqExpiry = document.getElementById('dq-expiry');
    if (dqExpiry) dqExpiry.textContent = an.expiry || '--';

    const regimeStatusChip = document.getElementById('regime-status-chip');
    if (regimeStatusChip) {
        regimeStatusChip.textContent = an.market_bias === 'NEUTRAL' ? 'RANGE BOUND' : `${an.market_bias} TREND`;
        regimeStatusChip.className = `status-chip ${an.market_bias === 'BULLISH' ? 'chip-green' : (an.market_bias === 'BEARISH' ? 'chip-red' : 'chip-orange')}`;
    }

    const evidenceList = document.getElementById('regime-evidence-list');
    if (evidenceList && an.evidence_factors) {
        evidenceList.innerHTML = '';
        an.evidence_factors.forEach(f => {
            const li = document.createElement('li');
            li.textContent = f;
            evidenceList.appendChild(li);
        });
    }
}

function renderOIStructure(an) {
    if (!an) return;

    // Panel 2 Elements
    const pcrBadge = document.getElementById('pcr-badge');
    if (pcrBadge) {
        pcrBadge.textContent = `PCR: ${an.pcr_oi.toFixed(2)}`;
        pcrBadge.className = `pcr-badge ${an.pcr_oi >= 1.15 ? 'text-green' : (an.pcr_oi <= 0.85 ? 'text-red' : '')}`;
    }

    const wallR1 = document.getElementById('wall-r1');
    if (wallR1) wallR1.textContent = an.r1_resistance ? Math.round(an.r1_resistance) : '--';

    const distR1 = document.getElementById('dist-r1');
    if (distR1) distR1.textContent = `${Math.round(an.dist_to_r1)} pts above`;

    const wallR2Sub = document.getElementById('wall-r2-sub');
    if (wallR2Sub && an.r2_resistance) wallR2Sub.textContent = `R2: ${Math.round(an.r2_resistance)}`;

    const wallS1 = document.getElementById('wall-s1');
    if (wallS1) wallS1.textContent = an.s1_support ? Math.round(an.s1_support) : '--';

    const distS1 = document.getElementById('dist-s1');
    if (distS1) distS1.textContent = `${Math.round(an.dist_to_s1)} pts below`;

    const wallS2Sub = document.getElementById('wall-s2-sub');
    if (wallS2Sub && an.s2_support) wallS2Sub.textContent = `S2: ${Math.round(an.s2_support)}`;

    const wmBadge = document.getElementById('writer-momentum-badge');
    if (wmBadge) {
        wmBadge.textContent = (an.writer_momentum || 'BALANCED').replace(/_/g, ' ');
        wmBadge.className = `flow-badge ${an.writer_momentum.includes('BULLISH') ? 'text-green' : (an.writer_momentum.includes('BEARISH') ? 'text-red' : '')}`;
    }

    const oiRatioVals = document.getElementById('oi-ratio-vals');
    if (oiRatioVals) {
        oiRatioVals.textContent = `${(an.total_pe_oi / 1000).toFixed(0)}k PE / ${(an.total_ce_oi / 1000).toFixed(0)}k CE`;
    }

    const netDeltaVal = document.getElementById('net-delta-val');
    if (netDeltaVal) {
        const netD = an.net_delta_pe_ce_oi || 0;
        netDeltaVal.textContent = `${netD >= 0 ? '+' : ''}${(netD / 1000).toFixed(1)}k contracts`;
        netDeltaVal.className = netD >= 0 ? 'text-green' : 'text-red';
    }
}

function renderAutoSelection(sel, isEnabled) {
    if (!sel) return;

    // Badge text
    const badgeText = document.getElementById('as-badge-text');
    if (badgeText) {
        if (sel.selection_type === 'MULTIPLE') {
            badgeText.textContent = `DATA AUTO-SELECTED: DUAL SETUP (${sel.selected_strategies ? sel.selected_strategies.length : 2})`;
        } else if (sel.selection_type === 'SINGLE') {
            badgeText.textContent = 'DATA AUTO-SELECTED SETUP';
        } else {
            badgeText.textContent = 'AUTO-SELECT: NO TRADE REGIME';
        }
    }

    // Mode Pill
    const modePill = document.getElementById('as-mode-pill');
    if (modePill) {
        if (sel.selection_type === 'MULTIPLE') {
            modePill.textContent = sel.engine_types.join(' & ') || 'DUAL HORIZON';
            modePill.className = 'as-mode-pill multi';
        } else if (sel.selection_type === 'SINGLE') {
            modePill.textContent = (sel.engine_types[0] || currentEngine) + ' HORIZON';
            modePill.className = 'as-mode-pill';
        } else {
            modePill.textContent = 'STANDBY';
            modePill.className = 'as-mode-pill';
        }
    }

    // Toggle Button & Status Dot
    const dot = document.getElementById('as-status-dot');
    const toggleLabel = document.getElementById('as-toggle-label');
    if (dot && toggleLabel) {
        if (isEnabled) {
            dot.className = 'as-status-dot active';
            toggleLabel.textContent = 'Auto-Trade: ON';
        } else {
            dot.className = 'as-status-dot paused';
            toggleLabel.textContent = 'Auto-Trade: OFF';
        }
    }

    // Execute Selected Button
    const btnExecuteAuto = document.getElementById('btn-auto-execute-now');
    if (btnExecuteAuto) {
        const hasExecutable = sel.selected_strategies && sel.selected_strategies.length > 0;
        btnExecuteAuto.disabled = !hasExecutable;
    }

    // Chips Row
    const chipsRow = document.getElementById('as-chips-row');
    if (chipsRow) {
        chipsRow.innerHTML = '';
        if (sel.selected_strategies && sel.selected_strategies.length > 0) {
            sel.selected_strategies.forEach((strat, idx) => {
                if (idx > 0) {
                    const conn = document.createElement('span');
                    conn.className = 'as-chip-connector';
                    conn.textContent = '+';
                    chipsRow.appendChild(conn);
                }

                const chip = document.createElement('div');
                const isViewing = (strat.strategy_id === currentStrategy && strat.engine_type === currentEngine);
                chip.className = `as-strategy-chip ${isViewing ? 'active-view' : ''}`;
                chip.title = `Click to inspect ${strat.strategy_name} (${strat.engine_type}) legs & payoff below`;

                chip.innerHTML = `
                    <span class="as-chip-name">${strat.strategy_name}</span>
                    <span class="as-chip-engine">${strat.engine_type}</span>
                    <span class="as-chip-score">Score: ${Math.round(strat.confidence_score || 85)}%</span>
                `;

                chip.addEventListener('click', () => {
                    // Switch engine tab
                    currentEngine = strat.engine_type;
                    document.querySelectorAll('.engine-tab').forEach(t => {
                        t.classList.toggle('active', t.dataset.engine === currentEngine);
                    });

                    // Switch strategy pill
                    currentStrategy = strat.strategy_id;
                    document.querySelectorAll('.strat-pill').forEach(p => {
                        p.classList.toggle('active', p.dataset.strat === currentStrategy);
                    });

                    renderAutoSelection(currentCandidates.auto_selection, currentCandidates.auto_trade_enabled);
                    renderStrategySetup();
                });

                chipsRow.appendChild(chip);
            });
        } else {
            chipsRow.innerHTML = `<span class="text-muted" style="font-size:0.78rem;">No active strategy meets strict risk/reward or regime criteria. Continuous market surveillance active.</span>`;
        }
    }

    // Rationale Text
    const ratEl = document.getElementById('as-rationale-text');
    if (ratEl) {
        const evidenceStr = (sel.data_evidence && sel.data_evidence.length > 0) ? ` [Evidence: ${sel.data_evidence.join(' • ')}]` : '';
        ratEl.innerHTML = `<strong>Data Engine Rationale:</strong> ${sel.selection_rationale}${evidenceStr}`;
    }
}

function renderStrategySetup() {
    // First look in specific engine candidates
    const engineCands = (currentEngine === 'POSITIONAL') ? currentCandidates.positional : currentCandidates.intraday;
    let cand = engineCands ? engineCands.find(c => c.strategy_id === currentStrategy) : null;
    if (!cand && currentCandidates.previews) {
        cand = currentCandidates.previews.find(c => c.strategy_id === currentStrategy);
    }
    if (!cand) return;

    const titleEl = document.getElementById('setup-strat-title');
    if (titleEl) titleEl.textContent = `${cand.strategy_name} (${currentEngine})`;

    const eligChip = document.getElementById('setup-eligibility-chip');
    const btnExecute = document.getElementById('btn-execute-paper');

    if (eligChip) {
        if (cand.is_eligible) {
            eligChip.textContent = 'ELIGIBLE SETUP';
            eligChip.className = 'eligibility-chip chip-green';
            if (btnExecute) btnExecute.disabled = false;
        } else {
            eligChip.textContent = 'WAIT / NO TRADE';
            eligChip.className = 'eligibility-chip chip-orange';
            if (btnExecute) btnExecute.disabled = true;
        }
    }

    // Read user-selected lot count (defaults to 1)
    const lotsSelect = document.getElementById('trade-lots-select');
    const lots = lotsSelect ? (parseInt(lotsSelect.value) || 1) : 1;
    const qty = 65 * lots;

    // Financial calculations scaled by selected lots
    const unitMargin = cand.estimated_margin_inr || (cand.strategy_id === 'IRON_CONDOR' ? 52000 : 38000);
    const totalMargin = unitMargin * (lots / (cand.lots || 1));
    const unitCharges = cand.estimated_costs_inr || (cand.strategy_id === 'IRON_CONDOR' ? 108.4 : 54.2);
    const totalCharges = unitCharges * (lots / (cand.lots || 1));
    const netCreditPts = cand.net_credit_pts || 0;
    const netCreditInr = netCreditPts * qty;
    const maxProfitInr = netCreditPts * qty;
    const maxLossInr = (cand.max_loss_pts || 0) * qty;

    // Legs Body
    const tbody = document.getElementById('setup-legs-body');
    if (tbody) {
        if (cand.legs && cand.legs.length > 0) {
            tbody.innerHTML = '';
            cand.legs.forEach(l => {
                const tr = document.createElement('tr');
                const isSell = l.action === 'SELL';
                tr.innerHTML = `
                    <td><span class="status-chip ${isSell ? 'chip-red' : 'chip-green'}">${l.action}</span></td>
                    <td class="font-bold">${Math.round(l.strike)}</td>
                    <td>${l.option_type}</td>
                    <td class="text-muted">₹${(isSell ? l.bid : l.ask).toFixed(2)}</td>
                    <td>${qty}</td>
                `;
                tbody.appendChild(tr);
            });
        } else {
            tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted">No leg quotes available for ${cand.strategy_name} (${currentEngine})</td></tr>`;
        }
    }

    // Strategy Cost & Margin Metrics
    const poMargin = document.getElementById('po-margin-req');
    if (poMargin) poMargin.innerHTML = `₹${totalMargin.toLocaleString('en-IN', { minimumFractionDigits: 0 })}`;

    const poCharges = document.getElementById('po-charges');
    if (poCharges) poCharges.innerHTML = `₹${totalCharges.toFixed(2)}`;

    // Payoffs
    const poCredit = document.getElementById('po-net-credit');
    if (poCredit) poCredit.innerHTML = `${netCreditPts.toFixed(2)} <span class="po-unit">pts (₹${Math.round(netCreditInr).toLocaleString('en-IN')})</span>`;

    const poProfit = document.getElementById('po-max-profit');
    if (poProfit) poProfit.innerHTML = `₹${maxProfitInr.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;

    const poLoss = document.getElementById('po-max-loss');
    if (poLoss) poLoss.innerHTML = `₹${maxLossInr.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;

    const poRr = document.getElementById('po-rr-ratio');
    if (poRr) poRr.innerHTML = `1 : ${(1 / (cand.risk_reward_ratio || 1)).toFixed(1)}`;

    // Action Footer Cost Summary
    const tcsMargin = document.getElementById('tcs-margin');
    if (tcsMargin) tcsMargin.textContent = `₹${totalMargin.toLocaleString('en-IN', { minimumFractionDigits: 0 })}`;

    const tcsFees = document.getElementById('tcs-fees');
    if (tcsFees) tcsFees.textContent = `₹${totalCharges.toFixed(2)}`;

    const rationaleBox = document.getElementById('setup-rationale-box');
    if (rationaleBox) {
        rationaleBox.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="margin-top:2px; flex-shrink:0; stroke:var(--accent-gold);"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>
            <span><strong>Rationale:</strong> ${cand.rationale || 'Awaiting market confirmation...'}</span>
        `;
    }
}

function renderRiskAndPositions(data) {
    const summary = data.summary || {};
    const openPositions = data.open_positions || [];

    // Panel 4: Risk Meter
    const openRiskInr = openPositions.reduce((acc, p) => acc + (p.max_loss_inr || 0), 0);
    const maxRiskInr = (summary.total_account_equity || 1000000) * 0.02; // 2%
    const riskPct = Math.min(100, Math.round((openRiskInr / maxRiskInr) * 100));

    const riskPctLabel = document.getElementById('risk-pct-label');
    if (riskPctLabel) riskPctLabel.textContent = `${(riskPct * 0.02).toFixed(1)}% / 2.0% Max`;

    const riskBarFill = document.getElementById('risk-bar-fill');
    if (riskBarFill) riskBarFill.style.width = `${riskPct}%`;

    const intradayOpenRisk = openPositions.filter(p => p.engine_type === 'INTRADAY').reduce((acc, p) => acc + p.max_loss_inr, 0);
    const positionalOpenRisk = openPositions.filter(p => p.engine_type === 'POSITIONAL').reduce((acc, p) => acc + p.max_loss_inr, 0);

    const elIntraday = document.getElementById('risk-intraday-open');
    if (elIntraday) elIntraday.textContent = `₹${intradayOpenRisk.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;

    const elPositional = document.getElementById('risk-positional-open');
    if (elPositional) elPositional.textContent = `₹${positionalOpenRisk.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;

    // Panel 5: Positions List
    const posList = document.getElementById('open-positions-list');
    const posCount = document.getElementById('open-positions-count');
    if (posCount) posCount.textContent = `${openPositions.length} OPEN`;

    if (posList) {
        if (openPositions.length === 0) {
            posList.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon-3d">
                        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg>
                    </div>
                    <p>No active paper positions. Market conditions continuously monitored for eligible setups.</p>
                </div>
            `;
        } else {
            posList.innerHTML = '';
            openPositions.forEach(p => {
                const card = document.createElement('div');
                card.className = 'pos-card';
                const pnl = p.current_pnl_inr || 0;
                const isProfit = pnl >= 0;

                card.innerHTML = `
                    <div class="pos-card-header">
                        <div>
                            <span class="badge-index">${p.engine_type}</span>
                            <span class="pos-strat-title">${p.strategy_name}</span>
                        </div>
                        <span class="pos-pnl-large ${isProfit ? 'text-green' : 'text-red'}">
                            ${isProfit ? '+' : ''}₹${pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                        </span>
                    </div>
                    <div class="pos-details-row">
                        <span>Entry Credit: ${p.entry_net_credit_pts.toFixed(2)} pts</span>
                        <span>Current Debit: ${p.current_net_debit_pts.toFixed(2)} pts</span>
                        <span>Max Risk: ₹${p.max_loss_inr.toLocaleString('en-IN')}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:0.35rem;">
                        <span class="text-muted" style="font-size:0.7rem;">Entry: ${p.entry_time.split(' ')[1]}</span>
                        <button class="btn-3d btn-outline-danger btn-sm" onclick="closePosition(${p.id})" title="Close Position Immediately" aria-label="Close Position">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                            <span>Close</span>
                        </button>
                    </div>
                `;
                posList.appendChild(card);
            });
        }
    }
}

function renderPerformance(sum) {
    if (!sum) return;

    const elNetPnl = document.getElementById('perf-net-pnl');
    if (elNetPnl) {
        elNetPnl.textContent = `${sum.total_net_pnl_inr >= 0 ? '+' : ''}₹${sum.total_net_pnl_inr.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
        elNetPnl.className = `pb-val ${sum.total_net_pnl_inr >= 0 ? 'text-green' : 'text-red'}`;
    }

    const elPnlBreakdown = document.getElementById('perf-pnl-sub');
    if (elPnlBreakdown) {
        const real = sum.realized_net_pnl_inr || 0;
        const mtm = sum.unrealized_pnl_inr || 0;
        elPnlBreakdown.textContent = `Realized: ${real >= 0 ? '+' : ''}₹${real.toLocaleString('en-IN', {minimumFractionDigits: 0})} | MTM: ${mtm >= 0 ? '+' : ''}₹${mtm.toLocaleString('en-IN', {minimumFractionDigits: 0})}`;
    }

    const elWinRate = document.getElementById('perf-win-rate');
    if (elWinRate) elWinRate.textContent = `${sum.win_rate_pct}%`;

    const elWinSub = document.getElementById('perf-win-sub');
    if (elWinSub) elWinSub.textContent = `${sum.win_count || 0} Wins / ${sum.loss_count || 0} Losses`;

    const elPf = document.getElementById('perf-profit-factor');
    if (elPf) elPf.textContent = sum.profit_factor.toFixed(2);

    const elTrades = document.getElementById('perf-total-trades');
    if (elTrades) elTrades.textContent = sum.total_trades || 0;

    const elTradesSub = document.getElementById('perf-trades-sub');
    if (elTradesSub) elTradesSub.textContent = `${sum.open_trades_count || 0} Open / ${sum.closed_trades_count || 0} Closed`;

    const elIntradayPnl = document.getElementById('perf-intraday-pnl');
    if (elIntradayPnl) {
        elIntradayPnl.textContent = `${sum.intraday_net_pnl_inr >= 0 ? '+' : ''}₹${sum.intraday_net_pnl_inr.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
        elIntradayPnl.className = `esc-pnl ${sum.intraday_net_pnl_inr >= 0 ? 'text-green' : 'text-red'}`;
    }

    const elPositionalPnl = document.getElementById('perf-positional-pnl');
    if (elPositionalPnl) {
        elPositionalPnl.textContent = `${sum.positional_net_pnl_inr >= 0 ? '+' : ''}₹${sum.positional_net_pnl_inr.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
        elPositionalPnl.className = `esc-pnl ${sum.positional_net_pnl_inr >= 0 ? 'text-green' : 'text-red'}`;
    }
}

async function executeSelectedCandidate() {
    const lotsSelect = document.getElementById('trade-lots-select');
    const lots = lotsSelect ? parseInt(lotsSelect.value) : 1;

    try {
        const res = await fetch('/api/execute-candidate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                strategy_id: currentStrategy,
                engine_type: currentEngine,
                lots: lots,
            })
        });

        const data = await res.json();
        if (res.ok) {
            alert(`✅ ${data.message}`);
            fetchDashboardData();
        } else {
            alert(`⚠️ ${data.message || 'Execution blocked by risk or market condition.'}`);
        }
    } catch (err) {
        alert("Execution request failed: " + err);
    }
}

async function closePosition(posId) {
    if (!confirm(`Are you sure you want to close position #${posId}?`)) return;

    try {
        const res = await fetch(`/api/close-position/${posId}`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            alert(`Position #${posId} closed.`);
            fetchDashboardData();
        } else {
            alert(`Failed closing position: ${data.message}`);
        }
    } catch (err) {
        alert("Close request failed: " + err);
    }
}

async function toggleAutoTrade() {
    try {
        const res = await fetch('/api/auto-trade/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await res.json();
        if (data.status === 'success') {
            fetchDashboardData();
        } else {
            alert(`Auto-trade toggle failed: ${data.message}`);
        }
    } catch (err) {
        alert("Failed to toggle auto-trading: " + err);
    }
}

async function executeAutoNow() {
    const btn = document.getElementById('btn-auto-execute-now');
    if (btn) btn.disabled = true;

    try {
        const res = await fetch('/api/auto-trade/execute-now', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await res.json();
        if (data.status === 'success') {
            alert(`⚡ ${data.message}`);
            fetchDashboardData();
        } else {
            alert(`⚠️ ${data.message}`);
        }
    } catch (err) {
        alert("Auto execution request failed: " + err);
    } finally {
        if (btn) btn.disabled = false;
    }
}

