"""Server-rendered admin dashboard."""

from __future__ import annotations


def dashboard_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AgentFeishu Admin</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2328;
      --muted: #667085;
      --line: #d9dee7;
      --ok: #137333;
      --warn: #b25e09;
      --bad: #b42318;
      --accent: #1456d9;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 16px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { font-size: 20px; margin: 0; }
    main {
      padding: 20px 24px 32px;
      display: grid;
      grid-template-columns: minmax(320px, 1fr) minmax(320px, 1fr);
      gap: 16px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
      overflow-x: auto;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 15px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      table-layout: fixed;
    }
    th, td {
      text-align: left;
      padding: 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th { color: var(--muted); font-weight: 600; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .muted { color: var(--muted); }
    .status { font-weight: 700; }
    .ready, .available, .running, .succeeded { color: var(--ok); }
    .degraded, .optional, .waiting_for_auth { color: var(--warn); }
    .missing, .failed, .error { color: var(--bad); }
    .wide { grid-column: 1 / -1; }
    button {
      min-height: 30px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #ffffff;
      color: var(--accent);
      font-weight: 600;
      cursor: pointer;
    }
    button:disabled {
      color: var(--muted);
      cursor: not-allowed;
    }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; padding: 12px; }
      header { padding: 14px 12px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>AgentFeishu Admin</h1>
    <span class="muted" id="refreshed">loading</span>
  </header>
  <main>
    <section>
      <h2>Capabilities</h2>
      <table id="capabilities"></table>
    </section>
    <section>
      <h2>Configuration</h2>
      <table id="configuration"></table>
    </section>
    <section class="wide">
      <h2>Auth State</h2>
      <table id="auth"></table>
    </section>
    <section class="wide">
      <h2>Dependencies</h2>
      <table id="dependencies"></table>
    </section>
    <section>
      <h2>Recent Tasks</h2>
      <table id="tasks"></table>
    </section>
    <section>
      <h2>Recent Errors</h2>
      <table id="errors"></table>
    </section>
  </main>
  <script>
    const cell = (v) => `<td>${escapeHtml(String(v ?? ""))}</td>`;
    const status = (v) => `<span class="status ${escapeHtml(String(v))}">${escapeHtml(String(v))}</span>`;
    function escapeHtml(value) {
      return value.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function renderTable(id, headers, rows) {
      const table = document.getElementById(id);
      table.innerHTML = `<thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead>` +
        `<tbody>${rows.join("") || `<tr><td colspan="${headers.length}" class="muted">No data</td></tr>`}</tbody>`;
    }
    fetch('/admin/api/overview').then(r => r.json()).then(data => {
      document.getElementById('refreshed').textContent = new Date().toLocaleString();
      renderTable('capabilities', ['Name', 'Status', 'Auth', 'Warnings'],
        data.capabilities.map(c => `<tr>${cell(c.name)}<td>${status(c.status)}</td>${cell(c.auth_state)}${cell((c.warnings || []).join('; '))}</tr>`));
      renderTable('configuration', ['Key', 'Value'],
        Object.entries(data.configuration).map(([k, v]) => `<tr>${cell(k)}${cell(Array.isArray(v) ? v.join(', ') : v)}</tr>`));
      renderTable('auth', ['Capability', 'Site', 'State', 'Action'],
        data.auth.map((a, i) => `<tr>${cell(a.capability)}${cell(a.site || '')}<td>${status(a.state)}</td><td>${authButton(a, i)}</td></tr>`));
      renderTable('dependencies', ['Capability', 'Name', 'Status', 'Message'],
        data.dependencies.map(d => `<tr>${cell(d.capability)}${cell(d.name)}<td>${status(d.status)}</td>${cell(d.message || d.install_hint)}</tr>`));
      renderTable('tasks', ['Task', 'Capability', 'Status', 'Updated'],
        data.recent_tasks.map(t => `<tr>${cell(t.id)}${cell(t.capability)}<td>${status(t.status)}</td>${cell(t.updated_at)}</tr>`));
      renderTable('errors', ['Task', 'Capability', 'Error'],
        data.recent_errors.map(t => `<tr>${cell(t.id)}${cell(t.capability)}${cell(t.error || (t.result && t.result.summary) || '')}</tr>`));
    }).catch(err => {
      document.getElementById('refreshed').textContent = 'failed to load: ' + err.message;
    });
    function authButton(row, index) {
      if (!row.login_url) return '';
      return `<button type="button" data-login-url="${escapeHtml(row.login_url)}" data-index="${index}">Open Login</button>`;
    }
    document.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-login-url]');
      if (!button) return;
      button.disabled = true;
      button.textContent = 'Opening';
      try {
        const response = await fetch('/admin/api/auth/browser/open', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({url: button.dataset.loginUrl})
        });
        button.textContent = response.ok ? 'Opened' : 'Unavailable';
      } catch (err) {
        button.textContent = 'Failed';
      }
    });
  </script>
</body>
</html>"""
