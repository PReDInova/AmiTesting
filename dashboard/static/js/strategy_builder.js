/**
 * strategy_builder.js
 * Extracted from strategy_builder.html
 * Handles CodeMirror initialization, AFL generation via Claude API
 */

var editor;

document.addEventListener('DOMContentLoaded', function() {
    editor = CodeMirror.fromTextArea(document.getElementById('afl-code'), {
        mode: 'text/x-csrc',
        theme: 'dracula',
        lineNumbers: true,
        matchBrackets: true,
        styleActiveLine: true,
        indentUnit: 4,
        tabSize: 4,
        lineWrapping: true
    });
    editor.setSize(null, 400);

    document.getElementById('create-form').addEventListener('submit', function() {
        editor.save();
    });
});

function generateAFL() {
    var btn = document.getElementById('btnGenerate');
    var status = document.getElementById('generateStatus');
    var warnings = document.getElementById('aflWarnings');

    var name = document.getElementById('strategyName').value.trim();
    var desc = document.getElementById('strategyDesc').value.trim();
    var symbol = document.getElementById('strategySymbol').value.trim();

    if (!name && !desc) {
        alert('Please enter a strategy name or description first.');
        return;
    }

    // Confirm if editor already has non-placeholder content
    var current = editor.getValue().trim();
    if (current && !current.startsWith('// Paste your') && !current.startsWith('// ===')) {
        if (!confirm('This will replace the current AFL code. Continue?')) {
            return;
        }
    }

    // Show loading state
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Generating...';
    status.style.display = 'inline';
    status.textContent = 'Calling Claude Code (this may take 10-30s)...';
    warnings.style.display = 'none';

    fetch('/api/strategy-builder/generate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            strategy_name: name,
            description: desc,
            symbol: symbol
        })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) {
            status.textContent = '';
            status.style.display = 'none';
            alert('Generation failed: ' + data.error);
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-stars me-1"></i>Generate with Claude';
            return;
        }

        // Populate editor with generated AFL
        editor.setValue(data.afl_code);
        editor.refresh();

        // Show cost if available
        var costMsg = data.cost_usd ? ' ($' + data.cost_usd.toFixed(4) + ')' : '';
        status.textContent = 'Generated successfully' + costMsg;

        // Show warnings if any
        if (data.warnings && data.warnings.length > 0) {
            var html = '<div class="alert alert-warning py-2 px-3 mb-0">';
            html += '<strong><i class="bi bi-exclamation-triangle me-1"></i>AFL Warnings:</strong>';
            html += '<ul class="mb-0 mt-1">';
            data.warnings.forEach(function(w) {
                html += '<li class="small">' + w + '</li>';
            });
            html += '</ul></div>';
            warnings.innerHTML = html;
            warnings.style.display = 'block';
        }

        // Switch button to "Regenerate"
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-arrow-repeat me-1"></i>Regenerate';
    })
    .catch(function(err) {
        status.textContent = '';
        status.style.display = 'none';
        alert('Network error: ' + err.message);
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-stars me-1"></i>Generate with Claude';
    });
}
