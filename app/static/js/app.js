(function () {
  "use strict";
  const BASE = window.BASE_PATH || "";

  let selectedDbId = null; // set when entering the detail screen
  let selectedDbMeta = null;

  function withDbId(path) {
    if (!selectedDbId) return path;
    const sep = path.includes("?") ? "&" : "?";
    return `${path}${sep}db_id=${encodeURIComponent(selectedDbId)}`;
  }
  const api = (path) => fetch(BASE + withDbId(path), { credentials: "same-origin" });
  const apiPost = (path, body) =>
    fetch(BASE + withDbId(path), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  const apiDelete = (path) => fetch(BASE + path, { method: "DELETE", credentials: "same-origin" });
  // For PUT/DELETE calls that need db_id and/or a JSON body (apiDelete above predates db_id support).
  const apiMethod = (method, path, body) =>
    fetch(BASE + withDbId(path), {
      method, credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  // Screen-1 (Databases list) calls never need a db_id.
  const apiGlobal = (path) => fetch(BASE + path, { credentials: "same-origin" });

  // ---------------------------------------------------------------- utils
  function fmtBytes(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  }
  function fmtDuration(seconds) {
    if (seconds == null) return "—";
    seconds = Math.max(0, Math.round(seconds));
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60), s = seconds % 60;
    if (m < 60) return `${m}m ${s}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }
  function fmtNum(n) {
    if (n == null) return "—";
    return Number(n).toLocaleString();
  }
  function fmtDate(iso) {
    if (!iso) return "never";
    return new Date(iso).toLocaleString();
  }
  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    // textContent->innerHTML escapes &, <, > but NOT quote characters --
    // fine for plain text nodes, but esc() is also used all over this file
    // to build double-quoted HTML attributes (data-*, title, ...). A value
    // containing a literal " would otherwise terminate the attribute early
    // and corrupt everything after it in that tag (this is exactly what
    // broke the Grants panel's Revoke button: a JSON blob embedded in
    // data-revoke-grant="..." got truncated at its first "). Escape quotes
    // too so esc() is safe in both contexts.
    return d.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function timeLabel(iso) {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function severityClass(sev) {
    return { high: "sev-high", medium: "sev-medium", low: "sev-low" }[sev] || "sev-low";
  }
  function statusClass(status) {
    return "ai-status-" + (status || "unknown");
  }
  function renderAssessmentCard(containerId, data) {
    const card = document.getElementById(containerId);
    card.classList.remove("hidden");
    if (data.raw) {
      card.innerHTML = `<div class="ai-headline">${esc(data.headline || "")}</div><div class="ai-text">${esc(data.raw)}</div>`;
      return;
    }
    const issues = (data.issues || [])
      .map((i) => `<div class="ai-issue ${severityClass(i.severity)}"><div class="ai-issue-title">${esc(i.title)}</div><div>${esc(i.detail)}</div></div>`)
      .join("") || `<div class="hint-text">No issues found.</div>`;
    const recs = (data.recommendations || []).map((r) => `<li><code>${esc(r)}</code></li>`).join("");
    card.innerHTML = `
      <div class="ai-summary-head">
        <span class="ai-score">${data.score != null ? data.score : "?"}</span>
        <span class="ai-status ${statusClass(data.status)}">${esc(data.status || "unknown")}</span>
      </div>
      <div class="ai-headline">${esc(data.headline || "")}</div>
      ${issues}
      ${recs ? `<ul class="ai-recs">${recs}</ul>` : ""}
    `;
  }
  async function runAssessment(btn, cardId, busyText, idleText, endpoint) {
    btn.disabled = true;
    btn.textContent = busyText;
    const card = document.getElementById(cardId);
    card.classList.remove("hidden");
    card.innerHTML = `<div class="hint-text">Gathering live data and asking Claude...</div>`;
    try {
      const resp = await apiPost(endpoint);
      if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
      renderAssessmentCard(cardId, await resp.json());
    } catch (err) {
      card.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = idleText;
    }
  }

  function statusBadge(status, latencyMs) {
    if (status === "up") return `<span class="badge badge-up">● up${latencyMs != null ? ` · ${latencyMs}ms` : ""}</span>`;
    return `<span class="badge badge-down">● down</span>`;
  }

  // ============================================================ SCREENS
  const screens = { databases: document.getElementById("screen-databases"), detail: document.getElementById("screen-detail") };
  const breadcrumb = document.getElementById("breadcrumb");
  let listPollInterval = null;
  let detailOverviewInterval = null;

  function showScreen(name) {
    Object.values(screens).forEach((s) => s.classList.remove("active"));
    screens[name].classList.add("active");
    document.getElementById("sidebar-detail-nav")?.classList.toggle("hidden", name !== "detail");
    if (listPollInterval) { clearInterval(listPollInterval); listPollInterval = null; }
    if (detailOverviewInterval) { clearInterval(detailOverviewInterval); detailOverviewInterval = null; }
    stopConnectionsPoll();
    stopPerfPoll();

    if (name === "databases") {
      breadcrumb.classList.add("hidden");
      loadDatabaseList();
      listPollInterval = setInterval(loadDatabaseList, 15000);
    } else {
      detailOverviewInterval = setInterval(loadOverview, 15000);
    }
  }

  document.getElementById("home-link")?.addEventListener("click", (e) => {
    e.preventDefault();
    showScreen("databases");
  });
  document.getElementById("btn-back-to-list")?.addEventListener("click", () => showScreen("databases"));

  // ------------------------------------------------------------ account menu
  const acctTrigger = document.getElementById("account-menu-trigger");
  const acctDropdown = document.getElementById("account-menu-dropdown");
  if (acctTrigger && acctDropdown) {
    acctTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = !acctDropdown.classList.contains("hidden");
      acctDropdown.classList.toggle("hidden", isOpen);
      acctTrigger.setAttribute("aria-expanded", String(!isOpen));
    });
    document.addEventListener("click", (e) => {
      if (!acctDropdown.classList.contains("hidden") && !document.getElementById("account-menu").contains(e.target)) {
        acctDropdown.classList.add("hidden");
        acctTrigger.setAttribute("aria-expanded", "false");
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        acctDropdown.classList.add("hidden");
        acctTrigger.setAttribute("aria-expanded", "false");
      }
    });
  }

  // ------------------------------------------------------------- theme
  function currentTheme() {
    try { return localStorage.getItem("pgmon-theme") === "dark" ? "dark" : "light"; } catch (e) { return "light"; }
  }
  function applyTheme(theme) {
    if (theme === "dark") document.documentElement.setAttribute("data-theme", "dark");
    else document.documentElement.removeAttribute("data-theme");
    try { localStorage.setItem("pgmon-theme", theme); } catch (e) {}
    const label = document.getElementById("theme-toggle-label");
    if (label) label.textContent = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
  }
  document.getElementById("btn-toggle-theme")?.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });
  applyTheme(currentTheme());

  function openDatabase(dbEntry) {
    selectedDbId = dbEntry.id;
    selectedDbMeta = dbEntry;
    backupScopeSchemasLoaded = false;
    backupScopeTables = [];
    breadcrumb.classList.remove("hidden");
    breadcrumb.textContent = `· ${dbEntry.name}`;
    document.getElementById("detail-db-name").textContent = dbEntry.name;
    document.getElementById("detail-db-sub").textContent = `${dbEntry.host}:${dbEntry.port} / ${dbEntry.dbname}`;
    document.getElementById("detail-db-status").outerHTML =
      `<span id="detail-db-status" class="badge ${dbEntry.status === "up" ? "badge-up" : "badge-down"}">${dbEntry.status === "up" ? "● up" : "● down"}</span>`;
    resetChat();
    document.getElementById("ai-summary-card")?.classList.add("hidden");
    document.getElementById("ai-security-card")?.classList.add("hidden");
    document.getElementById("ai-backup-card")?.classList.add("hidden");
    document.getElementById("ai-administration-card")?.classList.add("hidden");
    document.getElementById("ai-performance-card")?.classList.add("hidden");
    showScreen("detail");
    // land on the first sub-tab every time a database is opened
    detailTabButtons.forEach((b) => b.classList.remove("active"));
    detailTabPanels.forEach((p) => p.classList.remove("active"));
    document.querySelector('.tab-strip-item[data-dtab="details"]').classList.add("active");
    document.getElementById("dtab-details").classList.add("active");
    loadOverview();
    loadHistory();
    loadSettings();
  }

  // Background alert summary from app/poller.py's cache -- computed on the
  // regular ~30s poll cycle for every registered database, not just the one
  // currently being viewed, so this can show something without paying for
  // a live check per row.
  function dbAlertBadge(a) {
    if (!a || !a.checked_at) return `<span class="cell-empty" title="No background check has run for this database yet">pending&hellip;</span>`;
    if (a.error_count > 0) return `<span class="badge badge-down" title="${a.error_count} error(s), ${a.warning_count} warning(s), ${a.info_count} info">${a.error_count} error${a.error_count === 1 ? "" : "s"}</span>`;
    if (a.warning_count > 0) return `<span class="badge badge-muted" title="${a.warning_count} warning(s), ${a.info_count} info">${a.warning_count} warning${a.warning_count === 1 ? "" : "s"}</span>`;
    if (a.info_count > 0) return `<span class="badge badge-muted" title="${a.info_count} info item(s)">${a.info_count} info</span>`;
    return `<span class="badge badge-up">clean</span>`;
  }

  // --------------------------------------------------------- db list table
  async function loadDatabaseList() {
    try {
      const dbs = await apiGlobal("/api/databases").then((r) => r.json());
      dbs.sort((a, b) => (a.status === "down" ? 0 : 1) - (b.status === "down" ? 0 : 1)); // stable: down first, order otherwise preserved
      const tbody = document.getElementById("db-grid");
      tbody.innerHTML = dbs.map((d) => `
        <tr data-id="${esc(d.id)}">
          <td>${esc(d.name)}</td>
          <td class="mono">${esc(d.host)}</td>
          <td class="mono">${d.port}</td>
          <td class="mono">${esc(d.dbname)}</td>
          <td class="mono">${d.version ? esc(d.version) : "—"}</td>
          <td>${statusBadge(d.status, d.latency_ms)}${d.status === "down" && d.error ? `<div class="db-row-error" title="${esc(d.error)}">${esc(d.error)}</div>` : ""}</td>
          <td>${dbAlertBadge(d.alerts)}</td>
          <td>${fmtDuration(d.uptime_seconds)}</td>
          <td>${fmtNum(d.connection_count)}</td>
          <td>${d.cache_hit_ratio_pct != null ? d.cache_hit_ratio_pct + "%" : "—"}</td>
          <td>${d.tps != null ? d.tps : "—"}</td>
          <td>${d.is_default ? '<span class="badge badge-muted">default</span>' : `<button class="btn-icon-danger" data-del="${esc(d.id)}" data-name="${esc(d.name)}">Remove</button>`}</td>
        </tr>`).join("") || `<tr><td colspan="12" class="empty-note">No databases registered yet. Click "+ Add database" above to get started.</td></tr>`;

      tbody.querySelectorAll("tr[data-id]").forEach((row) => {
        row.addEventListener("click", (e) => {
          if (e.target.closest("[data-del]")) return;
          const entry = dbs.find((d) => d.id === row.dataset.id);
          if (entry) openDatabase(entry);
        });
      });
      tbody.querySelectorAll("[data-del]").forEach((btn) => {
        btn.addEventListener("click", (e) => { e.stopPropagation(); deleteDatabase(btn.dataset.del, btn.dataset.name); });
      });
    } catch (e) { console.error("databases load failed", e); }
  }

  async function deleteDatabase(id, name) {
    if (!confirm(`Remove "${name}" from monitoring? Its stored history is left in place on that database.`)) return;
    try {
      const resp = await apiDelete(`/api/databases/${encodeURIComponent(id)}`);
      if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
      await loadDatabaseList();
    } catch (e) {
      alert("Failed to remove database: " + e.message);
    }
  }

  function addDbFormPayload() {
    return {
      name: document.getElementById("db-name").value.trim(),
      host: document.getElementById("db-host").value.trim(),
      port: Number(document.getElementById("db-port").value),
      dbname: document.getElementById("db-dbname").value.trim(),
      username: document.getElementById("db-username").value.trim(),
      password: document.getElementById("db-password").value,
      enable_replication_hba: document.getElementById("db-enable-replication-hba").checked,
    };
  }
  function setAddDbStatus(kind, text) {
    const el = document.getElementById("add-db-status");
    el.className = "add-db-status" + (kind ? ` status-${kind}` : "");
    el.textContent = text;
  }
  function apiGlobalPost(path, body) {
    return fetch(BASE + path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  document.getElementById("btn-test-db")?.addEventListener("click", async () => {
    setAddDbStatus(null, "Testing connection...");
    try {
      const resp = await apiGlobalPost("/api/databases/test", addDbFormPayload());
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      setAddDbStatus("ok", "✓ Connected successfully.");
    } catch (e) {
      setAddDbStatus("err", "✗ " + e.message);
    }
  });
  document.getElementById("add-db-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    setAddDbStatus(null, "Testing connection and adding...");
    try {
      const resp = await apiGlobalPost("/api/databases", addDbFormPayload());
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      setAddDbStatus("ok", `✓ Added "${data.name}".` + (data.hba_note ? ` ${data.hba_note}` : ""));
      e.target.reset();
      document.getElementById("db-port").value = 5432;
      await loadDatabaseList();
      // Longer flash when there's an extra note to actually read.
      setTimeout(closeAddDbModal, data.hba_note ? 2500 : 700);
    } catch (err) {
      setAddDbStatus("err", "✗ " + err.message);
    }
  });

  // ---------------------------------------------------------- add-db modal
  const addDbModal = document.getElementById("add-db-modal");
  function openAddDbModal() {
    document.getElementById("add-db-form").reset();
    document.getElementById("db-port").value = 5432;
    setAddDbStatus(null, "");
    addDbModal.classList.remove("hidden");
    document.getElementById("db-name").focus();
  }
  function closeAddDbModal() {
    addDbModal.classList.add("hidden");
  }
  document.getElementById("btn-open-add-db")?.addEventListener("click", openAddDbModal);
  document.getElementById("btn-close-add-db")?.addEventListener("click", closeAddDbModal);
  addDbModal?.addEventListener("click", (e) => { if (e.target === addDbModal) closeAddDbModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !addDbModal.classList.contains("hidden")) closeAddDbModal();
  });

  // ------------------------------------------------------- detail tabs
  const detailTabButtons = document.querySelectorAll(".tab-strip-item");
  const detailTabPanels = document.querySelectorAll(".dtab-panel");
  const dtabLoaders = {};
  detailTabButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      detailTabButtons.forEach((b) => b.classList.remove("active"));
      detailTabPanels.forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      const tab = btn.dataset.dtab;
      document.getElementById(`dtab-${tab}`).classList.add("active");
      if (tab !== "connections") stopConnectionsPoll();
      if (tab !== "performance") stopPerfPoll();
      if (tab !== "migration") stopMigrationPoll();
      if (tab !== "alerts") stopAlertsPoll();
      if (dtabLoaders[tab]) dtabLoaders[tab]();
    });
  });

  // ----------------------------------------------------------- overview
  let connChart, cacheChart;

  function renderOverviewStats(o) {
    const set = (key, val, cls) => {
      const el = document.querySelector(`[data-stat="${key}"]`);
      if (!el) return;
      el.textContent = val;
      el.className = "stat-value" + (cls ? " " + cls : "");
    };
    set("db_size", fmtBytes(o.db_size_bytes));
    const conns = o.connections || {};
    set("active_conn", fmtNum(conns.active || 0));
    const total = conns.total || 0, max = o.max_connections || 0;
    const pctUsed = max ? total / max : 0;
    set("total_conn", `${fmtNum(total)} / ${fmtNum(max)}`, pctUsed > 0.8 ? "stat-bad" : pctUsed > 0.6 ? "stat-warn" : "");
    const hit = o.cache_hit_ratio_pct;
    set("cache_hit", hit == null ? "—" : `${hit}%`, hit != null && hit < 90 ? "stat-warn" : "");
    set("uptime", fmtDuration(o.uptime_seconds));
    const db = o.db_stat || {};
    set("commits", `${fmtNum(db.xact_commit)} / ${fmtNum(db.xact_rollback)}`);
  }

  async function loadOverview() {
    if (!selectedDbId) return;
    try {
      const o = await api("/api/overview").then((r) => r.json());
      renderOverviewStats(o);
    } catch (e) { console.error("overview load failed", e); }
  }

  function buildChart(ctx, label, color) {
    return new Chart(ctx, {
      type: "line",
      data: { labels: [], datasets: [{ label, data: [], borderColor: color, backgroundColor: color + "22", tension: 0.3, fill: true, pointRadius: 0 }] },
      options: {
        responsive: true,
        scales: {
          x: { ticks: { color: "#93a1c2", maxTicksLimit: 8 }, grid: { color: "#253154" } },
          y: { ticks: { color: "#93a1c2" }, grid: { color: "#253154" }, beginAtZero: true },
        },
        plugins: { legend: { display: false } },
      },
    });
  }

  async function loadHistory() {
    if (!selectedDbId) return;
    try {
      const rows = await api("/api/history?hours=6").then((r) => r.json());
      const labels = rows.map((r) => timeLabel(r.captured_at));
      const connData = rows.map((r) => (r.metrics.overview?.connections?.total) ?? null);
      const cacheData = rows.map((r) => r.metrics.overview?.cache_hit_ratio_pct ?? null);

      if (!connChart) connChart = buildChart(document.getElementById("chart-connections"), "Connections", "#3b82f6");
      if (!cacheChart) cacheChart = buildChart(document.getElementById("chart-cache"), "Cache hit %", "#a78bfa");

      connChart.data.labels = labels;
      connChart.data.datasets[0].data = connData;
      connChart.update();

      cacheChart.data.labels = labels;
      cacheChart.data.datasets[0].data = cacheData;
      cacheChart.update();
    } catch (e) { console.error("history load failed", e); }
  }

  document.getElementById("btn-health-summary")?.addEventListener("click", (e) =>
    runAssessment(e.currentTarget, "ai-summary-card", "✦ Analyzing...", "✦ Generate AI Health Summary", "/api/ai/health-summary"));

  // ------------------------------------------------------- server parameters
  let allSettings = [];

  function fmtSettingVal(setting, unit) {
    if (setting == null || setting === "") return "—";
    return unit ? `${setting} ${unit}` : setting;
  }

  function renderSettingsTable() {
    const filterText = document.getElementById("settings-filter").value.trim().toLowerCase();
    const onlyChanged = document.getElementById("settings-only-changed").checked;
    const rows = allSettings.filter((s) => {
      if (onlyChanged && !s.changed) return false;
      if (filterText && !s.name.toLowerCase().includes(filterText)) return false;
      return true;
    });
    const tbody = document.querySelector("#table-settings tbody");
    tbody.innerHTML = rows.map((s) => `
      <tr class="${s.changed ? "row-changed-setting" : ""}" title="${esc(s.short_desc || "")}">
        <td class="mono">${esc(s.name)}${s.changed ? ' <span class="settings-changed-badge" title="Changed from default via: ' + esc(s.source) + '">●</span>' : ""}</td>
        <td class="mono">${esc(fmtSettingVal(s.setting, s.unit))}</td>
        <td class="mono">${esc(fmtSettingVal(s.boot_val, s.unit))}</td>
        <td>${esc(s.category || "—")}</td>
        <td>${s.editable ? `<button class="btn btn-ghost btn-sm" data-edit-setting="${esc(s.name)}">Edit&hellip;</button>` : ""}</td>
      </tr>`).join("") || `<tr><td colspan="5" class="empty-note">No parameters match this filter.</td></tr>`;
    tbody.querySelectorAll("[data-edit-setting]").forEach((btn) => {
      btn.addEventListener("click", () => openSettingModal(btn.dataset.editSetting));
    });
  }

  async function loadSettings() {
    if (!selectedDbId) return;
    try {
      const data = await api("/api/settings").then((r) => r.json());
      allSettings = data.settings || [];
      document.getElementById("settings-summary").textContent =
        `${fmtNum(data.changed_count)} of ${fmtNum(data.total_count)} changed from default`;
      renderSettingsTable();
    } catch (e) { console.error("settings load failed", e); }
  }
  document.getElementById("settings-filter")?.addEventListener("input", renderSettingsTable);
  document.getElementById("settings-only-changed")?.addEventListener("change", renderSettingsTable);

  dtabLoaders.details = () => { loadOverview(); loadHistory(); loadSettings(); };

  // ---------------------------------------------------------- edit-setting modal
  const settingModal = document.getElementById("setting-modal");
  let settingTarget = null;

  const CONTEXT_IMPACT_NOTES = {
    postmaster: "⚠ Requires a full Postgres server restart to take effect. pgmon writes it to postgresql.auto.conf and reloads config now, but the running server keeps its old value until restarted.",
    backend: "Only affects new connections -- existing sessions (including this app's) keep their old value until they reconnect.",
    "superuser-backend": "Only affects new connections -- existing sessions (including this app's) keep their old value until they reconnect.",
  };

  function settingValueInputHtml(s) {
    if (s.vartype === "bool") {
      const val = (s.setting || "").toLowerCase();
      return `<select id="setting-value-input"><option value="on" ${val === "on" ? "selected" : ""}>on</option><option value="off" ${val === "off" ? "selected" : ""}>off</option></select>`;
    }
    if (s.vartype === "enum" && Array.isArray(s.enumvals)) {
      return `<select id="setting-value-input">${s.enumvals.map((v) => `<option value="${esc(v)}" ${v === s.setting ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
    }
    const type = (s.vartype === "integer" || s.vartype === "real") ? "number" : "text";
    const range = (s.min_val != null && s.max_val != null) ? ` min="${esc(s.min_val)}" max="${esc(s.max_val)}"` : "";
    const input = `<input type="${type}" id="setting-value-input" value="${esc(s.setting ?? "")}"${range}>`;
    // the raw `setting` for a numeric GUC is always in its base `unit` (e.g.
    // kB for work_mem, not whatever shorthand someone used in postgresql.conf) --
    // show that unit next to the box so the number being typed is unambiguous.
    return s.unit ? `<div class="setting-value-with-unit">${input}<span class="setting-unit-suffix">${esc(s.unit)}</span></div>` : input;
  }

  function openSettingModal(name) {
    const s = allSettings.find((x) => x.name === name);
    if (!s) return;
    settingTarget = s;
    document.getElementById("setting-target").textContent = s.name;
    document.getElementById("setting-desc").textContent = s.short_desc || "";
    document.getElementById("setting-current").textContent = fmtSettingVal(s.setting, s.unit);
    document.getElementById("setting-value-wrap").innerHTML = settingValueInputHtml(s);
    const note = CONTEXT_IMPACT_NOTES[s.context] || (s.min_val != null && s.max_val != null ? `Valid range: ${s.min_val} – ${s.max_val}${s.unit ? " " + s.unit : ""}.` : "");
    document.getElementById("setting-impact-note").textContent = note;
    document.getElementById("setting-status").innerHTML = "";
    document.getElementById("setting-status").className = "add-db-status";
    const applyBtn = document.getElementById("btn-apply-setting");
    applyBtn.disabled = false; applyBtn.textContent = "Apply";
    settingModal.classList.remove("hidden");
  }
  function closeSettingModal() {
    settingModal.classList.add("hidden");
    settingTarget = null;
  }
  document.getElementById("btn-close-setting").addEventListener("click", closeSettingModal);
  settingModal.addEventListener("click", (e) => { if (e.target === settingModal) closeSettingModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !settingModal.classList.contains("hidden")) closeSettingModal();
  });

  async function applySettingChange(reset) {
    if (!settingTarget) return;
    const value = reset ? null : document.getElementById("setting-value-input").value;
    if (!reset && settingTarget.context === "postmaster" &&
        !confirm(`${settingTarget.name} requires a full Postgres restart to actually take effect. Write the new value to postgresql.auto.conf now anyway?`)) {
      return;
    }
    const applyBtn = document.getElementById("btn-apply-setting");
    const resetBtn = document.getElementById("btn-reset-setting");
    const statusEl = document.getElementById("setting-status");
    applyBtn.disabled = true; resetBtn.disabled = true;
    statusEl.innerHTML = "";
    try {
      const resp = await apiPost(`/api/settings/${encodeURIComponent(settingTarget.name)}`, { value, reset });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      statusEl.className = "add-db-status status-ok";
      statusEl.textContent = `${data.name} is now ${fmtSettingVal(data.after?.setting, data.after?.unit)}.` + (data.note ? " " + data.note : "");
      loadSettings();
    } catch (e) {
      statusEl.className = "add-db-status status-err";
      statusEl.textContent = "Failed: " + e.message;
    } finally {
      applyBtn.disabled = false; resetBtn.disabled = false;
    }
  }
  document.getElementById("btn-apply-setting").addEventListener("click", () => applySettingChange(false));
  document.getElementById("btn-reset-setting").addEventListener("click", () => {
    if (confirm(`Reset ${settingTarget?.name} to its compiled-in default?`)) applySettingChange(true);
  });

  // ------------------------------------------------- optimize database modal
  // A deterministic, formula-based advisor (see app/tuning.py) -- not an AI
  // call -- so it works regardless of whether AI features are configured.
  const optimizeModal = document.getElementById("optimize-modal");

  function openOptimizeModal() {
    document.getElementById("optimize-results-section").classList.add("hidden");
    document.getElementById("optimize-form-section").classList.remove("hidden");
    document.getElementById("optimize-status").innerHTML = "";
    optimizeModal.classList.remove("hidden");
  }
  function closeOptimizeModal() { optimizeModal.classList.add("hidden"); }
  document.getElementById("btn-open-optimize")?.addEventListener("click", openOptimizeModal);
  document.getElementById("btn-close-optimize").addEventListener("click", closeOptimizeModal);
  optimizeModal.addEventListener("click", (e) => { if (e.target === optimizeModal) closeOptimizeModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !optimizeModal.classList.contains("hidden")) closeOptimizeModal();
  });
  document.getElementById("btn-optimize-back").addEventListener("click", () => {
    document.getElementById("optimize-results-section").classList.add("hidden");
    document.getElementById("optimize-form-section").classList.remove("hidden");
  });

  // Converts a raw pg_settings value + unit (e.g. 65536 + "8kB") into the
  // same human-readable MB/GB form the recommendations use, so "Current"
  // and "Recommended" are actually comparable at a glance instead of one
  // being a raw block count and the other "512MB".
  const MEMORY_UNIT_BYTES = { "8kB": 8 * 1024, "kB": 1024, "MB": 1024 * 1024, "GB": 1024 * 1024 * 1024 };
  function humanizeForCompare(setting, unit) {
    const n = Number(setting);
    if (setting == null || setting === "" || Number.isNaN(n) || !(unit in MEMORY_UNIT_BYTES)) {
      return fmtSettingVal(setting, unit);
    }
    const mb = (n * MEMORY_UNIT_BYTES[unit]) / (1024 * 1024);
    if (mb >= 1024) return `${+(mb / 1024).toFixed(1)}GB`;
    if (mb >= 1) return `${Math.round(mb)}MB`;
    return `${Math.round(n * MEMORY_UNIT_BYTES[unit] / 1024)}kB`;
  }

  function renderOptimizeResults(recommendations) {
    const tbody = document.querySelector("#table-optimize-results tbody");
    tbody.innerHTML = recommendations.map((r) => {
      const current = allSettings.find((s) => s.name === r.name);
      return `
      <tr data-param="${esc(r.name)}">
        <td class="mono">${esc(r.name)}</td>
        <td class="mono">${current ? esc(humanizeForCompare(current.setting, current.unit)) : "—"}</td>
        <td class="mono">${esc(r.value)}</td>
        <td class="hint-text">${esc(r.note)}</td>
        <td><button type="button" class="btn btn-ghost btn-sm" data-apply-rec="${esc(r.name)}" data-value="${esc(r.value)}">Apply</button></td>
      </tr>`;
    }).join("") || `<tr><td colspan="5" class="empty-note">No recommendations.</td></tr>`;

    tbody.querySelectorAll("[data-apply-rec]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.applyRec;
        const value = btn.dataset.value;
        const current = allSettings.find((s) => s.name === name);
        if (current?.context === "postmaster" &&
            !confirm(`${name} requires a full Postgres restart to actually take effect. Write the new value to postgresql.auto.conf now anyway?`)) {
          return;
        }
        const original = btn.textContent;
        btn.disabled = true; btn.textContent = "Applying...";
        try {
          const resp = await apiPost(`/api/settings/${encodeURIComponent(name)}`, { value, reset: false });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.detail || resp.statusText);
          btn.textContent = "Applied ✓";
          loadSettings();
        } catch (e) {
          alert(`Failed to apply ${name}: ` + e.message);
          btn.disabled = false; btn.textContent = original;
        }
      });
    });
  }

  document.getElementById("btn-get-recommendations").addEventListener("click", async () => {
    const statusEl = document.getElementById("optimize-status");
    const cpu = document.getElementById("opt-cpu").value;
    const ram = document.getElementById("opt-ram").value;
    if (!cpu || !ram || Number(cpu) <= 0 || Number(ram) <= 0) {
      statusEl.className = "add-db-status status-err";
      statusEl.textContent = "Enter CPU cores and RAM (GB) -- both are required.";
      return;
    }
    const body = {
      cpu_count: Number(cpu),
      ram_gb: Number(ram),
      db_type: document.getElementById("opt-db-type").value,
      storage: document.getElementById("opt-storage").value,
      max_connections: document.getElementById("opt-max-conn").value || null,
    };
    const btn = document.getElementById("btn-get-recommendations");
    btn.disabled = true; btn.textContent = "Calculating...";
    statusEl.innerHTML = "";
    try {
      const resp = await apiPost("/api/settings/optimize-advice", body);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      renderOptimizeResults(data.recommendations || []);
      document.getElementById("optimize-form-section").classList.add("hidden");
      document.getElementById("optimize-results-section").classList.remove("hidden");
    } catch (e) {
      statusEl.className = "add-db-status status-err";
      statusEl.textContent = "Failed: " + e.message;
    } finally {
      btn.disabled = false; btn.textContent = "Get recommendations";
    }
  });

  // --------------------------------------------------------- connections
  function stateClass(state) {
    if (state === "active") return "state-active";
    if (state === "idle in transaction") return "state-idle-in-transaction";
    return "";
  }

  async function loadConnections() {
    try {
      const rows = await api("/api/connections").then((r) => r.json());
      const tbody = document.querySelector("#table-connections tbody");
      tbody.innerHTML = rows.map((c) => `
        <tr data-pid="${c.pid}" data-state="${esc(c.state || "")}" title="Right-click for options">
          <td class="mono">${c.pid}</td>
          <td>${esc(c.usename)}</td>
          <td>${esc(c.application_name || "")}</td>
          <td><span class="state-pill ${stateClass(c.state)}">${esc(c.state || "")}</span></td>
          <td>${esc(c.wait_event || "")}</td>
          <td>${fmtDuration(c.query_seconds)}</td>
          <td><span class="q-snippet" title="${esc(c.query || "")}">${esc(c.query || "")}</span></td>
          <td><button class="btn-icon-danger" data-kill="${c.pid}">Kill session</button></td>
        </tr>`).join("") || `<tr><td colspan="8" class="empty-note">No other connections right now.</td></tr>`;
      tbody.querySelectorAll("[data-kill]").forEach((btn) => {
        btn.addEventListener("click", () => killConnection(Number(btn.dataset.kill), btn));
      });
    } catch (e) { console.error("connections load failed", e); }
  }

  async function loadLocks() {
    try {
      const rows = await api("/api/locks").then((r) => r.json());
      const tbody = document.querySelector("#table-locks tbody");
      const emptyNote = document.getElementById("locks-empty");
      if (!rows.length) {
        tbody.innerHTML = "";
        emptyNote.style.display = "block";
        return;
      }
      emptyNote.style.display = "none";
      tbody.innerHTML = rows.map((l) => `
        <tr>
          <td class="mono">${l.blocked_pid}</td>
          <td><span class="q-snippet" title="${esc(l.blocked_query || "")}">${esc(l.blocked_query || "")}</span></td>
          <td class="mono">${l.blocking_pid}</td>
          <td><span class="q-snippet" title="${esc(l.blocking_query || "")}">${esc(l.blocking_query || "")}</span></td>
          <td><button class="btn-icon-danger" data-kill="${l.blocking_pid}">Kill session</button></td>
        </tr>`).join("");
      tbody.querySelectorAll("[data-kill]").forEach((btn) => {
        btn.addEventListener("click", () => killConnection(Number(btn.dataset.kill), btn));
      });
    } catch (e) { console.error("locks load failed", e); }
  }

  async function killConnection(pid, btn) {
    if (!confirm(`Terminate connection ${pid}? This forcibly disconnects it and rolls back its open transaction, releasing whatever lock it's holding.`)) return;
    if (btn) { btn.disabled = true; btn.textContent = "Killing..."; }
    try {
      const resp = await apiPost(`/api/connections/${pid}/terminate`);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      loadConnections();
      loadLocks();
    } catch (e) {
      alert("Failed to terminate connection " + pid + ": " + e.message);
      if (btn) { btn.disabled = false; btn.textContent = "Kill session"; }
    }
  }

  // ----------------------------------------------------- session context menu
  const sessionContextMenu = document.getElementById("session-context-menu");
  const sessionContextMenuLabel = document.getElementById("session-context-menu-label");
  const sessionContextMenuKillBtn = document.getElementById("session-context-menu-kill");
  let contextMenuPid = null;

  function hideSessionContextMenu() {
    sessionContextMenu.classList.add("hidden");
    contextMenuPid = null;
  }

  function showSessionContextMenu(x, y, pid, state) {
    contextMenuPid = pid;
    sessionContextMenuLabel.textContent = `PID ${pid} · ${state || "unknown state"}`;
    sessionContextMenu.classList.remove("hidden");
    // Clamp so the menu never renders off-screen.
    const rect = sessionContextMenu.getBoundingClientRect();
    const maxX = window.innerWidth - rect.width - 8;
    const maxY = window.innerHeight - rect.height - 8;
    sessionContextMenu.style.left = `${Math.max(8, Math.min(x, maxX))}px`;
    sessionContextMenu.style.top = `${Math.max(8, Math.min(y, maxY))}px`;
  }

  document.querySelector("#table-connections tbody")?.addEventListener("contextmenu", (e) => {
    const row = e.target.closest("tr[data-pid]");
    if (!row) return;
    e.preventDefault();
    showSessionContextMenu(e.clientX, e.clientY, Number(row.dataset.pid), row.dataset.state);
  });

  sessionContextMenuKillBtn.addEventListener("click", () => {
    const pid = contextMenuPid;
    hideSessionContextMenu();
    if (pid != null) killConnection(pid);
  });

  document.addEventListener("click", (e) => {
    if (!sessionContextMenu.classList.contains("hidden") && !sessionContextMenu.contains(e.target)) {
      hideSessionContextMenu();
    }
  });
  document.addEventListener("scroll", hideSessionContextMenu, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideSessionContextMenu(); });

  let connectionsPoll = null;
  function stopConnectionsPoll() {
    if (connectionsPoll) { clearInterval(connectionsPoll); connectionsPoll = null; }
  }
  dtabLoaders.connections = () => {
    loadConnections(); loadLocks();
    stopConnectionsPoll();
    connectionsPoll = setInterval(() => { loadConnections(); loadLocks(); }, 5000);
  };

  // ------------------------------------------------------------------ alerts
  // Rule-based checks (see app/alerts.py) -- not an AI feature, always on.
  let latestAlerts = [];

  function renderAlertsList() {
    const activeSeverities = new Set(
      Array.from(document.querySelectorAll(".alert-severity-filter:checked")).map((cb) => cb.value)
    );
    const shown = latestAlerts.filter((a) => activeSeverities.has(a.severity));
    const listEl = document.getElementById("alerts-list");
    listEl.innerHTML = shown.map((a) => `
      <div class="alert-item sev-${esc(a.severity)}">
        <div class="alert-item-head">
          <span class="alert-severity-badge sev-${esc(a.severity)}">${esc(a.severity)}</span>
          <span class="alert-item-title">${esc(a.title)}</span>
          <span class="alert-item-category">${esc(a.category)}</span>
        </div>
        <div class="alert-item-detail">${esc(a.detail)}</div>
      </div>`).join("") ||
      `<div class="empty-note">${latestAlerts.length ? "No alerts match this filter." : "✓ No issues detected right now."}</div>`;

    const badge = document.getElementById("alerts-tab-badge");
    const urgentCount = latestAlerts.filter((a) => a.severity === "error" || a.severity === "warning").length;
    if (urgentCount > 0) {
      const anyError = latestAlerts.some((a) => a.severity === "error");
      badge.textContent = String(urgentCount);
      badge.className = `badge ${anyError ? "badge-down" : "badge-muted"}`;
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }
  document.querySelectorAll(".alert-severity-filter").forEach((cb) => cb.addEventListener("change", renderAlertsList));

  async function loadAlerts() {
    try {
      latestAlerts = await api("/api/alerts").then((r) => r.json());
      renderAlertsList();
    } catch (e) { console.error("alerts load failed", e); }
  }
  let alertsPoll = null;
  function stopAlertsPoll() {
    if (alertsPoll) { clearInterval(alertsPoll); alertsPoll = null; }
  }
  dtabLoaders.alerts = () => {
    loadAlerts();
    stopAlertsPoll();
    alertsPoll = setInterval(loadAlerts, 15000);
  };

  // -------------------------------------------------------------- queries
  async function loadPgssStatus() {
    try {
      const s = await api("/api/queries/status").then((r) => r.json());
      const set = (key, val) => {
        const el = document.querySelector(`[data-pgssstat="${key}"]`);
        if (el) el.textContent = val;
      };
      set("installed", s.installed ? `installed (v${s.extversion})` : (s.preloaded ? "not installed" : "not loaded"));
      set("track", s.track ?? "—");
      set("max", s.pg_stat_statements_max ?? "—");
      set("io_timing", s.track_io_timing ?? "—");
      set("since", fmtDate(s.stats_since));
      const installBtn = document.getElementById("btn-pgss-install");
      installBtn.classList.toggle("hidden", !(s.preloaded && !s.installed));
      return s;
    } catch (e) { console.error("pg_stat_statements status load failed", e); return null; }
  }
  document.getElementById("btn-pgss-install")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = "Installing...";
    try {
      const resp = await apiPost("/api/queries/install-extension");
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      await loadPgssStatus();
      loadQueries();
    } catch (e2) {
      alert("Install failed: " + e2.message);
    } finally {
      btn.disabled = false; btn.textContent = "Install pg_stat_statements";
    }
  });
  document.getElementById("btn-queries-reset")?.addEventListener("click", async () => {
    if (!confirm("Reset all pg_stat_statements counters? This clears the Top queries history (server-wide, all databases) and cannot be undone.")) return;
    try {
      const resp = await apiPost("/api/queries/reset");
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      loadPgssStatus();
      loadQueries();
    } catch (e) { alert("Reset failed: " + e.message); }
  });
  document.getElementById("btn-queries-refresh")?.addEventListener("click", loadQueries);
  document.getElementById("queries-order-by")?.addEventListener("change", loadQueries);
  document.getElementById("queries-all-databases")?.addEventListener("change", loadQueries);
  document.getElementById("queries-include-monitoring")?.addEventListener("change", loadQueries);

  // Bumped on every call and captured per-request so a slower, older
  // request (e.g. still waiting on loadPgssStatus's own round-trip below)
  // can't land after a newer one and overwrite the table with results
  // sorted by whatever order_by/checkbox state was selected *before* the
  // user's latest change -- the dropdown already shows the new choice by
  // then, so it'd look like sorting was simply ignored.
  let queriesRequestSeq = 0;

  async function loadQueries() {
    const seq = ++queriesRequestSeq;
    try {
      await loadPgssStatus();
      const orderBy = document.getElementById("queries-order-by").value;
      const allDbs = document.getElementById("queries-all-databases").checked;
      const includeMonitoring = document.getElementById("queries-include-monitoring").checked;
      const qs = `&order_by=${encodeURIComponent(orderBy)}&all_databases=${allDbs}&include_monitoring=${includeMonitoring}`;
      const data = await api("/api/queries?limit=25" + qs).then((r) => r.json());
      if (seq !== queriesRequestSeq) return; // a newer loadQueries() call has since started -- discard this one
      const note = document.getElementById("queries-unavailable-note");
      const tbody = document.querySelector("#table-queries tbody");
      if (!data.available) {
        note.classList.remove("hidden");
        note.textContent = data.reason;
        tbody.innerHTML = "";
        return;
      }
      note.classList.add("hidden");
      tbody.innerHTML = (data.queries || []).map((q) => {
        const hit = q.shared_blks_hit || 0, read = q.shared_blks_read || 0;
        const total = hit + read;
        const cacheHitPct = total > 0 ? (100 * hit / total) : null;
        const spilled = (q.temp_blks_written || 0) > 0;
        const lowCache = cacheHitPct !== null && cacheHitPct < 90 && total >= 10;
        const ioMs = (q.blk_read_ms || 0) + (q.blk_write_ms || 0);
        const rowClass = lowCache ? "row-low-cache" : (spilled ? "row-spill" : "");
        return `
        <tr class="${rowClass}">
          <td><span class="q-snippet" title="${esc(q.query)}">${esc(q.query)}</span></td>
          <td>${fmtNum(q.calls)}</td>
          <td>${fmtNum(q.total_ms)}</td>
          <td>${fmtNum(q.mean_ms)}</td>
          <td>${fmtNum(q.min_ms)}</td>
          <td>${fmtNum(q.max_ms)}</td>
          <td>${fmtNum(q.stddev_ms)}</td>
          <td>${fmtNum(q.rows)}</td>
          <td>${cacheHitPct === null ? "—" : cacheHitPct.toFixed(1) + "%"}</td>
          <td>${fmtNum(q.shared_blks_read)}</td>
          <td>${fmtNum(q.temp_blks_written)}</td>
          <td>${ioMs ? fmtNum(Math.round(ioMs * 100) / 100) : "—"}</td>
          <td>${q.wal_bytes != null ? fmtBytes(q.wal_bytes) : "—"}</td>
          <td><button type="button" class="btn-icon-sm" title="View plan &amp; tuning advice" aria-label="View plan and tuning advice" data-plan-query="${esc(q.query)}">P</button></td>
        </tr>`;
      }).join("") || `<tr><td colspan="14" class="empty-note">No query stats yet.</td></tr>`;
      tbody.querySelectorAll("[data-plan-query]").forEach((btn) =>
        btn.addEventListener("click", () => openPlanModal(btn.dataset.planQuery)));
    } catch (e) { console.error("queries load failed", e); }
  }
  dtabLoaders["sql-performance"] = loadQueries;

  // ------------------------------------------------- query plan / tuning advice
  // No AI required -- a plain EXPLAIN (metrics.get_query_plan / get_tuning_advice),
  // so this works even when the AI Query Optimizer below is disabled.
  const planModal = document.getElementById("plan-modal");
  let planModalQuery = "";

  function closePlanModal() { planModal.classList.add("hidden"); }
  document.getElementById("btn-close-plan").addEventListener("click", closePlanModal);
  planModal.addEventListener("click", (e) => { if (e.target === planModal) closePlanModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !planModal.classList.contains("hidden")) closePlanModal();
  });

  function renderPlanAdvice(advice) {
    const severityClass = { high: "stat-bad", medium: "stat-warn", low: "hint-text", info: "hint-text" };
    return (advice || []).map((a) => `
      <div class="ai-summary-card" style="margin-top:0.6rem;">
        <strong class="${severityClass[a.severity] || ""}">${esc(a.title)}</strong>
        <p class="hint-text" style="margin:0.3rem 0 0;">${esc(a.detail)}</p>
        ${a.suggested_sql ? `<pre class="log-box" style="margin-top:0.4rem;">${esc(a.suggested_sql)}</pre>` : ""}
      </div>`).join("");
  }

  async function runPlan(mode) {
    const statusEl = document.getElementById("plan-status");
    const unresolvedBox = document.getElementById("plan-unresolved-actions");
    const adviceSection = document.getElementById("plan-advice-section");
    const rawWrap = document.getElementById("plan-raw-wrap");
    const analyze = document.getElementById("plan-analyze-checkbox").checked;
    unresolvedBox.classList.add("hidden");
    adviceSection.classList.add("hidden");
    statusEl.textContent = mode === "advice" ? "Analyzing..." : "Running EXPLAIN...";
    try {
      const path = mode === "advice" ? "/api/queries/tuning-advice" : "/api/queries/plan";
      const resp = await apiPost(path, { query: planModalQuery, analyze });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      statusEl.textContent = "";
      document.getElementById("plan-substituted-note").classList.toggle("hidden", !data.substituted);
      if (data.substituted) {
        document.getElementById("plan-substituted-note").textContent =
          "Parameter placeholders ($1, $2, ...) were auto-filled with a representative real value from the referenced table(s) to make EXPLAIN possible -- the plan reflects that substitution, not necessarily the exact values your application actually sends.";
      }
      document.getElementById("plan-raw-json").textContent = JSON.stringify(data.plan, null, 2);
      rawWrap.classList.remove("hidden");
      rawWrap.open = mode !== "advice";
      if (mode === "advice") {
        document.getElementById("plan-advice-list").innerHTML = renderPlanAdvice(data.advice);
        adviceSection.classList.remove("hidden");
      }
    } catch (err) {
      statusEl.textContent = "Failed: " + err.message;
      if (/couldn't automatically resolve parameter/i.test(err.message)) {
        unresolvedBox.classList.remove("hidden");
      }
    }
  }

  function openPlanModal(query) {
    planModalQuery = query;
    document.getElementById("plan-query-text").textContent = query;
    document.getElementById("plan-analyze-checkbox").checked = false;
    document.getElementById("plan-substituted-note").classList.add("hidden");
    document.getElementById("plan-unresolved-actions").classList.add("hidden");
    document.getElementById("plan-advice-section").classList.add("hidden");
    document.getElementById("plan-raw-wrap").classList.add("hidden");
    document.getElementById("plan-raw-json").textContent = "";
    planModal.classList.remove("hidden");
    runPlan("plan");
  }
  document.getElementById("btn-plan-refresh").addEventListener("click", () => runPlan("plan"));
  document.getElementById("btn-plan-tuning-advice").addEventListener("click", () => runPlan("advice"));
  document.getElementById("btn-plan-edit-manually").addEventListener("click", () => {
    closePlanModal();
    document.getElementById("query-input").value = planModalQuery;
    document.getElementById("query-input").scrollIntoView({ behavior: "smooth", block: "center" });
  });

  document.getElementById("btn-explain-query")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const query = document.getElementById("query-input").value.trim();
    const resultBox = document.getElementById("explain-result");
    const analysisBox = document.getElementById("explain-analysis");
    const rawBox = document.getElementById("explain-plan-raw");
    if (!query) return;
    btn.disabled = true;
    btn.textContent = "✦ Running EXPLAIN + AI analysis...";
    resultBox.classList.remove("hidden");
    analysisBox.textContent = "Working...";
    rawBox.textContent = "";
    try {
      const resp = await apiPost("/api/ai/explain-query", { query });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      analysisBox.textContent = data.analysis;
      rawBox.textContent = JSON.stringify(data.plan, null, 2);
    } catch (err) {
      analysisBox.textContent = "Error: " + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = "✦ Explain with AI";
    }
  });

  // --------------------------------------------------------------- backup
  async function loadBackup() {
    try {
      const b = await api("/api/backup").then((r) => r.json());
      const set = (key, val) => {
        const el = document.querySelector(`[data-bstat="${key}"]`);
        if (el) el.textContent = val;
      };
      set("wal_level", b.wal_level ?? "—");
      set("archive_mode", b.archive_mode ?? "—");
      set("archived_count", b.archiver ? fmtNum(b.archiver.archived_count) : "—");
      set("failed_count", b.archiver ? fmtNum(b.archiver.failed_count) : "—");
      set("checkpoint_time", b.checkpoint ? fmtDate(b.checkpoint.checkpoint_time) : "—");
      set("replica_count", fmtNum((b.replicas || []).length));
    } catch (e) { console.error("backup load failed", e); }
  }
  document.getElementById("btn-backup-review")?.addEventListener("click", (e) =>
    runAssessment(e.currentTarget, "ai-backup-card", "✦ Analyzing...", "✦ AI Backup Readiness Review", "/api/ai/backup-review"));

  // ----------------------------------------------------- on-demand backups
  function backupDownloadUrl(filename) {
    return BASE + withDbId(`/api/backup/download/${encodeURIComponent(filename)}`);
  }

  const BACKUP_TYPE_LABELS = { logical: "Logical", physical: "Physical", incremental: "Incremental" };
  let latestBackups = [];
  let physicalRestoreAvailable = false;
  let pitrAvailable = false;

  async function checkPhysicalRestoreAvailable() {
    try {
      const data = await api("/api/backup/physical-restore-available").then((r) => r.json());
      physicalRestoreAvailable = !!data.ok;
    } catch (e) { physicalRestoreAvailable = false; }
    try {
      const data = await api("/api/backup/pitr-available").then((r) => r.json());
      pitrAvailable = !!data.ok;
    } catch (e) { pitrAvailable = false; }
  }

  function populateIncrementalBaseOptions() {
    const sel = document.getElementById("backup-base-select");
    const bases = latestBackups.filter((b) => b.type === "physical" || b.type === "incremental");
    sel.innerHTML = bases.map((b) => `<option value="${esc(b.filename)}">${esc(b.filename)} (${BACKUP_TYPE_LABELS[b.type]})</option>`).join("") ||
      `<option value="">No physical/incremental backup yet -- take one first</option>`;
  }

  async function loadBackupList() {
    try {
      latestBackups = await api("/api/backup/list").then((r) => r.json());
      const tbody = document.querySelector("#table-backups tbody");
      const scopeLabel = (b) => {
        if (b.type !== "logical" || !b.scope_type || b.scope_type === "whole") return "";
        const n = (b.scope || []).length;
        const noun = b.scope_type === "schemas" ? "schema" : "table";
        return ` <span class="hint-text" title="${esc((b.scope || []).join(", "))}">(${n} ${noun}${n === 1 ? "" : "s"})</span>`;
      };
      const verifiedLabel = (b) => {
        if (b.type === "logical") return `<span class="hint-text">&mdash;</span>`;
        if (b.verified_at == null) return `<span class="hint-text">Not verified</span>`;
        const when = fmtDate(b.verified_at * 1000);
        return b.verified_ok
          ? `<span class="stat-good" title="${esc(when)}">&#10003; Verified</span>`
          : `<span class="stat-bad" title="${esc(when)}">&#10007; Failed</span>`;
      };
      tbody.innerHTML = latestBackups.map((b) => `
        <tr>
          <td class="mono">${esc(b.filename)}</td>
          <td><span class="badge badge-type-${esc(b.type)}">${esc(BACKUP_TYPE_LABELS[b.type] || b.type)}</span>${scopeLabel(b)}</td>
          <td>${fmtDate(b.created_at * 1000)}</td>
          <td>${fmtBytes(b.size_bytes)}</td>
          <td>${verifiedLabel(b)}</td>
          <td>
            <a href="${esc(backupDownloadUrl(b.filename))}" class="btn btn-ghost btn-sm">Download</a>
            ${(b.type === "logical" || physicalRestoreAvailable) ? `<button type="button" class="btn btn-ghost btn-sm" data-restore="${esc(b.filename)}">Restore&hellip;</button>` : ""}
            ${b.type !== "logical" ? `<button type="button" class="btn btn-ghost btn-sm" data-verify-backup="${esc(b.filename)}">Verify</button>` : ""}
            <button type="button" class="btn-icon-danger" data-delete-backup="${esc(b.filename)}">Delete</button>
          </td>
        </tr>`).join("") || `<tr><td colspan="6" class="empty-note">No backups yet -- click "Run backup now" above.</td></tr>`;

      tbody.querySelectorAll("[data-restore]").forEach((btn) => {
        btn.addEventListener("click", () => openRestoreModal(btn.dataset.restore));
      });
      tbody.querySelectorAll("[data-verify-backup]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const filename = btn.dataset.verifyBackup;
          btn.disabled = true;
          try {
            const resp = await apiPost(`/api/backup/${encodeURIComponent(filename)}/verify`, {});
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || resp.statusText);
            pollBackupJob(data.job_id);
          } catch (e) {
            alert("Failed to start verification: " + e.message);
            btn.disabled = false;
          }
        });
      });
      tbody.querySelectorAll("[data-delete-backup]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const filename = btn.dataset.deleteBackup;
          if (!confirm(`Permanently delete backup ${filename}? This cannot be undone.`)) return;
          try {
            const resp = await fetch(BASE + withDbId(`/api/backup/${encodeURIComponent(filename)}`), {
              method: "DELETE", credentials: "same-origin",
            });
            if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
            loadBackupList();
          } catch (e) { alert("Failed to delete: " + e.message); }
        });
      });
      if (!document.getElementById("backup-base-row").classList.contains("hidden")) {
        populateIncrementalBaseOptions();
      }
      populateWalmineBackupOptions();
    } catch (e) { console.error("backup list load failed", e); }
  }

  async function runBackupImport(btn, fileInputId, url, kind) {
    const input = document.getElementById(fileInputId);
    const statusEl = document.getElementById("import-status");
    const file = input.files[0];
    if (!file) { alert(`Choose a ${kind} backup file first.`); return; }
    const form = new FormData();
    form.append("file", file);
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Importing...";
    try {
      const resp = await fetch(BASE + withDbId(url), { method: "POST", credentials: "same-origin", body: form });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      statusEl.textContent = `Imported as ${data.filename}.`;
      input.value = "";
      loadBackupList();
    } catch (e) {
      alert(`Import failed: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }
  document.getElementById("btn-import-logical")?.addEventListener("click", (e) =>
    runBackupImport(e.currentTarget, "import-logical-file", "/api/backup/import-logical", "logical"));
  document.getElementById("btn-import-physical")?.addEventListener("click", (e) =>
    runBackupImport(e.currentTarget, "import-physical-file", "/api/backup/import-physical", "physical"));

  function pollImportRolesJob(jobId) {
    const summaryEl = document.getElementById("import-roles-job-summary");
    const logEl = document.getElementById("import-roles-job-log");
    document.getElementById("import-roles-job-section").classList.remove("hidden");
    const tick = async () => {
      try {
        const job = await api(`/api/backup/jobs/${jobId}`).then((r) => r.json());
        const elapsed = (job.finished_at || Date.now() / 1000) - job.started_at;
        summaryEl.textContent = `Roles script: ${job.status} (${fmtDuration(elapsed)} elapsed)` + (job.error ? ` — ${job.error}` : "");
        logEl.textContent = (job.log || []).join("\n");
        logEl.scrollTop = logEl.scrollHeight;
        if (job.status === "running") setTimeout(tick, 1500);
      } catch (e) { console.error("import-roles job poll failed", e); }
    };
    tick();
  }
  document.getElementById("btn-import-roles")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const input = document.getElementById("import-roles-file");
    const statusEl = document.getElementById("import-status");
    const file = input.files[0];
    if (!file) { alert("Choose a roles script file first."); return; }
    const form = new FormData();
    form.append("file", file);
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Applying...";
    try {
      const resp = await fetch(BASE + withDbId("/api/backup/import-roles"), { method: "POST", credentials: "same-origin", body: form });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      statusEl.textContent = "Roles script running -- see the log below.";
      input.value = "";
      pollImportRolesJob(data.job_id);
    } catch (e) {
      alert(`Applying roles script failed: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  });

  const BACKUP_TYPE_HINTS = {
    logical: 'A <code>pg_dump</code> of just this database. Portable, and restorable with one click below.',
    physical: 'A full <code>pg_basebackup</code> of the <strong>entire server</strong> (every database, roles, config) -- physical backups always operate at that level. Restoring is manual (see below the table), not one-click.',
    incremental: 'Only the changes since a prior physical/incremental backup (PG17+ feature) -- much smaller and faster than a full physical backup, but needs one to build on. Also whole-server; restoring is manual.',
  };
  document.getElementById("backup-type-select")?.addEventListener("change", (e) => {
    const type = e.target.value;
    document.getElementById("backup-type-hint").innerHTML = BACKUP_TYPE_HINTS[type];
    const baseRow = document.getElementById("backup-base-row");
    baseRow.classList.toggle("hidden", type !== "incremental");
    if (type === "incremental") populateIncrementalBaseOptions();
    // Scoped backups (selected schemas, optionally narrowed to specific
    // tables within them) only make sense for a logical pg_dump --
    // physical/incremental always capture the whole server, that's
    // inherent to how pg_basebackup works (see backup.py).
    document.getElementById("backup-scope-row").classList.toggle("hidden", type !== "logical");
    if (type !== "logical") {
      document.getElementById("backup-scope-select").value = "whole";
      document.getElementById("backup-scope-schemas-row").classList.add("hidden");
      document.getElementById("backup-scope-tables-row").classList.add("hidden");
    }
  });

  let backupScopeSchemasLoaded = false;
  let backupScopeTables = []; // [{schema, table, key: "schema.table", checked}], for whichever schema(s) are currently selected

  async function populateBackupScopeSchemas() {
    if (backupScopeSchemasLoaded) return;
    const select = document.getElementById("backup-scope-schemas-select");
    try {
      const schemas = await api("/api/schemas").then((r) => r.json());
      select.innerHTML = schemas.filter((s) => !s.is_system).map((s) =>
        `<option value="${esc(s.schema_name)}">${esc(s.schema_name)}</option>`).join("");
      backupScopeSchemasLoaded = true;
    } catch (e) { console.error("schema list load failed for backup scope", e); }
  }

  function renderBackupScopeTables() {
    const filterText = document.getElementById("backup-scope-tables-filter").value.trim().toLowerCase();
    const tbody = document.querySelector("#table-backup-scope-tables tbody");
    const rows = backupScopeTables.filter((t) => t.key.toLowerCase().includes(filterText));
    tbody.innerHTML = rows.map((t) => `
      <tr>
        <td><input type="checkbox" class="backup-scope-table-check" data-key="${esc(t.key)}" ${t.checked ? "checked" : ""}></td>
        <td class="mono">${esc(t.key)}</td>
      </tr>`).join("") || `<tr><td colspan="2" class="empty-note">No tables in the selected schema(s).</td></tr>`;
    tbody.querySelectorAll(".backup-scope-table-check").forEach((cb) => {
      cb.addEventListener("change", () => {
        const t = backupScopeTables.find((x) => x.key === cb.dataset.key);
        if (t) t.checked = cb.checked;
        // Any manual box change means "whole schema" no longer describes
        // the selection exactly -- reflect that rather than leaving a
        // stale "Whole schema" checkbox checked while a table is unchecked.
        document.getElementById("backup-scope-tables-select-all").checked = backupScopeTables.every((x) => x.checked);
      });
    });
  }
  document.getElementById("backup-scope-tables-filter")?.addEventListener("input", renderBackupScopeTables);
  document.getElementById("backup-scope-tables-select-all")?.addEventListener("change", (e) => {
    backupScopeTables.forEach((t) => { t.checked = e.target.checked; });
    renderBackupScopeTables();
  });

  async function populateBackupScopeTablesForSchemas(schemaNames) {
    const tbody = document.querySelector("#table-backup-scope-tables tbody");
    if (!schemaNames.length) {
      backupScopeTables = [];
      tbody.innerHTML = `<tr><td colspan="2" class="empty-note">Select a schema above first.</td></tr>`;
      return;
    }
    tbody.innerHTML = `<tr><td colspan="2" class="empty-note">Loading&hellip;</td></tr>`;
    try {
      const previouslyUnchecked = new Set(backupScopeTables.filter((t) => !t.checked).map((t) => t.key));
      const next = [];
      for (const schema of schemaNames) {
        const objects = await api(`/api/schemas/${encodeURIComponent(schema)}/objects`).then((r) => r.json());
        for (const o of objects) {
          if (o.kind !== "table") continue; // real/partitioned tables only, not views/materialized views/sequences
          const key = `${schema}.${o.name}`;
          // Whole-schema (every table checked) by default -- carries over
          // an explicit uncheck across a schema-selection change instead
          // of silently re-checking something the user just excluded.
          next.push({ schema, table: o.name, key, checked: !previouslyUnchecked.has(key) });
        }
      }
      backupScopeTables = next;
      document.getElementById("backup-scope-tables-select-all").checked = backupScopeTables.every((t) => t.checked);
      renderBackupScopeTables();
    } catch (e) {
      console.error("table list load failed for backup scope", e);
      tbody.innerHTML = `<tr><td colspan="2" class="empty-note">Failed to load tables.</td></tr>`;
    }
  }

  document.getElementById("backup-scope-select")?.addEventListener("change", (e) => {
    const scope = e.target.value;
    document.getElementById("backup-scope-schemas-row").classList.toggle("hidden", scope !== "schemas");
    document.getElementById("backup-scope-tables-row").classList.toggle("hidden", scope !== "schemas");
    if (scope === "schemas") populateBackupScopeSchemas();
  });
  document.getElementById("backup-scope-schemas-select")?.addEventListener("change", (e) => {
    const selected = Array.from(e.target.selectedOptions).map((o) => o.value);
    populateBackupScopeTablesForSchemas(selected);
  });

  async function pollBackupJob(jobId, onDone) {
    const summaryEl = document.getElementById("backup-job-summary");
    const logEl = document.getElementById("backup-job-log");
    document.getElementById("backup-job-section").classList.remove("hidden");
    const tick = async () => {
      try {
        const job = await api(`/api/backup/jobs/${jobId}`).then((r) => r.json());
        const elapsed = (job.finished_at || Date.now() / 1000) - job.started_at;
        const kindLabel = job.kind === "physical-restore" ? "Physical restore" : job.kind === "restore" ? "Restore"
          : job.kind === "verify" ? "Verification" : "Backup";
        summaryEl.textContent = `${kindLabel}: ${job.status} (${fmtDuration(elapsed)} elapsed)` + (job.error ? ` — ${job.error}` : "");
        logEl.textContent = (job.log || []).join("\n");
        logEl.scrollTop = logEl.scrollHeight;
        if (job.status === "running") {
          setTimeout(tick, 1500);
        } else {
          loadBackupList();
          if (onDone) onDone(job);
        }
      } catch (e) { console.error("backup job poll failed", e); }
    };
    tick();
  }

  document.getElementById("btn-run-backup")?.addEventListener("click", async () => {
    const btn = document.getElementById("btn-run-backup");
    const backupType = document.getElementById("backup-type-select").value;
    const incrementalBase = document.getElementById("backup-base-select").value || null;
    if (backupType === "incremental" && !incrementalBase) {
      alert("Take a physical (or another incremental) backup first -- incremental backups need one to build on.");
      return;
    }
    let scopeType = backupType === "logical" ? document.getElementById("backup-scope-select").value : "whole";
    let scope = [];
    if (scopeType === "schemas") {
      const selectedSchemas = Array.from(document.getElementById("backup-scope-schemas-select").selectedOptions).map((o) => o.value);
      if (!selectedSchemas.length) { alert("Select at least one schema to back up."); return; }
      if (document.getElementById("backup-scope-tables-select-all").checked) {
        // Every table in these schema(s) -- send as a schema-level dump
        // (pg_dump -n) rather than an explicit table list, so it also
        // covers any table created after this list was loaded.
        scope = selectedSchemas;
      } else {
        scopeType = "tables";
        scope = backupScopeTables.filter((t) => t.checked).map((t) => t.key);
        if (!scope.length) { alert("Select at least one table to back up (or re-check \"Whole schema\")."); return; }
      }
    }
    btn.disabled = true; btn.textContent = "Starting...";
    try {
      const resp = await apiPost("/api/backup/run", {
        backup_type: backupType, incremental_base: incrementalBase, scope_type: scopeType, scope,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      pollBackupJob(data.job_id);
    } catch (e) {
      alert("Failed to start backup: " + e.message);
    } finally {
      btn.disabled = false; btn.textContent = "Run backup now";
    }
  });

  // ----------------------------------------------------------- restore modal
  // Four restore paths share this one modal:
  //   logical + same database   -> POST /api/backup/restore {filename}
  //   logical + new database    -> POST /api/backup/restore {filename, target_dbname}
  //   physical/incremental + in place      -> POST /api/backup/physical-restore
  //   physical/incremental + new instance  -> POST /api/backup/new-instance-restore (non-destructive, no typed confirmation needed)
  const restoreModal = document.getElementById("restore-modal");
  let restoreTargetFilename = null;
  let restoreTargetType = null; // "logical" | "physical" | "incremental"
  let restoreAvailableTables = []; // "schema.table" strings actually inside the backup being restored
  let restoreScopeTables = []; // [{schema, table, key, checked}], for whichever schema(s) are currently selected

  function restoreScopeType() {
    return document.getElementById("restore-scope-select").value;
  }

  function updateRestoreScopeWarningText() {
    const el = document.getElementById("restore-scope-warning-text");
    if (restoreScopeType() === "whole") {
      el.textContent = "the backup contains";
      return;
    }
    const selected = restoreScopeSelection();
    const noun = selected.scopeType === "schemas" ? "schema(s)" : "table(s)";
    el.textContent = selected.scope.length ? `in the selected ${noun} (${selected.scope.join(", ")})` : `in the selected ${noun}`;
  }

  function renderRestoreScopeTables() {
    const filterText = document.getElementById("restore-scope-tables-filter").value.trim().toLowerCase();
    const tbody = document.querySelector("#table-restore-scope-tables tbody");
    const rows = restoreScopeTables.filter((t) => t.key.toLowerCase().includes(filterText));
    tbody.innerHTML = rows.map((t) => `
      <tr>
        <td><input type="checkbox" class="restore-scope-table-check" data-key="${esc(t.key)}" ${t.checked ? "checked" : ""}></td>
        <td class="mono">${esc(t.key)}</td>
      </tr>`).join("") || `<tr><td colspan="2" class="empty-note">No tables in the selected schema(s).</td></tr>`;
    tbody.querySelectorAll(".restore-scope-table-check").forEach((cb) => {
      cb.addEventListener("change", () => {
        const t = restoreScopeTables.find((x) => x.key === cb.dataset.key);
        if (t) t.checked = cb.checked;
        document.getElementById("restore-scope-tables-select-all").checked = restoreScopeTables.every((x) => x.checked);
        updateRestoreScopeWarningText();
        updateRestoreButtonState();
      });
    });
  }
  document.getElementById("restore-scope-tables-filter")?.addEventListener("input", renderRestoreScopeTables);
  document.getElementById("restore-scope-tables-select-all")?.addEventListener("change", (e) => {
    restoreScopeTables.forEach((t) => { t.checked = e.target.checked; });
    renderRestoreScopeTables();
    updateRestoreScopeWarningText();
    updateRestoreButtonState();
  });

  function populateRestoreScopeTablesForSchemas(schemaNames) {
    const previouslyUnchecked = new Set(restoreScopeTables.filter((t) => !t.checked).map((t) => t.key));
    restoreScopeTables = restoreAvailableTables
      .filter((q) => schemaNames.includes(q.split(".", 1)[0]))
      .map((q) => {
        const dot = q.indexOf(".");
        return { schema: q.slice(0, dot), table: q.slice(dot + 1), key: q, checked: !previouslyUnchecked.has(q) };
      });
    document.getElementById("restore-scope-tables-select-all").checked = restoreScopeTables.every((t) => t.checked);
    renderRestoreScopeTables();
  }

  document.getElementById("restore-scope-select")?.addEventListener("change", (e) => {
    const scope = e.target.value;
    document.getElementById("restore-scope-schemas-row").classList.toggle("hidden", scope !== "schemas");
    document.getElementById("restore-scope-tables-row").classList.toggle("hidden", scope !== "schemas");
    if (scope === "schemas") {
      const select = document.getElementById("restore-scope-schemas-select");
      const schemas = Array.from(new Set(restoreAvailableTables.map((q) => q.split(".", 1)[0])));
      select.innerHTML = schemas.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
      populateRestoreScopeTablesForSchemas([]);
    }
    updateRestoreScopeWarningText();
    updateRestoreButtonState();
  });
  document.getElementById("restore-scope-schemas-select")?.addEventListener("change", (e) => {
    populateRestoreScopeTablesForSchemas(Array.from(e.target.selectedOptions).map((o) => o.value));
    updateRestoreScopeWarningText();
    updateRestoreButtonState();
  });

  function restoreScopeSelection() {
    const scopeType = restoreScopeType();
    if (scopeType !== "schemas") return { scopeType: "whole", scope: [] };
    const selectedSchemas = Array.from(document.getElementById("restore-scope-schemas-select").selectedOptions).map((o) => o.value);
    if (document.getElementById("restore-scope-tables-select-all").checked) {
      return { scopeType: "schemas", scope: selectedSchemas };
    }
    return { scopeType: "tables", scope: restoreScopeTables.filter((t) => t.checked).map((t) => t.key) };
  }

  // ------------------------------------------------- advanced restore options
  function restoreOptionsPayload() {
    const numOrNull = (id) => {
      const v = document.getElementById(id).value.trim();
      return v === "" ? null : Number(v);
    };
    const rolname = document.getElementById("restore-opt-reassign-role").value.trim();
    const preserveOwnership = document.getElementById("restore-opt-preserve-ownership").checked;
    return {
      content: document.getElementById("restore-opt-content").value,
      exclude_schemas: Array.from(document.getElementById("restore-opt-exclude-schemas").selectedOptions).map((o) => o.value),
      section: document.getElementById("restore-opt-section").value || null,
      jobs: numOrNull("restore-opt-jobs"),
      transaction_size: numOrNull("restore-opt-transaction-size"),
      exit_on_error: document.getElementById("restore-opt-exit-on-error").checked,
      single_transaction: document.getElementById("restore-opt-single-transaction").checked,
      disable_triggers: document.getElementById("restore-opt-disable-triggers").checked,
      no_comments: document.getElementById("restore-opt-no-comments").checked,
      no_security_labels: document.getElementById("restore-opt-no-security-labels").checked,
      no_publications: document.getElementById("restore-opt-no-publications").checked,
      no_subscriptions: document.getElementById("restore-opt-no-subscriptions").checked,
      no_table_access_method: document.getElementById("restore-opt-no-table-access-method").checked,
      no_tablespaces: document.getElementById("restore-opt-no-tablespaces").checked,
      preserve_ownership: preserveOwnership,
      use_set_session_authorization: preserveOwnership && document.getElementById("restore-opt-use-ssa").checked,
      reassign_owner: (!preserveOwnership && rolname) ? {
        rolname,
        provision: document.getElementById("restore-opt-reassign-provision").value,
        password: document.getElementById("restore-opt-reassign-password").value || null,
      } : null,
    };
  }

  function populateRestoreOptionDefaults() {
    document.getElementById("restore-opt-content").value = "all";
    document.getElementById("restore-opt-exclude-schemas").innerHTML = "";
    document.getElementById("restore-opt-section").value = "";
    document.getElementById("restore-opt-jobs").value = "";
    document.getElementById("restore-opt-transaction-size").value = "";
    document.getElementById("restore-opt-exit-on-error").checked = false;
    document.getElementById("restore-opt-single-transaction").checked = false;
    document.getElementById("restore-opt-disable-triggers").checked = false;
    document.getElementById("restore-opt-no-comments").checked = false;
    document.getElementById("restore-opt-no-security-labels").checked = false;
    document.getElementById("restore-opt-no-publications").checked = false;
    document.getElementById("restore-opt-no-subscriptions").checked = false;
    document.getElementById("restore-opt-no-table-access-method").checked = false;
    document.getElementById("restore-opt-no-tablespaces").checked = false;
    document.getElementById("restore-opt-preserve-ownership").checked = false;
    document.getElementById("restore-opt-use-ssa").checked = false;
    document.getElementById("restore-opt-reassign-role").value = "";
    document.getElementById("restore-opt-reassign-provision").value = "none";
    document.getElementById("restore-opt-reassign-password").value = "";
    document.getElementById("restore-opt-use-ssa-row").classList.add("hidden");
    document.getElementById("restore-opt-disable-triggers-row").classList.add("hidden");
    document.getElementById("restore-opt-reassign-section").classList.remove("hidden");
    if (document.getElementById("restore-advanced-disclosure")) {
      document.getElementById("restore-advanced-disclosure").open = false;
    }
    api("/api/security").then((r) => r.json()).then((data) => {
      const datalist = document.getElementById("restore-opt-role-datalist");
      datalist.innerHTML = (data.roles || []).map((r) => `<option value="${esc(r.rolname)}">`).join("");
    }).catch((e) => console.error("role list load failed for restore options", e));
  }

  document.getElementById("restore-opt-content")?.addEventListener("change", (e) => {
    const isData = e.target.value === "data";
    document.getElementById("restore-opt-disable-triggers-row").classList.toggle("hidden", !isData);
    document.getElementById("restore-opt-section").disabled = e.target.value !== "all";
    if (e.target.value !== "all") document.getElementById("restore-opt-section").value = "";
  });
  document.getElementById("restore-opt-preserve-ownership")?.addEventListener("change", (e) => {
    document.getElementById("restore-opt-use-ssa-row").classList.toggle("hidden", !e.target.checked);
    document.getElementById("restore-opt-reassign-section").classList.toggle("hidden", e.target.checked);
    if (e.target.checked) document.getElementById("restore-opt-reassign-role").value = "";
    else document.getElementById("restore-opt-use-ssa").checked = false;
  });

  function parseTablespaceMapping(textareaId) {
    const raw = document.getElementById(textareaId)?.value || "";
    const mapping = {};
    raw.split("\n").forEach((line) => {
      line = line.trim();
      if (!line) return;
      const eq = line.indexOf("=");
      if (eq < 1) return;
      mapping[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
    });
    return mapping;
  }

  const PITR_TARGET_LABELS = {
    time: "Target time (UTC)", lsn: "Target LSN", xid: "Target transaction ID", name: "Restore point name",
  };
  const PITR_TARGET_PLACEHOLDERS = {
    lsn: "e.g. 16/B374D848", xid: "e.g. 482913", name: "e.g. before_migration",
  };
  document.getElementById("restore-pitr-target-type")?.addEventListener("change", (e) => {
    const type = e.target.value;
    document.getElementById("restore-pitr-target-time-row").classList.toggle("hidden", type !== "time");
    document.getElementById("restore-pitr-target-value-row").classList.toggle("hidden", type === "time");
    if (type !== "time") {
      document.getElementById("restore-pitr-target-value-label").textContent = PITR_TARGET_LABELS[type];
      document.getElementById("restore-pitr-target-value").placeholder = PITR_TARGET_PLACEHOLDERS[type] || "";
    }
    updateRestoreButtonState();
  });
  document.getElementById("restore-pitr-target-value")?.addEventListener("input", updateRestoreButtonState);

  function pitrRecoveryTargetPayload() {
    const type = document.getElementById("restore-pitr-target-type").value;
    const value = type === "time" ? pitrTargetTimeValue() : document.getElementById("restore-pitr-target-value").value.trim();
    if (!value) return null;
    return {
      type, value,
      action: document.getElementById("restore-pitr-action").value,
      timeline: document.getElementById("restore-pitr-timeline").value.trim() || "latest",
      inclusive: document.getElementById("restore-pitr-inclusive").checked,
    };
  }

  function restoreLogicalMode() {
    return document.getElementById("restore-mode-new").checked ? "new" : "same";
  }
  function restorePhysicalMode() {
    if (document.getElementById("restore-physical-mode-pitr")?.checked) return "pitr";
    return document.getElementById("restore-physical-mode-new").checked ? "new-instance" : "inplace";
  }
  // Sent as-is to the backend, which validates it against the same ISO
  // pattern -- a bare "YYYY-MM-DDTHH:MM" from <input type=datetime-local>
  // (no timezone, no seconds) is padded out to seconds and treated as UTC,
  // matching how the rest of this app already shows/reasons about times.
  function pitrTargetTimeValue() {
    const raw = document.getElementById("restore-pitr-target-time")?.value || "";
    if (!raw) return "";
    return (raw.length === 16 ? raw + ":00" : raw) + "+00";
  }

  function updateRestoreButtonState() {
    const btn = document.getElementById("btn-confirm-restore");
    if (restoreTargetType === "logical") {
      let ok;
      if (restoreLogicalMode() === "new") {
        const name = document.getElementById("restore-new-db-name").value.trim();
        ok = /^[A-Za-z_][A-Za-z0-9_]*$/.test(name);
      } else {
        const dbname = selectedDbMeta?.dbname || "";
        ok = document.getElementById("restore-confirm-input").value === dbname;
      }
      if (ok && restoreScopeType() === "schemas" && !restoreScopeSelection().scope.length) ok = false;
      btn.disabled = !ok;
    } else {
      const mode = restorePhysicalMode();
      if (mode === "new-instance") {
        btn.disabled = false; // non-destructive -- no typed confirmation needed
      } else if (mode === "pitr") {
        btn.disabled = !pitrRecoveryTargetPayload(); // also non-destructive (restores to a new instance)
      } else {
        btn.disabled = document.getElementById("restore-confirm-input-physical").value !== "RESTORE";
      }
    }
  }

  function openRestoreModal(filename) {
    const b = latestBackups.find((x) => x.filename === filename);
    restoreTargetFilename = filename;
    restoreTargetType = b ? b.type : "logical";
    const isPhysical = restoreTargetType !== "logical";
    const dbname = selectedDbMeta?.dbname || "this database";

    document.getElementById("restore-logical-warning").classList.toggle("hidden", isPhysical);
    document.getElementById("restore-physical-warning").classList.toggle("hidden", !isPhysical);

    if (isPhysical) {
      document.getElementById("restore-physical-filename").textContent = filename;
      document.getElementById("restore-physical-filename-2").textContent = filename;
      document.getElementById("restore-confirm-input-physical").value = "";
      document.getElementById("restore-physical-mode-inplace").checked = true;
      document.getElementById("restore-physical-inplace-section").classList.remove("hidden");
      document.getElementById("restore-physical-newinstance-section").classList.add("hidden");
      document.getElementById("restore-physical-pitr-section").classList.add("hidden");
      document.getElementById("restore-pitr-target-type").value = "time";
      document.getElementById("restore-pitr-target-time").value = "";
      document.getElementById("restore-pitr-target-value").value = "";
      document.getElementById("restore-pitr-target-time-row").classList.remove("hidden");
      document.getElementById("restore-pitr-target-value-row").classList.add("hidden");
      document.getElementById("restore-pitr-action").value = "promote";
      document.getElementById("restore-pitr-timeline").value = "latest";
      document.getElementById("restore-pitr-inclusive").checked = true;
      document.getElementById("restore-inplace-tablespace-mapping").value = "";
      document.getElementById("restore-newinstance-tablespace-mapping").value = "";
      document.getElementById("restore-pitr-tablespace-mapping").value = "";
      document.getElementById("restore-physical-mode-pitr-row").classList.toggle("hidden", !pitrAvailable);
    } else {
      document.getElementById("restore-filename").textContent = filename;
      document.getElementById("restore-dbname").textContent = dbname;
      document.getElementById("restore-dbname-2").textContent = dbname;
      document.getElementById("restore-dbname-3").textContent = dbname;
      document.getElementById("restore-dbname-4").textContent = dbname;
      document.getElementById("restore-confirm-input").value = "";
      document.getElementById("restore-new-db-name").value = "";
      document.getElementById("restore-mode-same").checked = true;
      document.getElementById("restore-same-db-section").classList.remove("hidden");
      document.getElementById("restore-new-db-section").classList.add("hidden");

      document.getElementById("restore-scope-select").value = "whole";
      document.getElementById("restore-scope-schemas-row").classList.add("hidden");
      document.getElementById("restore-scope-tables-row").classList.add("hidden");
      document.getElementById("restore-scope-tables-select-all").checked = true;
      restoreAvailableTables = [];
      restoreScopeTables = [];
      updateRestoreScopeWarningText();
      populateRestoreOptionDefaults();
      api(`/api/backup/${encodeURIComponent(filename)}/tables`).then((r) => r.json()).then((data) => {
        restoreAvailableTables = data.tables || [];
        const schemas = Array.from(new Set(restoreAvailableTables.map((q) => q.split(".", 1)[0])));
        document.getElementById("restore-opt-exclude-schemas").innerHTML =
          schemas.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
      }).catch((e) => console.error("backup contents load failed", e));
    }
    document.getElementById("restore-modal-status").innerHTML = "";
    document.getElementById("btn-confirm-restore").textContent = "Restore";
    updateRestoreButtonState();
    restoreModal.classList.remove("hidden");
  }
  function closeRestoreModal() { restoreModal.classList.add("hidden"); restoreTargetFilename = null; restoreTargetType = null; }
  document.getElementById("btn-close-restore").addEventListener("click", closeRestoreModal);
  restoreModal.addEventListener("click", (e) => { if (e.target === restoreModal) closeRestoreModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !restoreModal.classList.contains("hidden")) closeRestoreModal();
  });

  document.querySelectorAll('input[name="restore-target-mode"]').forEach((el) => el.addEventListener("change", () => {
    document.getElementById("restore-same-db-section").classList.toggle("hidden", restoreLogicalMode() !== "same");
    document.getElementById("restore-new-db-section").classList.toggle("hidden", restoreLogicalMode() !== "new");
    updateRestoreButtonState();
  }));
  document.querySelectorAll('input[name="restore-physical-mode"]').forEach((el) => el.addEventListener("change", () => {
    const mode = restorePhysicalMode();
    document.getElementById("restore-physical-inplace-section").classList.toggle("hidden", mode !== "inplace");
    document.getElementById("restore-physical-newinstance-section").classList.toggle("hidden", mode !== "new-instance");
    document.getElementById("restore-physical-pitr-section").classList.toggle("hidden", mode !== "pitr");
    updateRestoreButtonState();
  }));
  document.getElementById("restore-confirm-input").addEventListener("input", updateRestoreButtonState);
  document.getElementById("restore-new-db-name").addEventListener("input", updateRestoreButtonState);
  document.getElementById("restore-confirm-input-physical").addEventListener("input", updateRestoreButtonState);
  document.getElementById("restore-pitr-target-time").addEventListener("input", updateRestoreButtonState);

  document.getElementById("btn-confirm-restore").addEventListener("click", async () => {
    if (!restoreTargetFilename) return;
    const btn = document.getElementById("btn-confirm-restore");
    const statusEl = document.getElementById("restore-modal-status");
    let url, body;
    if (restoreTargetType === "logical") {
      const { scopeType, scope } = restoreScopeSelection();
      url = "/api/backup/restore";
      body = { filename: restoreTargetFilename, scope_type: scopeType, scope, options: restoreOptionsPayload() };
      if (restoreLogicalMode() === "new") {
        body.target_dbname = document.getElementById("restore-new-db-name").value.trim();
      }
    } else if (restorePhysicalMode() === "new-instance") {
      url = "/api/backup/new-instance-restore";
      body = { filename: restoreTargetFilename, tablespace_mapping: parseTablespaceMapping("restore-newinstance-tablespace-mapping") };
    } else if (restorePhysicalMode() === "pitr") {
      url = "/api/backup/new-instance-restore";
      body = {
        filename: restoreTargetFilename, recovery_target: pitrRecoveryTargetPayload(),
        tablespace_mapping: parseTablespaceMapping("restore-pitr-tablespace-mapping"),
      };
    } else {
      url = "/api/backup/physical-restore";
      body = { filename: restoreTargetFilename, tablespace_mapping: parseTablespaceMapping("restore-inplace-tablespace-mapping") };
    }
    btn.disabled = true; btn.textContent = "Restoring...";
    statusEl.innerHTML = "";
    try {
      const resp = await apiPost(url, body);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      closeRestoreModal();
      pollBackupJob(data.job_id, () => { if (url === "/api/backup/new-instance-restore") loadTempInstances(); });
    } catch (e) {
      statusEl.className = "add-db-status status-err";
      statusEl.textContent = "Failed: " + e.message;
      btn.disabled = false; btn.textContent = "Restore";
    }
  });

  // ------------------------------------------------------- temporary instances
  async function loadTempInstances() {
    try {
      const instances = await api("/api/backup/temp-instances").then((r) => r.json());
      const tbody = document.querySelector("#table-temp-instances tbody");
      tbody.innerHTML = instances.map((inst) => `
        <tr>
          <td class="mono">${esc(inst.name)}</td>
          <td class="mono">${esc(inst.backup_filename)}</td>
          <td><span class="badge ${inst.ready ? "badge-up" : "badge-muted"}">${inst.ready ? "ready" : esc(inst.phase || "starting")}</span></td>
          <td class="mono">${esc(inst.host)}:${inst.port}</td>
          <td>
            <button type="button" class="btn btn-ghost btn-sm" data-promote-instance="${esc(inst.name)}" ${inst.ready ? "" : "disabled"}>Promote</button>
            <button type="button" class="btn-icon-danger" data-delete-instance="${esc(inst.name)}">Delete</button>
          </td>
        </tr>`).join("") || `<tr><td colspan="5" class="empty-note">No temporary instances right now.</td></tr>`;

      tbody.querySelectorAll("[data-promote-instance]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const name = btn.dataset.promoteInstance;
          if (!confirm(`Promote ${name}? If it's paused replaying WAL toward a recovery target, this brings it fully online (read/write) right now, wherever it's gotten to.`)) return;
          btn.disabled = true;
          try {
            const resp = await apiPost(`/api/backup/temp-instances/${encodeURIComponent(name)}/promote`, {});
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || resp.statusText);
            alert(`${name} promoted.`);
          } catch (e) {
            alert("Failed to promote: " + e.message);
            btn.disabled = false;
          }
        });
      });
      tbody.querySelectorAll("[data-delete-instance]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const name = btn.dataset.deleteInstance;
          if (!confirm(`Delete temporary instance ${name}? This removes its pod, service, and storage.`)) return;
          try {
            const resp = await fetch(BASE + `/api/backup/temp-instances/${encodeURIComponent(name)}`, {
              method: "DELETE", credentials: "same-origin",
            });
            if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
            loadTempInstances();
          } catch (e) { alert("Failed to delete: " + e.message); }
        });
      });
    } catch (e) { console.error("temp instances load failed", e); }
  }

  // ------------------------------------------------------------ WAL mining
  const WALMINE_RMGR_NAMES = [
    "XLOG", "Transaction", "Storage", "CLOG", "Database", "Tablespace", "MultiXact", "RelMap", "Standby",
    "Heap2", "Heap", "Btree", "Hash", "Gin", "Gist", "Sequence", "SPGist", "BRIN", "CommitTs",
    "ReplicationOrigin", "Generic", "LogicalMessage",
  ]; // confirmed against the server's own `pg_waldump --rmgr=list`
  (function populateWalmineRmgrOptions() {
    const select = document.getElementById("walmine-rmgr");
    if (!select) return;
    select.innerHTML = '<option value="">Any</option>' +
      WALMINE_RMGR_NAMES.map((r) => `<option value="${r}">${r}</option>`).join("");
  })();

  function populateWalmineBackupOptions() {
    const select = document.getElementById("walmine-backup-select");
    if (!select) return;
    const physicalBackups = latestBackups.filter((b) => b.type === "physical" || b.type === "incremental");
    const previous = select.value;
    select.innerHTML = physicalBackups.map((b) =>
      `<option value="${esc(b.filename)}">${esc(b.filename)} (${BACKUP_TYPE_LABELS[b.type]})</option>`
    ).join("") || `<option value="">No physical/incremental backup yet -- take one first</option>`;
    if (physicalBackups.some((b) => b.filename === previous)) select.value = previous;
  }

  document.getElementById("walmine-source")?.addEventListener("change", (e) => {
    const isLive = e.target.value === "live";
    if (isLive && !pitrAvailable) {
      alert("Mining the live WAL archive isn't configured on this deployment -- mining a specific "
          + "physical/incremental backup's own WAL still works.");
      e.target.value = "backup";
      return;
    }
    document.getElementById("walmine-backup-row").classList.toggle("hidden", isLive);
    document.getElementById("walmine-segment-limit-row").classList.toggle("hidden", !isLive);
  });

  function pollWalmineJob(jobId) {
    const summaryEl = document.getElementById("walmine-job-summary");
    const logEl = document.getElementById("walmine-job-log");
    const tbody = document.querySelector("#table-walmine-results tbody");
    document.getElementById("walmine-job-section").classList.remove("hidden");
    const tick = async () => {
      try {
        const job = await api(`/api/backup/jobs/${jobId}`).then((r) => r.json());
        const elapsed = (job.finished_at || Date.now() / 1000) - job.started_at;
        summaryEl.textContent = `WAL search: ${job.status} (${fmtDuration(elapsed)} elapsed)` + (job.error ? ` — ${job.error}` : "");
        logEl.textContent = (job.log || []).join("\n");
        logEl.scrollTop = logEl.scrollHeight;
        if (job.status === "running") { setTimeout(tick, 1500); return; }
        if (job.status === "done" && job.result) {
          const records = job.result.records || [];
          tbody.innerHTML = records.map((r) => `
            <tr>
              <td class="mono">${esc(r.lsn)}</td>
              <td class="mono">${r.xid}</td>
              <td>${esc(r.rmgr)}</td>
              <td>${esc(r.operation)}</td>
              <td class="mono">${esc((r.tables || []).join(", "))}</td>
              <td class="mono" title="${esc(r.desc)}">${esc(r.desc)}</td>
            </tr>`).join("") || `<tr><td colspan="6" class="empty-note">No matching WAL records.</td></tr>`;
          if (job.result.truncated) {
            summaryEl.textContent += ` — showing the first ${records.length} of ${job.result.total_matched} matches.`;
          }
        } else if (job.status === "error") {
          tbody.innerHTML = `<tr><td colspan="6" class="empty-note">Search failed -- see log above.</td></tr>`;
        }
      } catch (e) { console.error("walmine job poll failed", e); }
    };
    tick();
  }

  document.getElementById("btn-run-walmine")?.addEventListener("click", async () => {
    const btn = document.getElementById("btn-run-walmine");
    const source = document.getElementById("walmine-source").value;
    const xid = document.getElementById("walmine-xid").value.trim();
    const rmgr = document.getElementById("walmine-rmgr").value;
    const tableContains = document.getElementById("walmine-table-contains").value.trim();
    let url, body;
    if (source === "live") {
      url = "/api/backup/walmine/live";
      body = {
        segment_limit: Number(document.getElementById("walmine-segment-limit").value) || 10,
        xid, rmgr, table_contains: tableContains,
      };
    } else {
      const filename = document.getElementById("walmine-backup-select").value;
      if (!filename) { alert("No physical/incremental backup available to mine -- take one first."); return; }
      url = "/api/backup/walmine/backup";
      body = { filename, xid, rmgr, table_contains: tableContains };
    }
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Searching...";
    document.querySelector("#table-walmine-results tbody").innerHTML = "";
    try {
      const resp = await apiPost(url, body);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      pollWalmineJob(data.job_id);
    } catch (e) {
      alert("Failed to start WAL search: " + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  });

  // -------------------------------------------------------- backup schedules
  const FREQUENCY_LABELS = { hourly: "Hourly", daily: "Daily", weekly: "Weekly" };
  async function loadBackupSchedules() {
    try {
      const schedules = await api("/api/backup/schedules").then((r) => r.json());
      const tbody = document.querySelector("#table-backup-schedules tbody");
      tbody.innerHTML = schedules.map((s) => `
        <tr>
          <td>${BACKUP_TYPE_LABELS[s.backup_type] || esc(s.backup_type)}</td>
          <td>${FREQUENCY_LABELS[s.frequency] || esc(s.frequency)}</td>
          <td>${fmtNum(s.retention_days)} days</td>
          <td>${s.last_run_at ? new Date(s.last_run_at).toLocaleString() : "never"}${s.last_status ? ` (${esc(s.last_status)})` : ""}</td>
          <td><input type="checkbox" data-toggle-schedule="${s.id}" ${s.enabled ? "checked" : ""} title="Enabled"></td>
          <td><button type="button" class="btn-icon-danger" data-delete-schedule="${s.id}">Delete</button></td>
        </tr>`).join("") || `<tr><td colspan="6" class="empty-note">No backup schedules yet -- add one below.</td></tr>`;

      tbody.querySelectorAll("[data-toggle-schedule]").forEach((el) => {
        el.addEventListener("change", async () => {
          try {
            await apiMethod("POST", `/api/backup/schedules/${el.dataset.toggleSchedule}/enabled`, { enabled: el.checked });
          } catch (e) { alert("Failed to update: " + e.message); loadBackupSchedules(); }
        });
      });
      tbody.querySelectorAll("[data-delete-schedule]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          if (!confirm("Delete this backup schedule? Existing backups it already made are not deleted.")) return;
          try {
            await apiMethod("DELETE", `/api/backup/schedules/${btn.dataset.deleteSchedule}`);
            loadBackupSchedules();
          } catch (e) { alert("Failed to delete: " + e.message); }
        });
      });
    } catch (e) { console.error("backup schedules load failed", e); }
  }
  document.getElementById("btn-add-schedule")?.addEventListener("click", async () => {
    const btn = document.getElementById("btn-add-schedule");
    const backup_type = document.getElementById("schedule-type-select").value;
    const frequency = document.getElementById("schedule-frequency-select").value;
    const retention_days = parseInt(document.getElementById("schedule-retention-input").value, 10);
    if (!retention_days || retention_days < 1) { alert("Retention must be at least 1 day."); return; }
    btn.disabled = true;
    try {
      await apiPost("/api/backup/schedules", { backup_type, frequency, retention_days });
      loadBackupSchedules();
    } catch (e) {
      alert("Failed to add schedule: " + e.message);
    } finally {
      btn.disabled = false;
    }
  });

  dtabLoaders.backup = () => {
    loadBackup();
    checkPhysicalRestoreAvailable().then(loadBackupList);
    loadTempInstances();
    loadBackupSchedules();
  };

  // -------------------------------------------------------------- security
  let cachedRoleNames = []; // populated by loadSecurity; feeds membership/reassign selects

  function fmtValidUntil(v) {
    if (!v) return "never";
    const d = new Date(v);
    if (isNaN(d.getTime())) return String(v);
    // Postgres represents "no expiry" as the max timestamp -- don't show that as a real date.
    return d.getFullYear() > 9000 ? "never" : d.toLocaleString();
  }

  // Collapses the 5 separate yes/no attribute columns the Roles table used
  // to have into one glanceable set of badges -- only the attributes that
  // are actually set show up, with superuser/elevated ones color-coded so
  // the risky roles jump out instead of being buried in a wall of checkmarks.
  function roleAttrBadges(r) {
    const badges = [];
    if (r.rolsuper) badges.push(`<span class="badge badge-attr-super">SUPERUSER</span>`);
    if (r.rolcreatedb) badges.push(`<span class="badge badge-attr-elevated">CREATEDB</span>`);
    if (r.rolcreaterole) badges.push(`<span class="badge badge-attr-elevated">CREATEROLE</span>`);
    if (r.rolbypassrls) badges.push(`<span class="badge badge-attr-elevated">BYPASSRLS</span>`);
    if (r.rolreplication) badges.push(`<span class="badge badge-attr-replication">REPLICATION</span>`);
    if (!r.rolcanlogin) badges.push(`<span class="badge badge-attr-nologin">NOLOGIN</span>`);
    return badges.length ? `<div class="chip-row">${badges.join("")}</div>` : `<span class="cell-empty">plain role</span>`;
  }
  // Only shows connection-limit / expiry when they're actually set to
  // something non-default -- "unlimited" and "never" on every single row
  // was most of why this table felt so wide.
  function roleLimitsCell(r) {
    const parts = [];
    if (r.rolconnlimit >= 0) parts.push(`<span class="chip">${r.rolconnlimit} conn</span>`);
    const vu = fmtValidUntil(r.rolvaliduntil);
    if (vu !== "never") parts.push(`<span class="chip" title="${esc(vu)}">until ${esc(vu)}</span>`);
    return parts.length ? `<div class="chip-row">${parts.join("")}</div>` : `<span class="cell-empty">—</span>`;
  }
  function roleMemberOfCell(list) {
    if (!list || !list.length) return `<span class="cell-empty">—</span>`;
    const shown = list.slice(0, 2).map((m) => `<span class="chip">${esc(m)}</span>`).join("");
    const rest = list.length - 2;
    const more = rest > 0 ? `<span class="chip chip-more" title="${esc(list.join(", "))}">+${rest} more</span>` : "";
    return `<div class="chip-row">${shown}${more}</div>`;
  }

  async function loadSecurity() {
    try {
      const s = await api("/api/security").then((r) => r.json());
      const set = (key, val) => {
        const el = document.querySelector(`[data-sstat="${key}"]`);
        if (el) el.textContent = val;
      };
      set("ssl_enabled", s.ssl_enabled ? "yes" : "no");
      set("password_encryption", s.password_encryption ?? "—");
      set("superuser_count", fmtNum(s.superuser_count));
      set("role_count", fmtNum(s.role_count));

      cachedRoleNames = (s.roles || []).map((r) => r.rolname);

      const roleRows = document.querySelector("#table-roles tbody");
      roleRows.innerHTML = (s.roles || []).map((r) => `
        <tr>
          <td class="mono">${esc(r.rolname)}</td>
          <td>${roleAttrBadges(r)}</td>
          <td>${roleLimitsCell(r)}</td>
          <td>${roleMemberOfCell(r.member_of)}</td>
          <td>${r.active_connections > 0 ? `<span class="badge badge-up">${r.active_connections}</span>` : `<span class="cell-empty">0</span>`}</td>
          <td class="row-actions">
            <button type="button" class="btn-icon-sm" title="Edit role" aria-label="Edit role" data-edit-role="${esc(r.rolname)}">E</button>
            <button type="button" class="btn-icon-sm" title="Grants &amp; sessions" aria-label="Grants and sessions" data-grants-role="${esc(r.rolname)}">G</button>
            <button type="button" class="btn-icon-sm btn-icon-danger-sm" title="Drop role" aria-label="Drop role" data-delete-role="${esc(r.rolname)}">D</button>
          </td>
        </tr>`).join("") || `<tr><td colspan="6" class="empty-note">No roles found.</td></tr>`;

      roleRows.querySelectorAll("[data-edit-role]").forEach((btn) =>
        btn.addEventListener("click", () => openRoleModal(btn.dataset.editRole)));
      roleRows.querySelectorAll("[data-grants-role]").forEach((btn) =>
        btn.addEventListener("click", () => openRoleGrantsModal(btn.dataset.grantsRole)));
      roleRows.querySelectorAll("[data-delete-role]").forEach((btn) =>
        btn.addEventListener("click", () => openRoleDeleteModal(btn.dataset.deleteRole)));

      const hbaNote = document.getElementById("hba-unavailable-note");
      const hbaRows = document.querySelector("#table-hba tbody");
      if (s.hba_rules_error) {
        hbaNote.classList.remove("hidden");
        hbaNote.textContent = s.hba_rules_error;
        hbaRows.innerHTML = "";
        latestHbaRules = [];
      } else {
        hbaNote.classList.add("hidden");
        latestHbaRules = s.hba_rules || [];
        hbaRows.innerHTML = latestHbaRules.map((r) => `
          <tr>
            <td class="mono">${r.line_number}</td>
            <td>${esc(r.type)}</td>
            <td class="mono">${esc(r.database ? r.database.join(",") : "")}</td>
            <td class="mono">${esc(r.user_name ? r.user_name.join(",") : "")}</td>
            <td class="mono">${esc(r.address || "")}</td>
            <td class="mono ${r.auth_method === "trust" ? "stat-bad" : ""}">${esc(r.auth_method || "")}</td>
            <td>
              <button type="button" class="btn-icon" data-edit-hba="${r.line_number}">Edit</button>
              <button type="button" class="btn-icon-danger" data-delete-hba="${r.line_number}">Delete</button>
            </td>
          </tr>`).join("") || `<tr><td colspan="7" class="empty-note">No rules readable.</td></tr>`;

        hbaRows.querySelectorAll("[data-edit-hba]").forEach((btn) => {
          btn.addEventListener("click", () => startEditHbaRule(Number(btn.dataset.editHba)));
        });
        hbaRows.querySelectorAll("[data-delete-hba]").forEach((btn) => {
          btn.addEventListener("click", () => deleteHbaRule(Number(btn.dataset.deleteHba)));
        });
      }

      const extRows = document.querySelector("#table-extensions tbody");
      extRows.innerHTML = (s.extensions || []).map((x) => `
        <tr><td class="mono">${esc(x.extname)}</td><td class="mono">${esc(x.extversion)}</td></tr>
      `).join("") || `<tr><td colspan="2" class="empty-note">No extensions installed.</td></tr>`;
    } catch (e) { console.error("security load failed", e); }
  }
  dtabLoaders.security = loadSecurity;

  document.getElementById("hba-type-select")?.addEventListener("change", (e) => {
    document.getElementById("hba-address-row").classList.toggle("hidden", e.target.value === "local");
  });

  // -------------------------------------------------- pg_hba edit/delete
  let latestHbaRules = [];
  let hbaEditingRule = null; // the rule object being edited, or null in "add" mode

  function setHbaFormMode(editing) {
    document.getElementById("hba-edit-banner").classList.toggle("hidden", !editing);
    document.getElementById("btn-add-hba-rule").textContent = editing ? "Save changes" : "Add rule";
    if (editing) document.getElementById("hba-edit-line-number").textContent = hbaEditingRule.line_number;
  }

  function startEditHbaRule(lineNumber) {
    const rule = latestHbaRules.find((r) => r.line_number === lineNumber);
    if (!rule) return;
    hbaEditingRule = rule;
    document.getElementById("hba-type-select").value = rule.type;
    document.getElementById("hba-address-row").classList.toggle("hidden", rule.type === "local");
    document.getElementById("hba-database-input").value = (rule.database || []).join(",");
    document.getElementById("hba-user-input").value = (rule.user_name || []).join(",");
    document.getElementById("hba-address-input").value = rule.address || "";
    document.getElementById("hba-auth-select").value = ["scram-sha-256", "md5", "cert"].includes(rule.auth_method) ? rule.auth_method : "scram-sha-256";
    setHbaFormMode(true);
    document.getElementById("hba-type-select").scrollIntoView({ behavior: "smooth", block: "center" });
  }
  document.getElementById("btn-cancel-hba-edit")?.addEventListener("click", () => {
    hbaEditingRule = null;
    setHbaFormMode(false);
  });

  async function deleteHbaRule(lineNumber) {
    const rule = latestHbaRules.find((r) => r.line_number === lineNumber);
    if (!rule) return;
    if (!confirm(`Remove pg_hba.conf line ${lineNumber} (${rule.type} ${(rule.database||[]).join(",")} ${(rule.user_name||[]).join(",")} ${rule.address||""} ${rule.auth_method})?\n\nThis can stop future connections that matched this rule, including this app's own -- it won't drop connections already open.`)) return;
    try {
      const resp = await apiMethod("DELETE", `/api/security/hba-rules/${lineNumber}`, { expected: rule });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      if (hbaEditingRule && hbaEditingRule.line_number === lineNumber) {
        hbaEditingRule = null;
        setHbaFormMode(false);
      }
      loadSecurity();
    } catch (e) {
      alert("Failed to delete rule: " + e.message);
    }
  }

  document.getElementById("btn-add-hba-rule")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const resultBox = document.getElementById("hba-add-result");
    const payload = {
      type: document.getElementById("hba-type-select").value,
      database: document.getElementById("hba-database-input").value,
      user_name: document.getElementById("hba-user-input").value,
      address: document.getElementById("hba-address-input").value,
      auth_method: document.getElementById("hba-auth-select").value,
    };
    const editing = !!hbaEditingRule;
    btn.disabled = true; btn.textContent = editing ? "Saving..." : "Adding...";
    resultBox.classList.add("hidden");
    try {
      let resp;
      if (editing) {
        resp = await apiMethod("PUT", `/api/security/hba-rules/${hbaEditingRule.line_number}`, { ...payload, expected: hbaEditingRule });
      } else {
        resp = await apiPost("/api/security/hba-rules", payload);
      }
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      resultBox.classList.remove("hidden");
      if (data.ok) {
        resultBox.textContent = editing
          ? `Saved -- now line ${data.added_line_number} (old line ${data.deleted_line_number} removed).`
          : `Added as line ${data.line_number}: ${data.line}`;
        resultBox.classList.remove("stat-bad");
      } else {
        resultBox.textContent = `Added, but Postgres rejected it: ${data.error || "unknown error"}`;
        resultBox.classList.add("stat-bad");
      }
      hbaEditingRule = null;
      setHbaFormMode(false);
      loadSecurity();
    } catch (e2) {
      resultBox.classList.remove("hidden");
      resultBox.classList.add("stat-bad");
      resultBox.textContent = `Failed to ${editing ? "save" : "add"} rule: ` + e2.message;
    } finally {
      btn.disabled = false; btn.textContent = hbaEditingRule ? "Save changes" : "Add rule";
    }
  });

  // ------------------------------------------------------------ extensions
  let extensionCatalogCache = [];

  function renderExtensionCatalog() {
    const categoryFilter = document.getElementById("ext-category-filter").value;
    const textFilter = document.getElementById("ext-filter").value.trim().toLowerCase();
    const availableOnly = document.getElementById("ext-show-available-only").checked;

    const rows = extensionCatalogCache.filter((x) => {
      if (categoryFilter && x.category !== categoryFilter) return false;
      if (availableOnly && !x.available) return false;
      if (textFilter && !x.name.toLowerCase().includes(textFilter) && !x.description.toLowerCase().includes(textFilter)) return false;
      return true;
    });

    const tbody = document.querySelector("#table-extension-catalog tbody");
    tbody.innerHTML = rows.map((x) => {
      let statusHtml, actionHtml = "";
      if (x.installed) {
        statusHtml = `<span class="badge badge-up">enabled (v${esc(x.installed_version)})</span>`;
        if (x.update_available) {
          statusHtml += ` <span class="badge badge-attr-elevated" title="This server's files support v${esc(x.default_version)} -- this database hasn't upgraded to it yet">v${esc(x.default_version)} available</span>`;
        }
        actionHtml = x.update_available
          ? `<button type="button" class="btn btn-primary btn-sm" data-update-ext="${esc(x.name)}">Update</button> <button type="button" class="btn btn-ghost btn-sm" data-disable-ext="${esc(x.name)}">Disable</button>`
          : `<button type="button" class="btn btn-ghost btn-sm" data-disable-ext="${esc(x.name)}">Disable</button>`;
      } else if (x.available) {
        statusHtml = `<span class="badge badge-muted">available</span>`;
        actionHtml = `<button type="button" class="btn btn-primary btn-sm" data-enable-ext="${esc(x.name)}">Enable</button>`;
      } else {
        statusHtml = `<span class="badge badge-muted" title="Not present on this server -- needs adding to its Postgres image first">not available here</span>`;
      }
      return `
        <tr>
          <td class="mono">${esc(x.name)}${x.bundled === false ? ` <span class="chip" title="Not a core/contrib module -- needs a custom-built Postgres image, like this app's own postgres-image/Dockerfile">custom build</span>` : ""}</td>
          <td>${esc(x.category)}</td>
          <td>${esc(x.description)}</td>
          <td>${statusHtml}</td>
          <td>${actionHtml}</td>
        </tr>`;
    }).join("") || `<tr><td colspan="5" class="empty-note">No extensions match this filter.</td></tr>`;

    tbody.querySelectorAll("[data-enable-ext]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.enableExt;
        btn.disabled = true; btn.textContent = "Enabling...";
        try {
          const resp = await apiPost(`/api/extensions/${encodeURIComponent(name)}/enable`);
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.detail || resp.statusText);
          loadExtensionCatalog();
        } catch (e) {
          alert("Failed to enable: " + e.message);
          btn.disabled = false; btn.textContent = "Enable";
        }
      });
    });
    tbody.querySelectorAll("[data-update-ext]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.updateExt;
        btn.disabled = true; btn.textContent = "Updating...";
        try {
          const resp = await apiPost(`/api/extensions/${encodeURIComponent(name)}/update`);
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.detail || resp.statusText);
          loadExtensionCatalog();
        } catch (e) {
          alert("Failed to update: " + e.message);
          btn.disabled = false; btn.textContent = "Update";
        }
      });
    });
    tbody.querySelectorAll("[data-disable-ext]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.disableExt;
        if (!confirm(`Disable ${name}? Anything depending on it (indexes, columns using its types, ...) must be removed first, or this will fail.`)) return;
        btn.disabled = true; btn.textContent = "Disabling...";
        try {
          const resp = await apiPost(`/api/extensions/${encodeURIComponent(name)}/disable`);
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.detail || resp.statusText);
          loadExtensionCatalog();
        } catch (e) {
          alert("Failed to disable: " + e.message);
          btn.disabled = false; btn.textContent = "Disable";
        }
      });
    });
  }

  async function loadExtensionCatalog() {
    document.getElementById("ext-dbname").textContent = selectedDbMeta?.dbname || "this database";
    try {
      extensionCatalogCache = await api("/api/extensions").then((r) => r.json());
      const categorySelect = document.getElementById("ext-category-filter");
      const categories = [...new Set(extensionCatalogCache.map((x) => x.category))].sort();
      const currentValue = categorySelect.value;
      categorySelect.innerHTML = `<option value="">All categories</option>` +
        categories.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
      categorySelect.value = currentValue;
      renderExtensionCatalog();
    } catch (e) { console.error("extension catalog load failed", e); }
  }
  document.getElementById("ext-category-filter")?.addEventListener("change", renderExtensionCatalog);
  document.getElementById("ext-filter")?.addEventListener("input", renderExtensionCatalog);
  document.getElementById("ext-show-available-only")?.addEventListener("change", renderExtensionCatalog);
  dtabLoaders.extensions = loadExtensionCatalog;

  document.getElementById("btn-security-review")?.addEventListener("click", (e) =>
    runAssessment(e.currentTarget, "ai-security-card", "✦ Analyzing...", "✦ AI Security Review", "/api/ai/security-review"));

  // ---------------------------------------------------- role create/edit modal
  const roleModal = document.getElementById("role-modal");
  let roleModalMode = "create"; // "create" | "edit"
  let roleModalOriginalName = null;

  function setRoleStatus(kind, text) {
    const el = document.getElementById("role-modal-status");
    el.className = "add-db-status" + (kind ? ` status-${kind}` : "");
    el.textContent = text;
  }
  function populateMemberOfSelect(selected) {
    const sel = document.getElementById("role-memberof-select");
    sel.innerHTML = cachedRoleNames.map((name) =>
      `<option value="${esc(name)}" ${selected && selected.includes(name) ? "selected" : ""}>${esc(name)}</option>`).join("");
  }
  function openRoleModal(rolname) {
    roleModalMode = rolname ? "edit" : "create";
    roleModalOriginalName = rolname || null;
    setRoleStatus(null, "");
    document.getElementById("role-modal-title").textContent = rolname ? `Edit role ${rolname}` : "Create role";
    document.getElementById("role-name-input").value = rolname || "";
    document.getElementById("role-name-input").disabled = !!rolname;
    document.getElementById("role-rename-row").classList.toggle("hidden", !rolname);
    document.getElementById("role-rename-input").value = "";
    document.getElementById("role-password-input").value = "";
    document.getElementById("role-password-hint-create").classList.toggle("hidden", !!rolname);
    document.getElementById("role-password-hint-edit").classList.toggle("hidden", !rolname);
    document.getElementById("role-self-warning").classList.add("hidden");
    document.getElementById("btn-save-role").disabled = false;

    if (rolname) {
      populateMemberOfSelect([]);
      api(`/api/roles/${encodeURIComponent(rolname)}`).then((r) => r.json()).then((detail) => {
        document.getElementById("role-connlimit-input").value = detail.rolconnlimit;
        document.getElementById("role-validuntil-input").value = "";
        document.getElementById("role-attr-login").checked = detail.rolcanlogin;
        document.getElementById("role-attr-inherit").checked = detail.rolinherit;
        document.getElementById("role-attr-createdb").checked = detail.rolcreatedb;
        document.getElementById("role-attr-createrole").checked = detail.rolcreaterole;
        document.getElementById("role-attr-superuser").checked = detail.rolsuper;
        document.getElementById("role-attr-replication").checked = detail.rolreplication;
        document.getElementById("role-attr-bypassrls").checked = detail.rolbypassrls;
        populateMemberOfSelect((detail.member_of || []).map((m) => m.rolname));
        if (detail.is_self) {
          document.getElementById("role-self-warning").classList.remove("hidden");
          document.getElementById("role-attr-login").disabled = true;
        } else {
          document.getElementById("role-attr-login").disabled = false;
        }
      }).catch((e) => setRoleStatus("err", "Failed to load role: " + e.message));
    } else {
      document.getElementById("role-connlimit-input").value = -1;
      document.getElementById("role-validuntil-input").value = "";
      document.getElementById("role-attr-login").checked = true;
      document.getElementById("role-attr-login").disabled = false;
      document.getElementById("role-attr-inherit").checked = true;
      document.getElementById("role-attr-createdb").checked = false;
      document.getElementById("role-attr-createrole").checked = false;
      document.getElementById("role-attr-superuser").checked = false;
      document.getElementById("role-attr-replication").checked = false;
      document.getElementById("role-attr-bypassrls").checked = false;
      populateMemberOfSelect([]);
    }
    roleModal.classList.remove("hidden");
  }
  function closeRoleModal() { roleModal.classList.add("hidden"); }
  document.getElementById("btn-open-create-role")?.addEventListener("click", () => openRoleModal(null));
  document.getElementById("btn-close-role")?.addEventListener("click", closeRoleModal);
  roleModal?.addEventListener("click", (e) => { if (e.target === roleModal) closeRoleModal(); });

  document.getElementById("btn-save-role")?.addEventListener("click", async () => {
    const memberOf = Array.from(document.getElementById("role-memberof-select").selectedOptions).map((o) => o.value);
    const payload = {
      can_login: document.getElementById("role-attr-login").checked,
      inherit: document.getElementById("role-attr-inherit").checked,
      createdb: document.getElementById("role-attr-createdb").checked,
      createrole: document.getElementById("role-attr-createrole").checked,
      superuser: document.getElementById("role-attr-superuser").checked,
      replication: document.getElementById("role-attr-replication").checked,
      bypassrls: document.getElementById("role-attr-bypassrls").checked,
      connlimit: document.getElementById("role-connlimit-input").value,
      member_of: memberOf,
    };
    // Only send valid_until when the admin actually typed something -- the field
    // starts blank on edit (we don't round-trip the current expiry into it), and
    // alter_role() treats a *present* valid_until key as "set it", defaulting to
    // infinity for an empty value. Omitting the key entirely leaves it untouched.
    const validUntil = document.getElementById("role-validuntil-input").value.trim();
    if (validUntil) payload.valid_until = validUntil;
    const password = document.getElementById("role-password-input").value;
    if (password) payload.password = password;

    const btn = document.getElementById("btn-save-role");
    btn.disabled = true;
    setRoleStatus(null, "Saving...");
    try {
      let resp;
      if (roleModalMode === "create") {
        payload.rolname = document.getElementById("role-name-input").value.trim();
        if (!payload.rolname) throw new Error("Role name is required.");
        resp = await apiPost("/api/roles", payload);
      } else {
        const newName = document.getElementById("role-rename-input").value.trim();
        if (newName) payload.new_name = newName;
        resp = await apiMethod("PUT", `/api/roles/${encodeURIComponent(roleModalOriginalName)}`, payload);
      }
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      closeRoleModal();
      loadSecurity();
    } catch (e) {
      setRoleStatus("err", "Failed: " + e.message);
    } finally {
      btn.disabled = false;
    }
  });

  // --------------------------------------------------------- role grants modal
  const roleGrantsModal = document.getElementById("role-grants-modal");
  const GRANT_PRIV_OPTIONS = {
    database: ["CONNECT", "CREATE", "TEMPORARY"],
    schema: ["USAGE", "CREATE"],
    table: ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"],
    sequence: ["USAGE", "SELECT", "UPDATE"],
    all_tables_in_schema: ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"],
    all_sequences_in_schema: ["USAGE", "SELECT", "UPDATE"],
  };
  function setRoleGrantsStatus(kind, text) {
    const el = document.getElementById("role-grants-modal-status");
    el.className = "add-db-status" + (kind ? ` status-${kind}` : "");
    el.textContent = text;
  }
  function renderGrantPrivilegeOptions() {
    const type = document.getElementById("grant-object-type").value;
    const opts = GRANT_PRIV_OPTIONS[type] || [];
    document.getElementById("grant-privileges-options").innerHTML =
      `<label>Privileges</label><div class="form-row-split">` +
      opts.map((p) => `<label class="checkbox-label"><input type="checkbox" class="grant-priv-cb" value="${p}"><span>${p}</span></label>`).join("") +
      `<label class="checkbox-label"><input type="checkbox" class="grant-priv-cb" value="ALL"><span>ALL</span></label></div>`;
    const nameRow = document.getElementById("grant-object-name-row");
    const nameInput = document.getElementById("grant-object-name");
    if (type === "database") {
      nameRow.classList.add("hidden");
    } else {
      nameRow.classList.remove("hidden");
      nameInput.placeholder = (type === "schema" || type === "all_tables_in_schema" || type === "all_sequences_in_schema")
        ? "schema name, e.g. public" : "schema.object, e.g. public.employees";
    }
  }
  document.getElementById("grant-object-type")?.addEventListener("change", renderGrantPrivilegeOptions);

  async function openRoleGrantsModal(rolname) {
    document.getElementById("role-grants-rolname").textContent = rolname;
    document.getElementById("grant-object-name").value = "";
    document.getElementById("grant-with-option").checked = false;
    renderGrantPrivilegeOptions();
    setRoleGrantsStatus(null, "");
    roleGrantsModal.classList.remove("hidden");

    try {
      const detail = await api(`/api/roles/${encodeURIComponent(rolname)}`).then((r) => r.json());
      const sessRows = document.querySelector("#table-role-sessions tbody");
      sessRows.innerHTML = (detail.active_sessions || []).map((s) => `
        <tr>
          <td class="mono">${s.pid}</td><td class="mono">${esc(s.datname || "")}</td>
          <td class="mono">${esc(s.client_addr || "local")}</td><td>${esc(s.state || "")}</td>
          <td class="mono">${fmtDate(s.backend_start)}</td>
          <td><button type="button" class="btn-icon-danger" data-kill-session="${s.pid}">Kill</button></td>
        </tr>`).join("") || `<tr><td colspan="6" class="empty-note">No active sessions.</td></tr>`;
      sessRows.querySelectorAll("[data-kill-session]").forEach((btn) =>
        btn.addEventListener("click", async () => {
          await killConnection(Number(btn.dataset.killSession), btn);
          openRoleGrantsModal(rolname);
        }));

      renderRoleGrants(rolname, detail.grants || []);
    } catch (e) {
      setRoleGrantsStatus("err", "Failed to load: " + e.message);
    }
  }
  function renderRoleGrants(rolname, grants) {
    const tbody = document.querySelector("#table-role-grants tbody");
    tbody.innerHTML = grants.map((g) => `
      <tr>
        <td class="mono">${esc(g.objtype)}</td><td class="mono">${esc(g.schema || "")}</td>
        <td class="mono">${esc(g.objname)}</td><td class="mono">${esc(g.privilege)}</td>
        <td>${g.grantable ? "✓" : ""}</td>
        <td><button type="button" class="btn-icon-danger" data-revoke-grant="${esc(JSON.stringify(g))}">Revoke</button></td>
      </tr>`).join("") || `<tr><td colspan="6" class="empty-note">No explicit object grants for this role.</td></tr>`;

    tbody.querySelectorAll("[data-revoke-grant]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const g = JSON.parse(btn.dataset.revokeGrant);
        const label = `${g.privilege} on ${g.objtype} ${g.schema ? g.schema + "." : ""}${g.objname}`;
        if (!confirm(`Revoke ${label} from ${rolname}?`)) return;
        const objectType = g.objtype;
        const objectName = objectType === "database" ? "" : (g.schema ? `${g.schema}.${g.objname}` : g.objname);
        try {
          const resp = await apiMethod("DELETE", `/api/roles/${encodeURIComponent(rolname)}/grants`, {
            object_type: objectType, object_name: objectName, privileges: [g.privilege],
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.detail || resp.statusText);
          openRoleGrantsModal(rolname);
        } catch (e) { setRoleGrantsStatus("err", "Revoke failed: " + e.message); }
      });
    });
  }
  document.getElementById("btn-grant-privilege")?.addEventListener("click", async () => {
    const rolname = document.getElementById("role-grants-rolname").textContent;
    const objectType = document.getElementById("grant-object-type").value;
    const objectName = document.getElementById("grant-object-name").value.trim();
    const privileges = Array.from(document.querySelectorAll(".grant-priv-cb:checked")).map((cb) => cb.value);
    const withGrantOption = document.getElementById("grant-with-option").checked;
    if (!privileges.length) return setRoleGrantsStatus("err", "Select at least one privilege.");
    if (objectType !== "database" && !objectName) return setRoleGrantsStatus("err", "Object name is required.");
    setRoleGrantsStatus(null, "Granting...");
    try {
      const resp = await apiPost(`/api/roles/${encodeURIComponent(rolname)}/grants`, {
        object_type: objectType, object_name: objectName, privileges, with_grant_option: withGrantOption,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      setRoleGrantsStatus("ok", "Granted.");
      openRoleGrantsModal(rolname);
    } catch (e) { setRoleGrantsStatus("err", "Grant failed: " + e.message); }
  });
  document.getElementById("btn-terminate-role-sessions")?.addEventListener("click", async () => {
    const rolname = document.getElementById("role-grants-rolname").textContent;
    if (!confirm(`Terminate every active session for ${rolname}?`)) return;
    try {
      const resp = await apiPost(`/api/roles/${encodeURIComponent(rolname)}/terminate-sessions`, {});
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      setRoleGrantsStatus("ok", `Terminated ${data.terminated} session(s).`);
      openRoleGrantsModal(rolname);
    } catch (e) { setRoleGrantsStatus("err", "Failed: " + e.message); }
  });
  document.getElementById("btn-close-role-grants")?.addEventListener("click", () => roleGrantsModal.classList.add("hidden"));
  roleGrantsModal?.addEventListener("click", (e) => { if (e.target === roleGrantsModal) roleGrantsModal.classList.add("hidden"); });

  // --------------------------------------------------------- role delete modal
  const roleDeleteModal = document.getElementById("role-delete-modal");
  let roleDeleteTarget = null;
  function setRoleDeleteStatus(kind, text) {
    const el = document.getElementById("role-delete-modal-status");
    el.className = "add-db-status" + (kind ? ` status-${kind}` : "");
    el.textContent = text;
  }
  function openRoleDeleteModal(rolname) {
    roleDeleteTarget = rolname;
    document.getElementById("role-delete-rolname").textContent = rolname;
    document.getElementById("role-delete-rolname-2").textContent = rolname;
    document.getElementById("role-delete-confirm-input").value = "";
    document.getElementById("role-delete-terminate").checked = true;
    document.getElementById("btn-confirm-role-delete").disabled = true;
    setRoleDeleteStatus(null, "");
    const sel = document.getElementById("role-delete-reassign");
    sel.innerHTML = `<option value="">(don't reassign &mdash; fail if it owns anything)</option>` +
      cachedRoleNames.filter((n) => n !== rolname).map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    roleDeleteModal.classList.remove("hidden");
  }
  document.getElementById("role-delete-confirm-input")?.addEventListener("input", (e) => {
    document.getElementById("btn-confirm-role-delete").disabled = e.target.value !== roleDeleteTarget;
  });
  document.getElementById("btn-close-role-delete")?.addEventListener("click", () => roleDeleteModal.classList.add("hidden"));
  roleDeleteModal?.addEventListener("click", (e) => { if (e.target === roleDeleteModal) roleDeleteModal.classList.add("hidden"); });

  document.getElementById("btn-confirm-role-delete")?.addEventListener("click", async () => {
    const reassignTo = document.getElementById("role-delete-reassign").value;
    const terminate = document.getElementById("role-delete-terminate").checked;
    const btn = document.getElementById("btn-confirm-role-delete");
    btn.disabled = true;
    setRoleDeleteStatus(null, "Dropping...");
    try {
      let path = `/api/roles/${encodeURIComponent(roleDeleteTarget)}?terminate=${terminate}`;
      if (reassignTo) path += `&reassign_to=${encodeURIComponent(reassignTo)}`;
      const resp = await apiMethod("DELETE", path);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      roleDeleteModal.classList.add("hidden");
      loadSecurity();
    } catch (e) {
      setRoleDeleteStatus("err", "Failed: " + e.message);
      btn.disabled = false;
    }
  });

  // ------------------------------------------------- schema (Toad-style tree)
  let schemasCache = [];
  let schemaObjectsCache = {};  // schemaName -> objects[] (lazy-loaded on first expand)
  let treeExpanded = {};        // "schema:x" / "cat:x:table" -> bool
  let selectedTreeKey = null;   // "obj:schema:name"

  const TREE_ICON_GLYPH = { table: "T", view: "V", materialized_view: "M", sequence: "S" };
  const CATEGORY_DEFS = [
    { kind: "table", label: "Tables" },
    { kind: "materialized_view", label: "Materialized Views" },
    { kind: "view", label: "Views" },
    { kind: "sequence", label: "Sequences" },
  ];

  function renderObjectNode(schemaName, obj) {
    const objKey = `obj:${schemaName}:${obj.name}`;
    const isSelected = selectedTreeKey === objKey;
    return `
      <div class="tree-node-row ${isSelected ? "selected" : ""}" data-select-object="${esc(schemaName)}::${esc(obj.name)}">
        <span class="tree-caret leaf">&#9656;</span>
        <span class="tree-icon tree-icon-${esc(obj.kind)}">${TREE_ICON_GLYPH[obj.kind] || "?"}</span>
        <span class="tree-label">${esc(obj.name)}</span>
      </div>`;
  }

  function renderCategoryNode(schemaName, catDef, items, filterText) {
    if (!items.length) return "";
    const key = `cat:${schemaName}:${catDef.kind}`;
    let visible = items;
    let autoExpanded = false;
    if (filterText) {
      visible = items.filter((o) => o.name.toLowerCase().includes(filterText));
      if (!visible.length) return "";
      autoExpanded = true;
    }
    const expanded = !!treeExpanded[key] || autoExpanded;
    const childrenHtml = visible.map((o) => renderObjectNode(schemaName, o)).join("");
    return `
      <div class="tree-node">
        <div class="tree-node-row" data-toggle-category="${esc(schemaName)}::${catDef.kind}">
          <span class="tree-caret ${expanded ? "expanded" : ""}">&#9656;</span>
          <span class="tree-icon tree-icon-category">&middot;</span>
          <span class="tree-label">${esc(catDef.label)}</span>
          <span class="tree-count">${items.length}</span>
        </div>
        <div class="tree-children ${expanded ? "expanded" : ""}">${childrenHtml}</div>
      </div>`;
  }

  function renderSchemaNode(schema, filterText) {
    const key = `schema:${schema.schema_name}`;
    const objects = schemaObjectsCache[schema.schema_name];
    const totalObjects = schema.table_count + schema.view_count + schema.sequence_count;
    const nameMatches = !filterText || schema.schema_name.toLowerCase().includes(filterText);
    const objectMatches = filterText && objects ? objects.some((o) => o.name.toLowerCase().includes(filterText)) : false;
    if (filterText && !nameMatches && !objectMatches) return "";

    const expanded = !!treeExpanded[key] || (filterText && objectMatches);
    let childrenHtml = "";
    if (expanded) {
      if (objects == null) {
        childrenHtml = `<div class="empty-note" style="padding-left:0.5rem;">Loading...</div>`;
      } else {
        childrenHtml = CATEGORY_DEFS.map((cat) =>
          renderCategoryNode(schema.schema_name, cat, objects.filter((o) => o.kind === cat.kind), nameMatches ? "" : filterText)
        ).join("") || `<div class="empty-note" style="padding-left:0.5rem;">No objects.</div>`;
      }
    }

    return `
      <div class="tree-node">
        <div class="tree-node-row" data-toggle-schema="${esc(schema.schema_name)}">
          <span class="tree-caret ${expanded ? "expanded" : ""}">&#9656;</span>
          <span class="tree-label"><strong>${esc(schema.schema_name)}</strong></span>
          <span class="tree-count">${totalObjects}</span>
        </div>
        <div class="tree-children ${expanded ? "expanded" : ""}">${childrenHtml}</div>
      </div>`;
  }

  function wireTreeEvents() {
    document.querySelectorAll("[data-toggle-schema]").forEach((el) => {
      el.addEventListener("click", () => toggleSchemaNode(el.dataset.toggleSchema));
    });
    document.querySelectorAll("[data-toggle-category]").forEach((el) => {
      el.addEventListener("click", () => {
        const [schemaName, kind] = el.dataset.toggleCategory.split("::");
        treeExpanded[`cat:${schemaName}:${kind}`] = !treeExpanded[`cat:${schemaName}:${kind}`];
        renderSchemaTree();
      });
    });
    document.querySelectorAll("[data-select-object]").forEach((el) => {
      el.addEventListener("click", () => {
        const [schemaName, objectName] = el.dataset.selectObject.split("::");
        selectTreeObject(schemaName, objectName);
      });
    });
  }

  function renderSchemaTree() {
    const showSystem = document.getElementById("chk-show-system-schemas").checked;
    const filterText = document.getElementById("schema-tree-filter").value.trim().toLowerCase();
    const container = document.getElementById("schema-tree");
    const schemas = schemasCache.filter((s) => showSystem || !s.is_system);
    if (!schemas.length) {
      container.innerHTML = `<div class="empty-note">No schemas found.</div>`;
      return;
    }
    const html = schemas.map((s) => renderSchemaNode(s, filterText)).join("");
    container.innerHTML = html || `<div class="empty-note">No matches.</div>`;
    wireTreeEvents();
  }

  async function toggleSchemaNode(schemaName) {
    const key = `schema:${schemaName}`;
    treeExpanded[key] = !treeExpanded[key];
    if (treeExpanded[key] && schemaObjectsCache[schemaName] == null) {
      renderSchemaTree();
      try {
        schemaObjectsCache[schemaName] = await api(`/api/schemas/${encodeURIComponent(schemaName)}/objects`).then((r) => r.json());
      } catch (e) {
        schemaObjectsCache[schemaName] = [];
        console.error("failed to load schema objects", e);
      }
    }
    renderSchemaTree();
  }

  async function loadSchemas() {
    try {
      schemasCache = await api("/api/schemas").then((r) => r.json());
      renderSchemaTree();
    } catch (e) {
      document.getElementById("schema-tree").innerHTML = `<div class="empty-note">Failed to load schemas.</div>`;
      console.error("schemas load failed", e);
    }
  }

  document.getElementById("chk-show-system-schemas")?.addEventListener("change", renderSchemaTree);
  document.getElementById("schema-tree-filter")?.addEventListener("input", renderSchemaTree);

  function columnsTableHtml(columns, pk) {
    pk = pk || [];
    return `<div class="table-wrap"><table class="data-table"><thead><tr><th>Column</th><th>Type</th><th>Nullable</th><th>Default</th></tr></thead><tbody>${
      (columns || []).map((c) => `
        <tr>
          <td class="mono ${pk.includes(c.column_name) ? "stat-warn" : ""}">${esc(c.column_name)}</td>
          <td class="mono">${esc(c.data_type)}${c.character_maximum_length ? `(${c.character_maximum_length})` : ""}</td>
          <td>${c.is_nullable === "NO" ? "not null" : ""}</td>
          <td class="mono">${esc(c.column_default || "")}</td>
        </tr>`).join("")
    }</tbody></table></div>`;
  }

  function indexesTableHtml(indexes) {
    return `<div class="table-wrap"><table class="data-table"><thead><tr><th>Index</th><th>Definition</th><th>Scans</th><th>Size</th></tr></thead><tbody>${
      (indexes || []).map((i) => `
        <tr>
          <td class="mono">${esc(i.indexname)}</td>
          <td class="mono">${esc(i.indexdef)}</td>
          <td class="${(i.idx_scan || 0) === 0 ? "stat-warn" : ""}">${fmtNum(i.idx_scan)}</td>
          <td>${fmtBytes(i.index_bytes)}</td>
        </tr>`).join("") || `<tr><td colspan="4" class="empty-note">No indexes.</td></tr>`
    }</tbody></table></div>`;
  }

  function statsKvHtml(detail) {
    const s = detail.stats || {};
    return `<div class="kv-grid">
      <div class="kv-item"><div class="kv-item-label">Size</div><div class="kv-item-value">${fmtBytes(detail.size_bytes)}</div></div>
      <div class="kv-item"><div class="kv-item-label">Live rows</div><div class="kv-item-value">${fmtNum(s.n_live_tup)}</div></div>
      <div class="kv-item"><div class="kv-item-label">Dead rows</div><div class="kv-item-value">${fmtNum(s.n_dead_tup)}</div></div>
      <div class="kv-item"><div class="kv-item-label">Seq scans</div><div class="kv-item-value">${fmtNum(s.seq_scan)}</div></div>
      <div class="kv-item"><div class="kv-item-label">Index scans</div><div class="kv-item-value">${fmtNum(s.idx_scan)}</div></div>
      <div class="kv-item"><div class="kv-item-label">Last autovacuum</div><div class="kv-item-value">${fmtDate(s.last_autovacuum)}</div></div>
      <div class="kv-item"><div class="kv-item-label">Last autoanalyze</div><div class="kv-item-value">${fmtDate(s.last_autoanalyze)}</div></div>
    </div>`;
  }

  function partitionsTabHtml(detail) {
    const p = detail.partitioning;
    const rows = (p.partitions || []).map((part) => `
      <tr>
        <td class="mono">${esc(part.schema)}.${esc(part.name)}</td>
        <td class="mono">${esc(part.bound || "")}</td>
        <td>${fmtNum(part.row_estimate)}</td>
        <td>${fmtBytes(part.size_bytes)}</td>
        <td><button type="button" class="btn-icon-danger" data-detach-partition="${esc(part.name)}">Detach</button></td>
      </tr>`).join("") || `<tr><td colspan="5" class="empty-note">No partitions attached yet.</td></tr>`;
    return `
      <div class="object-detail-meta">Partition key: <code>${esc(p.partition_key || "")}</code></div>
      <div class="table-wrap">
        <table class="data-table" id="table-partitions">
          <thead><tr><th>Partition</th><th>Bound</th><th>Rows (est.)</th><th>Size</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="add-db-form" style="max-width:none;">
        <div class="form-row-split">
          <div>
            <label for="new-partition-name">New partition name</label>
            <input type="text" id="new-partition-name" autocomplete="off" placeholder="e.g. ${esc(detail.name)}_2027_01">
          </div>
          <div>
            <label for="new-partition-bound">Bound clause</label>
            <input type="text" id="new-partition-bound" autocomplete="off" placeholder="FOR VALUES FROM ('2027-01-01') TO ('2027-02-01')">
          </div>
        </div>
        <div class="form-actions">
          <button type="button" id="btn-add-partition" class="btn btn-primary btn-sm">Add partition</button>
        </div>
        <div id="partition-status" class="add-db-status"></div>
      </div>`;
  }

  function wirePartitionsTab(detail) {
    const panel = document.querySelector('[data-subpanel="partitions"]');
    if (!panel) return;
    panel.querySelectorAll("[data-detach-partition]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.detachPartition;
        if (!confirm(`Detach ${name} from ${detail.name}? It keeps its data as a standalone table -- this doesn't drop anything.`)) return;
        try {
          const resp = await apiMethod("DELETE",
            `/api/schemas/${encodeURIComponent(detail.schema)}/objects/${encodeURIComponent(detail.name)}/partitions/${encodeURIComponent(name)}`);
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.detail || resp.statusText);
          selectTreeObject(detail.schema, detail.name);
        } catch (e) { alert("Failed to detach: " + e.message); }
      });
    });
    panel.querySelector("#btn-add-partition")?.addEventListener("click", async () => {
      const statusEl = panel.querySelector("#partition-status");
      const name = panel.querySelector("#new-partition-name").value.trim();
      const bound = panel.querySelector("#new-partition-bound").value.trim();
      statusEl.innerHTML = "";
      if (!name || !bound) {
        statusEl.className = "add-db-status status-err";
        statusEl.textContent = "Both a name and a bound clause are required.";
        return;
      }
      try {
        const resp = await apiPost(`/api/schemas/${encodeURIComponent(detail.schema)}/objects/${encodeURIComponent(detail.name)}/partitions`,
          { name, bound });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        selectTreeObject(detail.schema, detail.name);
      } catch (e) {
        statusEl.className = "add-db-status status-err";
        statusEl.textContent = "Failed: " + e.message;
      }
    });
  }

  function renderObjectDetailTabs(detail) {
    const tabstrip = document.getElementById("schema-detail-tabstrip");
    const panelsContainer = document.getElementById("schema-detail-panels");
    let meta = `<div class="object-detail-meta">Owner: <code>${esc(detail.owner)}</code>${detail.comment ? ` &middot; ${esc(detail.comment)}` : ""}</div>`;
    if (detail.partition_of) {
      const po = detail.partition_of;
      meta += `<div class="object-detail-meta">Partition of <code>${esc(po.schema)}.${esc(po.name)}</code> &mdash; <code>${esc(po.bound || "")}</code></div>`;
    }

    const dataTabEntry = { id: "data", label: "Data", html: `<div id="data-tab-container"><div class="hint-text">Loading rows...</div></div>` };

    let tabs;
    if (detail.kind === "table" || detail.kind === "materialized_view") {
      tabs = [
        { id: "columns", label: "Columns", html: meta + columnsTableHtml(detail.columns, detail.primary_key) },
        { id: "indexes", label: "Indexes", html: indexesTableHtml(detail.indexes) },
        { id: "statistics", label: "Statistics", html: statsKvHtml(detail) },
        dataTabEntry,
      ];
      if (detail.partitioning) {
        tabs.splice(3, 0, { id: "partitions", label: "Partitions", html: partitionsTabHtml(detail) });
      }
    } else if (detail.kind === "view") {
      tabs = [
        { id: "columns", label: "Columns", html: meta + columnsTableHtml(detail.columns, []) },
        { id: "definition", label: "Definition", html: `<pre class="view-definition">${esc(detail.definition || "(unavailable)")}</pre>` },
        dataTabEntry,
      ];
    } else if (detail.kind === "sequence") {
      const s = detail.sequence || {};
      tabs = [{
        id: "properties", label: "Properties", html: meta + `<div class="kv-grid">
          <div class="kv-item"><div class="kv-item-label">Current value</div><div class="kv-item-value">${fmtNum(s.last_value)}</div></div>
          <div class="kv-item"><div class="kv-item-label">Data type</div><div class="kv-item-value">${esc(s.data_type || "")}</div></div>
          <div class="kv-item"><div class="kv-item-label">Start</div><div class="kv-item-value">${fmtNum(s.start_value)}</div></div>
          <div class="kv-item"><div class="kv-item-label">Increment</div><div class="kv-item-value">${fmtNum(s.increment_by)}</div></div>
          <div class="kv-item"><div class="kv-item-label">Min / Max</div><div class="kv-item-value">${fmtNum(s.min_value)} / ${fmtNum(s.max_value)}</div></div>
          <div class="kv-item"><div class="kv-item-label">Cycle</div><div class="kv-item-value">${s.cycle ? "yes" : "no"}</div></div>
        </div>`,
      }];
    } else {
      tabs = [{ id: "info", label: "Info", html: meta + `<div class="hint-text">No detail view available for this object kind.</div>` }];
    }

    tabstrip.innerHTML = tabs.map((t, i) => `<button class="tab-strip-item subtab-item ${i === 0 ? "active" : ""}" data-subtab="${t.id}">${esc(t.label)}</button>`).join("");
    panelsContainer.innerHTML = tabs.map((t, i) => `<div class="dtab-panel subtab-panel ${i === 0 ? "active" : ""}" data-subpanel="${t.id}">${t.html}</div>`).join("");

    if (detail.partitioning) wirePartitionsTab(detail);

    tabstrip.querySelectorAll("[data-subtab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        tabstrip.querySelectorAll("[data-subtab]").forEach((b) => b.classList.remove("active"));
        panelsContainer.querySelectorAll("[data-subpanel]").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        panelsContainer.querySelector(`[data-subpanel="${btn.dataset.subtab}"]`).classList.add("active");
        if (btn.dataset.subtab === "data") loadObjectData(detail.schema, detail.name);
      });
    });
  }

  // ---------------------------------------------------------- data grid
  let dataTabState = { schema: null, object: null, offset: 0, limit: 50, orderBy: null, orderDir: "asc" };

  function formatCellValue(v) {
    if (v === null) return `<span class="cell-null">NULL</span>`;
    const s = typeof v === "object" ? JSON.stringify(v) : String(v);
    const truncated = s.length > 200 ? s.slice(0, 200) + "…" : s;
    return `<span class="q-snippet" title="${esc(s)}">${esc(truncated)}</span>`;
  }

  function renderDataGrid(schemaName, objectName, data) {
    const container = document.getElementById("data-tab-container");
    if (!container) return;
    const { columns, rows, row_count_estimate, limit, offset } = data;
    const from = offset + 1;
    const to = offset + rows.length;
    const totalLabel = row_count_estimate != null ? `~${fmtNum(row_count_estimate)}` : "unknown";

    const headerHtml = columns.map((c) => {
      const isSorted = dataTabState.orderBy === c;
      const arrow = isSorted ? (dataTabState.orderDir === "desc" ? " ▾" : " ▴") : "";
      return `<th class="data-grid-sortable" data-sort-col="${esc(c)}">${esc(c)}${arrow}</th>`;
    }).join("");

    const rowsHtml = rows.map((row) => `<tr>${row.map((v) => `<td class="mono">${formatCellValue(v)}</td>`).join("")}</tr>`).join("")
      || `<tr><td colspan="${columns.length || 1}" class="empty-note">No rows.</td></tr>`;

    container.innerHTML = `
      <div class="data-grid-toolbar">
        <span class="hint-text">Showing ${rows.length ? `${from}-${to}` : "0"} of ${totalLabel} rows</span>
        <div class="data-grid-nav">
          <button class="btn btn-ghost btn-sm" id="btn-data-prev" ${offset === 0 ? "disabled" : ""}>&larr; Prev</button>
          <button class="btn btn-ghost btn-sm" id="btn-data-next" ${rows.length < limit ? "disabled" : ""}>Next &rarr;</button>
        </div>
      </div>
      <div class="table-wrap data-grid-wrap">
        <table class="data-table" id="data-grid-table">
          <thead><tr>${headerHtml || "<th></th>"}</tr></thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>`;

    document.getElementById("btn-data-prev")?.addEventListener("click", () => {
      dataTabState.offset = Math.max(0, dataTabState.offset - dataTabState.limit);
      loadObjectData(schemaName, objectName);
    });
    document.getElementById("btn-data-next")?.addEventListener("click", () => {
      dataTabState.offset += dataTabState.limit;
      loadObjectData(schemaName, objectName);
    });
    container.querySelectorAll("[data-sort-col]").forEach((th) => {
      th.addEventListener("click", () => {
        const col = th.dataset.sortCol;
        if (dataTabState.orderBy === col) {
          dataTabState.orderDir = dataTabState.orderDir === "asc" ? "desc" : "asc";
        } else {
          dataTabState.orderBy = col;
          dataTabState.orderDir = "asc";
        }
        dataTabState.offset = 0;
        loadObjectData(schemaName, objectName);
      });
    });
  }

  async function loadObjectData(schemaName, objectName) {
    if (dataTabState.schema !== schemaName || dataTabState.object !== objectName) {
      dataTabState = { schema: schemaName, object: objectName, offset: 0, limit: 50, orderBy: null, orderDir: "asc" };
    }
    const container = document.getElementById("data-tab-container");
    if (!container) return;
    container.innerHTML = `<div class="hint-text">Loading rows...</div>`;
    try {
      const params = new URLSearchParams({ limit: dataTabState.limit, offset: dataTabState.offset });
      if (dataTabState.orderBy) { params.set("order_by", dataTabState.orderBy); params.set("order_dir", dataTabState.orderDir); }
      const data = await api(`/api/schemas/${encodeURIComponent(schemaName)}/objects/${encodeURIComponent(objectName)}/rows?${params}`).then((r) => r.json());
      renderDataGrid(schemaName, objectName, data);
    } catch (e) {
      container.innerHTML = `<div class="alert alert-error">Failed to load data.</div>`;
      console.error("object data load failed", e);
    }
  }

  async function selectTreeObject(schemaName, objectName) {
    selectedTreeKey = `obj:${schemaName}:${objectName}`;
    renderSchemaTree();

    document.getElementById("schema-detail-empty").classList.add("hidden");
    document.getElementById("schema-detail-content").classList.remove("hidden");
    document.getElementById("schema-detail-name").textContent = objectName;
    document.getElementById("schema-detail-path").textContent = `${schemaName}.${objectName}`;

    const tabstrip = document.getElementById("schema-detail-tabstrip");
    const panelsContainer = document.getElementById("schema-detail-panels");
    const iconEl = document.getElementById("schema-detail-icon");
    iconEl.textContent = "…";
    iconEl.className = "schema-detail-icon";
    tabstrip.innerHTML = "";
    panelsContainer.innerHTML = `<div class="hint-text">Loading...</div>`;

    try {
      const detail = await api(`/api/schemas/${encodeURIComponent(schemaName)}/objects/${encodeURIComponent(objectName)}`).then((r) => r.json());
      iconEl.textContent = TREE_ICON_GLYPH[detail.kind] || "?";
      iconEl.className = `schema-detail-icon tree-icon-${detail.kind}`;
      renderObjectDetailTabs(detail);
    } catch (e) {
      panelsContainer.innerHTML = `<div class="alert alert-error">Failed to load object detail.</div>`;
      console.error("object detail load failed", e);
    }
  }

  dtabLoaders.schema = () => {
    schemaObjectsCache = {};
    treeExpanded = {};
    selectedTreeKey = null;
    document.getElementById("schema-tree-filter").value = "";
    document.getElementById("schema-detail-empty").classList.remove("hidden");
    document.getElementById("schema-detail-content").classList.add("hidden");
    loadSchemas();
  };

  // ------------------------------------------------------ administration
  function fmtSetting(cfg) {
    if (!cfg) return "—";
    return cfg.unit ? `${cfg.value} ${cfg.unit}` : cfg.value;
  }

  let repackAvailable = false;
  async function checkRepackAvailable() {
    try {
      const data = await api("/api/administration/repack-status").then((r) => r.json());
      repackAvailable = !!data.installed;
    } catch (e) { repackAvailable = false; }
  }

  async function pollRepackJob(jobId) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      let job;
      try {
        job = await api(`/api/administration/repack-jobs/${jobId}`).then((r) => r.json());
      } catch (e) { continue; }
      if (job.status !== "running") {
        if (job.status === "error") alert("Repack failed: " + (job.error || "see server logs"));
        else alert("Repack complete.");
        loadAdministration();
        return;
      }
    }
  }

  async function loadAdministration() {
    try {
      await checkRepackAvailable();
      const a = await api("/api/administration").then((r) => r.json());

      // Autovacuum
      const av = a.autovacuum || {};
      const s = av.settings || {};
      const setAv = (key, val) => {
        const el = document.querySelector(`[data-avstat="${key}"]`);
        if (el) el.textContent = val;
      };
      setAv("autovacuum", s.autovacuum === "on" ? "on" : (s.autovacuum || "—"));
      setAv("autovacuum_vacuum_scale_factor", s.autovacuum_vacuum_scale_factor ?? "—");
      setAv("autovacuum_vacuum_threshold", s.autovacuum_vacuum_threshold ?? "—");
      setAv("autovacuum_naptime", s.autovacuum_naptime ?? "—");
      setAv("autovacuum_max_workers", s.autovacuum_max_workers ?? "—");
      setAv("active_workers", fmtNum((av.active_workers || []).length));

      document.querySelector("#table-autovacuum tbody").innerHTML = (av.tables || []).map((t) => `
        <tr>
          <td class="mono">${esc(t.table_name)}</td>
          <td>${fmtNum(t.n_live_tup)}</td>
          <td class="${t.dead_tuple_pct > 20 ? "stat-warn" : ""}">${t.dead_tuple_pct ?? 0}%</td>
          <td>${fmtDate(t.last_autovacuum)}</td>
          <td>${fmtNum(t.autovacuum_count)}</td>
          <td>${fmtDate(t.last_autoanalyze)}</td>
          <td>${fmtNum(t.autoanalyze_count)}</td>
        </tr>`).join("") || `<tr><td colspan="7" class="empty-note">No user tables found.</td></tr>`;

      // Estimated bloat
      const bloatTbody = document.querySelector("#table-bloat tbody");
      bloatTbody.innerHTML = (a.table_bloat_estimate || []).map((t) => `
        <tr>
          <td class="mono">${esc(t.schema_name || "public")}.${esc(t.table_name)}</td>
          <td>${fmtNum(t.n_live_tup)}</td>
          <td>${fmtNum(t.n_dead_tup)}</td>
          <td class="${t.dead_tuple_pct > 20 ? "stat-bad" : t.dead_tuple_pct > 10 ? "stat-warn" : ""}">${t.dead_tuple_pct}%</td>
          <td>
            <button class="btn btn-ghost btn-sm" data-vacuum-schema="${esc(t.schema_name || "public")}" data-vacuum-table="${esc(t.table_name)}">Vacuum&hellip;</button>
            ${repackAvailable ? `<button class="btn btn-ghost btn-sm" data-repack-schema="${esc(t.schema_name || "public")}" data-repack-table="${esc(t.table_name)}">Repack (online)&hellip;</button>` : ""}
          </td>
        </tr>`).join("") || `<tr><td colspan="5" class="empty-note">No dead tuples found — nothing to estimate bloat from.</td></tr>`;
      bloatTbody.querySelectorAll("[data-vacuum-table]").forEach((btn) => {
        btn.addEventListener("click", () => openVacuumModal(btn.dataset.vacuumSchema, btn.dataset.vacuumTable));
      });
      bloatTbody.querySelectorAll("[data-repack-table]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const schema = btn.dataset.repackSchema, table = btn.dataset.repackTable;
          if (!confirm(`Repack ${schema}.${table} online?\n\nRebuilds the table in the background without a long exclusive lock (unlike VACUUM FULL) -- takes a brief lock only at the very end to swap in the result. Can take a while on a large table.`)) return;
          btn.disabled = true; btn.textContent = "Repacking...";
          try {
            const resp = await apiPost("/api/administration/repack", { schema, table });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || resp.statusText);
            pollRepackJob(data.job_id);
          } catch (e) {
            alert("Failed to start repack: " + e.message);
            btn.disabled = false; btn.textContent = "Repack (online)...";
          }
        });
      });

      // Disk usage & growth
      const disk = a.disk_usage || {};
      const growth = a.growth_rate || {};
      const setDisk = (key, val) => {
        const el = document.querySelector(`[data-diskstat="${key}"]`);
        if (el) el.textContent = val;
      };
      setDisk("db_size", fmtBytes(disk.db_size_bytes));
      setDisk("wal_size", disk.wal_dir_bytes != null ? fmtBytes(disk.wal_dir_bytes) : "n/a");
      setDisk("growth", growth.available ? `${growth.mb_per_day} MB/day` : "not enough history yet");
      setDisk("temp", `${fmtNum(disk.temp_files)} / ${fmtBytes(disk.temp_bytes)}`);

      // WAL rate
      const wal = a.wal_rate || {};
      const setWal = (key, val) => {
        const el = document.querySelector(`[data-walstat="${key}"]`);
        if (el) el.textContent = val;
      };
      setWal("bytes_per_sec", wal.available ? `${fmtBytes(wal.bytes_per_sec)}/s` : (wal.reason || "unavailable"));
      setWal("records_per_sec", wal.available ? fmtNum(wal.records_per_sec) : "—");

      // Checkpoints
      const ck = a.checkpoint || {};
      const ckStats = ck.stats || {};
      const setCk = (key, val) => {
        const el = document.querySelector(`[data-ckstat="${key}"]`);
        if (el) el.textContent = val;
      };
      setCk("per_hour", ck.checkpoints_per_hour != null ? ck.checkpoints_per_hour : "—");
      setCk("timed_vs_requested", `${fmtNum(ckStats.num_timed)} / ${fmtNum(ckStats.num_requested)}`);
      setCk("checkpoint_timeout", fmtSetting((ck.config || {}).checkpoint_timeout));
      setCk("max_wal_size", fmtSetting((ck.config || {}).max_wal_size));

      // Index usage
      const idx = a.index_usage || {};
      const setIdx = (key, val) => {
        const el = document.querySelector(`[data-idxstat="${key}"]`);
        if (el) el.textContent = val;
      };
      setIdx("seq_scan", fmtNum(idx.total_seq_scan));
      setIdx("idx_scan", fmtNum(idx.total_idx_scan));
      setIdx("pct_index", idx.pct_reads_via_index != null ? `${idx.pct_reads_via_index}%` : "—");

      document.querySelector("#table-admin-unused-indexes tbody").innerHTML = (idx.top_unused_indexes || []).map((i) => `
        <tr>
          <td class="mono">${esc(i.schemaname)}.${esc(i.table_name)}</td>
          <td class="mono">${esc(i.index_name)}</td>
          <td>${fmtBytes(i.index_bytes)}</td>
        </tr>`).join("") || `<tr><td colspan="3" class="empty-note">No unused indexes — nice.</td></tr>`;
    } catch (e) { console.error("administration load failed", e); }
  }
  dtabLoaders.administration = loadAdministration;

  document.getElementById("btn-administration-review")?.addEventListener("click", (e) =>
    runAssessment(e.currentTarget, "ai-administration-card", "✦ Analyzing...", "✦ AI Administration Review", "/api/ai/administration-review"));

  document.getElementById("btn-partitioning-advisor")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const statusEl = document.getElementById("partitioning-advisor-status");
    const wrap = document.getElementById("partitioning-advisor-wrap");
    const minRows = document.getElementById("partitioning-min-rows").value || "0";
    const minBytes = (Number(document.getElementById("partitioning-min-mb").value) || 0) * 1024 * 1024;
    btn.disabled = true; btn.textContent = "Analyzing...";
    statusEl.textContent = "";
    wrap.classList.add("hidden");
    try {
      const data = await api(`/api/administration/partitioning-advisor?min_rows=${encodeURIComponent(minRows)}&min_bytes=${encodeURIComponent(minBytes)}`).then((r) => r.json());
      if (!data.suggestions.length) {
        statusEl.textContent = data.candidates_scanned
          ? `Scanned ${data.candidates_scanned} large table(s) -- none had a clear partitioning column, or they're already partitioned.`
          : "No tables meet the size/row thresholds above -- lower them to scan smaller tables.";
        return;
      }
      document.querySelector("#table-partitioning-advisor tbody").innerHTML = data.suggestions.map((s, i) => `
        <tr>
          <td class="mono">${esc(s.schema)}.${esc(s.table)}</td>
          <td>${fmtBytes(s.total_bytes)}</td>
          <td>${fmtNum(s.row_estimate)}</td>
          <td><span class="badge">${esc(s.technique)}</span></td>
          <td class="mono">${esc(s.column)}</td>
          <td>${esc(s.reason)}</td>
          <td>
            <details class="raw-plan"><summary>DDL</summary><pre>${esc(s.example_ddl)}</pre></details>
            <button type="button" class="btn btn-ghost btn-sm" data-convert-partition="${i}">Convert&hellip;</button>
          </td>
        </tr>`).join("");
      wrap.classList.remove("hidden");
      partitioningSuggestions = data.suggestions;
      document.querySelectorAll("[data-convert-partition]").forEach((btn) =>
        btn.addEventListener("click", () => openPartitionConvertModal(partitioningSuggestions[Number(btn.dataset.convertPartition)])));
    } catch (err) {
      statusEl.textContent = "Failed: " + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = "Analyze for partitioning candidates";
    }
  });

  // ------------------------------------------------- partition convert modal
  let partitioningSuggestions = [];
  let partitionConvertTarget = null; // {schema, table, technique, column}
  const partitionConvertModal = document.getElementById("partition-convert-modal");

  function closePartitionConvertModal() { partitionConvertModal.classList.add("hidden"); partitionConvertTarget = null; }
  document.getElementById("btn-close-partition-convert").addEventListener("click", closePartitionConvertModal);
  partitionConvertModal.addEventListener("click", (e) => { if (e.target === partitionConvertModal) closePartitionConvertModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !partitionConvertModal.classList.contains("hidden")) closePartitionConvertModal();
  });

  async function openPartitionConvertModal(suggestion) {
    partitionConvertTarget = suggestion;
    const fullName = `${suggestion.schema}.${suggestion.table}`;
    document.getElementById("partition-convert-target").textContent = fullName;
    document.getElementById("partition-convert-technique").textContent = suggestion.technique;
    document.getElementById("partition-convert-column").textContent = suggestion.column;
    document.getElementById("partition-convert-name-echo").textContent = fullName;
    document.getElementById("partition-convert-confirm-input").value = "";
    document.getElementById("partition-convert-confirm-section").classList.add("hidden");
    document.getElementById("partition-convert-job-section").classList.add("hidden");
    document.getElementById("partition-convert-blocking").classList.add("hidden");
    document.getElementById("partition-convert-notices").innerHTML = "";
    document.getElementById("partition-convert-check-status").textContent = "Checking prerequisites...";
    document.getElementById("btn-confirm-partition-convert").disabled = true;
    partitionConvertModal.classList.remove("hidden");

    try {
      const resp = await apiPost("/api/administration/partitioning-advisor/check", {
        schema: suggestion.schema, table: suggestion.table, column: suggestion.column,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      document.getElementById("partition-convert-check-status").textContent = "";
      if (data.notices && data.notices.length) {
        document.getElementById("partition-convert-notices").innerHTML = data.notices.map((n) =>
          `<div class="hint-text">&#9432; ${esc(n)}</div>`).join("");
      }
      if (data.blocking && data.blocking.length) {
        const blockEl = document.getElementById("partition-convert-blocking");
        blockEl.innerHTML = "<strong>Can't convert this table automatically:</strong><ul>"
          + data.blocking.map((b) => `<li>${esc(b)}</li>`).join("") + "</ul>";
        blockEl.classList.remove("hidden");
      } else {
        document.getElementById("partition-convert-confirm-section").classList.remove("hidden");
      }
    } catch (err) {
      document.getElementById("partition-convert-check-status").textContent = "Failed to check: " + err.message;
    }
  }

  document.getElementById("partition-convert-confirm-input")?.addEventListener("input", (e) => {
    const expected = partitionConvertTarget ? `${partitionConvertTarget.schema}.${partitionConvertTarget.table}` : "";
    document.getElementById("btn-confirm-partition-convert").disabled = e.target.value !== expected;
  });

  document.getElementById("btn-confirm-partition-convert")?.addEventListener("click", async () => {
    if (!partitionConvertTarget) return;
    const btn = document.getElementById("btn-confirm-partition-convert");
    const t = partitionConvertTarget;
    btn.disabled = true; btn.textContent = "Starting...";
    try {
      const resp = await apiPost("/api/administration/partitioning-advisor/convert", t);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      document.getElementById("partition-convert-confirm-section").classList.add("hidden");
      pollPartitionConvertJob(data.job_id);
    } catch (err) {
      alert("Failed to start conversion: " + err.message);
      btn.disabled = false; btn.textContent = "Convert";
    }
  });

  async function pollPartitionConvertJob(jobId) {
    const summaryEl = document.getElementById("partition-convert-job-summary");
    const logEl = document.getElementById("partition-convert-job-log");
    document.getElementById("partition-convert-job-section").classList.remove("hidden");
    const tick = async () => {
      try {
        const job = await api(`/api/administration/partitioning-jobs/${jobId}`).then((r) => r.json());
        const elapsed = (job.finished_at || Date.now() / 1000) - job.started_at;
        summaryEl.textContent = `${job.status} (${fmtDuration(elapsed)} elapsed)` + (job.error ? " — " + job.error : "");
        logEl.textContent = (job.log || []).join("\n");
        logEl.scrollTop = logEl.scrollHeight;
        if (job.status === "running") {
          setTimeout(tick, 1000);
        } else if (job.status === "done") {
          loadAdministration();
          document.getElementById("btn-partitioning-advisor")?.click();
        }
      } catch (e) { console.error("partition convert job poll failed", e); }
    };
    tick();
  }

  // -------------------------------------------------------- vacuum modal
  const vacuumModal = document.getElementById("vacuum-modal");
  const vacuumOptFull = document.getElementById("vacuum-opt-full");
  const vacuumFullWarning = document.getElementById("vacuum-full-warning");
  let vacuumTarget = null;

  function openVacuumModal(schema, table) {
    vacuumTarget = { schema, table };
    document.getElementById("vacuum-target").textContent = `${schema}.${table}`;
    document.getElementById("vacuum-opt-analyze").checked = true;
    document.getElementById("vacuum-opt-freeze").checked = false;
    vacuumOptFull.checked = false;
    vacuumFullWarning.classList.add("hidden");
    document.getElementById("vacuum-status").innerHTML = "";
    const runBtn = document.getElementById("btn-run-vacuum");
    runBtn.disabled = false;
    runBtn.textContent = "Run VACUUM";
    vacuumModal.classList.remove("hidden");
  }
  function closeVacuumModal() {
    vacuumModal.classList.add("hidden");
    vacuumTarget = null;
  }
  vacuumOptFull.addEventListener("change", () => vacuumFullWarning.classList.toggle("hidden", !vacuumOptFull.checked));
  document.getElementById("btn-close-vacuum").addEventListener("click", closeVacuumModal);
  vacuumModal.addEventListener("click", (e) => { if (e.target === vacuumModal) closeVacuumModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !vacuumModal.classList.contains("hidden")) closeVacuumModal();
  });

  document.getElementById("btn-run-vacuum").addEventListener("click", async () => {
    if (!vacuumTarget) return;
    const analyze = document.getElementById("vacuum-opt-analyze").checked;
    const freeze = document.getElementById("vacuum-opt-freeze").checked;
    const full = vacuumOptFull.checked;
    if (full && !confirm(
      `Run VACUUM FULL on ${vacuumTarget.schema}.${vacuumTarget.table}?\n\nThis takes an ACCESS EXCLUSIVE lock, blocking ALL reads and writes on this table until it finishes. Continue?`
    )) return;

    const runBtn = document.getElementById("btn-run-vacuum");
    const statusEl = document.getElementById("vacuum-status");
    runBtn.disabled = true;
    runBtn.textContent = "Running VACUUM...";
    statusEl.innerHTML = "";
    try {
      const resp = await apiPost("/api/administration/vacuum", {
        schema: vacuumTarget.schema, table: vacuumTarget.table, analyze, freeze, full,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      const before = data.before || {};
      const after = data.after || {};
      statusEl.className = "add-db-status status-ok";
      statusEl.textContent = `Done in ${data.elapsed_seconds}s. Dead rows: ${fmtNum(before.n_dead_tup)} → ${fmtNum(after.n_dead_tup)}. Size: ${fmtBytes(before.size_bytes)} → ${fmtBytes(after.size_bytes)}.`;
      runBtn.textContent = "Run VACUUM";
      runBtn.disabled = false;
      loadAdministration();
    } catch (e) {
      statusEl.className = "add-db-status status-err";
      statusEl.textContent = "Failed: " + e.message;
      runBtn.disabled = false;
      runBtn.textContent = "Run VACUUM";
    }
  });

  // -------------------------------------------------------------- migration
  let migrationTables = [];       // last list-tables result, normalized
  let migrationPollTimer = null;
  let migrationJobId = null;

  function migrationSourceType() {
    return document.getElementById("migration-type-oracle").checked ? "oracle" : "postgres";
  }
  function migrationOracleMode() {
    return document.getElementById("migration-mode-full").checked ? "full" : "tables";
  }
  // PostgreSQL sources only ever do row-copy; Oracle sources choose between
  // row-copy and the full-schema (ora2pg) engine via migration-mode-section.
  function updateMigrationSectionVisibility() {
    const isOracle = migrationSourceType() === "oracle";
    const wantsFull = isOracle && migrationOracleMode() === "full";
    document.getElementById("migration-mode-section").classList.toggle("hidden", !isOracle);
    document.getElementById("migration-rowcopy-section").classList.toggle("hidden", wantsFull);
    document.getElementById("ora2pg-section").classList.toggle("hidden", !wantsFull);
  }
  document.querySelectorAll('input[name="migration-source-type"]').forEach((el) => {
    el.addEventListener("change", () => {
      const isOracle = migrationSourceType() === "oracle";
      document.getElementById("migration-form-oracle").classList.toggle("hidden", !isOracle);
      document.getElementById("migration-form-postgres").classList.toggle("hidden", isOracle);
      document.getElementById("migration-tables-section").classList.add("hidden");
      document.getElementById("migration-test-status").innerHTML = "";
      updateMigrationSectionVisibility();
    });
  });
  document.querySelectorAll('input[name="migration-oracle-mode"]').forEach((el) => {
    el.addEventListener("change", updateMigrationSectionVisibility);
  });

  function migrationConnParams() {
    if (migrationSourceType() === "oracle") {
      return {
        host: document.getElementById("mig-ora-host").value.trim(),
        port: document.getElementById("mig-ora-port").value || 1521,
        service_name: document.getElementById("mig-ora-service").value.trim(),
        username: document.getElementById("mig-ora-username").value.trim(),
        password: document.getElementById("mig-ora-password").value,
        owner: document.getElementById("mig-ora-owner").value.trim(),
      };
    }
    return {
      host: document.getElementById("mig-pg-host").value.trim(),
      port: document.getElementById("mig-pg-port").value || 5432,
      dbname: document.getElementById("mig-pg-dbname").value.trim(),
      username: document.getElementById("mig-pg-username").value.trim(),
      password: document.getElementById("mig-pg-password").value,
    };
  }

  function renderMigrationTables() {
    const filterText = document.getElementById("migration-tables-filter").value.trim().toLowerCase();
    const tbody = document.querySelector("#table-migration-tables tbody");
    const rows = migrationTables.filter((t) => t.label.toLowerCase().includes(filterText));
    tbody.innerHTML = rows.map((t) => `
      <tr>
        <td><input type="checkbox" class="migration-table-check" data-key="${esc(t.key)}" ${t.checked ? "checked" : ""}></td>
        <td class="mono">${esc(t.label)}</td>
        <td>${t.row_estimate != null ? fmtNum(Math.round(t.row_estimate)) : "—"}</td>
      </tr>`).join("") || `<tr><td colspan="3" class="empty-note">No tables match this filter.</td></tr>`;
    tbody.querySelectorAll(".migration-table-check").forEach((cb) => {
      cb.addEventListener("change", () => {
        const t = migrationTables.find((x) => x.key === cb.dataset.key);
        if (t) t.checked = cb.checked;
      });
    });
  }
  document.getElementById("migration-tables-filter")?.addEventListener("input", renderMigrationTables);
  document.getElementById("migration-select-all")?.addEventListener("change", (e) => {
    migrationTables.forEach((t) => { t.checked = e.target.checked; });
    renderMigrationTables();
  });

  document.getElementById("btn-migration-test").addEventListener("click", async () => {
    const statusEl = document.getElementById("migration-test-status");
    const btn = document.getElementById("btn-migration-test");
    const params = migrationConnParams();
    const type = migrationSourceType();
    btn.disabled = true; btn.textContent = "Testing...";
    statusEl.className = "add-db-status";
    statusEl.textContent = "";
    document.getElementById("migration-tables-section").classList.add("hidden");
    try {
      const testResp = await apiPost(type === "oracle" ? "/api/migration/test-oracle" : "/api/migration/test-postgres", params);
      const testData = await testResp.json();
      if (!testResp.ok) throw new Error(testData.detail || testResp.statusText);

      const tablesResp = await apiPost(type === "oracle" ? "/api/migration/tables-oracle" : "/api/migration/tables-postgres", params);
      const tablesData = await tablesResp.json();
      if (!tablesResp.ok) throw new Error(tablesData.detail || tablesResp.statusText);

      migrationTables = (tablesData || []).map((t) => (
        type === "oracle"
          ? { key: t.table_name, label: t.table_name, row_estimate: t.row_estimate, checked: false, oracle_table: t.table_name }
          : { key: `${t.schema_name}.${t.table_name}`, label: `${t.schema_name}.${t.table_name}`, row_estimate: t.row_estimate, checked: false, schema_name: t.schema_name, table_name: t.table_name }
      ));
      statusEl.className = "add-db-status status-ok";
      statusEl.textContent = `Connected${testData.version ? " (" + testData.version + ")" : ""}. Found ${migrationTables.length} table(s).`;
      document.getElementById("migration-select-all").checked = false;
      renderMigrationTables();
      document.getElementById("migration-tables-section").classList.remove("hidden");
    } catch (e) {
      statusEl.className = "add-db-status status-err";
      statusEl.textContent = "Failed: " + e.message;
    } finally {
      btn.disabled = false; btn.textContent = "Test & list tables";
    }
  });

  function migrationStatusBadgeClass(status) {
    if (status === "done") return "stat-good";
    if (status === "error") return "stat-bad";
    if (status === "running") return "";
    return "";
  }

  function renderMigrationProgress(job) {
    document.getElementById("migration-progress-section").classList.remove("hidden");
    const elapsed = job.finished_at ? (job.finished_at - job.started_at) : (Date.now() / 1000 - job.started_at);
    document.getElementById("migration-job-summary").textContent =
      `Status: ${job.status} — ${job.tables.filter((t) => t.status === "done").length} of ${job.tables.length} table(s) done, ${fmtDuration(elapsed)} elapsed.`;
    document.querySelector("#table-migration-progress tbody").innerHTML = job.tables.map((t) => `
      <tr>
        <td class="mono">${esc(t.source)}</td>
        <td class="mono">${esc(t.dest)}</td>
        <td class="${migrationStatusBadgeClass(t.status)}">${esc(t.status)}${t.error ? ` — ${esc(t.error)}` : ""}</td>
        <td>${fmtNum(t.rows_copied)}${t.row_estimate != null ? ` / ~${fmtNum(Math.round(t.row_estimate))}` : ""}</td>
      </tr>`).join("");
    document.getElementById("btn-migration-cancel").disabled = job.status !== "running";
  }

  function stopMigrationPoll() {
    if (migrationPollTimer) { clearInterval(migrationPollTimer); migrationPollTimer = null; }
  }

  async function pollMigrationJob() {
    if (!migrationJobId) return;
    try {
      const resp = await api(`/api/migration/jobs/${migrationJobId}`);
      const job = await resp.json();
      if (!resp.ok) throw new Error(job.detail || resp.statusText);
      renderMigrationProgress(job);
      if (job.status !== "running") stopMigrationPoll();
    } catch (e) {
      console.error("migration job poll failed", e);
      stopMigrationPoll();
    }
  }

  document.getElementById("btn-migration-start").addEventListener("click", async () => {
    const selected = migrationTables.filter((t) => t.checked);
    if (selected.length === 0) { alert("Select at least one table."); return; }
    const type = migrationSourceType();
    const params = migrationConnParams();
    const body = {
      ...params,
      dest_schema: document.getElementById("migration-dest-schema").value.trim() || "public",
      create_if_missing: document.getElementById("migration-create-if-missing").checked,
      truncate_first: document.getElementById("migration-truncate-first").checked,
    };
    if (body.truncate_first && !confirm(`This will TRUNCATE ${selected.length} destination table(s) before loading, permanently deleting any rows already there. Continue?`)) {
      return;
    }
    if (type === "oracle") {
      body.tables = selected.map((t) => t.oracle_table);
    } else {
      body.tables = selected.map((t) => ({ schema_name: t.schema_name, table_name: t.table_name }));
    }

    const startBtn = document.getElementById("btn-migration-start");
    startBtn.disabled = true;
    try {
      const resp = await apiPost(type === "oracle" ? "/api/migration/start-oracle" : "/api/migration/start-postgres", body);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      migrationJobId = data.job_id;
      stopMigrationPoll();
      migrationPollTimer = setInterval(pollMigrationJob, 1500);
      pollMigrationJob();
    } catch (e) {
      alert("Failed to start migration: " + e.message);
    } finally {
      startBtn.disabled = false;
    }
  });

  document.getElementById("btn-migration-cancel").addEventListener("click", async () => {
    if (!migrationJobId) return;
    if (!confirm("Cancel this migration? Tables already finished stay as they are; the table in progress will stop partway through.")) return;
    try {
      await apiPost(`/api/migration/jobs/${migrationJobId}/cancel`);
    } catch (e) { console.error("cancel failed", e); }
  });

  dtabLoaders.migration = () => {
    updateMigrationSectionVisibility();
    if (migrationJobId && !migrationPollTimer) {
      migrationPollTimer = setInterval(pollMigrationJob, 1500);
      pollMigrationJob();
    }
    checkOra2pgAvailable();
    if (ora2pgJobId && !ora2pgPollTimer) {
      ora2pgPollTimer = setInterval(pollOra2pgJob, 1500);
      pollOra2pgJob();
    }
  };

  // ------------------------------------------------- full schema (ora2pg)
  let ora2pgJobId = null;
  let ora2pgPollTimer = null;

  async function checkOra2pgAvailable() {
    const box = document.getElementById("ora2pg-unavailable");
    const startBtn = document.getElementById("btn-ora2pg-start");
    try {
      const resp = await api("/api/migration/ora2pg-available");
      const data = await resp.json();
      if (!resp.ok || !data.ok) {
        box.textContent = "Full schema migration is unavailable: " + (data.reason || resp.statusText);
        box.classList.remove("hidden");
        startBtn.disabled = true;
      } else {
        box.classList.add("hidden");
        startBtn.disabled = false;
      }
    } catch (e) {
      box.textContent = "Could not check ora2pg availability: " + e.message;
      box.classList.remove("hidden");
      startBtn.disabled = true;
    }
  }

  function renderOra2pgProgress(job) {
    document.getElementById("ora2pg-progress-section").classList.remove("hidden");
    const elapsed = job.finished_at ? (job.finished_at - job.started_at) : (Date.now() / 1000 - job.started_at);
    document.getElementById("ora2pg-job-summary").textContent =
      `Status: ${job.status} (${job.phase}) — ${job.source_label} → ${job.dest_label}, ${fmtDuration(elapsed)} elapsed.` +
      (job.error ? ` Error: ${job.error}` : "");
    const logEl = document.getElementById("ora2pg-log");
    const wasAtBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 4;
    logEl.textContent = (job.log || []).join("\n");
    if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
    document.getElementById("btn-ora2pg-cancel").disabled = job.status !== "running";
  }

  function stopOra2pgPoll() {
    if (ora2pgPollTimer) { clearInterval(ora2pgPollTimer); ora2pgPollTimer = null; }
  }

  async function pollOra2pgJob() {
    if (!ora2pgJobId) return;
    try {
      const resp = await api(`/api/migration/ora2pg-jobs/${ora2pgJobId}`);
      const job = await resp.json();
      if (!resp.ok) throw new Error(job.detail || resp.statusText);
      renderOra2pgProgress(job);
      if (job.status !== "running") stopOra2pgPoll();
    } catch (e) {
      console.error("ora2pg job poll failed", e);
      stopOra2pgPoll();
    }
  }

  document.getElementById("btn-ora2pg-start").addEventListener("click", async () => {
    const params = migrationConnParams(); // always Oracle here -- section is hidden otherwise
    const migrateData = document.getElementById("ora2pg-migrate-data").checked;
    const body = {
      ...params,
      dest_schema: document.getElementById("ora2pg-dest-schema").value.trim() || "public",
      migrate_data: migrateData,
    };
    const warning = migrateData
      ? `This will create schema objects (tables, views, sequences, triggers, functions...) for ${params.owner || params.username} in the "${body.dest_schema}" schema of this database, and load all table data. Continue?`
      : `This will create schema objects (tables, views, sequences, triggers, functions...) for ${params.owner || params.username} in the "${body.dest_schema}" schema of this database. No data will be loaded. Continue?`;
    if (!confirm(warning)) return;

    const startBtn = document.getElementById("btn-ora2pg-start");
    startBtn.disabled = true;
    try {
      const resp = await apiPost("/api/migration/start-ora2pg", body);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      ora2pgJobId = data.job_id;
      stopOra2pgPoll();
      ora2pgPollTimer = setInterval(pollOra2pgJob, 1500);
      pollOra2pgJob();
    } catch (e) {
      alert("Failed to start full schema migration: " + e.message);
    } finally {
      startBtn.disabled = false;
    }
  });

  document.getElementById("btn-ora2pg-cancel").addEventListener("click", async () => {
    if (!ora2pgJobId) return;
    if (!confirm("Cancel this migration? Objects/data already created stay as they are; the step in progress will stop partway through.")) return;
    try {
      await apiPost(`/api/migration/ora2pg-jobs/${ora2pgJobId}/cancel`);
    } catch (e) { console.error("cancel failed", e); }
  });

  // --------------------------------------------------- performance hub
  const WAIT_EVENT_COLORS = {
    "Running (CPU)": "#34d399", "Lock": "#f87171", "LWLock": "#fb923c", "BufferPin": "#fbbf24",
    "Activity": "#60a5fa", "Client": "#3b82f6", "Extension": "#a78bfa", "IPC": "#818cf8",
    "IO": "#f472b6", "Timeout": "#94a3b8",
  };
  function waitColor(bucket) { return WAIT_EVENT_COLORS[bucket] || "#93a1c2"; }

  function fmtPgSize(setting) {
    if (!setting) return "—";
    const value = Number(setting.value);
    const unit = setting.unit;
    let bytes;
    if (unit === "8kB") bytes = value * 8192;
    else if (unit === "kB") bytes = value * 1024;
    else if (unit === "MB") bytes = value * 1024 * 1024;
    else return `${setting.value}${unit ? " " + unit : ""}`;
    return fmtBytes(bytes);
  }

  let perfRangeHours = 0.25;
  let perfCustomRange = null; // {start, end} ISO strings (UTC), or null when using a relative preset
  let ioChart, waitEventsChart;
  let perfPoll = null;
  function stopPerfPoll() { if (perfPoll) { clearInterval(perfPoll); perfPoll = null; } }

  const perfCustomPanel = document.getElementById("perf-custom-range");

  document.querySelectorAll(".perf-range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".perf-range-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      if (btn.dataset.hours === "custom") {
        perfCustomPanel.classList.remove("hidden");
        return; // wait for Apply -- don't reload yet
      }
      perfCustomPanel.classList.add("hidden");
      perfCustomRange = null;
      perfRangeHours = Number(btn.dataset.hours);
      loadPerformanceHistory();
    });
  });

  document.getElementById("perf-custom-apply")?.addEventListener("click", () => {
    const startEl = document.getElementById("perf-custom-start");
    const endEl = document.getElementById("perf-custom-end");
    if (!startEl.value || !endEl.value) { alert("Pick both a start and end time."); return; }
    const start = new Date(startEl.value);
    const end = new Date(endEl.value);
    if (start >= end) { alert("Start must be before end."); return; }
    perfCustomRange = { start: start.toISOString(), end: end.toISOString() };
    loadPerformanceHistory();
  });

  async function loadPerformanceCurrent() {
    try {
      const p = await api("/api/performance").then((r) => r.json());

      const io = p.io_rate || {};
      const setIo = (k, v) => { const el = document.querySelector(`[data-iostat="${k}"]`); if (el) el.textContent = v; };
      const ioNote = document.getElementById("io-unavailable-note");
      if (io.available) {
        ioNote.classList.add("hidden");
        setIo("reads_per_sec", `${fmtNum(io.reads_per_sec)}/s`);
        setIo("hits_per_sec", `${fmtNum(io.hits_per_sec)}/s`);
        setIo("writes_per_sec", `${fmtNum(io.writes_per_sec)}/s`);
      } else {
        ioNote.classList.remove("hidden");
        ioNote.textContent = io.reason || "Not enough monitoring history yet -- check back in a minute.";
        setIo("reads_per_sec", "—"); setIo("hits_per_sec", "—"); setIo("writes_per_sec", "—");
      }

      // waitEvents.total counts every currently-active session (idle excluded),
      // including ones with no wait_event_type at all -- i.e. genuinely running
      // on CPU, not blocked on anything. So this is "active right now", not
      // "waiting right now"; the table below breaks out how many of them, if
      // any, are actually waiting vs. just running.
      const waitEvents = p.wait_events || {};
      const totalBadge = document.getElementById("wait-event-total-badge");
      const total = waitEvents.total || 0;
      totalBadge.textContent = total === 1 ? "1 active session" : `${fmtNum(total)} active sessions`;
      totalBadge.className = "badge " + (total > 0 ? "badge-attr-elevated" : "badge-muted");

      const detailRows = waitEvents.detail || [];
      const detailTbody = document.querySelector("#table-wait-detail tbody");
      const detailEmpty = document.getElementById("wait-detail-empty");
      if (detailRows.length) {
        detailEmpty.style.display = "none";
        detailTbody.innerHTML = detailRows.map((d) => `
          <tr>
            <td><span class="wait-event-badge"><span class="wait-event-dot" style="background:${waitColor(d.wait_event_type)}"></span>${esc(d.wait_event_type)}</span></td>
            <td class="mono">${esc(d.wait_event)}</td>
            <td>${fmtNum(d.n)}</td>
            <td>${fmtDuration(d.max_seconds)}</td>
            <td><span class="q-snippet" title="${esc(d.sample_query || "")}">${esc(d.sample_query || "")}</span></td>
          </tr>`).join("");
      } else {
        detailTbody.innerHTML = "";
        detailEmpty.style.display = "block";
      }

      const mem = p.memory || {};
      const s = mem.settings || {};
      const setMem = (k, v, title) => {
        const el = document.querySelector(`[data-memstat="${k}"]`);
        if (el) { el.textContent = v; if (title) el.closest(".stat-card").title = title; }
      };
      setMem("cache_hit", mem.cache_hit_ratio_pct != null ? `${mem.cache_hit_ratio_pct}%` : "—");
      setMem("shared_buffers", fmtPgSize(s.shared_buffers));
      setMem("effective_cache_size", fmtPgSize(s.effective_cache_size));
      setMem("work_mem", fmtPgSize(s.work_mem));
      setMem("maintenance_work_mem", fmtPgSize(s.maintenance_work_mem));
      setMem("wal_buffers", fmtPgSize(s.wal_buffers));
      setMem("temp", `${fmtNum(mem.temp_files)} / ${fmtBytes(mem.temp_bytes)}`);
      if (mem.buffercache) {
        const total = mem.buffercache.total_buffers, used = mem.buffercache.used_buffers;
        const pct = total ? Math.round((100 * used) / total) : null;
        setMem("buffercache", pct != null ? `${pct}% (${fmtNum(used)}/${fmtNum(total)})` : "—");
      } else {
        setMem("buffercache", "unavailable", mem.buffercache_error || "");
      }
    } catch (e) { console.error("performance current load failed", e); }
  }

  function buildMultiLineChart(canvas, seriesMeta) {
    return new Chart(canvas, {
      type: "line",
      data: { labels: [], datasets: seriesMeta.map((s) => ({
        label: s.label, data: [], borderColor: s.color, backgroundColor: s.color + "33",
        tension: 0.25, fill: true, pointRadius: 0,
      })) },
      options: {
        responsive: true,
        scales: {
          x: { ticks: { color: "#93a1c2", maxTicksLimit: 8 }, grid: { color: "#253154" } },
          y: { ticks: { color: "#93a1c2" }, grid: { color: "#253154" }, beginAtZero: true },
        },
        plugins: { legend: { display: true, labels: { color: "#93a1c2", boxWidth: 12, font: { size: 11 } } } },
      },
    });
  }

  function perfHistoryParams() {
    return perfCustomRange
      ? `start=${encodeURIComponent(perfCustomRange.start)}&end=${encodeURIComponent(perfCustomRange.end)}`
      : `hours=${perfRangeHours}`;
  }

  async function loadPerformanceHistory() {
    try {
      const ioRows = await api(`/api/performance/io-history?${perfHistoryParams()}`).then((r) => r.json());
      if (!ioChart) {
        ioChart = buildMultiLineChart(document.getElementById("chart-io"), [
          { label: "Reads/sec (disk)", color: "#f472b6" },
          { label: "Hits/sec (cache)", color: "#3b82f6" },
          { label: "Writes/sec", color: "#fbbf24" },
        ]);
      }
      ioChart.data.labels = ioRows.map((r) => timeLabel(r.captured_at));
      ioChart.data.datasets[0].data = ioRows.map((r) => r.reads_per_sec);
      ioChart.data.datasets[1].data = ioRows.map((r) => r.hits_per_sec);
      ioChart.data.datasets[2].data = ioRows.map((r) => r.writes_per_sec);
      ioChart.update();
    } catch (e) { console.error("io history load failed", e); }

    try {
      const weRows = await api(`/api/performance/wait-events-history?${perfHistoryParams()}`).then((r) => r.json());
      const bucketNames = Array.from(new Set(weRows.flatMap((r) => Object.keys(r.buckets || {}))));
      const hasWaitData = bucketNames.length > 0;
      const weCanvas = document.getElementById("chart-wait-events");
      const weEmptyEl = document.getElementById("wait-events-chart-empty");

      // Unhide before Chart.js touches the canvas (creation or update) so it
      // never gets sized against a display:none container -- only hide again
      // afterward, once we know for sure there's nothing to show.
      weCanvas.classList.remove("hidden");
      weEmptyEl.classList.add("hidden");

      if (!waitEventsChart) {
        waitEventsChart = new Chart(weCanvas, {
          type: "line",
          data: { labels: [], datasets: [] },
          options: {
            responsive: true,
            scales: {
              x: { ticks: { color: "#93a1c2", maxTicksLimit: 8 }, grid: { color: "#253154" } },
              y: { ticks: { color: "#93a1c2" }, grid: { color: "#253154" }, beginAtZero: true, stacked: true },
            },
            plugins: { legend: { display: true, labels: { color: "#93a1c2", boxWidth: 12, font: { size: 11 } } } },
          },
        });
      }
      waitEventsChart.data.labels = weRows.map((r) => timeLabel(r.captured_at));
      waitEventsChart.data.datasets = bucketNames.map((name) => ({
        label: name, data: weRows.map((r) => (r.buckets || {})[name] || 0),
        borderColor: waitColor(name), backgroundColor: waitColor(name) + "55", fill: true, tension: 0.2, pointRadius: 0,
      }));
      waitEventsChart.update();

      if (!hasWaitData) {
        weCanvas.classList.add("hidden");
        weEmptyEl.classList.remove("hidden");
      }
    } catch (e) { console.error("wait events history load failed", e); }
  }

  dtabLoaders.performance = () => {
    loadPerformanceCurrent();
    loadPerformanceHistory();
    stopPerfPoll();
    perfPoll = setInterval(() => {
      loadPerformanceCurrent();
      // A fixed custom historical window shouldn't keep changing underfoot;
      // only relative presets (Real-Time/1H/6H/24H) refresh automatically.
      if (!perfCustomRange) loadPerformanceHistory();
    }, 180000); // 3 minutes
  };

  document.getElementById("btn-performance-refresh")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "Refreshing...";
    try {
      await Promise.all([loadPerformanceCurrent(), loadPerformanceHistory()]);
    } finally {
      btn.disabled = false;
      btn.textContent = "↻ Refresh";
    }
  });

  document.getElementById("btn-performance-review")?.addEventListener("click", (e) =>
    runAssessment(e.currentTarget, "ai-performance-card", "✦ Analyzing...", "✦ AI Performance Review", "/api/ai/performance-review"));

  // ------------------------------------------------------------- chat
  const chatMessages = []; // [{role, content}]
  const chatWindow = document.getElementById("chat-window");
  const CHAT_GREETING = 'Hi! I can see this database’s live metrics. Ask me things like “why is the DB slow right now?” or “is autovacuum keeping up?”';

  function resetChat() {
    chatMessages.length = 0;
    if (chatWindow) {
      chatWindow.innerHTML = "";
      appendChatMsg("assistant", CHAT_GREETING);
    }
  }

  function appendChatMsg(role, text, pending) {
    const div = document.createElement("div");
    div.className = `chat-msg chat-${role}` + (pending ? " pending" : "");
    div.textContent = text;
    chatWindow.appendChild(div);
    chatWindow.scrollTop = chatWindow.scrollHeight;
    return div;
  }

  async function sendChat() {
    const input = document.getElementById("chat-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    appendChatMsg("user", text);
    chatMessages.push({ role: "user", content: text });
    const pendingEl = appendChatMsg("assistant", "Thinking...", true);
    try {
      const resp = await apiPost("/api/ai/chat", { messages: chatMessages });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      pendingEl.classList.remove("pending");
      pendingEl.textContent = data.reply;
      chatMessages.push({ role: "assistant", content: data.reply });
    } catch (err) {
      pendingEl.classList.remove("pending");
      pendingEl.textContent = "Error: " + err.message;
    }
  }

  document.getElementById("btn-chat-send")?.addEventListener("click", sendChat);
  document.getElementById("chat-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });

  // ---------------------------------------------------- AI provider settings
  // Global (not per-database) -- Anthropic's cloud API, or any
  // OpenAI-compatible endpoint (self-hosted/local models, an internal
  // enterprise LLM gateway, Azure OpenAI, ...). Applies to every AI
  // feature across the app, not just this tab.
  function applyAiEnabledState(enabled) {
    document.querySelectorAll(".ai-disabled-notice").forEach((el) => el.classList.toggle("hidden", enabled));
    document.querySelectorAll(".btn-ai").forEach((el) => { el.disabled = !enabled; });
    const queryInput = document.getElementById("query-input");
    const chatInput = document.getElementById("chat-input");
    if (queryInput) queryInput.disabled = !enabled;
    if (chatInput) chatInput.disabled = !enabled;
  }

  function updateAiProviderHints() {
    const provider = document.getElementById("ai-provider-select").value;
    const baseUrlInput = document.getElementById("ai-base-url-input");
    const baseUrlHint = document.getElementById("ai-base-url-hint");
    const modelInput = document.getElementById("ai-model-input");
    if (provider === "openai_compatible") {
      baseUrlHint.textContent = "(required -- e.g. http://internal-llm.yourcompany.com/v1)";
      baseUrlInput.placeholder = "http://internal-llm.yourcompany.com/v1";
      modelInput.placeholder = "e.g. llama-3.1-70b-instruct";
    } else {
      baseUrlHint.textContent = "(optional -- only needed for an Anthropic-compatible enterprise gateway)";
      baseUrlInput.placeholder = "https://api.anthropic.com";
      modelInput.placeholder = "claude-sonnet-5";
    }
  }
  document.getElementById("ai-provider-select")?.addEventListener("change", updateAiProviderHints);

  async function loadAiSettings() {
    try {
      const s = await apiGlobal("/api/ai/settings").then((r) => r.json());
      document.getElementById("ai-provider-select").value = s.provider;
      document.getElementById("ai-model-input").value = s.model || "";
      document.getElementById("ai-base-url-input").value = s.base_url || "";
      document.getElementById("ai-api-key-input").value = "";
      updateAiProviderHints();
      const keyHint = document.getElementById("ai-api-key-hint");
      const statusEl = document.getElementById("ai-settings-status");
      if (s.has_api_key) {
        keyHint.textContent = s.key_source === "database" ? "(a key is saved -- leave blank to keep it)" : "(using ANTHROPIC_API_KEY from the environment -- leave blank to keep it)";
        statusEl.textContent = `Currently using ${s.provider === "anthropic" ? "Anthropic" : "an OpenAI-compatible endpoint"}${s.model ? " (" + s.model + ")" : ""}.`;
      } else {
        keyHint.textContent = "(required)";
        statusEl.textContent = "No AI provider configured yet.";
      }
      applyAiEnabledState(s.has_api_key);
    } catch (e) { console.error("AI settings load failed", e); }
  }
  dtabLoaders.assistant = loadAiSettings;

  document.getElementById("btn-ai-settings-toggle")?.addEventListener("click", () => {
    document.getElementById("ai-settings-panel").classList.toggle("hidden");
  });

  function aiSettingsFormPayload() {
    return {
      provider: document.getElementById("ai-provider-select").value,
      api_key: document.getElementById("ai-api-key-input").value.trim(),
      base_url: document.getElementById("ai-base-url-input").value.trim(),
      model: document.getElementById("ai-model-input").value.trim(),
    };
  }

  document.getElementById("btn-ai-settings-test")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const statusEl = document.getElementById("ai-settings-form-status");
    btn.disabled = true; btn.textContent = "Testing...";
    statusEl.className = "add-db-status";
    statusEl.textContent = "";
    try {
      const resp = await apiGlobalPost("/api/ai/settings/test", aiSettingsFormPayload());
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      statusEl.className = "add-db-status status-ok";
      statusEl.textContent = `Success -- the model replied: "${data.reply.trim()}"`;
    } catch (err) {
      statusEl.className = "add-db-status status-err";
      statusEl.textContent = "Failed: " + err.message;
    } finally {
      btn.disabled = false; btn.textContent = "Test connection";
    }
  });

  document.getElementById("btn-ai-settings-save")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const statusEl = document.getElementById("ai-settings-form-status");
    btn.disabled = true; btn.textContent = "Saving...";
    statusEl.className = "add-db-status";
    statusEl.textContent = "";
    try {
      const resp = await apiGlobalPost("/api/ai/settings", aiSettingsFormPayload());
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      statusEl.className = "add-db-status status-ok";
      statusEl.textContent = "Saved.";
      await loadAiSettings();
    } catch (err) {
      statusEl.className = "add-db-status status-err";
      statusEl.textContent = "Failed: " + err.message;
    } finally {
      btn.disabled = false; btn.textContent = "Save";
    }
  });

  document.getElementById("btn-ai-settings-clear")?.addEventListener("click", async (e) => {
    if (!confirm("Reset AI provider settings back to the environment default (ANTHROPIC_API_KEY)? This clears any key/model/base URL saved here.")) return;
    const btn = e.currentTarget;
    const statusEl = document.getElementById("ai-settings-form-status");
    btn.disabled = true;
    try {
      const resp = await fetch(BASE + "/api/ai/settings", { method: "DELETE", credentials: "same-origin" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      statusEl.className = "add-db-status status-ok";
      statusEl.textContent = "Reset.";
      await loadAiSettings();
    } catch (err) {
      statusEl.className = "add-db-status status-err";
      statusEl.textContent = "Failed: " + err.message;
    } finally {
      btn.disabled = false;
    }
  });

  // ------------------------------------------------------------- init
  showScreen("databases");
})();
