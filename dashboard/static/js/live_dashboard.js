/**
 * live_dashboard.js
 * Extracted from live_dashboard.html
 * Handles live signal scanner: account loading, session management,
 * status polling, indicator display, proximity monitoring, trade execution UI.
 *
 * Expects a global CONFIG object set by the template:
 *   CONFIG.initialState - server-side live state (JSON)
 */

(function() {
    // State
    var pollInterval = null;
    var pollMs = 3000;
    var startedAt = null;
    var initialState = window.LIVE_CONFIG.initialState;

    // Elements
    var setupSection = document.getElementById('setupSection');
    var runningSection = document.getElementById('runningSection');
    var stoppedSection = document.getElementById('stoppedSection');
    var statusDot = document.getElementById('statusDot');
    var statusText = document.getElementById('statusText');
    var btnLaunch = document.getElementById('btnLaunch');
    var btnLoadAccounts = document.getElementById('btnLoadAccounts');
    var strategySelect = document.getElementById('strategySelect');
    var accountSelect = document.getElementById('accountSelect');

    // Initialize based on server state
    if (initialState.running) {
        showRunning(initialState);
    } else if (initialState.stopped_at) {
        showStopped(initialState);
    } else {
        showSetup();
    }

    // -- Account Loading --
    btnLoadAccounts.addEventListener('click', function() {
        var errEl = document.getElementById('accountError');
        var loadEl = document.getElementById('accountLoading');
        errEl.style.display = 'none';
        loadEl.style.display = 'block';
        accountSelect.disabled = true;
        accountSelect.innerHTML = '<option value="">Loading...</option>';

        fetch('/api/live/accounts')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                loadEl.style.display = 'none';
                if (data.error) {
                    errEl.textContent = data.error;
                    errEl.style.display = 'block';
                    accountSelect.innerHTML = '<option value="">-- Failed to load --</option>';
                    return;
                }
                accountSelect.innerHTML = '<option value="">-- Select Account --</option>';
                data.accounts.forEach(function(a) {
                    var opt = document.createElement('option');
                    opt.value = a.id;
                    opt.dataset.name = a.name;
                    var label = a.name + ' ($' + a.balance.toLocaleString(undefined, {minimumFractionDigits: 2}) + ')';
                    if (a.simulated) label += ' [Practice]';
                    if (!a.canTrade) label += ' [No Trade]';
                    opt.textContent = label;
                    accountSelect.appendChild(opt);
                });
                accountSelect.disabled = false;
                updateLaunchButton();
            })
            .catch(function(err) {
                loadEl.style.display = 'none';
                errEl.textContent = 'Network error: ' + err.message;
                errEl.style.display = 'block';
                accountSelect.innerHTML = '<option value="">-- Error --</option>';
            });
    });

    // -- Trade Execution Toggle --
    var tradeEnabled = document.getElementById('tradeEnabled');
    var tradeSettings = document.getElementById('tradeSettings');
    tradeEnabled.addEventListener('change', function() {
        tradeSettings.style.display = tradeEnabled.checked ? 'block' : 'none';
    });

    // -- Form Validation --
    strategySelect.addEventListener('change', updateLaunchButton);
    accountSelect.addEventListener('change', updateLaunchButton);

    function updateLaunchButton() {
        btnLaunch.disabled = !(strategySelect.value && accountSelect.value);
    }

    // -- Launch Flow --
    btnLaunch.addEventListener('click', function() {
        var stratOpt = strategySelect.options[strategySelect.selectedIndex];
        var acctOpt = accountSelect.options[accountSelect.selectedIndex];

        var details = document.getElementById('confirmDetails');
        var html =
            '<div class="confirm-detail"><span class="label">Strategy:</span> ' + stratOpt.dataset.name + '</div>' +
            '<div class="confirm-detail"><span class="label">Account:</span> ' + acctOpt.dataset.name + '</div>' +
            '<div class="confirm-detail"><span class="label">PX Symbol:</span> ' + document.getElementById('pxSymbol').value + '</div>' +
            '<div class="confirm-detail"><span class="label">AMI Symbol:</span> ' + document.getElementById('amiSymbol').value + '</div>' +
            '<div class="confirm-detail"><span class="label">Bar Interval:</span> ' + document.getElementById('barInterval').value + ' min</div>' +
            '<div class="confirm-detail"><span class="label">Alerts:</span> ' + getAlertChannels().join(', ') + '</div>';

        if (tradeEnabled.checked) {
            html += '<hr><div class="callout-amber"><small><strong>TRADE EXECUTION ENABLED</strong> — ' +
                'Market orders will be placed: ' + document.getElementById('tradeSize').value +
                ' contract(s), ' + document.getElementById('tradeTimeout').value + 's timeout</small></div>';
        }
        details.innerHTML = html;

        new bootstrap.Modal(document.getElementById('confirmModal')).show();
    });

    document.getElementById('btnConfirmLaunch').addEventListener('click', function() {
        bootstrap.Modal.getInstance(document.getElementById('confirmModal')).hide();
        startSession();
    });

    function getAlertChannels() {
        var channels = [];
        if (document.getElementById('alertLog').checked) channels.push('log');
        if (document.getElementById('alertDesktop').checked) channels.push('desktop');
        if (document.getElementById('alertSound').checked) channels.push('sound');
        if (document.getElementById('alertWebhook').checked) channels.push('webhook');
        return channels;
    }

    function startSession() {
        btnLaunch.disabled = true;
        btnLaunch.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Starting...';

        var acctOpt = accountSelect.options[accountSelect.selectedIndex];

        var payload = {
            strategy_id: strategySelect.value,
            account_id: parseInt(accountSelect.value),
            account_name: acctOpt.dataset.name,
            symbol: document.getElementById('pxSymbol').value,
            ami_symbol: document.getElementById('amiSymbol').value,
            bar_interval: parseInt(document.getElementById('barInterval').value),
            alert_channels: getAlertChannels(),
            trade_enabled: tradeEnabled.checked,
            trade_size: parseInt(document.getElementById('tradeSize').value || '1'),
            trade_timeout: parseFloat(document.getElementById('tradeTimeout').value || '30'),
        };

        fetch('/api/live/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                alert('Failed to start: ' + data.error);
                btnLaunch.disabled = false;
                btnLaunch.innerHTML = '<i class="bi bi-play-fill me-2"></i>Go Live';
                return;
            }
            // Start polling
            showRunning({
                strategy_name: data.strategy,
                account_name: acctOpt.dataset.name,
                symbol: document.getElementById('pxSymbol').value,
                ami_symbol: document.getElementById('amiSymbol').value,
                started_at: new Date().toISOString(),
                bars_injected: 0,
                scans_run: 0,
                alerts_dispatched: 0,
                alert_history: [],
                feed_status: 'Starting...',
                feed_connected: false,
                trade_enabled: tradeEnabled.checked,
                trades_filled: 0,
                trade_history: [],
            });
        })
        .catch(function(err) {
            alert('Network error: ' + err.message);
            btnLaunch.disabled = false;
            btnLaunch.innerHTML = '<i class="bi bi-play-fill me-2"></i>Go Live';
        });
    }

    // -- Section Visibility --
    function showSetup() {
        setupSection.style.display = 'block';
        runningSection.style.display = 'none';
        stoppedSection.style.display = 'none';
        statusDot.className = 'status-dot stopped';
        statusText.textContent = 'Idle';
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
        btnLaunch.disabled = true;
        btnLaunch.innerHTML = '<i class="bi bi-play-fill me-2"></i>Go Live';
        updateLaunchButton();
    }

    function showRunning(state) {
        setupSection.style.display = 'none';
        runningSection.style.display = 'block';
        stoppedSection.style.display = 'none';
        statusDot.className = 'status-dot running';
        statusText.textContent = 'Running';
        startedAt = state.started_at ? new Date(state.started_at) : new Date();

        // Populate session info
        document.getElementById('infoStrategy').textContent = state.strategy_name || '-';
        document.getElementById('infoAccount').textContent = state.account_name || '-';
        document.getElementById('infoSymbol').textContent =
            (state.symbol || 'NQH6') + ' -> ' + (state.ami_symbol || 'NQ');

        // Show/hide trade UI elements
        var isTrading = state.trade_enabled;
        document.getElementById('metricTradesCol').style.display = isTrading ? '' : 'none';
        document.getElementById('tradeHistoryCard').style.display = isTrading ? '' : 'none';
        document.getElementById('btnKill').style.display = isTrading ? '' : 'none';

        updateMetrics(state);

        // Start polling
        if (!pollInterval) {
            pollInterval = setInterval(pollStatus, pollMs);
        }
    }

    function showStopped(state) {
        setupSection.style.display = 'none';
        runningSection.style.display = 'none';
        stoppedSection.style.display = 'block';
        statusDot.className = 'status-dot stopped';
        statusText.textContent = 'Stopped';
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }

        document.getElementById('finalBars').textContent = state.bars_injected || 0;
        document.getElementById('finalScans').textContent = state.scans_run || 0;
        document.getElementById('finalAlerts').textContent = state.alerts_dispatched || 0;

        // Trade stats
        var hasTrades = state.trade_enabled || (state.trades_filled || 0) > 0;
        document.getElementById('finalTradesCol').style.display = hasTrades ? '' : 'none';
        document.getElementById('finalTradesCancelledCol').style.display = hasTrades ? '' : 'none';
        if (hasTrades) {
            document.getElementById('finalTradesFilled').textContent = state.trades_filled || 0;
            document.getElementById('finalTradesCancelled').textContent =
                (state.trades_cancelled || 0) + (state.trades_rejected || 0);
        }

        if (state.error) {
            document.getElementById('finalError').style.display = 'block';
            document.getElementById('finalErrorText').textContent = state.error;
        } else {
            document.getElementById('finalError').style.display = 'none';
        }
    }

    // -- Status Polling --
    function pollStatus() {
        fetch('/api/live/status')
            .then(function(r) { return r.json(); })
            .then(function(state) {
                if (!state.running && runningSection.style.display !== 'none') {
                    showStopped(state);
                    return;
                }
                if (state.running) {
                    updateMetrics(state);
                }
            })
            .catch(function() {});
    }

    function updateMetrics(state) {
        document.getElementById('metricBars').textContent = state.bars_injected || 0;
        document.getElementById('metricScans').textContent = state.scans_run || 0;
        document.getElementById('metricAlerts').textContent = state.alerts_dispatched || 0;

        // Uptime
        if (startedAt) {
            var elapsed = Math.floor((Date.now() - startedAt.getTime()) / 1000);
            var h = Math.floor(elapsed / 3600);
            var m = Math.floor((elapsed % 3600) / 60);
            var s = elapsed % 60;
            document.getElementById('metricUptime').textContent =
                (h > 0 ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
        }

        // Bar / scan info
        document.getElementById('infoBarInterval').textContent =
            (state.bar_interval || '1 min');
        if (state.last_scan_time) {
            var scanDate = new Date(state.last_scan_time);
            document.getElementById('infoLastScan').textContent =
                scanDate.toLocaleTimeString();
        }

        // Feed status
        var feedBar = document.getElementById('feedStatusBar');
        var feedIcon = document.getElementById('feedStatusIcon');
        var feedText = document.getElementById('feedStatusText');
        if (state.feed_connected) {
            feedBar.className = 'feed-bar connected w-100';
            feedIcon.style.color = '#198754';
            statusDot.className = 'status-dot running';
        } else {
            feedBar.className = 'feed-bar disconnected w-100';
            feedIcon.style.color = '#ffc107';
            statusDot.className = 'status-dot connecting';
        }
        feedText.textContent = state.feed_status || 'Waiting...';

        // Error
        if (state.error) {
            document.getElementById('errorDisplay').style.display = 'block';
            document.getElementById('errorText').textContent = state.error;
            statusDot.className = 'status-dot error';
        } else {
            document.getElementById('errorDisplay').style.display = 'none';
        }

        // Trade metrics
        if (state.trade_enabled) {
            document.getElementById('metricTrades').textContent = state.trades_filled || 0;
            // Update kill button state if trades have been disabled
            if (state.trade_enabled === false) {
                var killBtn = document.getElementById('btnKill');
                killBtn.disabled = true;
                killBtn.innerHTML = '<i class="bi bi-x-octagon me-1"></i>Killed';
            }
        }

        // Alert history
        updateAlertTable(state.alert_history || []);

        // Trade history
        if (state.trade_enabled) {
            updateTradeTable(state.trade_history || []);
        }

        // Live indicators
        updateIndicators(state);

        // Proximity to signal
        updateProximity();
    }

    function updateAlertTable(alerts) {
        var tbody = document.getElementById('alertTableBody');
        var noRow = document.getElementById('noAlertsRow');
        document.getElementById('alertCount').textContent = alerts.length;

        if (alerts.length === 0) {
            if (!noRow) {
                tbody.innerHTML = '<tr id="noAlertsRow"><td colspan="5" class="text-center text-muted py-4">No alerts yet. Waiting for signals...</td></tr>';
            }
            return;
        }

        var html = '';
        alerts.forEach(function(a) {
            var rowClass = a.signal_type === 'Buy' ? 'alert-row-buy' : 'alert-row-short';
            var badgeClass = a.signal_type === 'Buy' ? 'bg-success' : 'bg-danger';
            var ts = a.timestamp ? new Date(a.timestamp).toLocaleString() : '-';
            html += '<tr class="' + rowClass + '">' +
                '<td><small>' + ts + '</small></td>' +
                '<td><span class="badge ' + badgeClass + '">' + a.signal_type + '</span></td>' +
                '<td>' + (a.symbol || '-') + '</td>' +
                '<td class="text-mono">' + (a.price ? a.price.toFixed(2) : '-') + '</td>' +
                '<td><small>' + (a.strategy || '-') + '</small></td>' +
                '</tr>';
        });
        tbody.innerHTML = html;
    }

    function updateTradeTable(trades) {
        var tbody = document.getElementById('tradeTableBody');
        var noRow = document.getElementById('noTradesRow');
        document.getElementById('tradeCount').textContent = trades.length;

        if (trades.length === 0) {
            if (!noRow) {
                tbody.innerHTML = '<tr id="noTradesRow"><td colspan="7" class="text-center text-muted py-4">No trades yet. Waiting for signals...</td></tr>';
            }
            return;
        }

        var html = '';
        trades.forEach(function(t) {
            var statusClass = '';
            var statusBadge = '';
            if (t.status === 'filled') {
                statusClass = 'alert-row-buy';
                statusBadge = '<span class="badge bg-success">Filled</span>';
            } else if (t.status === 'timeout' || t.status === 'cancelled') {
                statusClass = '';
                statusBadge = '<span class="badge bg-warning text-dark">' + t.status + '</span>';
            } else {
                statusClass = 'alert-row-short';
                statusBadge = '<span class="badge bg-danger">' + t.status + '</span>';
            }

            var signalBadge = '';
            if (t.signal_type === 'Buy' || t.signal_type === 'Cover') {
                signalBadge = '<span class="badge bg-success">' + t.signal_type + '</span>';
            } else {
                signalBadge = '<span class="badge bg-danger">' + t.signal_type + '</span>';
            }

            var ts = t.timestamp ? new Date(t.timestamp).toLocaleString() : '-';
            var fillPrice = t.fill_price ? t.fill_price.toFixed(2) : '-';
            var elapsed = t.elapsed ? t.elapsed.toFixed(1) + 's' : '-';

            html += '<tr class="' + statusClass + '">' +
                '<td><small>' + ts + '</small></td>' +
                '<td>' + signalBadge + '</td>' +
                '<td>' + (t.symbol || '-') + '</td>' +
                '<td>' + (t.size || '-') + '</td>' +
                '<td class="text-mono">' + fillPrice + '</td>' +
                '<td>' + statusBadge + '</td>' +
                '<td><small>' + elapsed + '</small></td>' +
                '</tr>';
        });
        tbody.innerHTML = html;
    }

    // -- Kill Switch --
    document.getElementById('btnKill').addEventListener('click', function() {
        if (!confirm('KILL SWITCH: This will immediately disable all trade execution. Continue?')) {
            return;
        }
        fetch('/api/live/kill', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var killBtn = document.getElementById('btnKill');
                killBtn.disabled = true;
                killBtn.innerHTML = '<i class="bi bi-x-octagon me-1"></i>Killed';
                killBtn.classList.remove('btn-danger');
                killBtn.classList.add('btn-secondary');
            })
            .catch(function() {});
    });

    // -- Stop Flow --
    document.getElementById('btnStop').addEventListener('click', function() {
        new bootstrap.Modal(document.getElementById('stopModal')).show();
    });

    document.getElementById('btnConfirmStop').addEventListener('click', function() {
        bootstrap.Modal.getInstance(document.getElementById('stopModal')).hide();

        fetch('/api/live/stop', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                statusText.textContent = 'Stopping...';
                statusDot.className = 'status-dot connecting';
            })
            .catch(function() {});
    });

    // -- Restart --
    document.getElementById('btnRestart').addEventListener('click', function() {
        showSetup();
    });

    // -- Refresh Rate Control --
    document.getElementById('refreshRate').addEventListener('change', function() {
        pollMs = parseInt(this.value) || 3000;
        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = setInterval(pollStatus, pollMs);
        }
    });

    // -- Live Indicators --

    // Human-readable labels for built-in and common indicator names
    var indicatorLabels = {
        'BarsSinceBuy': 'Bars Since Buy',
        'BarsSinceSell': 'Bars Since Sell',
        'BarsSinceShort': 'Bars Since Short',
        'BarsSinceCover': 'Bars Since Cover',
        'Close': 'Close',
        'Open': 'Open',
        'High': 'High',
        'Low': 'Low',
        'Volume': 'Volume',
    };

    // Formatting: integer columns get no decimals, prices get 2-4
    var integerColumns = {
        'BarsSinceBuy': true, 'BarsSinceSell': true,
        'BarsSinceShort': true, 'BarsSinceCover': true,
        'Volume': true,
    };

    function formatIndicatorValue(name, val) {
        if (typeof val !== 'number') return val;
        if (integerColumns[name]) {
            return val.toLocaleString(undefined, {
                minimumFractionDigits: 0, maximumFractionDigits: 0
            });
        }
        return val.toLocaleString(undefined, {
            minimumFractionDigits: 2, maximumFractionDigits: 4
        });
    }

    function updateIndicators(state) {
        var values = state.indicator_values || {};
        var barTime = state.indicator_time;
        var grid = document.getElementById('indicatorGrid');
        var empty = document.getElementById('indicatorEmpty');
        var timeEl = document.getElementById('indicatorTime');

        var keys = Object.keys(values);
        if (keys.length === 0) {
            grid.style.display = 'none';
            empty.style.display = 'block';
            timeEl.textContent = 'No data yet';
            return;
        }

        empty.style.display = 'none';
        grid.style.display = 'flex';

        // Update timestamp
        if (barTime) {
            try {
                var dt = new Date(barTime);
                timeEl.textContent = 'as of ' + dt.toLocaleTimeString();
            } catch(e) {
                timeEl.textContent = 'as of ' + barTime;
            }
        }

        // Build indicator grid
        var html = '';
        keys.forEach(function(name) {
            var val = values[name];
            var label = indicatorLabels[name] || name;
            var formatted = formatIndicatorValue(name, val);
            html += '<div class="col-md-6 col-lg-4">' +
                '<div class="indicator-item">' +
                '<span class="ind-name">' + label + '</span>' +
                '<span class="ind-value">' + formatted + '</span>' +
                '</div></div>';
        });
        grid.innerHTML = html;
    }

    // -- Proximity to Signal --
    function updateProximity() {
        fetch('/api/live/proximity')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var container = document.getElementById('proximityContainer');
                var empty = document.getElementById('proximityEmpty');
                var status = document.getElementById('proximityStatus');
                var prox = data.proximity || [];

                if (prox.length === 0) {
                    container.style.display = 'none';
                    empty.style.display = 'block';
                    status.textContent = data.error || 'No conditions parsed from AFL';
                    return;
                }

                empty.style.display = 'none';
                container.style.display = 'block';

                var buyCount = prox.filter(function(p) { return p.signal === 'Buy'; }).length;
                var shortCount = prox.filter(function(p) { return p.signal === 'Short'; }).length;
                var metCount = prox.filter(function(p) { return p.met; }).length;
                status.textContent = metCount + '/' + prox.length + ' conditions met';

                var buyHtml = '';
                var shortHtml = '';

                prox.forEach(function(p) {
                    var pct = Math.min(p.proximity_pct, 100);
                    var barColor = p.met ? 'bg-success' : (pct > 80 ? 'bg-warning' : 'bg-secondary');
                    var metIcon = p.met ? '<i class="bi bi-check-circle-fill text-success"></i>' : '<i class="bi bi-circle text-muted"></i>';

                    var html = '<div class="mb-2">' +
                        '<div class="d-flex justify-content-between align-items-center mb-1">' +
                        '<small>' + metIcon + ' <strong>' + p.indicator + '</strong> ' + p.operator + ' ' + p.threshold + '</small>' +
                        '<small class="text-mono">' + p.current_value + '</small>' +
                        '</div>' +
                        '<div class="progress" style="height: 6px;">' +
                        '<div class="progress-bar ' + barColor + '" style="width: ' + pct + '%;"></div>' +
                        '</div>' +
                        '</div>';

                    if (p.signal === 'Buy') {
                        buyHtml += html;
                    } else {
                        shortHtml += html;
                    }
                });

                document.getElementById('proximityBuy').innerHTML = buyHtml || '<small class="text-muted">No Buy conditions detected</small>';
                document.getElementById('proximityShort').innerHTML = shortHtml || '<small class="text-muted">No Short conditions detected</small>';
            })
            .catch(function() {});
    }

})();
