/* Watchdog dashboard — vanilla ES2020, no dependencies. */
"use strict";

const state = {
  view: "overview",
  token: sessionStorage.getItem("watchdog.token") || "",
  health: null,
  overview: null,
  configDirty: false,
  authPrompted: false,
  runs: { offset: 0, limit: 25 },
  listings: { offset: 0, limit: 25, kind: "", source: "", q: "" },
  alerts: { offset: 0, limit: 25, status: "", rule: "", channel: "" },
  runPoll: null,
};

const PAGE_META = {
  overview: {
    eyebrow: "Operations",
    title: "Overview",
    subtitle: "Live state of your job and price watchdog.",
  },
  runs: {
    eyebrow: "History",
    title: "Runs",
    subtitle: "Every pipeline execution, its totals, and any errors.",
  },
  listings: {
    eyebrow: "Inventory",
    title: "Listings",
    subtitle: "Normalized jobs and products collected by the ETL.",
  },
  alerts: {
    eyebrow: "Delivery",
    title: "Alerts",
    subtitle: "The idempotent ledger behind every notification and retry.",
  },
  config: {
    eyebrow: "Control",
    title: "Configuration",
    subtitle: "Edit rules and thresholds with the same strict validation as the CLI.",
  },
};

const STATUS_LABELS = {
  ok: "OK",
  partial: "Partial",
  failed: "Failed",
  running: "Running",
  sent: "Sent",
  console: "Console",
  suppressed: "Suppressed",
  dry_run: "Dry run",
  skipped: "Skipped",
  never: "Never run",
};

const REASON_LABELS = {
  new_match: "New match",
  below_target: "Below target",
  new_low: "New low",
};

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ESCAPES[character]);
}

function safeUrl(value) {
  try {
    const url = new URL(String(value), window.location.origin);
    if (url.protocol === "http:" || url.protocol === "https:") return esc(url.href);
  } catch (error) {
    /* fall through */
  }
  return "#";
}

function statusLabel(status) {
  return STATUS_LABELS[status] || String(status || "Unknown");
}

function statusBadge(status) {
  const key = status || "neutral";
  const css = ["ok", "partial", "failed", "running", "sent", "console", "suppressed", "dry_run", "skipped"].includes(key)
    ? key
    : "neutral";
  return `<span class="badge ${css}">${esc(statusLabel(status))}</span>`;
}

function kindChip(kind) {
  if (kind === "job") return '<span class="chip kind-job">Job</span>';
  if (kind === "price") return '<span class="chip kind-price">Product</span>';
  return `<span class="chip">${esc(kind || "—")}</span>`;
}

function fmtDate(value, withTime = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return esc(value);
  return date.toLocaleString(undefined, withTime
    ? { dateStyle: "medium", timeStyle: "short" }
    : { dateStyle: "medium" });
}

function relativeTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  const future = seconds < 0;
  const absolute = Math.abs(seconds);
  let amount;
  let unit;
  if (absolute < 60) return future ? "in a moment" : "just now";
  if (absolute < 3600) {
    amount = Math.round(absolute / 60);
    unit = "minute";
  } else if (absolute < 86400) {
    amount = Math.round(absolute / 3600);
    unit = "hour";
  } else {
    amount = Math.round(absolute / 86400);
    unit = "day";
  }
  return future ? `in ${amount} ${unit}${amount === 1 ? "" : "s"}` : `${amount} ${unit}${amount === 1 ? "" : "s"} ago`;
}

function fmtDuration(start, end) {
  if (!start) return "—";
  const from = new Date(start).getTime();
  const to = end ? new Date(end).getTime() : Date.now();
  if (Number.isNaN(from) || Number.isNaN(to)) return "—";
  const seconds = Math.max(0, (to - from) / 1000);
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function fmtNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString();
}

function fmtMoney(value, currency) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return `${esc(currency || "")} ${esc(value)}`.trim();
  const formatted = number.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${currency ? `${esc(currency)} ` : ""}${formatted}`;
}

function salaryText(listing) {
  const currency = listing.salary_currency || "";
  const period = listing.salary_period ? `/${listing.salary_period}` : "";
  const minimum = listing.salary_min === null || listing.salary_min === undefined
    ? null
    : Number(listing.salary_min);
  const maximum = listing.salary_max === null || listing.salary_max === undefined
    ? null
    : Number(listing.salary_max);
  if (minimum === null && maximum === null) return "—";
  if (minimum !== null && maximum !== null) {
    if (minimum === maximum) return `${esc(currency)} ${fmtNumber(minimum)}${esc(period)}`.trim();
    return `${esc(currency)} ${fmtNumber(minimum)} – ${fmtNumber(maximum)}${esc(period)}`.trim();
  }
  if (minimum !== null) return `${esc(currency)} ${fmtNumber(minimum)}+${esc(period)}`.trim();
  return `${esc(currency)} up to ${fmtNumber(maximum)}${esc(period)}`.trim();
}

function listingValue(listing) {
  if (listing.kind === "price") return fmtMoney(listing.price, listing.currency);
  return salaryText(listing);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (state.token) headers.set("X-Watchdog-Token", state.token);
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (error) {
      data = { error: text };
    }
  }
  if (response.status === 401) {
    promptAuth("The dashboard token was rejected. Check WATCHDOG_UI_TOKEN and try again.");
    throw new Error("Authentication required");
  }
  if (!response.ok) {
    const message = data && data.error ? data.error : `Request failed (${response.status})`;
    throw new Error(message);
  }
  return data;
}

/* ---------------------------------------------------------------- shell -- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("watchdog.theme", theme);
  } catch (error) {
    /* ignore storage failures */
  }
}

function initTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem("watchdog.theme");
  } catch (error) {
    /* ignore */
  }
  if (stored === "light" || stored === "dark") {
    applyTheme(stored);
    return;
  }
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(prefersDark ? "dark" : "light");
}

const LAYOUTS = ["auto", "desktop", "mobile"];
const LAYOUT_KEY = "watchdog.layout";

function applyLayout(layout) {
  const next = LAYOUTS.includes(layout) ? layout : "auto";
  document.documentElement.dataset.layout = next;
  try {
    localStorage.setItem(LAYOUT_KEY, next);
  } catch (error) {
    /* ignore storage failures */
  }
  document.querySelectorAll("#layout-switch [data-layout]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.layout === next));
  });
}

function initLayout() {
  let stored = "auto";
  try {
    stored = localStorage.getItem(LAYOUT_KEY) || "auto";
  } catch (error) {
    /* ignore */
  }
  applyLayout(stored);
  const switcher = $("#layout-switch");
  if (switcher) {
    switcher.addEventListener("click", (event) => {
      const button = event.target.closest("[data-layout]");
      if (button) applyLayout(button.dataset.layout);
    });
  }
}

function showToast(message, kind = "info") {
  const stack = $("#toast-stack");
  const toast = document.createElement("div");
  toast.className = `toast ${kind}`;
  toast.textContent = message;
  stack.appendChild(toast);
  window.setTimeout(() => {
    toast.style.opacity = "0";
    window.setTimeout(() => toast.remove(), 180);
  }, 4200);
}

function showError(message) {
  const banner = $("#global-error");
  banner.textContent = message;
  banner.hidden = false;
}

function clearError() {
  const banner = $("#global-error");
  banner.textContent = "";
  banner.hidden = true;
}

function handleLoadError(error) {
  if (error.message !== "Authentication required") showError(error.message);
}

function setConnection(status, label) {
  const dot = $("#conn-dot");
  dot.classList.toggle("is-online", status === "online");
  dot.classList.toggle("is-offline", status === "offline");
  $("#conn-status").textContent = label;
}

async function checkHealth() {
  try {
    const health = await api("/api/health");
    state.health = health;
    setConnection("online", `API online · v${health.version}`);
    if (health.auth_required && !state.token && !state.authPrompted) promptAuth();
  } catch (error) {
    setConnection("offline", "API unavailable");
  }
}

function switchView(view) {
  if (!PAGE_META[view]) return;
  state.view = view;
  $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view));
  $$(".view").forEach((section) => {
    section.hidden = section.id !== `view-${view}`;
  });
  $("#view-eyebrow").textContent = PAGE_META[view].eyebrow;
  $("#view-title").textContent = PAGE_META[view].title;
  $("#view-subtitle").textContent = PAGE_META[view].subtitle;
  clearError();
  loadView();
}

async function loadView() {
  if (state.view === "overview") return loadOverview();
  if (state.view === "runs") return loadRuns();
  if (state.view === "listings") return loadListings();
  if (state.view === "alerts") return loadAlerts();
  if (state.view === "config") return loadConfig();
  return undefined;
}

/* -------------------------------------------------------------- overview -- */

async function loadOverview() {
  try {
    const data = await api("/api/overview");
    state.overview = data;
    renderStats(data.summary || {});
    renderRules(data.rules || []);
    renderChannels(data.channels || []);
    renderRunState(data.run);
    renderRuns(data.recent_runs || [], $("#overview-runs"), true);
    renderAlerts(data.recent_alerts || [], $("#overview-alerts"), true);
    renderPriceWatch(data.price_watch || []);
  } catch (error) {
    handleLoadError(error);
  }
}

function renderStats(summary) {
  const listings = Number(summary.listings || 0);
  const priceListings = Number(summary.price_listings || 0);
  $("#stat-listings").textContent = fmtNumber(listings);
  $("#stat-listings-sub").textContent = `${fmtNumber(priceListings)} products · ${fmtNumber(listings - priceListings)} jobs`;

  $("#stat-prices").textContent = fmtNumber(summary.price_observations || 0);
  $("#stat-prices-sub").textContent = "exact decimal observations stored";

  $("#stat-alerts").textContent = fmtNumber(summary.alerts_sent || 0);
  const ledgerRows = Number(summary.alerts || 0);
  $("#stat-alerts-sub").textContent = `${fmtNumber(ledgerRows)} total ledger rows`;

  const lastRun = summary.last_run || null;
  const value = $("#stat-last-run");
  value.classList.add("text");
  if (!lastRun) {
    value.textContent = "—";
    $("#stat-last-run-sub").textContent = "no runs recorded yet";
    return;
  }
  value.innerHTML = statusBadge(lastRun.status);
  $("#stat-last-run-sub").textContent = `${fmtDate(lastRun.started_at)} · ${relativeTime(lastRun.started_at)}`;
}

function matchChips(match) {
  if (!match) return "";
  const chips = [];
  if (match.keywords && match.keywords.length) {
    const text = match.keywords.slice(0, 3).join(", ");
    const extra = match.keywords.length > 3 ? ` +${match.keywords.length - 3}` : "";
    chips.push(`<span class="chip">kw: ${esc(text)}${esc(extra)}</span>`);
  }
  if (match.max_price !== null && match.max_price !== undefined) {
    chips.push(`<span class="chip">≤ ${fmtMoney(match.max_price, match.currency)}</span>`);
  }
  if (match.min_salary !== null && match.min_salary !== undefined) {
    chips.push(`<span class="chip">≥ ${fmtNumber(match.min_salary)} ${esc(match.currency || "")}</span>`);
  }
  if (match.max_age_days) chips.push(`<span class="chip">≤ ${esc(match.max_age_days)}d</span>`);
  if (match.remote_only) chips.push('<span class="chip">remote</span>');
  if (match.require_salary) chips.push('<span class="chip">salary required</span>');
  return chips.join("");
}

function renderRules(rules) {
  const container = $("#rules-list");
  $("#rules-count").textContent = `${rules.filter((rule) => rule.enabled).length} active / ${rules.length}`;
  if (!rules.length) {
    container.innerHTML = '<div class="empty">No rules configured.</div>';
    return;
  }
  container.innerHTML = rules.map((rule) => {
    const ruleState = rule.state || null;
    return `
      <div class="rule-item">
        <div class="rule-main">
          <div class="rule-title">
            <span class="status-dot ${rule.enabled ? "on" : "off"}" title="${rule.enabled ? "Enabled" : "Disabled"}"></span>
            <code>${esc(rule.name)}</code>
            ${rule.enabled ? "" : '<span class="chip">disabled</span>'}
          </div>
          <div class="rule-meta">
            ${kindChip(rule.kind)}
            <span class="chip source">${esc(rule.source.format)}</span>
            ${matchChips(rule.match)}
          </div>
        </div>
        <div class="rule-side">
          ${statusBadge(ruleState ? ruleState.last_status : "never")}
          <div class="tiny">${ruleState ? `${fmtNumber(ruleState.run_count)} runs` : "not run yet"}</div>
        </div>
      </div>`;
  }).join("");
}

function renderChannels(channels) {
  const container = $("#channels-list");
  if (!channels.length) {
    container.innerHTML = '<div class="empty">No delivery channels.</div>';
    return;
  }
  container.innerHTML = channels.map((channel) => `
    <div class="channel ${channel.configured ? "on" : ""}">
      <span class="channel-icon">${esc((channel.name || "?").slice(0, 2).toUpperCase())}</span>
      <span>
        <span class="channel-name">${esc(channel.name)}</span><br>
        <span class="channel-state">${channel.configured ? "configured" : "not configured"}</span>
      </span>
    </div>`).join("");
}

function renderRunState(job) {
  const container = $("#run-state");
  if (!job) {
    container.innerHTML = `
      <div class="callout-row">
        <div><strong>No dashboard runs yet</strong><br><span class="muted">The cron workflow may still be running on GitHub Actions.</span></div>
      </div>`;
    return;
  }
  const dry = job.dry_run ? "dry run" : "live";
  if (job.status === "running") {
    container.innerHTML = `
      <div class="callout-row">
        <div><strong>${esc(job.job_id)}</strong><br><span class="muted">${esc(dry)} · started ${esc(relativeTime(job.started_at))}</span></div>
        <div class="callout-actions">${statusBadge("running")} <button class="link-btn" type="button" data-action="open-run">View live log →</button></div>
      </div>`;
    return;
  }
  const totals = (job.report && job.report.totals) || {};
  container.innerHTML = `
    <div class="callout-row">
      <div><strong>${esc(job.job_id)}</strong><br><span class="muted">${esc(dry)} · ${esc(fmtDate(job.finished_at))}</span></div>
      <div class="callout-actions">${statusBadge(job.status)} <button class="link-btn" type="button" data-action="open-run">Review run →</button></div>
    </div>
    <div class="run-metrics-inline">
      Fetched ${fmtNumber(totals.fetched || 0)} · Matched ${fmtNumber(totals.matched || 0)} · New ${fmtNumber(totals.new_listings || 0)} · Alerts ${fmtNumber(totals.alerts || 0)} · Errors ${fmtNumber(totals.errors || 0)}
    </div>`;
}

function runsTable(runs) {
  if (!runs.length) return '<div class="empty">No runs recorded yet. Trigger a dry run to populate history.</div>';
  return `
    <table class="data">
      <thead><tr>
        <th>Run</th><th>Status</th><th class="num">Fetched</th><th class="num">Matched</th>
        <th class="num">New</th><th class="num">Alerts</th><th class="num">Errors</th>
      </tr></thead>
      <tbody>
        ${runs.map((run) => {
          const totals = run.totals || {};
          return `<tr class="clickable" tabindex="0" data-run="${esc(run.run_id)}">
            <td><div class="cell-title mono">${esc(run.run_id)}</div><div class="cell-sub">${esc(fmtDate(run.started_at))} · ${esc(fmtDuration(run.started_at, run.finished_at))}</div></td>
            <td>${statusBadge(run.status)}</td>
            <td class="num">${fmtNumber(totals.fetched || 0)}</td>
            <td class="num">${fmtNumber(totals.matched || 0)}</td>
            <td class="num">${fmtNumber(totals.new_listings || 0)}</td>
            <td class="num">${fmtNumber(totals.alerts || 0)}</td>
            <td class="num">${fmtNumber(totals.errors || 0)}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

function renderRuns(runs, container, compact = false) {
  if (!container) return;
  container.innerHTML = runsTable(runs);
  if (compact && runs.length) {
    container.dataset.compact = "true";
  }
}

function alertsTable(alerts = []) {
  if (!alerts.length) return '<div class="empty">No alerts recorded yet.</div>';
  return `
    <table class="data">
      <thead><tr>
        <th>Listing</th><th>Reason</th><th>Channel</th><th>Status</th><th>When</th>
      </tr></thead>
      <tbody>
        ${alerts.map((alert) => `
          <tr class="clickable" tabindex="0" data-listing="${esc(alert.fingerprint)}">
            <td>
              <div class="cell-title">${esc(alert.listing_title || "Unknown listing")}</div>
              <div class="cell-sub">${esc(alert.listing_company || alert.rule)} · ${esc(alert.rule)}</div>
            </td>
            <td>${esc(REASON_LABELS[alert.reason] || alert.reason)}</td>
            <td><span class="chip">${esc(alert.channel)}</span></td>
            <td>${statusBadge(alert.status)}</td>
            <td class="nowrap"><span title="${esc(fmtDate(alert.sent_at))}">${esc(relativeTime(alert.sent_at))}</span></td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

function renderAlerts(alerts, container, compact = false) {
  if (!container) return;
  container.innerHTML = alertsTable(alerts);
  if (compact && alerts.length) container.dataset.compact = "true";
}

function renderPriceWatch(items) {
  const container = $("#price-watch");
  if (!items.length) {
    container.innerHTML = '<div class="empty">No price listings yet. Add a price rule and run the pipeline.</div>';
    return;
  }
  container.innerHTML = items.map((item) => {
    const stats = item.price_stats || {};
    const latest = stats.latest !== undefined ? stats.latest : item.price;
    const minimum = stats.minimum;
    const isLow = minimum !== null && minimum !== undefined && latest !== null && latest !== undefined
      && Number(latest) <= Number(minimum);
    return `<button class="price-card" type="button" data-listing="${esc(item.fingerprint)}">
      <div class="price-top">
        <span class="title">${esc(item.title)}</span>
        <span class="chip kind-price">Product</span>
      </div>
      <div class="price-value">${fmtMoney(latest, item.currency)}</div>
      <div class="price-meta">
        <span class="${isLow ? "low" : ""}">${isLow ? "at low" : `low ${fmtMoney(minimum, item.currency)}`}</span>
        <span>${fmtNumber(stats.observations || 0)} obs.</span>
      </div>
    </button>`;
  }).join("");
}

/* --------------------------------------------------------------- listings -- */

async function loadRuns() {
  const { offset, limit } = state.runs;
  try {
    const data = await api(`/api/runs?limit=${limit}&offset=${offset}`);
    const total = Number(data.total || 0);
    if (offset > 0 && !data.items.length) {
      state.runs.offset = Math.max(0, total - limit);
      return loadRuns();
    }
    $("#runs-table").innerHTML = runsTable(data.items || []);
    $("#runs-pager").innerHTML = pager(total, limit, offset, "runs");
  } catch (error) {
    handleLoadError(error);
  }
}

async function loadListings() {
  const { offset, limit, kind, source, q } = state.listings;
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (kind) params.set("kind", kind);
  if (source) params.set("source", source);
  if (q) params.set("q", q);
  try {
    const data = await api(`/api/listings?${params.toString()}`);
    const total = Number(data.total || 0);
    if (offset > 0 && !(data.items || []).length) {
      state.listings.offset = Math.max(0, total - limit);
      return loadListings();
    }
    updateSourceOptions(data.sources || []);
    renderListings(data.items || []);
    $("#listings-pager").innerHTML = pager(total, limit, offset, "listings");
  } catch (error) {
    handleLoadError(error);
  }
}

function updateSourceOptions(sources) {
  const select = $("#listing-source");
  const current = state.listings.source;
  const options = ['<option value="">All sources</option>']
    .concat(sources.map((source) => `<option value="${esc(source)}">${esc(source)}</option>`));
  select.innerHTML = options.join("");
  if (current && sources.includes(current)) select.value = current;
  else if (current) {
    state.listings.source = "";
    select.value = "";
  }
}

function renderListings(items) {
  const container = $("#listings-table");
  if (!items.length) {
    container.innerHTML = '<div class="empty">No listings match these filters.</div>';
    return;
  }
  container.innerHTML = `
    <table class="data">
      <thead><tr>
        <th>Listing</th><th>Kind</th><th>Source</th><th>Value</th><th>Last seen</th>
      </tr></thead>
      <tbody>
        ${items.map((item) => `
          <tr class="clickable" tabindex="0" data-listing="${esc(item.fingerprint)}">
            <td>
              <div class="cell-title">${esc(item.title)}</div>
              <div class="cell-sub">${esc(item.company || item.location || "—")}</div>
            </td>
            <td>${kindChip(item.kind)}</td>
            <td><span class="chip source">${esc(item.source)}</span></td>
            <td class="nowrap">${listingValue(item)}</td>
            <td class="nowrap"><span title="${esc(fmtDate(item.last_seen_at))}">${esc(relativeTime(item.last_seen_at))}</span></td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

/* ----------------------------------------------------------------- alerts -- */

async function loadAlerts() {
  const { offset, limit, status, rule, channel } = state.alerts;
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) params.set("status", status);
  if (rule) params.set("rule", rule);
  if (channel) params.set("channel", channel);
  try {
    const data = await api(`/api/alerts?${params.toString()}`);
    const total = Number(data.total || 0);
    if (offset > 0 && !(data.items || []).length) {
      state.alerts.offset = Math.max(0, total - limit);
      return loadAlerts();
    }
    updateAlertRuleOptions(data.rules || []);
    $("#alerts-table").innerHTML = alertsTable(data.items || []);
    $("#alerts-pager").innerHTML = pager(total, limit, offset, "alerts");
  } catch (error) {
    handleLoadError(error);
  }
}

function updateAlertRuleOptions(rules) {
  const select = $("#alert-rule");
  const current = state.alerts.rule;
  const options = ['<option value="">All rules</option>']
    .concat(rules.map((rule) => `<option value="${esc(rule)}">${esc(rule)}</option>`));
  select.innerHTML = options.join("");
  if (current && rules.includes(current)) select.value = current;
  else if (current) {
    state.alerts.rule = "";
    select.value = "";
  }
}

function pager(total, limit, offset, dataset) {
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + limit, total);
  return `
    <div class="pager-info">Showing ${fmtNumber(start)}–${fmtNumber(end)} of ${fmtNumber(total)}</div>
    <div class="pager-actions">
      <button class="btn ghost small" type="button" data-page="${dataset}" data-direction="prev" ${offset > 0 ? "" : "disabled"}>Previous</button>
      <button class="btn ghost small" type="button" data-page="${dataset}" data-direction="next" ${offset + limit < total ? "" : "disabled"}>Next</button>
    </div>`;
}

/* ---------------------------------------------------------- listing drawer -- */

async function openListing(fingerprint) {
  openDrawer("Listing", "Loading…", "Details");
  try {
    const listing = await api(`/api/listings/${encodeURIComponent(fingerprint)}`);
    $("#drawer-eyebrow").textContent = listing.kind === "price" ? "Product" : "Job";
    $("#drawer-title").textContent = listing.title || "Untitled listing";
    $("#drawer-body").innerHTML = listingBody(listing);
  } catch (error) {
    if (error.message !== "Authentication required") {
      $("#drawer-body").innerHTML = `<div class="banner error">${esc(error.message)}</div>`;
    }
  }
}

function detailItem(label, value) {
  return `<div class="detail-item"><div class="label">${esc(label)}</div><div class="value">${value || "—"}</div></div>`;
}

function listingBody(listing) {
  const description = listing.description
    ? `<div class="section-title">Description</div><p class="desc">${esc(listing.description)}</p>`
    : "";
  const tags = (listing.tags || []).length
    ? `<div class="tags">${listing.tags.map((tag) => `<span class="tag">${esc(tag)}</span>`).join("")}</div>`
    : "";
  const remote = listing.remote === null || listing.remote === undefined
    ? "unknown"
    : listing.remote ? "yes" : "no";
  const priceSection = listing.kind === "price"
    ? `<div class="section-title">Price history</div>${priceChart(listing.price_history || [], listing.currency)}${historyTable(listing.price_history || [], listing.currency)}`
    : "";
  const listingAlerts = (listing.alerts || []).length
    ? `<div class="section-title">Alerts for this listing</div>
       <div class="table-wrap">
         <table class="data">
           <thead><tr><th>Rule</th><th>Reason</th><th>Channel</th><th>Status</th><th>When</th></tr></thead>
           <tbody>${listing.alerts.map((alert) => `
             <tr>
               <td class="mono">${esc(alert.rule)}</td>
               <td>${esc(REASON_LABELS[alert.reason] || alert.reason)}</td>
               <td><span class="chip">${esc(alert.channel)}</span></td>
               <td>${statusBadge(alert.status)}</td>
               <td class="nowrap">${esc(fmtDate(alert.sent_at))}</td>
             </tr>`).join("")}
           </tbody>
         </table>
       </div>`
    : "";
  const link = listing.url
    ? `<a class="btn secondary small" href="${safeUrl(listing.url)}" target="_blank" rel="noopener noreferrer">Open original ↗</a>`
    : "";
  return `
    <div class="detail-actions">
      ${link}
      ${kindChip(listing.kind)}
      <span class="chip source">${esc(listing.source)}</span>
      ${listing.remote ? '<span class="chip">remote</span>' : ""}
    </div>
    <div class="detail-grid">
      ${detailItem("Company", esc(listing.company || "—"))}
      ${detailItem("Location", esc(listing.location || "—"))}
      ${detailItem("Remote", esc(remote))}
      ${detailItem(listing.kind === "price" ? "Current price" : "Salary", listing.kind === "price" ? fmtMoney(listing.price, listing.currency) : salaryText(listing))}
      ${detailItem("Posted", esc(fmtDate(listing.posted_at)))}
      ${detailItem("First seen", esc(fmtDate(listing.first_seen_at)))}
      ${detailItem("Last seen", esc(fmtDate(listing.last_seen_at)))}
      ${detailItem("Fingerprint", `<span class="mono">${esc((listing.fingerprint || "").slice(0, 16))}…</span>`)}
    </div>
    ${description}
    ${tags}
    ${priceSection}
    ${listingAlerts}`;
}

function priceChart(history, currency) {
  const points = (history || [])
    .map((entry) => ({ value: Number(entry.price), at: entry.observed_at }))
    .filter((entry) => Number.isFinite(entry.value));
  if (!points.length) return '<div class="empty">No price observations yet.</div>';
  const width = 640;
  const height = 190;
  const pad = { left: 50, right: 16, top: 16, bottom: 30 };
  const values = points.map((point) => point.value);
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  if (minimum === maximum) {
    minimum -= 1;
    maximum += 1;
  }
  const spread = maximum - minimum;
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const x = (index) => points.length === 1
    ? pad.left + plotWidth / 2
    : pad.left + (index * plotWidth) / (points.length - 1);
  const y = (value) => pad.top + plotHeight - ((value - minimum) / spread) * plotHeight;
  const coordinates = points.map((point, index) => [x(index), y(point.value)]);
  const line = coordinates.map((point) => `${point[0].toFixed(1)},${point[1].toFixed(1)}`).join(" ");
  const area = `${pad.left},${pad.top + plotHeight} ${line} ${pad.left + plotWidth},${pad.top + plotHeight}`;
  const dots = points.length <= 40
    ? coordinates.map((point) => `<circle class="dot" cx="${point[0].toFixed(1)}" cy="${point[1].toFixed(1)}" r="3"/>`).join("")
    : "";
  const grid = [maximum, (maximum + minimum) / 2, minimum].map((value) => {
    const labelY = y(value);
    return `<line class="grid" x1="${pad.left}" y1="${labelY.toFixed(1)}" x2="${pad.left + plotWidth}" y2="${labelY.toFixed(1)}"/>
      <text x="6" y="${(labelY + 3).toFixed(1)}">${esc(fmtMoney(value, currency))}</text>`;
  }).join("");
  const firstDate = fmtDate(points[0].at, false);
  const lastDate = fmtDate(points[points.length - 1].at, false);
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Price history chart">
    <title>Price history from ${esc(firstDate)} to ${esc(lastDate)}</title>
    ${grid}
    <polygon class="area" points="${area}"/>
    <polyline class="line" points="${line}"/>
    ${dots}
    <text x="${pad.left}" y="${height - 8}">${esc(firstDate)}</text>
    <text x="${pad.left + plotWidth}" y="${height - 8}" text-anchor="end">${esc(lastDate)}</text>
  </svg>`;
}

function historyTable(history, currency) {
  if (!history || !history.length) return "";
  const rows = history.slice().reverse().slice(0, 12).map((entry, index, list) => {
    const previous = list[index + 1];
    const delta = previous ? Number(entry.price) - Number(previous.price) : null;
    const deltaText = delta === null || delta === 0
      ? ""
      : `<span class="${delta < 0 ? "low" : ""}">${delta > 0 ? "+" : ""}${delta.toFixed(2)}</span>`;
    return `<div class="history-row"><span>${fmtMoney(entry.price, entry.currency || currency)}</span><span class="when">${esc(fmtDate(entry.observed_at))} ${deltaText}</span></div>`;
  }).join("");
  return `<div class="history-list">${rows}</div>`;
}

/* -------------------------------------------------------------- run detail -- */

async function openRunDetail(runId) {
  openDrawer("Run", "Loading…", "Execution");
  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    const totals = run.totals || {};
    const states = (run.rule_states || []).map((ruleState) => `
      <tr>
        <td class="mono">${esc(ruleState.rule)}</td>
        <td>${statusBadge(ruleState.last_status)}</td>
        <td class="num">${fmtNumber(ruleState.run_count)}</td>
        <td class="nowrap">${esc(fmtDate(ruleState.last_run_at))}</td>
      </tr>`).join("");
    $("#drawer-title").textContent = run.run_id;
    $("#drawer-body").innerHTML = `
      <div class="detail-actions">${statusBadge(run.status)}<span class="chip">${esc(fmtDuration(run.started_at, run.finished_at))}</span></div>
      <div class="detail-grid">
        ${detailItem("Started", esc(fmtDate(run.started_at)))}
        ${detailItem("Finished", esc(fmtDate(run.finished_at)))}
        ${detailItem("Fetched", fmtNumber(totals.fetched || 0))}
        ${detailItem("Matched", fmtNumber(totals.matched || 0))}
        ${detailItem("New listings", fmtNumber(totals.new_listings || 0))}
        ${detailItem("Alerts", fmtNumber(totals.alerts || 0))}
        ${detailItem("Deliveries", fmtNumber(totals.deliveries || 0))}
        ${detailItem("Errors", fmtNumber(totals.errors || 0))}
      </div>
      ${run.error ? `<div class="banner error">${esc(run.error)}</div>` : ""}
      <div class="section-title">Rule state</div>
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>Rule</th><th>Last status</th><th class="num">Runs</th><th>Last run</th></tr></thead>
          <tbody>${states || '<tr><td colspan="4" class="muted">No rule state recorded.</td></tr>'}</tbody>
        </table>
      </div>`;
  } catch (error) {
    if (error.message !== "Authentication required") {
      $("#drawer-body").innerHTML = `<div class="banner error">${esc(error.message)}</div>`;
    }
  }
}

/* ---------------------------------------------------------------- run modal -- */

function ensureRunModalEvents() {
  $("#run-close").addEventListener("click", () => hideModal("run"));
  $("#run-close-foot").addEventListener("click", () => hideModal("run"));
  $("#run-backdrop").addEventListener("click", () => hideModal("run"));
  $("#run-refresh").addEventListener("click", () => refreshRunStatus(true));
}

function openRunModal() {
  openModal("run");
  if (state.overview && state.overview.run) {
    refreshRunStatus();
    return;
  }
  $("#run-eyebrow").textContent = "Pipeline run";
  $("#run-title").textContent = "No run started yet";
  $("#run-meta").innerHTML = "";
  $("#run-summary").innerHTML = '<div class="empty" style="grid-column:1/-1">Use “Dry run” or “Run live” to execute the pipeline.</div>';
  $("#run-logs").textContent = "Waiting for output…";
  $("#run-live-dot").classList.remove("on");
}

async function refreshRunStatus(showToastOnDone = false) {
  try {
    const data = await api("/api/run/status");
    if (!data.job) {
      showToast("No run has been started from the dashboard yet.", "warning");
      return;
    }
    renderRun(data.job);
    if (data.job.status !== "running") {
      stopRunPolling();
      loadOverview();
      if (showToastOnDone) showToast("Run status refreshed.", "success");
    } else if (!state.runPoll) {
      startRunPolling();
    }
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderRun(job) {
  const running = job.status === "running";
  $("#run-eyebrow").textContent = job.dry_run ? "Dry run · no messages sent" : "Live run · alerts enabled";
  $("#run-title").textContent = running ? "Pipeline running…" : `Run ${statusLabel(job.status)}`;
  const rules = (job.rules || []).length ? job.rules.join(", ") : "all enabled rules";
  $("#run-meta").innerHTML = `
    ${statusBadge(job.status)}
    <span class="chip">${job.dry_run ? "dry run" : "live"}</span>
    <span class="chip">${esc(job.job_id)}</span>
    <span class="chip">${esc(rules)}</span>
    <span class="chip">${esc(fmtDuration(job.started_at, job.finished_at))}</span>`;
  const report = job.report || null;
  const totals = (report && report.totals) || {};
  const metrics = running
    ? `<div class="metric"><div class="label">Started</div><div class="value">${esc(relativeTime(job.started_at))}</div></div>
       <div class="metric"><div class="label">Rules</div><div class="value">${esc((job.rules || []).length || "all")}</div></div>
       <div class="metric"><div class="label">Mode</div><div class="value">${job.dry_run ? "Dry" : "Live"}</div></div>`
    : `<div class="metric"><div class="label">Fetched</div><div class="value">${fmtNumber(totals.fetched || 0)}</div></div>
       <div class="metric"><div class="label">Matched</div><div class="value">${fmtNumber(totals.matched || 0)}</div></div>
       <div class="metric"><div class="label">New</div><div class="value">${fmtNumber(totals.new_listings || 0)}</div></div>
       <div class="metric"><div class="label">Alerts</div><div class="value">${fmtNumber(totals.alerts || 0)}</div></div>
       <div class="metric"><div class="label">Deliveries</div><div class="value">${fmtNumber(totals.deliveries || 0)}</div></div>
       <div class="metric"><div class="label">Errors</div><div class="value">${fmtNumber(totals.errors || 0)}</div></div>`;
  $("#run-summary").innerHTML = metrics;
  if (job.error) {
    $("#run-summary").insertAdjacentHTML("beforeend", `<div class="banner error" style="grid-column:1/-1">${esc(job.error)}</div>`);
  }
  const logs = job.logs || [];
  const pre = $("#run-logs");
  const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.innerHTML = logs.length
    ? logs.map((entry) => {
      const level = String(entry.level || "INFO").toLowerCase();
      const time = entry.at ? new Date(entry.at).toLocaleTimeString() : "";
      return `<span class="log-${level}">${esc(time)} ${esc(entry.level)} ${esc(entry.message)}</span>`;
    }).join("\n")
    : "Waiting for output…";
  if (nearBottom || running) pre.scrollTop = pre.scrollHeight;
  $("#run-live-dot").classList.toggle("on", running);
}

function startRunPolling() {
  stopRunPolling();
  state.runPoll = window.setInterval(async () => {
    try {
      const data = await api("/api/run/status");
      if (!data.job) {
        stopRunPolling();
        return;
      }
      renderRun(data.job);
      if (data.job.status !== "running") {
        stopRunPolling();
        const finishedStatus = data.job.status;
        loadOverview();
        showToast(
          finishedStatus === "ok" ? "Run completed successfully." : `Run finished with status: ${statusLabel(finishedStatus)}.`,
          finishedStatus === "ok" ? "success" : "warning",
        );
      }
    } catch (error) {
      stopRunPolling();
      showToast(error.message, "error");
    }
  }, 1400);
}

function stopRunPolling() {
  if (state.runPoll) {
    window.clearInterval(state.runPoll);
    state.runPoll = null;
  }
}

async function startRun(dryRun) {
  clearError();
  if (!dryRun) {
    const confirmed = window.confirm(
      "Run the pipeline live? It can send Discord/Telegram alerts and will update the real SQLite state.",
    );
    if (!confirmed) return;
  }
  setRunButtons(true);
  try {
    const data = await api("/api/run", {
      method: "POST",
      body: JSON.stringify({ dry_run: dryRun }),
    });
    openModal("run");
    renderRun(data.job);
    startRunPolling();
    showToast(dryRun ? "Dry run started — no state will be written." : "Live run started.", "info");
    loadOverview();
  } catch (error) {
    if (error.message.includes("already in progress")) {
      openModal("run");
      refreshRunStatus();
      startRunPolling();
    } else {
      showToast(error.message, "error");
    }
  } finally {
    setRunButtons(false);
  }
}

function setRunButtons(disabled) {
  ["#dry-run-btn", "#live-run-btn"].forEach((selector) => {
    const button = $(selector);
    button.disabled = disabled;
    button.style.opacity = disabled ? "0.6" : "";
  });
}

/* --------------------------------------------------------------- config -- */

async function loadConfig() {
  try {
    const data = await api("/api/config");
    const editor = $("#config-editor");
    editor.value = data.content || "";
    state.configDirty = false;
    updateDirtyFlag();
    $("#config-path").textContent = data.path || "config/config.yaml";
    setConfigValidity(data.valid, data.error);
    renderConfigSummary(data.summary);
    const errorBox = $("#config-error");
    if (data.error) {
      errorBox.textContent = data.error;
      errorBox.hidden = false;
    } else {
      errorBox.hidden = true;
    }
  } catch (error) {
    handleLoadError(error);
  }
}

function setConfigValidity(valid, error) {
  const badge = $("#config-badge");
  badge.className = `badge ${valid ? "ok" : "failed"}`;
  badge.textContent = valid ? "Valid" : "Invalid";
  badge.title = error || "";
}

function renderConfigSummary(summary) {
  const container = $("#config-summary");
  if (!summary) {
    container.innerHTML = '<div class="empty">No parsed configuration available.</div>';
    return;
  }
  const rules = summary.rules || [];
  const active = rules.filter((rule) => rule.enabled).length;
  container.innerHTML = `
    <div class="summary-list">
      <div class="summary-item"><span>Schema version</span><span>${esc(summary.version)}</span></div>
      <div class="summary-item"><span>Database</span><span class="mono">${esc(summary.database)}</span></div>
      <div class="summary-item"><span>HTTP timeout</span><span>${esc(summary.http.timeout_seconds)}s · ${esc(summary.http.max_retries)} retries</span></div>
      <div class="summary-item"><span>Rate limit</span><span>${esc(summary.http.min_delay_seconds)}s per host</span></div>
      <div class="summary-item"><span>Burst warning</span><span>${esc(summary.notifications.alert_burst_warning_threshold)} alerts</span></div>
      <div class="summary-item"><span>Rules</span><span>${fmtNumber(active)} enabled / ${fmtNumber(rules.length)} total</span></div>
    </div>
    <div class="section-title">Rules</div>
    <div class="summary-rules">
      ${rules.map((rule) => `
        <div class="summary-rule">
          ${kindChip(rule.kind)}
          <code>${esc(rule.name)}</code>
          <span class="chip source">${esc(rule.source.format)}</span>
          ${rule.enabled ? "" : '<span class="chip">disabled</span>'}
          <span class="chip">${esc(rule.bootstrap)}</span>
        </div>`).join("") || '<div class="empty">No rules.</div>'}
    </div>`;
}

function updateDirtyFlag() {
  const dirty = $("#config-dirty");
  dirty.hidden = !state.configDirty;
}

async function validateConfig() {
  const errorBox = $("#config-error");
  errorBox.hidden = true;
  try {
    const data = await api("/api/config/validate", {
      method: "POST",
      body: JSON.stringify({ content: $("#config-editor").value }),
    });
    setConfigValidity(true, null);
    renderConfigSummary(data.summary);
    showToast("Configuration is valid. Nothing was written.", "success");
    return true;
  } catch (error) {
    setConfigValidity(false, error.message);
    errorBox.textContent = error.message;
    errorBox.hidden = false;
    showToast("Configuration is invalid.", "error");
    return false;
  }
}

async function saveConfig() {
  const errorBox = $("#config-error");
  errorBox.hidden = true;
  try {
    const data = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ content: $("#config-editor").value }),
    });
    state.configDirty = false;
    updateDirtyFlag();
    setConfigValidity(true, null);
    renderConfigSummary(data.summary);
    showToast("Configuration saved. The next run will use it.", "success");
    loadOverview();
    return true;
  } catch (error) {
    setConfigValidity(false, error.message);
    errorBox.textContent = error.message;
    errorBox.hidden = false;
    showToast("Could not save configuration.", "error");
    return false;
  }
}

/* ---------------------------------------------------------------- modals -- */

function openDrawer(eyebrow, title, bodyLabel) {
  $("#drawer-eyebrow").textContent = eyebrow || "Details";
  $("#drawer-title").textContent = title || "—";
  $("#drawer-body").innerHTML = bodyLabel ? `<div class="empty">${esc(bodyLabel)}</div>` : "";
  $("#drawer").hidden = false;
  $("#drawer-backdrop").hidden = false;
  document.body.style.overflow = "hidden";
  $("#drawer-close").focus();
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
  document.body.style.overflow = "";
}

function openModal(name) {
  if (name === "run") {
    $("#run-modal").hidden = false;
    $("#run-backdrop").hidden = false;
  }
  if (name === "auth") {
    $("#auth-modal").hidden = false;
    $("#auth-backdrop").hidden = false;
    $("#auth-token").focus();
  }
  document.body.style.overflow = "hidden";
}

function hideModal(name) {
  if (name === "run") {
    $("#run-modal").hidden = true;
    $("#run-backdrop").hidden = true;
  }
  if (name === "auth") {
    $("#auth-modal").hidden = true;
    $("#auth-backdrop").hidden = true;
    state.authPrompted = false;
  }
  const anyOpen = !$("#run-modal").hidden || !$("#auth-modal").hidden || !$("#drawer").hidden;
  if (!anyOpen) document.body.style.overflow = "";
}

function promptAuth(message) {
  if (message) {
    const error = $("#auth-error");
    error.textContent = message;
    error.hidden = false;
  }
  if (state.authPrompted) return;
  state.authPrompted = true;
  openModal("auth");
}

/* ------------------------------------------------------------------ wire -- */

function wireEvents() {
  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next);
  });

  $("#refresh-btn").addEventListener("click", () => {
    clearError();
    loadView();
    checkHealth();
  });

  $("#dry-run-btn").addEventListener("click", () => startRun(true));
  $("#live-run-btn").addEventListener("click", () => startRun(false));

  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  $("#auth-backdrop").addEventListener("click", () => hideModal("auth"));

  $("#auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = $("#auth-token").value.trim();
    if (!token) {
      $("#auth-error").textContent = "Enter the token configured in WATCHDOG_UI_TOKEN.";
      $("#auth-error").hidden = false;
      return;
    }
    state.token = token;
    state.authPrompted = false;
    sessionStorage.setItem("watchdog.token", token);
    $("#auth-error").hidden = true;
    hideModal("auth");
    try {
      // Verify the token before reporting success; a bad token re-opens the prompt.
      await api("/api/rules");
      showToast("Token accepted for this tab.", "success");
      loadView();
      checkHealth();
    } catch (error) {
      if (error.message !== "Authentication required") showToast(error.message, "error");
    }
  });

  $("#listing-search").addEventListener("input", (event) => {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(() => {
      state.listings.q = event.target.value.trim();
      state.listings.offset = 0;
      loadListings();
    }, 280);
  });
  $("#listing-kind").addEventListener("change", (event) => {
    state.listings.kind = event.target.value;
    state.listings.offset = 0;
    loadListings();
  });
  $("#listing-source").addEventListener("change", (event) => {
    state.listings.source = event.target.value;
    state.listings.offset = 0;
    loadListings();
  });
  $("#alert-status").addEventListener("change", (event) => {
    state.alerts.status = event.target.value;
    state.alerts.offset = 0;
    loadAlerts();
  });
  $("#alert-rule").addEventListener("change", (event) => {
    state.alerts.rule = event.target.value;
    state.alerts.offset = 0;
    loadAlerts();
  });
  $("#alert-channel").addEventListener("change", (event) => {
    state.alerts.channel = event.target.value;
    state.alerts.offset = 0;
    loadAlerts();
  });

  $("#config-editor").addEventListener("input", () => {
    state.configDirty = true;
    updateDirtyFlag();
  });
  $("#config-editor").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveConfig();
    }
  });
  $("#config-validate").addEventListener("click", validateConfig);
  $("#config-save").addEventListener("click", saveConfig);
  $("#config-reload").addEventListener("click", () => {
    if (!state.configDirty || window.confirm("Discard unsaved changes and reload from disk?")) {
      loadConfig();
    }
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest("a")) return;

    const viewButton = event.target.closest("[data-view]");
    if (viewButton) {
      switchView(viewButton.dataset.view);
      return;
    }

    const pageButton = event.target.closest("[data-page]");
    if (pageButton) {
      const dataset = pageButton.dataset.page;
      const direction = pageButton.dataset.direction;
      const target = state[dataset];
      if (!target) return;
      target.offset = direction === "next"
        ? target.offset + target.limit
        : Math.max(0, target.offset - target.limit);
      if (dataset === "runs") loadRuns();
      if (dataset === "listings") loadListings();
      if (dataset === "alerts") loadAlerts();
      return;
    }

    const action = event.target.closest("[data-action]");
    if (action) {
      if (action.dataset.action === "open-run") openRunModal();
      if (action.dataset.action === "refresh-runs") loadRuns();
      if (action.dataset.action === "refresh-listings") loadListings();
      if (action.dataset.action === "refresh-alerts") loadAlerts();
      return;
    }

    const runRow = event.target.closest("[data-run]");
    if (runRow) {
      openRunDetail(runRow.dataset.run);
      return;
    }

    const listing = event.target.closest("[data-listing]");
    if (listing) {
      openListing(listing.dataset.listing);
    }
  });

  document.addEventListener("keydown", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const row = target ? target.closest("tr[data-run], tr[data-listing]") : null;
    if (row && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      row.click();
      return;
    }
    if (event.key !== "Escape") return;
    if (!$("#drawer").hidden) closeDrawer();
    else if (!$("#run-modal").hidden) hideModal("run");
    else if (!$("#auth-modal").hidden) hideModal("auth");
  });
}

async function init() {
  initTheme();
  initLayout();
  wireEvents();
  ensureRunModalEvents();
  await checkHealth();
  loadOverview();
}

document.addEventListener("DOMContentLoaded", init);
