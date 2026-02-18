"""Patch dashboard to add Cockpit tab + Telegram daily digest."""
import re

html_path = '/opt/projects/context-engine/static/index.html'

with open(html_path, 'r') as f:
    html = f.read()

# 1. Add cockpit CSS after existing styles
cockpit_css = """
        /* Cockpit tab styles */
        .cockpit-section { margin-bottom: 1.5rem; }
        .cockpit-section h2 { font-size: 1rem; font-weight: 700; color: #e2e8f0; margin-bottom: 0.75rem; padding-bottom: 0.5rem; border-bottom: 1px solid #1e293b; }
        .cockpit-section h3 { font-size: 0.95rem; font-weight: 600; color: #f8fafc; margin-bottom: 0.5rem; }
        .cockpit-project { background: #111827; border-radius: 0.5rem; padding: 1rem; margin-bottom: 0.75rem; border-left: 3px solid #374151; }
        .cockpit-project.health-green { border-left-color: #22c55e; }
        .cockpit-project.health-yellow { border-left-color: #eab308; }
        .cockpit-project.health-red { border-left-color: #ef4444; }
        .cockpit-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: #6b7280; margin-right: 0.5rem; }
        .cockpit-value { font-size: 0.85rem; color: #d1d5db; }
        .cockpit-field { margin-bottom: 0.35rem; display: flex; flex-wrap: wrap; gap: 0.25rem; }
        .cockpit-alert-row { display: grid; grid-template-columns: 1fr auto auto; gap: 0.5rem; padding: 0.4rem 0; border-bottom: 1px solid #1e293b; font-size: 0.85rem; }
        .cockpit-checklist { list-style: none; padding: 0; }
        .cockpit-checklist li { padding: 0.35rem 0; font-size: 0.85rem; color: #d1d5db; }
        .cockpit-checklist li::before { content: '\25A1 '; color: #6b7280; }
        .cockpit-session-row { display: grid; grid-template-columns: 80px 1fr 1fr; gap: 0.5rem; padding: 0.35rem 0; border-bottom: 1px solid #1e293b; font-size: 0.8rem; }
        .cockpit-badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 9999px; font-size: 0.7rem; font-weight: 600; }
        .cockpit-badge-green { background: rgba(34,197,94,0.15); color: #4ade80; }
        .cockpit-badge-yellow { background: rgba(234,179,8,0.15); color: #facc15; }
        .cockpit-badge-red { background: rgba(239,68,68,0.15); color: #f87171; }
        .cockpit-parked { color: #6b7280; font-size: 0.8rem; }
"""

html = html.replace(
    '        pre { white-space: pre-wrap; word-break: break-word; }',
    '        pre { white-space: pre-wrap; word-break: break-word; }' + cockpit_css
)

# 2. Add cockpit tab as FIRST tab
html = html.replace(
    "<button onclick=\"showTab('overview')\" class=\"tab px-4 py-2 text-sm hover:text-white tab-active\" id=\"tab-overview\">Overview</button>",
    '<button onclick="showTab(\'cockpit\')" class="tab px-4 py-2 text-sm hover:text-white tab-active" id="tab-cockpit">\xf0\x9f\x8e\xaf Cockpit</button>\n            <button onclick="showTab(\'overview\')" class="tab px-4 py-2 text-sm hover:text-white" id="tab-overview">Overview</button>'
)

# 3. Add load_cockpit function before load_overview
cockpit_js = """
// ─── Cockpit (Project Status Dashboard) ─────────────────
async function load_cockpit() {
    const data = await api('/api/cockpit');
    if (!data.cockpit) {
        document.getElementById('content').innerHTML = `
            <div class="text-center py-12 text-gray-500">
                <p class="text-lg mb-2">No cockpit data yet</p>
                <p class="text-sm">The cockpit updates automatically after each ContextEngine session.</p>
            </div>`;
        return;
    }

    const md = data.cockpit;
    const lastMod = data.last_modified ? new Date(data.last_modified).toLocaleString() : 'unknown';

    // Parse the markdown into structured sections
    const rendered = renderCockpit(md);

    document.getElementById('content').innerHTML = `
        <div class="flex justify-between items-center mb-4">
            <div class="text-sm text-gray-500">Last updated: ${lastMod}</div>
            <button onclick="load_cockpit()" class="px-3 py-1 rounded bg-gray-800 hover:bg-gray-700 text-sm">\u21bb Refresh</button>
        </div>
        ${rendered}
    `;
}

function renderCockpit(md) {
    // Split into sections by --- dividers
    const sections = md.split(/^---$/m).map(s => s.trim()).filter(Boolean);
    let html = '';

    for (const section of sections) {
        html += renderCockpitSection(section);
    }
    return html;
}

function renderCockpitSection(section) {
    const lines = section.split('\\n');
    let html = '<div class="cockpit-section">';
    let inTable = false;
    let tableRows = [];
    let inChecklist = false;
    let checklistItems = [];

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim();
        if (!line) {
            if (inTable && tableRows.length) {
                html += renderCockpitTable(tableRows);
                tableRows = []; inTable = false;
            }
            if (inChecklist && checklistItems.length) {
                html += renderCockpitChecklist(checklistItems);
                checklistItems = []; inChecklist = false;
            }
            continue;
        }

        // Skip the HOW THIS WORKS block
        if (line.startsWith('## HOW THIS WORKS')) {
            while (i < lines.length - 1 && !lines[i + 1].trim().startsWith('## ')) i++;
            continue;
        }

        // Skip blockquotes
        if (line.startsWith('>')) continue;

        // Main title
        if (line.startsWith('# ')) {
            continue; // Skip the title, dashboard header covers it
        }

        // Section headers (## )
        if (line.startsWith('## ')) {
            if (inTable && tableRows.length) { html += renderCockpitTable(tableRows); tableRows = []; inTable = false; }
            html += `<h2>${escHtml(line.replace(/^## /, ''))}</h2>`;
            continue;
        }

        // Project headers (### )
        if (line.startsWith('### ')) {
            const title = line.replace(/^### /, '');
            let healthClass = 'health-green';
            if (title.includes('\u26a0') || title.includes('\ud83d\udfe1')) healthClass = 'health-yellow';
            if (title.includes('\ud83d\udd34')) healthClass = 'health-red';
            // Collect project fields until next ### or ## or ---
            let fields = [];
            let j = i + 1;
            while (j < lines.length) {
                const fl = lines[j].trim();
                if (!fl || fl.startsWith('### ') || fl.startsWith('## ') || fl === '---') break;
                fields.push(fl);
                j++;
            }
            html += renderProjectCard(title, fields, healthClass);
            i = j - 1;
            continue;
        }

        // Table rows
        if (line.startsWith('|')) {
            if (line.match(/^\|[\s-|]+$/)) continue; // Skip separator row
            inTable = true;
            tableRows.push(line);
            continue;
        }

        // Checklist items
        if (line.startsWith('- [ ]') || line.startsWith('- [x]')) {
            inChecklist = true;
            checklistItems.push(line);
            continue;
        }

        // List items (parked projects)
        if (line.startsWith('- ')) {
            html += `<div class="cockpit-parked">${escHtml(line.replace(/^- /, '\u2022 '))}</div>`;
            continue;
        }

        // Bold update line
        if (line.startsWith('**Last Updated:')) {
            continue; // Skip, we show this in the header
        }
        if (line.startsWith('**Updated By:')) {
            continue;
        }

        // Regular text
        html += `<p class="text-sm text-gray-400 mb-1">${escHtml(line)}</p>`;
    }

    if (inTable && tableRows.length) html += renderCockpitTable(tableRows);
    if (inChecklist && checklistItems.length) html += renderCockpitChecklist(checklistItems);

    html += '</div>';
    return html;
}

function renderProjectCard(title, fields, healthClass) {
    let fieldsHtml = '';
    for (const f of fields) {
        const match = f.match(/^\*\*(.+?):\*\*\\s*(.+)$/);
        if (match) {
            let label = match[1];
            let value = match[2];
            // Color-code health badges
            if (label === 'Health') {
                const badge = value.includes('Active') ? 'cockpit-badge-green' :
                              value.includes('Concept') || value.includes('Needs') || value.includes('Stale') ? 'cockpit-badge-yellow' :
                              'cockpit-badge-red';
                value = `<span class="cockpit-badge ${badge}">${escHtml(value)}</span>`;
                fieldsHtml += `<div class="cockpit-field"><span class="cockpit-label">${escHtml(label)}:</span> ${value}</div>`;
            } else {
                fieldsHtml += `<div class="cockpit-field"><span class="cockpit-label">${escHtml(label)}:</span> <span class="cockpit-value">${escHtml(value)}</span></div>`;
            }
        } else {
            fieldsHtml += `<div class="cockpit-value" style="font-size:0.8rem;">${escHtml(f)}</div>`;
        }
    }
    return `<div class="cockpit-project ${healthClass}"><h3>${escHtml(title)}</h3>${fieldsHtml}</div>`;
}

function renderCockpitTable(rows) {
    if (!rows.length) return '';
    const headers = rows[0].split('|').filter(c => c.trim()).map(c => c.trim());
    const dataRows = rows.slice(1);
    let html = '<div class="bg-gray-900 rounded-lg overflow-hidden mb-3"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700">';
    for (const h of headers) {
        html += `<th class="py-2 px-3 text-left text-xs text-gray-500 uppercase">${escHtml(h)}</th>`;
    }
    html += '</tr></thead><tbody>';
    for (const row of dataRows) {
        const cells = row.split('|').filter(c => c.trim()).map(c => c.trim());
        html += '<tr class="border-b border-gray-800 hover:bg-gray-900/50">';
        for (const c of cells) {
            let cls = 'text-gray-300';
            if (c.includes('\ud83d\udd34') || c.includes('Critical') || c.includes('High')) cls = 'text-red-400';
            else if (c.includes('\ud83d\udfe1') || c.includes('Medium')) cls = 'text-yellow-400';
            else if (c.includes('\ud83d\udfe2') || c.includes('Active')) cls = 'text-green-400';
            html += `<td class="py-1.5 px-3 text-xs ${cls}">${escHtml(c)}</td>`;
        }
        html += '</tr>';
    }
    html += '</tbody></table></div>';
    return html;
}

function renderCockpitChecklist(items) {
    let html = '<ul class="cockpit-checklist">';
    for (const item of items) {
        const checked = item.startsWith('- [x]');
        const text = item.replace(/^- \[[ x]\] /, '');
        const icon = checked ? '\u2611' : '\u2610';
        html += `<li style="opacity: ${checked ? '0.5' : '1'}">${icon} ${escHtml(text)}</li>`;
    }
    html += '</ul>';
    return html;
}

"""

html = html.replace(
    '// ─── Overview ─',
    cockpit_js + '\n// ─── Overview ─'
)

# 4. Change default tab init from overview to cockpit
html = html.replace(
    "let currentTab = 'overview';",
    "let currentTab = 'cockpit';"
)
html = html.replace(
    'load_overview();',
    'load_cockpit();'
)

with open(html_path, 'w') as f:
    f.write(html)

print(f'Dashboard patched: {len(html)} bytes')
print('Changes: cockpit CSS, cockpit tab (first), load_cockpit() function, default to cockpit')
