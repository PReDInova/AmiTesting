/**
 * results_detail.js
 * Extracted from partials/results_detail_js.html
 * Handles symbol switcher, optimization chart/sort, backtest table sorting/stats,
 * column chooser, equity curve, CodeMirror editor, trade candlestick chart,
 * data tooltips, and version preview.
 *
 * Expects window.RESULTS_CONFIG to be set inline with Jinja2 values:
 *   - symbolRunsMap, currentRunSymbol, isOptimization
 *   - optProfits (array, optimization only)
 *   - netProfitCol (string, optimization only)
 *   - equityCurveUrl (string, backtest only)
 *   - indicatorConfigs (array, backtest only)
 */
var _resultsCfg = window.RESULTS_CONFIG;
var symbolRunsMap = _resultsCfg.symbolRunsMap;
var currentRunSymbol = _resultsCfg.currentRunSymbol;

document.addEventListener('DOMContentLoaded', function() {

    /* ═══════════════════════════════════════════════════════
       SYMBOL SWITCHER -- Navigate to results for selected symbol
       ═══════════════════════════════════════════════════════ */
    (function() {
        var symbolSelect = document.getElementById('symbolSelectHeader');
        var noResultsBanner = document.getElementById('noResultsBanner');
        var noResultsText = document.getElementById('noResultsText');
        var noResultsRunBtn = document.getElementById('noResultsRunBtn');
        if (!symbolSelect) return;

        symbolSelect.addEventListener('change', function() {
            var sym = this.value || currentRunSymbol;
            if (noResultsBanner) noResultsBanner.classList.add('d-none');

            // Same symbol as current run -- do nothing
            if (sym === currentRunSymbol) return;

            if (symbolRunsMap[sym]) {
                // Completed run exists for this symbol -- navigate to it
                window.location.href = '/run/' + symbolRunsMap[sym].run_id;
            } else {
                // No results for this symbol -- show prompt
                if (noResultsText) {
                    noResultsText.textContent = '\u201c' + sym + '\u201d has no results. Would you like to run a backtest?';
                    noResultsBanner.classList.remove('d-none');
                }
            }
        });

        // "Run Backtest" button in the no-results banner reuses the existing form
        if (noResultsRunBtn) {
            noResultsRunBtn.addEventListener('click', function() {
                var form = symbolSelect.closest('form');
                if (form) form.submit();
            });
        }
    })();

    /* ═══════════════════════════════════════════════════════
       OPTIMIZATION -- Distribution Chart + Table Sort
       ═══════════════════════════════════════════════════════ */
    if (_resultsCfg.isOptimization) {
    (function() {
        // --- Net Profit Distribution Chart ---
        var distCanvas = document.getElementById('optDistChart');
        if (distCanvas) {
            var profits = _resultsCfg.optProfits || [];

            if (profits.length > 0) {
                // Build histogram bins
                var minP = Math.min.apply(null, profits);
                var maxP = Math.max.apply(null, profits);
                var range = maxP - minP;
                var binCount = Math.min(20, Math.max(5, Math.ceil(Math.sqrt(profits.length))));
                var binSize = range / binCount || 1;
                var bins = [];
                var labels = [];
                for (var b = 0; b < binCount; b++) {
                    bins.push(0);
                    var lo = minP + b * binSize;
                    var hi = lo + binSize;
                    labels.push('$' + Math.round(lo).toLocaleString());
                }
                profits.forEach(function(p) {
                    var idx = Math.min(Math.floor((p - minP) / binSize), binCount - 1);
                    if (idx < 0) idx = 0;
                    bins[idx]++;
                });

                new Chart(distCanvas.getContext('2d'), {
                    type: 'bar',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: 'Combinations',
                            data: bins,
                            backgroundColor: bins.map(function(_, i) {
                                var midVal = minP + (i + 0.5) * binSize;
                                return midVal >= 0 ? 'rgba(25, 135, 84, 0.6)' : 'rgba(220, 53, 69, 0.6)';
                            }),
                            borderRadius: 3,
                        }]
                    },
                    options: {
                        responsive: true,
                        plugins: { legend: { display: false } },
                        scales: {
                            y: {
                                beginAtZero: true,
                                title: { display: true, text: 'Count', font: { size: 11 } },
                                ticks: { stepSize: 1, font: { size: 10 } }
                            },
                            x: {
                                title: { display: true, text: 'Net Profit', font: { size: 11 } },
                                ticks: { font: { size: 9 }, maxRotation: 45 }
                            }
                        }
                    }
                });
            }
        }

        // --- Table Sorting (cached) ---
        var optTable = document.getElementById('optResultsTable');
        if (optTable) {
            var optTbody = optTable.querySelector('tbody');
            var optDomRows = optTbody.querySelectorAll('tr');
            // Build cache once
            var _optRows = [];
            for (var ri = 0; ri < optDomRows.length; ri++) {
                var tr = optDomRows[ri];
                var cells = [], nums = [];
                for (var ci = 0; ci < tr.children.length; ci++) {
                    var txt = tr.children[ci].textContent.trim();
                    cells.push(txt);
                    nums.push(parseFloat(txt));
                }
                _optRows.push({ el: tr, cells: cells, nums: nums });
            }

            var sortDir = {};
            var _optSortBusy = false;
            optTable.querySelectorAll('.opt-sortable').forEach(function(th) {
                th.addEventListener('click', function() {
                    if (_optSortBusy) return;
                    _optSortBusy = true;
                    var clickedTh = this;
                    var col = this.dataset.col;
                    var colIdx = Array.from(this.parentNode.children).indexOf(this);

                    sortDir[col] = sortDir[col] === 'asc' ? 'desc' : 'asc';
                    var dir = sortDir[col];

                    optTable.classList.add('bt-sorting');

                    requestAnimationFrame(function() {
                        _optRows.sort(function(a, b) {
                            var aN = a.nums[colIdx], bN = b.nums[colIdx];
                            if (!isNaN(aN) && !isNaN(bN)) {
                                return dir === 'asc' ? aN - bN : bN - aN;
                            }
                            return dir === 'asc' ? a.cells[colIdx].localeCompare(b.cells[colIdx]) : b.cells[colIdx].localeCompare(a.cells[colIdx]);
                        });

                        var frag = document.createDocumentFragment();
                        for (var i = 0; i < _optRows.length; i++) {
                            _optRows[i].el.children[0].textContent = i + 1;
                            _optRows[i].el.classList.remove('table-success');
                            frag.appendChild(_optRows[i].el);
                        }
                        optTbody.appendChild(frag);
                        if (dir === 'desc') {
                            _optRows[0].el.classList.add('table-success');
                        }

                        optTable.querySelectorAll('.opt-sortable i').forEach(function(icon) {
                            icon.className = 'bi bi-chevron-expand ms-1';
                            icon.style.opacity = '0.4';
                        });
                        var icon = clickedTh.querySelector('i');
                        icon.className = dir === 'asc' ? 'bi bi-chevron-up ms-1' : 'bi bi-chevron-down ms-1';
                        icon.style.opacity = '1';

                        optTable.classList.remove('bt-sorting');
                        _optSortBusy = false;
                    });
                });
            });
        }
    })();
    } // end isOptimization

    /* ═══════════════════════════════════════════════════════
       BACKTEST TABLE -- Cached Data Layer + Sorting + Stats
       ═══════════════════════════════════════════════════════
       Performance: all cell values are parsed ONCE into a JS
       cache on page load.  Sorting and histogram read from
       the cache -- zero DOM reads on user interaction.
       ═══════════════════════════════════════════════════════ */
    (function() {
        var btTable = document.getElementById('btTradeTable');
        if (!btTable) return;

        var tbody = btTable.querySelector('tbody');
        var headerCells = btTable.querySelectorAll('thead tr th');
        var numCols = headerCells.length;

        // ── Build cache: parse every cell value once ──
        var _rows = [];          // [{el, cells: [val,...], nums: [float|NaN,...], profit, isWin}]
        var domRows = tbody.querySelectorAll('tr');
        for (var ri = 0; ri < domRows.length; ri++) {
            var tr = domRows[ri];
            var cells = [];
            var nums = [];
            var ch = tr.children;
            for (var ci = 0; ci < ch.length; ci++) {
                var txt = ch[ci].textContent.trim();
                cells.push(txt);
                nums.push(parseFloat(txt));
            }
            var profit = parseFloat(tr.dataset.profit);
            _rows.push({
                el: tr,
                cells: cells,
                nums: nums,
                profit: profit,
                isWin: !isNaN(profit) ? profit > 0 : null
            });
        }

        // ── Sorting (single-click) -- reads from cache ──
        var btSortDir = {};
        var _sortBusy = false;
        btTable.querySelectorAll('.bt-sortable').forEach(function(th) {
            th.addEventListener('click', function() {
                if (_sortBusy) return;
                _sortBusy = true;
                var clickedTh = this;
                var col = this.dataset.col;
                var colIdx = Array.from(this.parentNode.children).indexOf(this);

                btSortDir[col] = btSortDir[col] === 'asc' ? 'desc' : 'asc';
                var dir = btSortDir[col];

                // Show shimmer -- let browser paint, then sort in next frame
                btTable.classList.add('bt-sorting');

                requestAnimationFrame(function() {
                    // Sort the cache array (no DOM reads)
                    _rows.sort(function(a, b) {
                        var aN = a.nums[colIdx], bN = b.nums[colIdx];
                        if (!isNaN(aN) && !isNaN(bN)) {
                            return dir === 'asc' ? aN - bN : bN - aN;
                        }
                        var aV = a.cells[colIdx], bV = b.cells[colIdx];
                        return dir === 'asc' ? aV.localeCompare(bV) : bV.localeCompare(aV);
                    });

                    // Batch DOM update with DocumentFragment
                    var frag = document.createDocumentFragment();
                    for (var i = 0; i < _rows.length; i++) {
                        _rows[i].el.children[0].textContent = i + 1;
                        frag.appendChild(_rows[i].el);
                    }
                    tbody.appendChild(frag);

                    // Update sort icons
                    btTable.querySelectorAll('.bt-sortable i').forEach(function(icon) {
                        icon.className = 'bi bi-chevron-expand ms-1';
                        icon.style.opacity = '0.4';
                    });
                    var icon = clickedTh.querySelector('i');
                    icon.className = dir === 'asc' ? 'bi bi-chevron-up ms-1' : 'bi bi-chevron-down ms-1';
                    icon.style.opacity = '1';

                    // Remove shimmer after animation completes
                    btTable.classList.remove('bt-sorting');
                    _sortBusy = false;
                });
            });

        });

        // ── Column Statistics -- triggered by stats button click ──
        btTable.querySelectorAll('.bt-stats-btn').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.stopPropagation(); // prevent sort
                e.preventDefault();
                var th = this.closest('th');
                var col = th.dataset.col;
                var colIdx = Array.from(th.parentNode.children).indexOf(th);

                // Collect entries from cache (zero DOM reads)
                // Try numeric first; fall back to HH:MM time parsing
                var entries = [];
                var isTimeParsed = false;
                for (var i = 0; i < _rows.length; i++) {
                    var v = _rows[i].nums[colIdx];
                    if (!isNaN(v)) {
                        entries.push({ value: v, isWin: _rows[i].isWin });
                    }
                }
                if (entries.length === 0) {
                    // Try HH:MM or H:MM time format -> decimal hours
                    var timeRe = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/;
                    for (var i = 0; i < _rows.length; i++) {
                        var txt = _rows[i].cells[colIdx];
                        var m = timeRe.exec(txt);
                        if (m) {
                            var hrs = parseInt(m[1]) + parseInt(m[2]) / 60 + (m[3] ? parseInt(m[3]) / 3600 : 0);
                            entries.push({ value: hrs, isWin: _rows[i].isWin });
                        }
                    }
                    if (entries.length > 0) isTimeParsed = true;
                }
                if (entries.length === 0) {
                    // No numeric/time data -- show message in modal
                    var loadingEl = document.getElementById('colStatsLoading');
                    var readyEl = document.getElementById('colStatsReady');
                    loadingEl.innerHTML = '<i class="bi bi-info-circle text-muted" style="font-size:2rem;"></i>' +
                        '<div class="loading-text">No numeric data in <strong>' + col + '</strong></div>';
                    readyEl.classList.remove('is-ready');
                    document.getElementById('colStatsTitle').textContent = col;
                    var modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('colStatsModal'));
                    modal.show();
                    // Reset loading content when modal closes
                    document.getElementById('colStatsModal').addEventListener('hidden.bs.modal', function resetLoading() {
                        loadingEl.innerHTML = '<div class="spinner-border text-primary" role="status"><span class="visually-hidden">Loading...</span></div>' +
                            '<div class="loading-text">Computing statistics...</div>';
                        document.getElementById('colStatsModal').removeEventListener('hidden.bs.modal', resetLoading);
                    });
                    return;
                }

                // ── Show modal with loading spinner immediately ──
                var loadingEl = document.getElementById('colStatsLoading');
                var readyEl = document.getElementById('colStatsReady');
                // Reset to spinner (may have been overwritten by "no data" message)
                loadingEl.innerHTML = '<div class="spinner-border text-primary" role="status"><span class="visually-hidden">Loading...</span></div>' +
                    '<div class="loading-text">Computing statistics...</div>';
                loadingEl.style.display = '';
                readyEl.classList.remove('is-ready');
                document.getElementById('colStatsTitle').textContent = col + ' \u2014 Distribution';

                // Destroy previous chart before showing spinner
                if (window._colStatsChartInstance) {
                    window._colStatsChartInstance.destroy();
                    window._colStatsChartInstance = null;
                }

                var modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('colStatsModal'));
                modal.show();

                // ── Defer heavy work so spinner paints first ──
                requestAnimationFrame(function() { setTimeout(function() {

                // State for the interactive histogram
                var allEntries = entries;
                var fullMin = Infinity, fullMax = -Infinity;
                for (var i = 0; i < entries.length; i++) {
                    if (entries[i].value < fullMin) fullMin = entries[i].value;
                    if (entries[i].value > fullMax) fullMax = entries[i].value;
                }
                var zoomStack = [];
                var showWinLoss = false;
                var currentFilter = 'all';

                // Refs
                var canvas = document.getElementById('colStatsChart');
                var slider = document.getElementById('colStatsBinSlider');
                var binCountEl = document.getElementById('colStatsBinCount');
                var zoomOutBtn = document.getElementById('colStatsZoomOut');
                var zoomInfoEl = document.getElementById('colStatsZoomInfo');
                var zoomRangeEl = document.getElementById('colStatsZoomRange');
                var wlBtn = document.getElementById('colStatsShowWL');
                var legendEl = document.getElementById('colStatsLegend');

                // Pre-split win/loss arrays once for fast filter
                var winEntries = [], lossEntries = [];
                for (var i = 0; i < allEntries.length; i++) {
                    if (allEntries[i].isWin === true) winEntries.push(allEntries[i]);
                    else if (allEntries[i].isWin === false) lossEntries.push(allEntries[i]);
                }

                function getFilteredEntries() {
                    if (currentFilter === 'wins') return winEntries;
                    if (currentFilter === 'losses') return lossEntries;
                    return allEntries;
                }

                function getCurrentRange() {
                    if (zoomStack.length > 0) return zoomStack[zoomStack.length - 1];
                    return { lo: fullMin, hi: fullMax };
                }

                function computeStats(vals) {
                    var n = vals.length;
                    if (n === 0) return null;
                    var sorted = vals.slice().sort(function(a, b) { return a - b; });
                    var sum = 0;
                    for (var i = 0; i < n; i++) sum += vals[i];
                    var mean = sum / n;
                    var med = n % 2 === 0 ? (sorted[n/2-1] + sorted[n/2]) / 2 : sorted[Math.floor(n/2)];
                    var vari = 0;
                    for (var i = 0; i < n; i++) { var d = vals[i] - mean; vari += d * d; }
                    return { count: n, mean: mean, median: med, min: sorted[0], max: sorted[n-1], stdDev: Math.sqrt(vari / n) };
                }

                function fmtStat(v) {
                    if (isTimeParsed) {
                        // Convert decimal hours back to HH:MM
                        var h = Math.floor(v);
                        var m = Math.round((v - h) * 60);
                        if (m === 60) { h++; m = 0; }
                        return h + ':' + (m < 10 ? '0' : '') + m;
                    }
                    if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, {maximumFractionDigits: 2});
                    if (Math.abs(v) >= 1) return v.toFixed(2);
                    return v.toFixed(4);
                }

                function updateStatsTable() {
                    var filtered = getFilteredEntries();
                    var vals = [];
                    for (var i = 0; i < filtered.length; i++) vals.push(filtered[i].value);
                    var s = computeStats(vals);
                    if (!s) {
                        document.getElementById('colStatsBody').innerHTML = '<tr><td class="text-muted">No data</td></tr>';
                        return;
                    }
                    document.getElementById('colStatsBody').innerHTML = [
                        ['Count', s.count], ['Mean', fmtStat(s.mean)], ['Median', fmtStat(s.median)],
                        ['Min', fmtStat(s.min)], ['Max', fmtStat(s.max)], ['Std Dev', fmtStat(s.stdDev)]
                    ].map(function(r) {
                        return '<tr><td class="text-muted" style="width:40%;">' + r[0] + '</td><td class="fw-semibold">' + r[1] + '</td></tr>';
                    }).join('');
                }

                function renderHistogram() {
                    var range = getCurrentRange();
                    var numBins = parseInt(slider.value);
                    binCountEl.textContent = numBins;
                    var lo = range.lo, hi = range.hi;
                    var binW = (hi - lo) / numBins || 1;

                    var filtered = getFilteredEntries();
                    var binsAll = new Array(numBins).fill(0);
                    var binsWin = new Array(numBins).fill(0);
                    var binsLoss = new Array(numBins).fill(0);
                    var inRangeCount = 0, inRangeWins = 0, inRangeLosses = 0;

                    for (var i = 0; i < filtered.length; i++) {
                        var v = filtered[i].value;
                        if (v < lo || v > hi) continue;
                        inRangeCount++;
                        var idx = Math.floor((v - lo) / binW);
                        if (idx >= numBins) idx = numBins - 1;
                        if (idx < 0) idx = 0;
                        binsAll[idx]++;
                        if (filtered[i].isWin === true) { binsWin[idx]++; inRangeWins++; }
                        else if (filtered[i].isWin === false) { binsLoss[idx]++; inRangeLosses++; }
                    }

                    var binLabels = new Array(numBins);
                    for (var b = 0; b < numBins; b++) {
                        binLabels[b] = fmtStat(lo + b * binW);
                    }

                    var datasets;
                    if (showWinLoss) {
                        datasets = [
                            { label: 'Wins', data: binsWin, backgroundColor: 'rgba(22,163,74,0.7)', borderColor: 'rgba(22,163,74,1)', borderWidth: 1 },
                            { label: 'Losses', data: binsLoss, backgroundColor: 'rgba(220,38,38,0.7)', borderColor: 'rgba(220,38,38,1)', borderWidth: 1 }
                        ];
                        legendEl.innerHTML = '<span class="legend-win">Wins (' + inRangeWins + ')</span>' +
                            '<span class="legend-loss">Losses (' + inRangeLosses + ')</span>';
                    } else {
                        var bgColors = new Array(numBins), bdColors = new Array(numBins);
                        for (var i = 0; i < numBins; i++) {
                            var mid = lo + (i + 0.5) * binW;
                            bgColors[i] = mid >= 0 ? 'rgba(59,130,246,0.6)' : 'rgba(148,163,184,0.5)';
                            bdColors[i] = mid >= 0 ? 'rgba(59,130,246,1)' : 'rgba(148,163,184,0.8)';
                        }
                        datasets = [{ label: 'Frequency', data: binsAll, backgroundColor: bgColors, borderColor: bdColors, borderWidth: 1 }];
                        legendEl.innerHTML = '<span class="legend-all">All trades (' + inRangeCount + ')</span>';
                    }

                    if (zoomStack.length > 0) {
                        zoomOutBtn.classList.remove('d-none');
                        zoomInfoEl.classList.remove('d-none');
                        zoomRangeEl.textContent = fmtStat(lo) + ' \u2014 ' + fmtStat(hi);
                    } else {
                        zoomOutBtn.classList.add('d-none');
                        zoomInfoEl.classList.add('d-none');
                    }

                    if (window._colStatsChartInstance) window._colStatsChartInstance.destroy();

                    window._colStatsChartInstance = new Chart(canvas.getContext('2d'), {
                        type: 'bar',
                        data: { labels: binLabels, datasets: datasets },
                        options: {
                            responsive: true,
                            maintainAspectRatio: false,
                            animation: { duration: 120 },
                            plugins: {
                                legend: { display: false },
                                tooltip: {
                                    callbacks: {
                                        title: function(items) {
                                            var i = items[0].dataIndex;
                                            return fmtStat(lo + i * binW) + ' \u2014 ' + fmtStat(lo + (i+1) * binW);
                                        }
                                    }
                                }
                            },
                            scales: {
                                y: { beginAtZero: true, stacked: showWinLoss, ticks: { stepSize: 1, font: { size: 10 } }, title: { display: true, text: 'Trades', font: { size: 10 } } },
                                x: { stacked: showWinLoss, ticks: { font: { size: 9 }, maxRotation: 45 }, title: { display: true, text: col, font: { size: 10 } } }
                            },
                            onClick: function(evt, elems) {
                                if (!elems || elems.length === 0) return;
                                var idx = elems[0].index;
                                var bLo = lo + idx * binW;
                                var bHi = bLo + binW;
                                if (binsAll[idx] < 2) return;
                                if (evt.native && evt.native.shiftKey) {
                                    if (zoomStack.length > 0) { zoomStack.pop(); renderHistogram(); }
                                } else {
                                    zoomStack.push({ lo: bLo, hi: bHi });
                                    renderHistogram();
                                }
                            }
                        }
                    });
                }

                slider.oninput = function() { binCountEl.textContent = this.value; renderHistogram(); };
                wlBtn.onclick = function() { showWinLoss = !showWinLoss; this.classList.toggle('active', showWinLoss); renderHistogram(); };
                zoomOutBtn.onclick = function() { zoomStack = []; renderHistogram(); };

                document.querySelectorAll('[data-stats-filter]').forEach(function(btn) {
                    btn.addEventListener('click', function() {
                        document.querySelectorAll('[data-stats-filter]').forEach(function(b) { b.classList.remove('active'); });
                        this.classList.add('active');
                        currentFilter = this.dataset.statsFilter;
                        updateStatsTable();
                        renderHistogram();
                    });
                });

                var initBins = Math.min(25, Math.max(8, Math.ceil(Math.sqrt(entries.length))));
                slider.value = initBins;
                currentFilter = 'all';
                document.querySelectorAll('[data-stats-filter]').forEach(function(b) {
                    b.classList.toggle('active', b.dataset.statsFilter === 'all');
                });
                showWinLoss = false;
                wlBtn.classList.remove('active');
                zoomStack = [];
                updateStatsTable();
                renderHistogram();

                // ── Swap loading -> ready ──
                loadingEl.style.display = 'none';
                readyEl.classList.add('is-ready');

                }, 16); }); // end rAF + setTimeout
            });
        });
    })();

    /* ═══════════════════════════════════════════════════════
       COLUMN CHOOSER -- Show/Hide Trade Table Columns
       ═══════════════════════════════════════════════════════ */
    (function() {
        var btTable = document.getElementById('btTradeTable');
        var menu = document.getElementById('colChooserMenu');
        if (!btTable || !menu) return;

        // Standard AmiBroker trade-list columns (everything else is "custom")
        var standardCols = ['symbol','trade','date','price','ex. date','ex. price',
                            '% chg','profit','% profit','contracts','shares',
                            'position value','cum. profit','# bars','profit/bar',
                            'mae','mfe','scale in/out','entry signal','exit signal',
                            'max. profit','max. loss'];

        // Columns shown by default (core + custom metrics)
        var defaultCols = ['symbol','trade','date','price','ex. date','ex. price',
                           'profit','% chg','shares','contracts','position value',
                           'cum. profit','# bars','profit/bar',
                           'temaslope@entry','1stderiv@entry','2ndderiv@entry',
                           'timeofday@entry'];

        // localStorage key based on current page (per-run persistence)
        var storageKey = 'bt_col_vis_' + window.location.pathname;

        // Load saved visibility (or null if never set)
        var savedVis = null;
        try {
            var raw = localStorage.getItem(storageKey);
            if (raw) savedVis = JSON.parse(raw);
        } catch(e) {}

        var checks = menu.querySelectorAll('.col-toggle-check');

        // CSS injection for column visibility -- one <style> rule hides
        // a column across ALL rows instantly (vs iterating every cell).
        var colStyleEl = document.createElement('style');
        document.head.appendChild(colStyleEl);

        function applyVisibility() {
            // Build CSS rules for hidden columns (nth-child is 1-based)
            var rules = [];
            checks.forEach(function(cb) {
                if (!cb.checked) {
                    var nth = parseInt(cb.dataset.colIndex) + 1; // colIndex is 1-based relative to data cols, +1 for CSS nth-child (col 0 is #)
                    rules.push('#btTradeTable th:nth-child(' + nth + '),#btTradeTable td:nth-child(' + nth + '){display:none}');
                }
            });
            colStyleEl.textContent = rules.join('\n');

            // Save to localStorage
            var vis = {};
            checks.forEach(function(cb) {
                vis[cb.dataset.colName] = cb.checked;
            });
            try { localStorage.setItem(storageKey, JSON.stringify(vis)); } catch(e) {}
        }

        // Initialise checkboxes from saved state or defaults
        checks.forEach(function(cb) {
            var colName = cb.dataset.colName;
            if (savedVis && colName in savedVis) {
                cb.checked = savedVis[colName];
            } else {
                // Default: show core columns, hide others
                cb.checked = defaultCols.indexOf(colName.toLowerCase()) !== -1;
            }
            cb.addEventListener('change', applyVisibility);
        });

        // Show All / Default links
        document.getElementById('colShowAll').addEventListener('click', function(e) {
            e.preventDefault();
            checks.forEach(function(cb) { cb.checked = true; });
            applyVisibility();
        });
        document.getElementById('colShowDefault').addEventListener('click', function(e) {
            e.preventDefault();
            checks.forEach(function(cb) {
                cb.checked = defaultCols.indexOf(cb.dataset.colName.toLowerCase()) !== -1;
            });
            applyVisibility();
        });

        // Tag custom columns with colored dot indicator (in dropdown and table header)
        checks.forEach(function(cb) {
            var colName = cb.dataset.colName;
            var isCustom = standardCols.indexOf(colName.toLowerCase()) === -1;
            if (isCustom) {
                // Add colored left border in dropdown
                cb.parentNode.classList.add('is-custom');
                // Add dot in dropdown label
                var label = cb.parentNode.querySelector('label');
                if (label && !label.querySelector('.col-custom-dot')) {
                    var dot = document.createElement('span');
                    dot.className = 'col-custom-dot';
                    label.insertBefore(dot, label.firstChild);
                }
                // Add class to table header (CSS ::before adds the dot)
                var colIdx = parseInt(cb.dataset.colIndex);
                var headerRow = btTable.querySelector('thead tr');
                if (headerRow && headerRow.children[colIdx]) {
                    headerRow.children[colIdx].classList.add('bt-custom-col');
                }
            }
        });

        // Apply on page load
        applyVisibility();
    })();

    /* ═══════════════════════════════════════════════════════
       EQUITY CURVE -- Dual-Mode Chart (backtest only)
       ═══════════════════════════════════════════════════════ */
    if (!_resultsCfg.isOptimization) {
    var chartInstance = null;
    var chartData = null;
    var canvasEl = document.getElementById('equityCurveChart');
    var ctx = canvasEl ? canvasEl.getContext('2d') : null;
    var activityCtx = document.getElementById('tradeActivityChart');
    var activityChart = null;

    var btnTrade = document.getElementById('btnTradeView');
    var btnTime = document.getElementById('btnTimeView');
    var activitySection = document.getElementById('activitySection');
    var summarySection = document.getElementById('summarySection');

    // Fetch equity curve data -- use run-specific API for GUID-based runs
    var equityCurveUrl = _resultsCfg.equityCurveUrl;
    fetch(equityCurveUrl)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            document.getElementById('equityCurveLoading').style.display = 'none';

            if (data.error) {
                document.getElementById('equityCurveErrorMsg').textContent = data.error;
                document.getElementById('equityCurveError').classList.remove('d-none');
                return;
            }

            chartData = data;
            canvasEl.style.display = 'block';

            // Default to trade view
            renderTradeView();

            // Populate summary stats if available
            if (data.summary && data.summary.date_range) {
                document.getElementById('summaryRange').textContent = data.summary.date_range;
                document.getElementById('summaryDays').textContent = data.summary.total_days;
                document.getElementById('summaryActiveDays').textContent = data.summary.active_trading_days;
                document.getElementById('summaryTradesMonth').textContent = data.summary.trades_per_month;
                document.getElementById('summaryHolding').textContent = data.summary.avg_holding_period_bars + ' bars';
            }
        })
        .catch(function(err) {
            document.getElementById('equityCurveLoading').style.display = 'none';
            document.getElementById('equityCurveErrorMsg').textContent = 'Failed to load equity curve: ' + err.message;
            document.getElementById('equityCurveError').classList.remove('d-none');
        });

    // Toggle handlers
    btnTrade.addEventListener('click', function() {
        btnTrade.classList.add('active');
        btnTime.classList.remove('active');
        activitySection.classList.add('d-none');
        summarySection.classList.add('d-none');
        if (chartData) renderTradeView();
    });

    btnTime.addEventListener('click', function() {
        btnTime.classList.add('active');
        btnTrade.classList.remove('active');
        activitySection.classList.remove('d-none');
        summarySection.classList.remove('d-none');
        if (chartData) renderTimeView();
    });

    function renderTradeView() {
        if (chartInstance) chartInstance.destroy();
        // Support both new (trade_view) and legacy (flat) API responses
        var tv = chartData.trade_view || chartData;
        chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: tv.labels,
                datasets: [{
                    label: 'Equity ($)',
                    data: tv.equity,
                    borderColor: '#3b82f6',
                    backgroundColor: 'rgba(59,130,246,0.08)',
                    fill: true,
                    tension: 0.15,
                    pointBackgroundColor: tv.colors,
                    pointBorderColor: tv.colors,
                    pointRadius: 6,
                    pointHoverRadius: 9,
                    borderWidth: 2
                }]
            },
            options: chartOptions('Trade #', tv.dates || [], tv.profits || [])
        });
    }

    function renderTimeView() {
        if (chartInstance) chartInstance.destroy();
        var tv = chartData.time_view;
        if (!tv || !tv.dates || tv.dates.length === 0) {
            document.getElementById('equityCurveErrorMsg').textContent = 'No date data available for time view.';
            document.getElementById('equityCurveError').classList.remove('d-none');
            return;
        }
        document.getElementById('equityCurveError').classList.add('d-none');

        // Main equity line + trade markers as scatter overlay
        chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: tv.dates,
                datasets: [
                    {
                        label: 'Equity ($)',
                        data: tv.equity,
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59,130,246,0.06)',
                        fill: true,
                        tension: 0.1,
                        pointRadius: 0,
                        borderWidth: 2
                    },
                    {
                        label: 'Trades',
                        data: tv.trade_dates.map(function(d, i) {
                            return { x: d, y: tv.trade_equities[i] };
                        }),
                        type: 'scatter',
                        pointBackgroundColor: tv.trade_colors,
                        pointBorderColor: tv.trade_colors,
                        pointRadius: 7,
                        pointHoverRadius: 10,
                        showLine: false
                    }
                ]
            },
            options: {
                responsive: true,
                interaction: { mode: 'nearest', intersect: true },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(15,23,42,0.95)',
                        titleFont: { size: 12 },
                        bodyFont: { size: 11 },
                        padding: 10,
                        callbacks: {
                            label: function(context) {
                                if (context.datasetIndex === 1) {
                                    var i = context.dataIndex;
                                    var p = tv.trade_profits[i];
                                    var sign = p >= 0 ? '+' : '';
                                    return 'Trade: $' + context.parsed.y.toLocaleString() + ' (' + sign + '$' + p.toLocaleString() + ')';
                                }
                                return 'Equity: $' + context.parsed.y.toLocaleString();
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        title: { display: true, text: 'Equity ($)', font: { size: 11 } },
                        ticks: { callback: function(v) { return '$' + v.toLocaleString(); }, font: { size: 10 } },
                        grid: { color: 'rgba(0,0,0,0.05)' }
                    },
                    x: {
                        title: { display: true, text: 'Date', font: { size: 11 } },
                        ticks: { maxTicksLimit: 12, font: { size: 10 } },
                        grid: { display: false }
                    }
                }
            }
        });

        // Render trade activity bar chart
        renderActivityChart(tv);
    }

    function renderActivityChart(tv) {
        if (activityChart) activityChart.destroy();
        if (!activityCtx) return;

        // Aggregate trade counts by month
        var monthMap = {};
        tv.trade_dates.forEach(function(d) {
            var month = d.substring(0, 7); // YYYY-MM
            monthMap[month] = (monthMap[month] || 0) + 1;
        });
        var months = Object.keys(monthMap).sort();
        var counts = months.map(function(m) { return monthMap[m]; });

        activityChart = new Chart(activityCtx.getContext('2d'), {
            type: 'bar',
            data: {
                labels: months,
                datasets: [{
                    label: 'Trades',
                    data: counts,
                    backgroundColor: 'rgba(59,130,246,0.6)',
                    borderRadius: 3
                }]
            },
            options: {
                responsive: true,
                plugins: { legend: { display: false } },
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: { stepSize: 1, font: { size: 10 } },
                        title: { display: true, text: 'Trades', font: { size: 10 } }
                    },
                    x: {
                        ticks: { font: { size: 10 } },
                        grid: { display: false }
                    }
                }
            }
        });
    }

    function chartOptions(xLabel, dates, profits) {
        return {
            responsive: true,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: 'rgba(15,23,42,0.95)',
                    titleFont: { size: 12 },
                    bodyFont: { size: 11 },
                    padding: 10,
                    callbacks: {
                        title: function(items) {
                            var i = items[0].dataIndex;
                            var d = dates[i];
                            return d ? items[0].label + ' \u2014 ' + d : items[0].label;
                        },
                        label: function(context) {
                            var p = profits[context.dataIndex];
                            var eq = context.parsed.y;
                            if (context.dataIndex === 0) {
                                return 'Starting Equity: $' + eq.toLocaleString();
                            }
                            var sign = p >= 0 ? '+' : '';
                            return 'Equity: $' + eq.toLocaleString() + '  (' + sign + '$' + p.toLocaleString() + ')';
                        }
                    }
                }
            },
            scales: {
                y: {
                    title: { display: true, text: 'Equity ($)', font: { size: 11 } },
                    ticks: { callback: function(v) { return '$' + v.toLocaleString(); }, font: { size: 10 } },
                    grid: { color: 'rgba(0,0,0,0.05)' }
                },
                x: {
                    title: { display: true, text: xLabel, font: { size: 11 } },
                    ticks: { font: { size: 10 } },
                    grid: { display: false }
                }
            }
        };
    }

    } // end backtest-only equity curve block

    /* ═══════════════════════════════════════════════════════
       CODEMIRROR -- AFL Editor Setup
       ═══════════════════════════════════════════════════════ */
    var editorEl = document.getElementById('afl-code');
    if (editorEl) {
        var editor = CodeMirror.fromTextArea(editorEl, {
            mode: 'text/x-csrc',
            theme: 'dracula',
            lineNumbers: true,
            matchBrackets: true,
            styleActiveLine: true,
            indentUnit: 4,
            tabSize: 4,
            lineWrapping: false
        });
        editor.setSize(null, 420);

        // Sync editor content to all afl_content fields on any form submit
        document.querySelectorAll('form.afl-form').forEach(function(form) {
            form.addEventListener('submit', function() {
                editor.save();
                var content = editor.getValue();
                document.querySelectorAll('input[name="afl_content"], textarea[name="afl_content"]').forEach(function(el) {
                    el.value = content;
                });
            });
        });

        // Ctrl+S / Cmd+S keyboard shortcut to save
        editor.setOption('extraKeys', {
            'Ctrl-S': function() {
                editor.save();
                var content = editor.getValue();
                document.querySelectorAll('textarea[name="afl_content"]').forEach(function(el) {
                    el.value = content;
                });
                document.getElementById('aflSaveForm').submit();
            },
            'Cmd-S': function() {
                editor.save();
                var content = editor.getValue();
                document.querySelectorAll('textarea[name="afl_content"]').forEach(function(el) {
                    el.value = content;
                });
                document.getElementById('aflSaveForm').submit();
            }
        });

        // Version label toggle
        var versionBtn = document.getElementById('btnSaveVersion');
        var versionInput = document.getElementById('versionLabelGroup');
        if (versionBtn && versionInput) {
            versionBtn.addEventListener('click', function() {
                versionInput.classList.toggle('d-none');
                if (!versionInput.classList.contains('d-none')) {
                    document.getElementById('versionLabel').focus();
                }
            });
        }
    }

    if (!_resultsCfg.isOptimization) {
    /* ═══════════════════════════════════════════════════════
       TRADE CHART -- Strategy-linked indicators with sub-pane
       ═══════════════════════════════════════════════════════ */
    var strategyIndicatorConfigs = _resultsCfg.indicatorConfigs || [];
    var candlestickChart = null;
    var tradeSubPaneChart = null;
    var tradeCandleSeries = null;
    var tradeVolumeSeries = null;
    var tradeMainSeries = {};
    var tradeSubPaneSeries = {};
    var candlestickModal = null;
    var currentTradeContext = null;
    var currentInterval = 60;
    var tradeTooltipState = { time: null, ohlc: null, volume: null, mainItems: [], subItems: [] };

    var INTERVAL_LABELS = {60: '1-min', 300: '5-min', 600: '10-min', 86400: 'Daily'};

    function fmtPrice(v) { return v != null ? v.toFixed(2) : '--'; }
    function fmtNum(v) { return v != null ? v.toFixed(2) : '--'; }
    function fmtVol(v) {
        if (v == null) return '--';
        if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
        if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
        return v.toString();
    }
    function escHtml(s) {
        var d = document.createElement('div');
        d.appendChild(document.createTextNode(s));
        return d.innerHTML;
    }

    // --- Trade row click handler ---
    document.querySelectorAll('.trade-row-clickable').forEach(function(row) {
        row.addEventListener('click', function(e) {
            if (e.target.closest('a, button')) return;
            var symbol = this.dataset.symbol;
            var entryDate = this.dataset.entryDate;
            var exitDate = this.dataset.exitDate;
            var entryPrice = parseFloat(this.dataset.entryPrice);
            var exitPrice = parseFloat(this.dataset.exitPrice);
            var tradeType = this.dataset.tradeType;
            var tradeIndex = this.dataset.tradeIndex;
            var profit = parseFloat(this.dataset.profit) || 0;
            if (!symbol || !entryDate || !exitDate) return;
            showCandlestickChart(symbol, entryDate, exitDate, entryPrice, exitPrice, tradeType, tradeIndex, profit);
        });
    });

    // --- Timeframe selector ---
    document.querySelectorAll('#tfSelector .btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var interval = parseInt(this.dataset.interval);
            currentInterval = interval;
            document.querySelectorAll('#tfSelector .btn').forEach(function(b) { b.classList.remove('active'); });
            this.classList.add('active');
            loadChartData(interval);
        });
    });

    // --- Indicator toggle handlers ---
    document.querySelectorAll('.indicator-toggle').forEach(function(toggle) {
        toggle.addEventListener('change', function() {
            loadChartData(currentInterval);
        });
    });

    function getActiveIndicators() {
        var indicators = [];
        document.querySelectorAll('.indicator-toggle:checked').forEach(function(el) {
            indicators.push({
                type: el.dataset.type,
                params: JSON.parse(el.dataset.params),
                overlay: el.dataset.overlay === 'true',
                color: el.dataset.color || '#FF6D00'
            });
        });
        return indicators;
    }

    function showCandlestickChart(symbol, entryDate, exitDate, entryPrice, exitPrice, tradeType, tradeIndex, profit) {
        currentTradeContext = {
            symbol: symbol, entryDate: entryDate, exitDate: exitDate,
            entryPrice: entryPrice, exitPrice: exitPrice,
            tradeType: tradeType, tradeIndex: tradeIndex, profit: profit
        };
        currentInterval = 60;

        if (!candlestickModal) {
            candlestickModal = new bootstrap.Modal(document.getElementById('candlestickModal'));
        }

        document.getElementById('chartTradeTitle').textContent = symbol + ' \u2014 Trade #' + tradeIndex;
        document.getElementById('chartDirection').textContent = tradeType;
        document.getElementById('chartEntryInfo').textContent = entryPrice.toFixed(1) + ' @ ' + entryDate;
        document.getElementById('chartExitInfo').textContent = exitPrice.toFixed(1) + ' @ ' + exitDate;

        var pnlEl = document.getElementById('chartPnL');
        pnlEl.textContent = (profit >= 0 ? '+' : '') + profit.toFixed(2);
        pnlEl.className = profit >= 0 ? 'text-success fw-bold' : 'text-danger fw-bold';

        document.querySelectorAll('#tfSelector .btn').forEach(function(b) {
            b.classList.toggle('active', b.dataset.interval === '60');
        });

        candlestickModal.show();
        loadChartData(60);
    }

    function loadChartData(interval) {
        var ctx = currentTradeContext;
        if (!ctx) return;

        var container = document.getElementById('candlestickChartContainer');
        if (candlestickChart) { candlestickChart.remove(); candlestickChart = null; }
        if (tradeSubPaneChart) { tradeSubPaneChart.remove(); tradeSubPaneChart = null; }
        tradeMainSeries = {};
        tradeSubPaneSeries = {};
        document.getElementById('tradeSubPaneCard').style.display = 'none';
        document.getElementById('tradeDataTooltip').classList.remove('visible');

        var loadingEl = document.getElementById('chartLoading');
        container.innerHTML = '';
        container.appendChild(loadingEl);
        loadingEl.style.display = 'flex';
        document.getElementById('chartError').classList.add('d-none');

        document.getElementById('chartIntervalLabel').textContent =
            (INTERVAL_LABELS[interval] || interval + 's') + ' candlestick data from AmiBroker database';

        var activeIndicators = getActiveIndicators();
        var apiConfigs = activeIndicators.map(function(c) {
            return { type: c.type, params: c.params };
        });

        var url = '/api/ohlcv/' + encodeURIComponent(ctx.symbol)
            + '?entry_date=' + encodeURIComponent(ctx.entryDate)
            + '&exit_date=' + encodeURIComponent(ctx.exitDate)
            + '&interval=' + interval;

        if (apiConfigs.length > 0) {
            url += '&indicators=' + encodeURIComponent(JSON.stringify(apiConfigs));
        }

        fetch(url)
            .then(function(r) {
                if (!r.ok) return r.text().then(function(t) { throw new Error('Server error (' + r.status + ')'); });
                return r.json();
            })
            .then(function(data) {
                loadingEl.style.display = 'none';
                if (data.error) {
                    document.getElementById('chartErrorMsg').textContent = data.error;
                    document.getElementById('chartError').classList.remove('d-none');
                    return;
                }
                if (!data.data || data.data.length === 0) {
                    document.getElementById('chartErrorMsg').textContent = 'No bar data available for this date range.';
                    document.getElementById('chartError').classList.remove('d-none');
                    return;
                }

                // Merge overlay/color from toggle configs into API indicator results
                var indicators = data.indicators || [];
                indicators.forEach(function(ind) {
                    var match = activeIndicators.find(function(c) { return c.type === ind.type; });
                    if (match) {
                        ind.overlay = match.overlay;
                        ind.color = match.color;
                    }
                });

                renderCandlestickChart(container, data.data, ctx.entryDate, ctx.exitDate,
                                       ctx.entryPrice, ctx.exitPrice, ctx.tradeType, indicators);
            })
            .catch(function(err) {
                loadingEl.style.display = 'none';
                document.getElementById('chartErrorMsg').textContent = 'Failed to fetch chart data: ' + err.message;
                document.getElementById('chartError').classList.remove('d-none');
            });
    }

    function renderCandlestickChart(container, bars, entryDate, exitDate, entryPrice, exitPrice, tradeType, indicators) {
        // Split indicators into overlay and sub-pane groups
        var overlayInds = indicators.filter(function(i) { return i.overlay !== false && !i.error; });
        var subPaneInds = indicators.filter(function(i) { return i.overlay === false && !i.error; });
        var mainHeight = subPaneInds.length > 0 ? 310 : 400;

        candlestickChart = LightweightCharts.createChart(container, {
            width: container.clientWidth,
            height: mainHeight,
            layout: {
                background: { type: 'solid', color: '#ffffff' },
                textColor: '#333',
                fontSize: 11,
            },
            grid: {
                vertLines: { color: 'rgba(197, 203, 206, 0.4)' },
                horzLines: { color: 'rgba(197, 203, 206, 0.4)' },
            },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            rightPriceScale: { borderColor: 'rgba(197, 203, 206, 0.8)' },
            timeScale: {
                borderColor: 'rgba(197, 203, 206, 0.8)',
                timeVisible: true,
                secondsVisible: false,
            },
        });

        // Candlestick series
        tradeCandleSeries = candlestickChart.addCandlestickSeries({
            upColor: '#26a69a', downColor: '#ef5350',
            borderDownColor: '#ef5350', borderUpColor: '#26a69a',
            wickDownColor: '#ef5350', wickUpColor: '#26a69a',
        });
        tradeCandleSeries.setData(bars);

        // Volume histogram
        tradeVolumeSeries = candlestickChart.addHistogramSeries({
            color: 'rgba(76, 175, 80, 0.3)',
            priceFormat: { type: 'volume' },
            priceScaleId: 'volume',
        });
        candlestickChart.priceScale('volume').applyOptions({
            scaleMargins: { top: 0.8, bottom: 0 },
        });
        tradeVolumeSeries.setData(bars.map(function(b) {
            return {
                time: b.time,
                value: b.volume || 0,
                color: b.close >= b.open ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)',
            };
        }));

        // --- Render overlay indicators on main chart ---
        tradeRenderIndicatorSeries(candlestickChart, tradeMainSeries, overlayInds);

        // --- Create sub-pane for non-overlay indicators ---
        if (subPaneInds.length > 0) {
            tradeCreateSubPane(subPaneInds, bars);
        }

        // --- Entry / exit markers ---
        var markers = [];
        var entryTime = findClosestBarTime(bars, entryDate);
        var exitTime = findClosestBarTime(bars, exitDate);
        if (entryTime) {
            markers.push({
                time: entryTime, position: 'belowBar', color: '#2196F3',
                shape: 'arrowUp', text: 'Entry @ ' + entryPrice.toFixed(1),
            });
        }
        if (exitTime) {
            markers.push({
                time: exitTime, position: 'aboveBar', color: '#FF9800',
                shape: 'arrowDown', text: 'Exit @ ' + exitPrice.toFixed(1),
            });
        }
        if (markers.length > 0) {
            markers.sort(function(a, b) { return a.time - b.time; });
            tradeCandleSeries.setMarkers(markers);
        }

        // --- Data tooltip: crosshair tracking on main chart ---
        candlestickChart.subscribeCrosshairMove(function(param) {
            if (!param.time || !param.point) {
                tradeTooltipState.time = null;
                updateTradeDataTooltip();
                return;
            }
            tradeTooltipState.time = param.time;
            tradeTooltipState.ohlc = null;
            tradeTooltipState.volume = null;
            tradeTooltipState.mainItems = [];
            if (param.seriesData) {
                param.seriesData.forEach(function(val, series) {
                    if (series === tradeCandleSeries && val) {
                        tradeTooltipState.ohlc = val;
                    } else if (series === tradeVolumeSeries && val) {
                        tradeTooltipState.volume = val.value;
                    } else if (series._tooltipName && val && val.value !== undefined) {
                        tradeTooltipState.mainItems.push({
                            name: series._tooltipName, value: val.value,
                            color: series._tooltipColor || '#aaa'
                        });
                    }
                });
            }
            // Update sub-pane indicator values from main chart crosshair
            if (tradeSubPaneChart) {
                tradeTooltipState.subItems = [];
                var subCoord = tradeSubPaneChart.timeScale().timeToCoordinate(param.time);
                var logIdx = subCoord !== null
                    ? tradeSubPaneChart.timeScale().coordinateToLogical(subCoord)
                    : candlestickChart.timeScale().coordinateToLogical(param.point.x);
                Object.keys(tradeSubPaneSeries).forEach(function(key) {
                    var s = tradeSubPaneSeries[key];
                    if (!s || !s._tooltipName) return;
                    try {
                        var dp = s.dataByIndex(logIdx);
                        if (dp && dp.value !== undefined) {
                            tradeTooltipState.subItems.push({
                                name: s._tooltipName, value: dp.value,
                                color: s._tooltipColor || '#aaa',
                            });
                        }
                    } catch(e) {}
                });
            }
            updateTradeDataTooltip();
        });

        // --- Auto-resize ---
        var resizeObserver = new ResizeObserver(function(entries) {
            for (var entry of entries) {
                candlestickChart.applyOptions({ width: entry.contentRect.width });
                if (tradeSubPaneChart) tradeSubPaneChart.applyOptions({ width: entry.contentRect.width });
            }
        });
        resizeObserver.observe(container);

        document.getElementById('candlestickModal').addEventListener('hidden.bs.modal', function() {
            resizeObserver.disconnect();
            document.getElementById('tradeDataTooltip').classList.remove('visible');
        }, { once: true });

        candlestickChart.timeScale().fitContent();
    }

    // --- Render indicator line series on a chart ---
    function tradeRenderIndicatorSeries(chart, seriesMap, indicators) {
        indicators.forEach(function(ind) {
            if (ind.error) return;
            var color = ind.color || '#FF6D00';

            if (ind.series) {
                Object.keys(ind.series).forEach(function(key) {
                    var lineData = ind.series[key];
                    if (!lineData || lineData.length === 0) return;

                    var lineColor = color, lineWidth = 2, lineStyle = 0;
                    if (key === 'upper1' || key === 'lower1' || key === 'upper' || key === 'lower') {
                        lineWidth = 1; lineStyle = 2;
                    }
                    if (key === 'upper2' || key === 'lower2') { lineWidth = 1; lineStyle = 3; }
                    if (key === 'middle' || key === 'vwap' || key === 'adx' || key === 'k') { lineWidth = 2; }
                    if (key === 'plus_di') lineColor = '#00C853';
                    if (key === 'minus_di') lineColor = '#FF1744';
                    if (key === 'd') { lineColor = '#FF9800'; lineStyle = 2; }
                    if (key === 'first_deriv') { lineColor = '#FF5722'; lineWidth = 2; }
                    if (key === 'second_deriv') { lineColor = '#E040FB'; lineWidth = 1; lineStyle = 2; }

                    var seriesTitle = ind.type.toUpperCase() + ' ' + key;
                    var series = chart.addLineSeries({
                        color: lineColor, lineWidth: lineWidth, lineStyle: lineStyle,
                        title: seriesTitle,
                        lastValueVisible: false, priceLineVisible: false,
                    });
                    series._tooltipName = seriesTitle;
                    series._tooltipColor = lineColor;
                    series.setData(lineData);
                    seriesMap[ind.type + '_' + key] = series;
                });
            } else if (ind.data) {
                var seriesTitle = ind.label || ind.type.toUpperCase();
                var series = chart.addLineSeries({
                    color: color, lineWidth: 2,
                    title: seriesTitle,
                    lastValueVisible: false, priceLineVisible: false,
                });
                series._tooltipName = seriesTitle;
                series._tooltipColor = color;
                series.setData(ind.data);
                seriesMap[ind.type] = series;
            }
        });
    }

    // --- Create sub-pane chart for non-overlay indicators ---
    function tradeCreateSubPane(subIndicators, bars) {
        var card = document.getElementById('tradeSubPaneCard');
        card.style.display = 'block';
        var labels = subIndicators.map(function(i) { return i.type.toUpperCase(); });
        document.getElementById('tradeSubPaneLabel').textContent = labels.join(' / ');

        var spContainer = document.getElementById('tradeSubPaneChart');
        tradeSubPaneChart = LightweightCharts.createChart(spContainer, {
            width: spContainer.clientWidth,
            height: 150,
            layout: {
                background: { type: 'solid', color: '#fafafa' },
                textColor: '#333', fontSize: 10,
            },
            grid: {
                vertLines: { color: 'rgba(197, 203, 206, 0.3)' },
                horzLines: { color: 'rgba(197, 203, 206, 0.3)' },
            },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            rightPriceScale: { borderColor: 'rgba(197, 203, 206, 0.8)' },
            timeScale: {
                borderColor: 'rgba(197, 203, 206, 0.8)',
                timeVisible: true, secondsVisible: false,
            },
        });

        // Render sub-pane indicator series
        tradeRenderIndicatorSeries(tradeSubPaneChart, tradeSubPaneSeries, subIndicators);

        // Threshold lines
        subIndicators.forEach(function(ind) {
            if (ind.type === 'rsi') {
                tradeAddHLine(tradeSubPaneChart, tradeSubPaneSeries, 'rsi_ob', 70, '#FF174480', bars);
                tradeAddHLine(tradeSubPaneChart, tradeSubPaneSeries, 'rsi_os', 30, '#00C85380', bars);
            }
            if (ind.type === 'stochastic') {
                tradeAddHLine(tradeSubPaneChart, tradeSubPaneSeries, 'stoch_ob', 80, '#FF174480', bars);
                tradeAddHLine(tradeSubPaneChart, tradeSubPaneSeries, 'stoch_os', 20, '#00C85380', bars);
            }
            if (ind.type === 'derivative') {
                tradeAddHLine(tradeSubPaneChart, tradeSubPaneSeries, 'deriv_zero', 0, '#9E9E9E80', bars);
            }
        });

        // Sync time scales between main and sub-pane
        var syncingTimeScale = false;
        candlestickChart.timeScale().subscribeVisibleLogicalRangeChange(function() {
            if (syncingTimeScale) return;
            syncingTimeScale = true;
            try {
                var range = candlestickChart.timeScale().getVisibleRange();
                if (range && tradeSubPaneChart) tradeSubPaneChart.timeScale().setVisibleRange(range);
            } catch(e) {}
            syncingTimeScale = false;
        });
        tradeSubPaneChart.timeScale().subscribeVisibleLogicalRangeChange(function() {
            if (syncingTimeScale) return;
            syncingTimeScale = true;
            try {
                var range = tradeSubPaneChart.timeScale().getVisibleRange();
                if (range && candlestickChart) candlestickChart.timeScale().setVisibleRange(range);
            } catch(e) {}
            syncingTimeScale = false;
        });

        // Sync crosshairs
        var syncingCrosshair = false;
        candlestickChart.subscribeCrosshairMove(function(param) {
            if (syncingCrosshair || !tradeSubPaneChart) return;
            syncingCrosshair = true;
            if (!param.time || !param.point) {
                tradeSubPaneChart.clearCrosshairPosition();
            } else {
                var subKeys = Object.keys(tradeSubPaneSeries);
                if (subKeys.length > 0) {
                    var targetSeries = tradeSubPaneSeries[subKeys[0]];
                    var subPrice = 0;
                    try {
                        var coord = tradeSubPaneChart.timeScale().timeToCoordinate(param.time);
                        if (coord !== null) {
                            var li = tradeSubPaneChart.timeScale().coordinateToLogical(coord);
                            var dp0 = targetSeries.dataByIndex(li);
                            if (dp0 && dp0.value !== undefined) subPrice = dp0.value;
                        }
                    } catch(e) {}
                    tradeSubPaneChart.setCrosshairPosition(subPrice, param.time, targetSeries);
                }
            }
            syncingCrosshair = false;
        });
        tradeSubPaneChart.subscribeCrosshairMove(function(param) {
            if (syncingCrosshair || !candlestickChart) return;
            syncingCrosshair = true;
            if (!param.time || !param.point) {
                candlestickChart.clearCrosshairPosition();
            } else if (tradeCandleSeries) {
                var price = 0;
                try {
                    var coord = candlestickChart.timeScale().timeToCoordinate(param.time);
                    if (coord !== null) {
                        var li = candlestickChart.timeScale().coordinateToLogical(coord);
                        var dp = tradeCandleSeries.dataByIndex(li);
                        if (dp && dp.close !== undefined) price = dp.close;
                    }
                } catch(e) {}
                candlestickChart.setCrosshairPosition(price, param.time, tradeCandleSeries);
            }
            syncingCrosshair = false;
        });

        // Sub-pane tooltip updates
        tradeSubPaneChart.subscribeCrosshairMove(function(param) {
            tradeTooltipState.subItems = [];
            if (param.seriesData) {
                param.seriesData.forEach(function(val, series) {
                    if (series._tooltipName && val && val.value !== undefined) {
                        tradeTooltipState.subItems.push({
                            name: series._tooltipName, value: val.value,
                            color: series._tooltipColor || '#aaa'
                        });
                    }
                });
            }
            updateTradeDataTooltip();
        });

        tradeSubPaneChart.timeScale().fitContent();
    }

    function tradeAddHLine(chart, seriesMap, key, value, color, bars) {
        if (seriesMap[key]) return;
        if (!bars || bars.length === 0) return;
        var ld = [
            { time: bars[0].time, value: value },
            { time: bars[bars.length - 1].time, value: value }
        ];
        var s = chart.addLineSeries({
            color: color, lineWidth: 1, lineStyle: 2,
            lastValueVisible: false, priceLineVisible: false,
        });
        s.setData(ld);
        seriesMap[key] = s;
    }

    function updateTradeDataTooltip() {
        var el = document.getElementById('tradeDataTooltip');
        if (!tradeTooltipState.time) {
            el.classList.remove('visible');
            return;
        }

        var html = '';
        var t = tradeTooltipState.time;
        var date = new Date(t * 1000);
        var timeStr = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
        if (currentInterval < 86400) {
            timeStr += '  ' + date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
        }
        html += '<div class="tt-time">' + timeStr + '</div>';

        if (tradeTooltipState.ohlc) {
            var o = tradeTooltipState.ohlc;
            var cls = o.close >= o.open ? 'tt-up' : 'tt-down';
            html += '<div class="tt-row"><span class="tt-label">O</span><span class="tt-val ' + cls + '">' + fmtPrice(o.open) + '</span></div>';
            html += '<div class="tt-row"><span class="tt-label">H</span><span class="tt-val ' + cls + '">' + fmtPrice(o.high) + '</span></div>';
            html += '<div class="tt-row"><span class="tt-label">L</span><span class="tt-val ' + cls + '">' + fmtPrice(o.low) + '</span></div>';
            html += '<div class="tt-row"><span class="tt-label">C</span><span class="tt-val ' + cls + '">' + fmtPrice(o.close) + '</span></div>';
        }

        if (tradeTooltipState.volume != null) {
            html += '<div class="tt-row"><span class="tt-label">Vol</span><span class="tt-val">' + fmtVol(tradeTooltipState.volume) + '</span></div>';
        }

        if (tradeTooltipState.mainItems.length > 0) {
            html += '<div class="tt-divider"></div>';
            tradeTooltipState.mainItems.forEach(function(item) {
                html += '<div class="tt-row"><span class="tt-label">' + escHtml(item.name) + '</span>' +
                    '<span class="tt-val" style="color:' + item.color + ';">' + fmtPrice(item.value) + '</span></div>';
            });
        }

        if (tradeTooltipState.subItems.length > 0) {
            html += '<div class="tt-divider"></div>';
            tradeTooltipState.subItems.forEach(function(item) {
                html += '<div class="tt-row"><span class="tt-label">' + escHtml(item.name) + '</span>' +
                    '<span class="tt-val" style="color:' + item.color + ';">' + fmtNum(item.value) + '</span></div>';
            });
        }

        el.innerHTML = html;
        el.classList.add('visible');
    }

    function findClosestBarTime(bars, dateStr) {
        var targetDate = new Date(dateStr);
        if (isNaN(targetDate.getTime())) return null;
        var targetTs = Math.floor(targetDate.getTime() / 1000);
        var closest = null;
        var closestDiff = Infinity;
        for (var i = 0; i < bars.length; i++) {
            var diff = Math.abs(bars[i].time - targetTs);
            if (diff < closestDiff) {
                closestDiff = diff;
                closest = bars[i].time;
            }
        }
        return closest;
    }

    } // end backtest-only candlestick chart block

    /* ═══════════════════════════════════════════════════════
       VERSION PREVIEW -- Modal Fetch
       ═══════════════════════════════════════════════════════ */
    document.querySelectorAll('.version-preview-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var versionName = this.getAttribute('data-version-name');
            var modal = new bootstrap.Modal(document.getElementById('versionPreviewModal'));
            var loadingEl = document.getElementById('versionPreviewLoading');
            var codeEl = document.getElementById('versionPreviewCode');
            var errorEl = document.getElementById('versionPreviewError');
            var errorMsgEl = document.getElementById('versionPreviewErrorMsg');

            // Reset state
            loadingEl.style.display = 'block';
            codeEl.style.display = 'none';
            errorEl.classList.add('d-none');
            document.getElementById('versionPreviewLabel').innerHTML =
                '<i class="bi bi-eye me-1"></i>Version: ' + versionName;

            modal.show();

            fetch('/api/afl/versions/' + encodeURIComponent(versionName))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    loadingEl.style.display = 'none';
                    if (data.error) {
                        errorMsgEl.textContent = data.error;
                        errorEl.classList.remove('d-none');
                        return;
                    }
                    codeEl.textContent = data.content || data.afl_content || '(empty)';
                    codeEl.style.display = 'block';
                })
                .catch(function(err) {
                    loadingEl.style.display = 'none';
                    errorMsgEl.textContent = 'Failed to load version: ' + err.message;
                    errorEl.classList.remove('d-none');
                });
        });
    });

});
