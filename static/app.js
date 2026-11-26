// Application controller: state, view routing, auto-refresh, theme, and the
// per-view render functions that combine api.js data with components.js /
// topology.js widgets. No provider-specific branching lives here -- every
// cloud badge/label comes pre-resolved from the backend.

import { api, ApiError, getStoredApiKey, setStoredApiKey } from "./api.js";
import { AGUIStreamClient, PORTAL_FRONTEND_TOOLS } from "./agui.js?v=20261124-helm1";
import { extractToolScopeLabel, renderInteractiveToolBody, safeJsonParse, summarizeToolResult } from "./agui_views.js?v=20261124-helm1";
import { createHelmViewsController } from "./helm.js?v=20261124-helm1";
import {
  h,
  renderInto,
  emptyState,
  errorState,
  loadingSkeleton,
  statCard,
  statGrid,
  panel,
  dataTable,
  fillSelect,
  pill,
  statusPill,
  warningBanner,
  providerBadge,
} from "./components.js";
import { renderTopologyGraph, topologyLegend, hideNodeTooltip, NETWORK_COLUMNS, STORAGE_COLUMNS, NAMESPACE_MAP_COLUMNS } from "./topology.js";
import { ResourceMapController, RESOURCE_TYPE_THEME } from "./resourcemap.js";

const $ = (id) => document.getElementById(id);

// Small icon set for statCard()'s optional 4th argument, used across the
// Overview/Nodes/Clusters/Diagnostics stat grids. Kept separate from
// RESOURCE_TYPE_THEME (imported above) since these are generic dashboard
// concepts (status, location, ...), not k8s resource kinds -- workload
// stat cards (Pods/Deployments/.../CronJobs) reuse RESOURCE_TYPE_THEME's
// icons directly instead, so a kind's icon is defined in exactly one place.
const STAT_ICONS = {
  version: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>`,
  status: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>`,
  location: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>`,
  server: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="6" rx="1.5"/><rect x="2" y="15" width="20" height="6" rx="1.5"/><circle cx="6" cy="6" r="1" fill="currentColor" stroke="none"/><circle cx="6" cy="18" r="1" fill="currentColor" stroke="none"/></svg>`,
  layers: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 9 5-9 5-9-5 9-5z"/><path d="m3 12 9 5 9-5M3 17l9 5 9-5"/></svg>`,
  shield: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 4 5v6c0 5.5 3.5 9.7 8 11 4.5-1.3 8-5.5 8-11V5z"/></svg>`,
  checkCircle: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 5-5"/></svg>`,
  crown: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m2 8 4 3 6-8 6 8 4-3-2 11H4Z"/></svg>`,
  grid: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>`,
  pulse: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2-7 4 14 2-7h6"/></svg>`,
  cloud: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 19H6a4 4 0 0 1-.5-7.97 5.5 5.5 0 0 1 10.6-1.94A4 4 0 0 1 17.5 19z"/></svg>`,
  funnel: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 3H2l8 9.5V19l4 2v-8.5L22 3z"/></svg>`,
  shieldCheck: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 4 5v6c0 5.5 3.5 9.7 8 11 4.5-1.3 8-5.5 8-11V5z"/><path d="m9 12 2 2 4-4"/></svg>`,
  folder: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>`,
};

const state = {
  teams: [],
  selectedClusterId: null,
  clusterBadge: null,
  activeView: "overview",
  refreshIntervalSec: 0,
  refreshTimer: null,
  lastTopologyKey: { network: null, storage: null, map: null, resourcemap: null },
  loadTokens: {},
  aiChatSessionId: null,
  aiChatMessages: [],
  userRole: "admin",
  userSubject: "anonymous",
  readOnlyMode: true,
};

function canAdmin() {
  return state.userRole === "admin";
}

function canOperate() {
  return !state.readOnlyMode && (state.userRole === "admin" || state.userRole === "operator");
}

function applyRoleVisibility() {
  const roleBadge = $("topbar-role-badge");
  if (roleBadge) {
    const r = (state.userRole || "viewer").toLowerCase();
    roleBadge.textContent = r.toUpperCase();
    roleBadge.title = `Signed in as ${state.userSubject} (Role: ${r.toUpperCase()})`;
    if (r === "admin") {
      roleBadge.style.background = "var(--accent-soft)";
      roleBadge.style.color = "var(--accent)";
    } else if (r === "operator") {
      roleBadge.style.background = "rgba(245, 158, 11, 0.14)";
      roleBadge.style.color = "var(--warning)";
    } else {
      roleBadge.style.background = "rgba(16, 185, 129, 0.14)";
      roleBadge.style.color = "var(--success)";
    }
  }

  const adminButtons = ["onboard-trigger", "onboard-trigger-sidebar", "clusters-page-add-btn", "clusters-modal-add-btn"];
  adminButtons.forEach((id) => {
    const el = $(id);
    if (el) {
      el.style.display = canAdmin() ? "" : "none";
    }
  });
}

// Each named loader gets a monotonically increasing token. If a namespace
// switch (or an auto-refresh tick) fires a new request before an in-flight
// one resolves, the older response is discarded instead of racing the newer
// one to render -- without this, two overlapping fetches could interleave
// and briefly show a mismatched or flickering view.
function nextLoadToken(name) {
  state.loadTokens[name] = (state.loadTokens[name] || 0) + 1;
  return state.loadTokens[name];
}
function isStaleToken(name, token) {
  return state.loadTokens[name] !== token;
}

// Client-side Stale-While-Revalidate (SWR) view cache:
// - Within VIEW_CACHE_STALE_MS (10s): instant 0ms paint with zero network calls.
// - Between 10s and VIEW_CACHE_TTL_MS (30s): instant paint + silent background refresh.
// - Older than 30s or forced (manual refresh / auto-refresh tick): skeleton + network fetch.
const VIEW_CACHE_TTL_MS = 30_000;
const VIEW_CACHE_STALE_MS = 10_000;
const SIDEBAR_COUNTS_TTL_MS = 60_000;
const viewCache = new Map(); // cacheKey -> { data, ts }
const sidebarCountsCache = { clusterId: null, ts: 0 };

function clearViewCache() {
  viewCache.clear();
  sidebarCountsCache.clusterId = null;
  sidebarCountsCache.ts = 0;
}

function recordNodesSidebarCount(clusterId, count) {
  if (count == null || state.selectedClusterId !== clusterId) return;
  setSidebarCount("count-nodes", count);
  sidebarCountsCache.clusterId = clusterId;
  sidebarCountsCache.ts = Date.now();
}

async function loadWithSWR({ key, tokenView, container, fetchFn, renderFn, force = false, showSkeleton = true, onError }) {
  const token = nextLoadToken(tokenView);
  const cached = !force ? viewCache.get(key) : null;
  const age = cached ? Date.now() - cached.ts : Infinity;

  if (cached && age < VIEW_CACHE_TTL_MS) {
    renderFn(cached.data);
    if (age < VIEW_CACHE_STALE_MS) return cached.data;
    fetchFn()
      .then((fresh) => {
        viewCache.set(key, { data: fresh, ts: Date.now() });
        if (isStaleToken(tokenView, token)) return;
        renderFn(fresh);
      })
      .catch(() => { /* keep visible stale data on transient background error */ });
    return cached.data;
  }

  if (showSkeleton && container) renderInto(container, loadingSkeleton());
  try {
    const data = await fetchFn();
    viewCache.set(key, { data, ts: Date.now() });
    if (isStaleToken(tokenView, token)) return null;
    renderFn(data);
    return data;
  } catch (err) {
    if (isStaleToken(tokenView, token)) return null;
    if (onError) {
      onError(err);
    } else if (container) {
      renderInto(
        container,
        errorState(err, () => loadWithSWR({ key, tokenView, container, fetchFn, renderFn, force: true, showSkeleton }))
      );
    }
    return null;
  }
}

// -- theme --------------------------------------------------------------

// Dark-first by design (matches Radar/k9s/Lens -- the reference tools this
// app is benchmarked against all default to dark), not just "follow the OS
// preference". The <html> tag already carries data-theme="dark" statically
// in index.html so there's no flash of light mode before this JS runs;
// this only needs to override it if the user explicitly chose light before.
function getStoredTheme() {
  return localStorage.getItem("kubeorbit_theme");
}
function effectiveTheme() {
  return getStoredTheme() || "dark";
}
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
}
function initTheme() {
  applyTheme(effectiveTheme());
}
function toggleTheme() {
  const next = effectiveTheme() === "dark" ? "light" : "dark";
  localStorage.setItem("kubeorbit_theme", next);
  applyTheme(next);
}

// -- misc helpers ---------------------------------------------------------

function capitalize(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function timeAgo(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

function updateLastUpdated() {
  $("last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

function setConnStatus(kind, text) {
  const dot = $("conn-dot");
  dot.className = `status-dot ${kind === "error" ? "status-dot--error" : "status-dot--ok"}`;
  const fallback = kind === "ok" ? `Connected to ${state.clusterBadge?.name || ""}` : kind === "connecting" ? "Connecting…" : "Disconnected";
  $("conn-text").textContent = text || fallback;
}

function updateClusterBadgeUI(badge) {
  if (!badge) return;
  const nameEl = $("topbar-cluster-name");
  if (nameEl) nameEl.textContent = badge.name || badge.cluster_id || state.selectedClusterId;

  const providerEl = $("topbar-provider-badge");
  if (providerEl) {
    const rendered = providerBadge(badge.provider, badge.label);
    providerEl.className = rendered.className;
    providerEl.replaceChildren(...rendered.childNodes);
  }

  const envEl = $("topbar-env-badge");
  if (envEl) {
    const env = badge.environment || "sandbox";
    const envColor = ENV_PILL_COLORS[env] || ENV_PILL_COLORS.default;
    envEl.textContent = env.toUpperCase();
    envEl.style.background = `${envColor}20`;
    envEl.style.color = envColor;
  }
}

// -- team / environment / cluster selection --------------------------------

function populateTeamSelect() {
  const select = $("team-select");
  if (select) fillSelect(select, state.teams, { valueKey: "id", labelKey: "name" });
  if (state.teams.length && !state.selectedTeamId) {
    state.selectedTeamId = state.teams[0].id;
  }
}

function currentTeam() {
  const select = $("team-select");
  if (select && select.value) {
    state.selectedTeamId = select.value;
  }
  return state.teams.find((t) => t.id === state.selectedTeamId) || state.teams[0];
}

function populateEnvSelect() {
  const team = currentTeam();
  const envs = team ? team.environments.filter((e) => e.clusters.length) : [];
  if ($("env-select")) {
    fillSelect($("env-select"), envs.map((e) => ({ id: e.id, label: capitalize(e.id) })), { valueKey: "id", labelKey: "label" });
  }
}

function currentEnvGroup() {
  const team = currentTeam();
  const envSelect = $("env-select");
  const envId = envSelect ? envSelect.value : (team?.environments[0]?.id || "sandbox");
  return team?.environments.find((e) => e.id === envId);
}

function populateClusterSelect() {
  const group = currentEnvGroup();
  if ($("cluster-select")) {
    fillSelect($("cluster-select"), group ? group.clusters : [], { valueKey: "id", labelKey: "name" });
  }
}

function saveSelection() {
  const team = currentTeam();
  const envSelect = $("env-select");
  const clusterSelect = $("cluster-select");
  localStorage.setItem(
    "kubeorbit_selection",
    JSON.stringify({
      teamId: team?.id || "platform",
      envId: envSelect ? envSelect.value : "sandbox",
      clusterId: clusterSelect ? clusterSelect.value : state.selectedClusterId
    })
  );
}

function restoreSelectionOrDefault() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem("kubeorbit_selection") || "null");
  } catch {
    saved = null;
  }
  const team = (saved && state.teams.find((t) => t.id === saved.teamId)) || state.teams[0];
  if (!team) return null;
  state.selectedTeamId = team.id;
  if ($("team-select")) $("team-select").value = team.id;
  populateEnvSelect();

  const envs = team.environments.filter((e) => e.clusters.length);
  const env = (saved && envs.find((e) => e.id === saved.envId)) || envs[0];
  if (!env) return null;
  if ($("env-select")) $("env-select").value = env.id;
  populateClusterSelect();

  const cluster = (saved && env.clusters.find((c) => c.id === saved.clusterId)) || env.clusters[0];
  if (!cluster) return null;
  if ($("cluster-select")) $("cluster-select").value = cluster.id;
  state.selectedClusterId = cluster.id;
  return cluster.id;
}

async function populateNamespaceSelects(clusterId) {
  const includeSystem = $("namespaces-include-system")?.checked || false;
  const rawGeneral = (await api.getNamespaces(clusterId, false)).items;
  // Put non-default workload namespaces (e.g. kubeorbit-demo) ahead of "default"
  // so single-namespace views (Resource Map, Network, Storage, Namespaces) open
  // directly into an active application namespace rather than an empty "default".
  const general = [...rawGeneral].sort((a, b) => {
    const aName = typeof a === "string" ? a : a.name;
    const bName = typeof b === "string" ? b : b.name;
    if (aName === "default" && bName !== "default") return 1;
    if (bName === "default" && aName !== "default") return -1;
    return aName.localeCompare(bName);
  });
  setSidebarCount("count-namespaces", general.length);

  fillSelect($("workloads-namespace-select"), general, { placeholder: "All namespaces (excl. system)" });
  if ($("applications-namespace-select")) fillSelect($("applications-namespace-select"), general, { placeholder: "All namespaces" });
  if ($("diagnostics-namespace-select")) fillSelect($("diagnostics-namespace-select"), general, { placeholder: "All namespaces (excl. system)" });
  if ($("resourcemap-namespace-select")) fillSelect($("resourcemap-namespace-select"), general);
  fillSelect($("events-namespace-select"), general, { placeholder: "All namespaces (excl. system)" });
  fillSelect($("network-namespace-select"), general);
  fillSelect($("storage-namespace-select"), general);
  fillSelect($("map-namespace-select"), general);

  const namespacesViewList = includeSystem ? (await api.getNamespaces(clusterId, true)).items : general;
  fillSelect($("namespace-select"), namespacesViewList);
}


function setSelectorsDisabled(disabled) {
  ["team-select", "env-select", "cluster-select"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = disabled;
  });
}

// The single place that actually switches the active cluster: updates the
// badge, re-populates every namespace <select> for the new cluster, and
// reloads whatever view is on screen. Both onClusterChange() (the legacy
// <select>-based switcher) and connectToCluster() (the card-based "Connect"
// button the UI actually uses now) must funnel through this -- a prior
// version had connectToCluster() do its own partial switch that skipped
// populateNamespaceSelects()/loadActiveView() entirely (dead code written
// for a #cluster-select element this UI no longer has), which silently left
// every namespace filter showing the *previous* cluster's namespaces after
// clicking "Connect" on a different one.
async function switchActiveCluster(clusterId, opts = {}) {
  if (!clusterId) return;
  clearViewCache();
  if (clusterId !== state.selectedClusterId) {
    resetAIChat(); // stale thread would otherwise answer for the wrong cluster
    helmController.resetHelmState();
  }
  state.selectedClusterId = clusterId;
  saveSelection();
  setConnStatus("connecting");
  setSelectorsDisabled(true);
  try {
    const badge = await api.switchCluster(clusterId);
    state.clusterBadge = badge;
    updateClusterBadgeUI(badge);
    setConnStatus("ok");
    await populateNamespaceSelects(clusterId);
    renderAGUIQuickPrompts();
    if (!opts.skipViewLoad) await loadActiveView({ force: true });
    if (!opts.skipClusterPanel) loadClusterPanel(); // sync sidebar active state
  } catch (err) {
    setConnStatus("error", err.message);
    const container = document.querySelector(`.view[data-view="${state.activeView}"] [id$="-content"]`);
    if (container) renderInto(container, errorState(err, () => switchActiveCluster(clusterId, opts)));
  } finally {
    setSelectorsDisabled(false);
  }
}

async function onClusterChange(opts = {}) {
  const clusterSelect = $("cluster-select");
  const clusterId = clusterSelect ? clusterSelect.value : state.selectedClusterId;
  await switchActiveCluster(clusterId, opts);
}

// -- view: overview -----------------------------------------------------

// Detail-list row for the Control Plane panel -- reuses the same .kv-row
// styling as the secret/manifest detail views elsewhere in the app.
function kvRow(label, value) {
  return h("div", { class: "kv-row" }, [
    h("div", { class: "kv-row__key" }, label),
    h("div", { class: "kv-row__value" }, value === null || value === undefined || value === "" ? "—" : String(value)),
  ]);
}

// A cloud feature flag (AAD RBAC, IRSA, Dataplane V2, ...) rendered as a
// pill so enabled/disabled reads at a glance without a column of booleans.
function featureChip(label, enabled) {
  return pill(label, enabled ? "healthy" : "muted");
}

function renderOverview(data) {
  const { badge, meta, node_count, node_fetch_error, helm, nodes } = data;
  $("overview-meta").textContent = `${badge.team_name} · ${capitalize(badge.environment)} · ${badge.cloud}`;
  const net = meta.network || {};
  const features = (meta.extra && meta.extra.features) || [];

  const children = [
    statGrid([
      statCard("Kubernetes Version", meta.kubernetes_version || "—", null, STAT_ICONS.version),
      statCard("Status", meta.status || "—", null, STAT_ICONS.status),
      statCard("Location", meta.location || "—", null, STAT_ICONS.location),
      statCard("Nodes", node_count, null, STAT_ICONS.server),
      statCard("Node Pools", meta.node_pools.length, null, STAT_ICONS.layers),
      statCard("Workload Identity", meta.workload_identity?.enabled ? "Enabled" : "Not configured", null, STAT_ICONS.shield),
    ]),
  ];

  if (meta.degraded) children.push(warningBanner(`Cluster metadata could not be fully loaded: ${meta.error || "unknown error"}`));
  if (node_fetch_error) children.push(warningBanner(`Node list could not be loaded: ${node_fetch_error}`));

  // -- Control Plane / Data Plane: switched via the same subtabs pattern
  // the Namespace Resources view uses (see renderNamespaceResources) --
  // one section visible at a time under a small tab bar, instead of two
  // panels permanently stacked on top of each other.
  const dataPlaneNodes = (nodes || []).filter(
    (n) => !(n.roles || []).some((r) => r.includes("control-plane") || r.includes("master"))
  );

  const planeSections = [
    {
      key: "control-plane",
      label: "Control Plane",
      render: () =>
        h("div", { style: "display:flex;flex-direction:column;gap:16px;padding:var(--space-4);" }, [
          h("div", { class: "kv-list" }, [
            // Kubernetes Version / Status / Location are intentionally left
            // out here -- they're already shown in the stat cards above the
            // tabs, so repeating them in the Control Plane body would just
            // print the same value twice.
            kvRow("API Server Endpoint", net.api_server_endpoint),
            kvRow("VPC / VNet", net.vpc_id),
            kvRow("Pod CIDR", net.pod_cidr),
            kvRow("Service CIDR", net.service_cidr),
            kvRow("CNI", net.cni),
            kvRow("Network Policy", net.network_policy),
            kvRow("Private Cluster", net.private_cluster ? "Yes" : "No"),
          ]),
          features.length
            ? h("div", {}, [
                h("div", { style: "font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.03em;margin-bottom:8px;" }, "Cloud-Specific Features"),
                h("div", { style: "display:flex;flex-wrap:wrap;gap:8px;" }, features.map((f) => featureChip(f.label, f.enabled))),
              ])
            : null,
        ].filter(Boolean)),
    },
    {
      key: "data-plane",
      label: "Data Plane",
      render: () =>
        h("div", {}, [
          h("div", { style: "padding:12px 16px 4px;font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.03em;" }, "Node Pools"),
          dataTable(
            [
              { key: "name", label: "Name", mono: true },
              { key: "node_count", label: "Nodes" },
              { key: "machine_type", label: "Machine Type", mono: true },
              { label: "Zones", render: (p) => (p.zones || []).join(", ") || "—" },
              { label: "Autoscaling", render: (p) => (p.autoscaling ? `${p.min_nodes}–${p.max_nodes}` : "Fixed") },
            ],
            meta.node_pools,
            { emptyTitle: "No node pools reported" }
          ),
          h("div", { style: "padding:16px 16px 4px;font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.03em;border-top:1px solid var(--border);margin-top:8px;" }, "Nodes"),
          dataTable(
            [
              { key: "name", label: "Name", mono: true, truncate: true },
              { key: "node_pool", label: "Pool", mono: true },
              { key: "instance_type", label: "Type", mono: true },
              { key: "internal_ip", label: "Internal IP", mono: true },
              { key: "external_ip", label: "External IP", mono: true },
              { key: "kubernetes_version", label: "Version", mono: true },
              { label: "Status", render: (n) => statusPill(n.status, n.status === "Ready") },
            ],
            dataPlaneNodes,
            { emptyTitle: "No nodes reported" }
          ),
        ]),
    },
  ];

  const planeTabsRow = h("div", { class: "subtabs", role: "tablist" });
  const planePanelBody = h("div", {});
  const showPlaneSection = (key) => {
    const section = planeSections.find((s) => s.key === key);
    planeTabsRow.querySelectorAll(".subtabs__item").forEach((btn) => btn.setAttribute("aria-selected", String(btn.dataset.key === key)));
    renderInto(planePanelBody, section.render());
  };
  for (const section of planeSections) {
    planeTabsRow.appendChild(
      h(
        "button",
        { class: "subtabs__item", role: "tab", "data-key": section.key, "aria-selected": "false", onClick: () => showPlaneSection(section.key) },
        section.label
      )
    );
  }
  showPlaneSection(planeSections[0].key);
  children.push(h("div", {}, [planeTabsRow, panel(null, planePanelBody)]));

  const wi = meta.workload_identity || {};
  children.push(
    panel(
      "Pod Identity",
      h("div", { style: "display:flex;flex-direction:column;gap:8px;" }, [
        h("div", {}, h("strong", {}, wi.mechanism || "—")),
        h("div", { class: "view-header__meta" }, wi.description || ""),
        h("div", {}, wi.detail || ""),
        wi.docs_url ? h("a", { href: wi.docs_url, target: "_blank", rel: "noopener noreferrer" }, "Documentation ↗") : null,
      ]),
      null,
      true
    )
  );

  if (meta.console_url || meta.logs_url) {
    children.push(h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;" }, [
      meta.console_url ? h("a", { href: meta.console_url, target: "_blank", rel: "noopener noreferrer", class: "btn btn--ghost" }, `Open in ${badge.cloud} Console ↗`) : null,
      meta.logs_url ? h("a", { href: meta.logs_url, target: "_blank", rel: "noopener noreferrer", class: "btn btn--ghost" }, `View Cloud Logs ↗`) : null,
    ].filter(Boolean)));
  }

  renderInto($("overview-content"), h("div", {}, children));
}

const helmController = createHelmViewsController({
  getClusterId: () => state.selectedClusterId,
  timeAgo,
  openManifestModal: (kind, name, namespace) => openManifestModal(kind, name, namespace),
  openPodInspectorModal: (podName, namespace) => openPodInspectorModal(podName, namespace),
  openResourceMapForNamespace: (ns) => {
    const typeEl = $("resourcemap-type-select");
    const resEl = $("resourcemap-resource-select");
    if (typeEl) typeEl.value = "";
    if (resEl) resEl.value = "";
    applyNamespaceFilterForView("resourcemap", ns || "default");
    switchView("resourcemap");
  },
  switchView: (view) => switchView(view),
  nextLoadToken,
  isStaleToken,
});

async function loadEcosystemView(force = false) {
  const container = $("ecosystem-view-content");
  if (!container) return;
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `ecosystem:${clusterId}`,
    tokenView: "ecosystem-view",
    container,
    force,
    fetchFn: () => api.getEcosystemOverview(clusterId),
    renderFn: (data) => {
      const all = data.integrations || [];
      const detected = all.filter((i) => i.installed);
      renderInto(container, h("div", {}, [
        statGrid([
          statCard("Categories Checked", data.total_checked || all.length, null, STAT_ICONS.grid),
          statCard("Installed Integrations", detected.length, null, STAT_ICONS.checkCircle),
        ]),
        panel(
          "Detected Platform & CNCF Integrations",
          detected.length
            ? h("div", { style: "padding:1rem;display:flex;flex-wrap:wrap;gap:10px;" }, detected.map((i) =>
                h("span", {
                  style: "display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:99px;background:var(--accent-soft);color:var(--accent);font-size:0.85rem;font-weight:600;"
                }, [i.category, h("span", { style: "opacity:0.75;font-weight:700;" }, `${i.total_count}`)])
              ))
            : emptyState("No ecosystem CRDs detected", `Checked ${data.total_checked || 11} categories (cert-manager, KEDA, Karpenter, Velero, External Secrets, Kyverno, CloudNativePG, Knative, Trivy, Argo CD, Flux).`),
          null,
          true
        ),
      ]));
    },
  });
}

async function loadCostView(force = false) {
  const container = $("cost-view-content");
  if (!container) return;
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `cost:${clusterId}`,
    tokenView: "cost-view",
    container,
    force,
    fetchFn: () => api.getCostEstimate(clusterId),
    renderFn: (data) => {
      const pools = data.pools || [];
      const totalNodes = pools.reduce((s, p) => s + (p.node_count || 0), 0);
      const monthlyFormatted = data.monthly_total_usd != null ? `$${data.monthly_total_usd.toFixed(2)}/mo` : "—";
      renderInto(container, h("div", {}, [
        statGrid([
          statCard("Estimated Monthly Compute", monthlyFormatted, null, STAT_ICONS.pulse),
          statCard("Node Pools", pools.length, null, STAT_ICONS.layers),
          statCard("Total Nodes", totalNodes, null, STAT_ICONS.server),
          statCard("Provider", (data.provider || "—").toUpperCase(), null, STAT_ICONS.cloud),
        ]),
        data.error && !data.monthly_total_usd ? warningBanner(data.error) : null,
        panel(
          "Node Pool Cost Breakdown",
          dataTable(
            [
              { key: "name", label: "Pool Name", mono: true },
              { label: "Machine Type", mono: true, render: (p) => p.machine_type || "—" },
              { key: "node_count", label: "Nodes" },
              { label: "Hourly Rate / Node", mono: true, render: (p) => (p.hourly_price_usd != null ? `$${p.hourly_price_usd.toFixed(4)}/hr` : "—") },
              { label: "Estimated Monthly", mono: true, render: (p) => (p.priced && p.monthly_estimate_usd != null ? `$${p.monthly_estimate_usd.toFixed(2)}/mo` : p.note || "unpriced") },
            ],
            pools,
            { emptyTitle: "No node pools reported for cost estimation" }
          )
        ),
        data.disclaimer
          ? h("div", { style: "margin-top:12px;padding:12px 16px;border-radius:12px;background:var(--bg-inset);border:1px solid var(--border);font-size:0.78rem;color:var(--fg-subtle);line-height:1.5;" }, data.disclaimer)
          : null,
      ].filter(Boolean)));
    },
  });
}

// Cost estimation shells out to a cloud pricing API (slow on first call for
// GKE especially -- see providers/pricing.py) -- loaded separately, after
// the main overview, same as the ecosystem panel below.
async function loadCostPanel(force = false) {
  const target = $("cost-panel-content");
  if (!target) return;
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `cost:${clusterId}`,
    tokenView: "cost-panel",
    container: target,
    force,
    showSkeleton: false,
    fetchFn: () => api.getCostEstimate(clusterId),
    renderFn: (data) => {
      if (data.error && !data.monthly_total_usd) {
        renderInto(target, h("div", { style: "font-size:0.85rem;color:var(--fg-subtle);" }, data.error));
        return;
      }

      const rows = (data.pools || []).map((p) => h("div", {
        style: "display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:0.82rem;"
      }, [
        h("span", {}, [
          h("span", { style: "font-family:var(--font-mono);font-weight:600;color:var(--fg);" }, p.name),
          h("span", { style: "color:var(--fg-subtle);margin-left:6px;" }, `${p.machine_type || "—"} × ${p.node_count}`),
        ]),
        p.priced
          ? h("span", { style: "font-weight:700;color:var(--fg);" }, `$${p.monthly_estimate_usd?.toFixed(2)}/mo`)
          : h("span", { style: "color:var(--fg-subtle);font-style:italic;", title: p.note || "" }, "unpriced"),
      ]));

      renderInto(target, h("div", { style: "display:flex;flex-direction:column;gap:10px;" }, [
        h("div", { style: "font-size:1.6rem;font-weight:800;color:var(--fg);" },
          data.monthly_total_usd != null ? `$${data.monthly_total_usd.toFixed(2)}/mo` : "—"),
        h("div", { style: "display:flex;flex-direction:column;" }, rows),
        h("div", { style: "font-size:0.72rem;color:var(--fg-subtle);font-style:italic;line-height:1.4;" }, data.disclaimer || ""),
      ]));
    },
    onError: () => {
      renderInto(target, h("div", { style: "font-size:0.82rem;color:var(--fg-subtle);" }, "Could not load cost estimate."));
    },
  });
}

// Ecosystem CRD detection takes several seconds on first load (it probes ~18
// resource types) -- loaded separately and after the main overview so it
// never blocks the rest of the page from appearing.
async function loadEcosystemPanel(force = false) {
  const target = $("ecosystem-panel-content");
  if (!target) return;
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `ecosystem:${clusterId}`,
    tokenView: "ecosystem-panel",
    container: target,
    force,
    showSkeleton: false,
    fetchFn: () => api.getEcosystemOverview(clusterId),
    renderFn: (data) => {
      const detected = (data.integrations || []).filter((i) => i.installed);
      if (!detected.length) {
        renderInto(target, h("div", { style: "font-size:0.85rem;color:var(--fg-subtle);" },
          `No known ecosystem CRDs detected (checked ${data.total_checked} categories: cert-manager, KEDA, Karpenter, Velero, External Secrets, Kyverno, CloudNativePG, Knative, Trivy, Argo CD, Flux).`));
        return;
      }
      renderInto(target, h("div", { style: "display:flex;flex-wrap:wrap;gap:8px;" }, detected.map((i) =>
        h("span", {
          style: "display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:99px;background:var(--accent-soft);color:var(--accent);font-size:0.8rem;font-weight:600;"
        }, [i.category, h("span", { style: "opacity:0.75;font-weight:700;" }, `${i.total_count}`)])
      )));
    },
    onError: () => {
      renderInto(target, h("div", { style: "font-size:0.82rem;color:var(--fg-subtle);" }, "Could not load ecosystem detection."));
    },
  });
}

async function loadOverview(force = false) {
  const container = $("overview-content");
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `overview:${clusterId}`,
    tokenView: "overview",
    container,
    force,
    fetchFn: () => api.getClusterInfo(clusterId),
    renderFn: (data) => {
      renderOverview(data);
      recordNodesSidebarCount(clusterId, data.node_count ?? data.nodes?.length);
      loadCostPanel(force);
      loadEcosystemPanel(force);
    },
  });
}

// -- view: workloads ------------------------------------------------------

function workloadDetail(r) {
  if (r.reason) return r.reason;
  if (r.desired !== undefined) return `${r.ready}/${r.desired} ready`;
  if (r.failed !== undefined) return `${r.failed} failed, ${r.succeeded}/${r.completions} succeeded`;
  if (r.restarts !== undefined) return `${r.restarts} restarts`;
  return "—";
}

// RESOURCE_TYPE_THEME's icon field is a bare shape (<polygon>/<rect>/...),
// meant to be dropped inside an already-open <svg> in the resource map's own
// canvas -- wrap it in one here so it's a self-contained icon usable inside
// statCard()'s plain HTML span.
function kindIcon(kind) {
  const theme = RESOURCE_TYPE_THEME[kind];
  return theme ? `<svg viewBox="0 0 24 24">${theme.icon}</svg>` : "";
}

// Workload-type categories shown as stat cards on the Overview. Each maps
// 1:1 to a CATALOG_TYPES key (defined further below) -- Pods used to have
// its own separate nav item/view; folded in here the same way as the rest.
const WORKLOAD_STAT_CATEGORIES = [
  ["Pods", "pods", "pods"],
  ["Deployments", "deployments", "deployments"],
  ["StatefulSets", "statefulsets", "statefulsets"],
  ["DaemonSets", "daemonsets", "daemonsets"],
  ["Jobs", "jobs", "jobs"],
];

let workloadsOverviewData = null; // cached so expanding/collapsing a type doesn't need to re-fetch the stat totals

// Overview used to be a dead end -- see the totals, then go dig through the
// separate Workload Catalog page to actually find a resource of that type.
// Clicking a stat card now opens that type's full list right here (the same
// row renderers CATALOG_TYPES already used), collapsing the two views the
// user asked to combine into one.
// statCard() returns a plain <div> -- not focusable or keyboard-activatable
// on its own, so a card meant to be clicked needs the same tabindex/role/
// keydown treatment any other custom interactive element in this app gets
// (see e.g. the Cluster Map's namespace chips) rather than only a mouse
// click handler.
function makeStatCardClickable(card, onActivate) {
  card.setAttribute("tabindex", "0");
  card.setAttribute("role", "button");
  card.addEventListener("click", onActivate);
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onActivate(); }
  });
}

function renderWorkloadStats(data) {
  if (!data) return;
  const o = data.overview;
  const cards = WORKLOAD_STAT_CATEGORIES.map(([label, statKey, catalogKey]) => {
    const stat = o[statKey];
    const card = statCard(
      label, stat.total,
      [pill(`${stat.healthy} healthy`, "healthy"), stat.unhealthy ? pill(`${stat.unhealthy} unhealthy`, "critical") : null],
      kindIcon(label.toLowerCase().replace(/s$/, ""))
    );
    card.classList.add("stat-card--clickable");
    if (catalogTypeFilter === catalogKey) card.classList.add("stat-card--selected");
    makeStatCardClickable(card, () => toggleWorkloadCatalogType(catalogKey));
    return card;
  });
  const cronCard = statCard("CronJobs", o.cronjobs.total, [pill(`${o.cronjobs.active} active`, "healthy")], kindIcon("cronjob"));
  cronCard.classList.add("stat-card--clickable");
  if (catalogTypeFilter === "cronjobs") cronCard.classList.add("stat-card--selected");
  makeStatCardClickable(cronCard, () => toggleWorkloadCatalogType("cronjobs"));
  cards.push(cronCard);
  renderInto($("workloads-stats"), statGrid(cards));
}

function renderWorkloadIssues(data) {
  const o = data.overview;
  const categories = [
    ["Pods", o.pods], ["Deployments", o.deployments], ["StatefulSets", o.statefulsets],
    ["DaemonSets", o.daemonsets], ["Jobs", o.jobs],
  ];
  const children = [];
  for (const [label, stat] of categories) {
    if (!stat.issues || !stat.issues.length) continue;
    children.push(
      panel(
        `${label} — Issues`,
        dataTable(
          [
            { key: "name", label: "Name", mono: true },
            { key: "namespace", label: "Namespace", mono: true },
            { label: "Status", render: (r) => statusPill(r.status, r.healthy) },
            { label: "Detail", render: workloadDetail },
          ],
          stat.issues
        )
      )
    );
  }
  if (categories.every(([, s]) => !s.unhealthy)) {
    children.push(emptyState("All workloads healthy", "No unhealthy Pods, Deployments, StatefulSets, DaemonSets, or Jobs were found."));
  }
  renderInto($("workloads-issues"), h("div", {}, children));
}

function renderWorkloads(data) {
  workloadsOverviewData = data;
  renderWorkloadStats(data);
  renderWorkloadIssues(data);
}

async function loadWorkloads(force = false) {
  const statsContainer = $("workloads-stats");
  renderInto($("workloads-issues"), h("div", {}));
  // Data is for a (possibly new) namespace -- collapse any open catalog
  // panel rather than leave it showing a stale, now-mismatched type list.
  catalogTypeFilter = null;
  const catalogPanel = $("workloads-catalog-panel");
  if (catalogPanel) catalogPanel.hidden = true;
  const clusterId = state.selectedClusterId;
  const namespace = $("workloads-namespace-select").value || undefined;
  await loadWithSWR({
    key: `workloads:${clusterId}:${namespace || "_all_"}`,
    tokenView: "workloads",
    container: statsContainer,
    force,
    fetchFn: () => api.getWorkloads(clusterId, namespace),
    renderFn: (data) => renderWorkloads(data),
  });
}

// -- view: namespace resources ----------------------------------------

function renderNamespacePodCard(pod, namespace) {
  const isRunning = pod.status === "Running" || pod.status === "Succeeded";
  const statusLabel = pod.reason || pod.status || "Unknown";
  const res = pod.resources || {};
  const cpuPct = res.cpu_pct ?? 0;
  const memPct = res.mem_pct ?? 0;
  const usedCpu = res.used_cpu_str || "0m";
  const usedMem = res.used_mem_str || "0Mi";
  const reqCpu = res.req_cpu_str || "—";
  const reqMem = res.req_mem_str || "—";
  const limCpu = res.lim_cpu_str || "—";
  const limMem = res.lim_mem_str || "—";

  const shortNode = pod.node
    ? (pod.node.length > 26 ? `...${pod.node.slice(-22)}` : pod.node)
    : null;
  const shortPodName = pod.name && pod.name.length > 26
    ? `${pod.name.slice(0, 23)}...`
    : (pod.name || "pod");

  return h("div", { class: "cmap-card ns-pod-card" }, [
    // Header row: status dot, pod name, status pill, and Logs / YAML / Inspect action buttons
    h("div", { class: "cmap-card__header" }, [
      h("div", { class: "cmap-card__name-row" }, [
        h("span", { class: `cmap-status-dot ${isRunning ? "cmap-status-dot--ok" : "cmap-status-dot--bad"}` }),
        h("span", {
          class: "cmap-card__name",
          title: pod.name,
          style: "cursor:pointer;",
          onclick: () => openPodInspectorModal(pod.name, namespace || pod.namespace),
        }, shortPodName),
        statusPill(statusLabel, isRunning),
      ]),
      h("div", { style: "display:flex;align-items:center;gap:6px;" }, [
        h("button", {
          class: "btn btn--secondary btn--sm",
          style: "padding:0.25rem 0.65rem;font-size:0.76rem;font-weight:700;",
          onclick: () => openPodInspectorModal(pod.name, namespace || pod.namespace),
          title: "Inspect probes, containers & specs",
        }, "Inspect"),
        h("button", {
          class: "btn btn--secondary btn--sm",
          style: "padding:0.25rem 0.65rem;font-size:0.76rem;font-weight:700;",
          onclick: () => openLogsModal(pod.name, namespace || pod.namespace),
        }, "Logs"),
        h("button", {
          class: "btn btn--secondary btn--sm",
          style: "padding:0.25rem 0.65rem;font-size:0.76rem;font-weight:700;",
          onclick: () => openManifestModal("Pod", pod.name, namespace || pod.namespace),
        }, "YAML"),
      ]),
    ]),

    // Metadata chip pills row: Ready X/Y | N restarts | age | node | Pod IP / QoS
    h("div", { class: "cmap-card__chips" }, [
      h("span", { class: "cmap-chip" }, `Ready ${pod.ready || "0/0"}`),
      h("span", {
        class: "cmap-chip",
        style: (pod.restarts || 0) > 0 ? "background:var(--warning-soft);color:var(--warning);" : "",
      }, `${pod.restarts ?? 0} restarts`),
      pod.created_at ? h("span", { class: "cmap-chip" }, timeAgo(pod.created_at)) : null,
      shortNode ? h("span", { class: "cmap-chip", title: pod.node, style: "font-family:var(--font-mono);" }, shortNode) : null,
      pod.pod_ip ? h("span", { class: "cmap-chip", style: "font-family:var(--font-mono);" }, pod.pod_ip) : null,
    ].filter(Boolean)),

    // Two radial gauges (CPU & MEM) matching the Cluster Map aesthetic
    h("div", { class: "cmap-gauges", style: "padding:6px 0;" }, [
      radialGauge(cpuPct, "CPU", `${usedCpu} / ${limCpu !== "—" ? limCpu : reqCpu} limit`, "#3b82f6"),
      radialGauge(memPct, "MEM", `${usedMem} / ${limMem !== "—" ? limMem : reqMem} limit`, "#8b5cf6"),
    ]),

    // Footer row: Req CPU · Mem on left, Limit CPU · Mem on right
    h("div", {
      style: "display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;padding-top:6px;border-top:1px solid var(--border);font-size:0.72rem;color:var(--fg-subtle);font-weight:600;",
    }, [
      h("span", {}, `Req ${reqCpu} CPU \u00b7 ${reqMem}`),
      h("span", {}, `Limit ${limCpu} CPU \u00b7 ${limMem}`),
    ]),
  ]);
}

function triggerGaugeAnimations(container) {
  if (!container) return;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      container.querySelectorAll(".cmap-gauge__fill").forEach((el) => {
        el.style.strokeDashoffset = el.dataset.targetOffset;
      });
    });
  });
}

function openConfigMapModal(cm, clusterId, namespace) {
  lastFocusedBeforeModal = document.activeElement;
  $("resource-modal-title").textContent = `ConfigMap — ${cm.name}`;
  const previewEntries = Object.entries(cm.data_preview || {});
  const rows = previewEntries.map(([k, v]) =>
    h("div", { class: "kv-row" }, [
      h("div", { class: "kv-row__key" }, k),
      h("div", { class: "kv-row__value" }, v || "—"),
    ])
  );
  $("resource-modal-body").replaceChildren(
    h("div", { style: "display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;" }, [
      h("div", { class: "actions-row" }, [
        pill("CONFIGMAP", "info"),
        pill(`${cm.key_count || 0} keys`, "healthy"),
      ]),
      h("button", {
        class: "btn btn--secondary btn--sm",
        onclick: () => {
          closeResourceModal();
          openManifestModal("ConfigMap", cm.name, namespace);
        },
      }, "Full YAML"),
    ]),
    h("div", { class: "kv-list" }, rows.length ? rows : [emptyState("Empty ConfigMap", "This ConfigMap has no data keys.")])
  );
  $("resource-modal").hidden = false;
  $("resource-modal-close").focus();
}

function renderNamespaceResources(data, { clusterId, namespace }) {
  const gatewayUnavailable = () =>
    emptyState("Gateway API not installed", "This cluster does not have the Gateway API CRDs registered.");

  const sections = [
    {
      key: "pods",
      label: "Pods",
      count: data.pods.length,
      isCards: true,
      render: () => {
        if (!data.pods.length) {
          return emptyState("No pods in this namespace", `Namespace '${namespace}' has no running or pending pods.`);
        }
        return h("div", { class: "cmap-grid" }, data.pods.map((p) => renderNamespacePodCard(p, namespace)));
      },
    },
    {
      key: "services",
      label: "Services",
      count: data.services.length,
      render: () =>
        dataTable(
          [
            { key: "name", label: "Name", mono: true, truncate: true },
            { label: "Type", render: (r) => pill(r.type || "ClusterIP", r.type === "LoadBalancer" ? "healthy" : "info") },
            { key: "cluster_ip", label: "Cluster IP", mono: true },
            { label: "External", render: (r) => (r.external_ips || []).join(", ") || "—" },
            {
              label: "Ports (Port → Target)",
              mono: true,
              render: (r) =>
                (r.ports || [])
                  .map((p) => `${p.port}${p.target_port && p.target_port !== p.port ? `→${p.target_port}` : ""}/${p.protocol || "TCP"}`)
                  .join(", ") || "—",
            },
            {
              label: "Selector",
              mono: true,
              render: (r) => {
                const entries = Object.entries(r.selector || {});
                return entries.length ? entries.map(([k, v]) => `${k}=${v}`).join(", ") : "—";
              },
            },
            { label: "Age", render: (r) => timeAgo(r.created_at) },
            {
              label: "Actions",
              render: (r) =>
                h("div", { style: "display:flex;gap:6px;" }, [
                  h("button", {
                    class: "btn btn--secondary btn--sm",
                    style: "padding:0.2rem 0.55rem;font-size:0.75rem;font-weight:600;",
                    onclick: () => openManifestModal("Service", r.name, namespace),
                  }, "YAML"),
                ]),
            },
          ],
          data.services,
          { emptyTitle: "No services in this namespace" }
        ),
    },
    {
      key: "configmaps",
      label: "ConfigMaps",
      count: data.configmaps.length,
      render: () =>
        dataTable(
          [
            { key: "name", label: "Name", mono: true, truncate: true },
            { key: "key_count", label: "Keys" },
            {
              label: "Key Names",
              mono: true,
              truncate: true,
              render: (r) => Object.keys(r.data_preview || {}).join(", ") || "—",
            },
            { label: "Age", render: (r) => timeAgo(r.created_at) },
            {
              label: "Actions",
              render: (r) =>
                h("div", { style: "display:flex;gap:6px;" }, [
                  h("button", {
                    class: "btn btn--primary btn--sm",
                    style: "padding:0.2rem 0.55rem;font-size:0.75rem;font-weight:600;",
                    onclick: () => openConfigMapModal(r, clusterId, namespace),
                  }, "Preview Keys"),
                  h("button", {
                    class: "btn btn--secondary btn--sm",
                    style: "padding:0.2rem 0.55rem;font-size:0.75rem;font-weight:600;",
                    onclick: () => openManifestModal("ConfigMap", r.name, namespace),
                  }, "YAML"),
                ]),
            },
          ],
          data.configmaps,
          { emptyTitle: "No ConfigMaps in this namespace" }
        ),
    },
    {
      key: "secrets",
      label: "Secrets",
      count: data.secrets.length,
      render: () =>
        dataTable(
          [
            { key: "name", label: "Name", mono: true, truncate: true },
            { key: "type", label: "Type", mono: true },
            { key: "key_count", label: "Keys" },
            { label: "Key Names", mono: true, truncate: true, render: (r) => (r.keys || []).join(", ") || "—" },
            { label: "Age", render: (r) => timeAgo(r.created_at) },
            {
              label: "Actions",
              render: () =>
                h(
                  "span",
                  {
                    class: "pill pill--unknown",
                    title: "Kubernetes Secret values and YAML are disabled for security reasons",
                  },
                  "Metadata only"
                ),
            },
          ],
          data.secrets,
          { emptyTitle: "No secrets in this namespace" }
        ),
    },
    {
      key: "gateways",
      label: "Gateways",
      count: data.gateways.available ? data.gateways.items.length : 0,
      render: () =>
        data.gateways.available
          ? dataTable(
              [
                { key: "name", label: "Name", mono: true, truncate: true },
                { key: "gateway_class", label: "Class" },
                { label: "Addresses", render: (r) => (r.addresses || []).join(", ") || "—" },
                { label: "Listeners", render: (r) => String((r.listeners || []).length) },
                {
                  label: "Actions",
                  render: (r) =>
                    h("button", {
                      class: "btn btn--secondary btn--sm",
                      style: "padding:0.2rem 0.55rem;font-size:0.75rem;font-weight:600;",
                      onclick: () => openManifestModal("Gateway", r.name, namespace),
                    }, "YAML"),
                },
              ],
              data.gateways.items
            )
          : gatewayUnavailable(),
    },
    {
      key: "httproutes",
      label: "HTTPRoutes",
      count: data.httproutes.available ? data.httproutes.items.length : 0,
      render: () =>
        data.httproutes.available
          ? dataTable(
              [
                { key: "name", label: "Name", mono: true, truncate: true },
                { label: "Hostnames", render: (r) => (r.hostnames || []).join(", ") || "—" },
                { label: "Parents", render: (r) => (r.parent_refs || []).join(", ") || "—" },
                { label: "Rules", render: (r) => String((r.rules || []).length) },
                {
                  label: "Actions",
                  render: (r) =>
                    h("button", {
                      class: "btn btn--secondary btn--sm",
                      style: "padding:0.2rem 0.55rem;font-size:0.75rem;font-weight:600;",
                      onclick: () => openManifestModal("HTTPRoute", r.name, namespace),
                    }, "YAML"),
                },
              ],
              data.httproutes.items
            )
          : gatewayUnavailable(),
    },
    {
      key: "pvcs",
      label: "PVCs",
      count: data.pvcs.length,
      render: () =>
        dataTable(
          [
            { key: "name", label: "Name", mono: true, truncate: true },
            { label: "Status", render: (r) => statusPill(r.status, r.status === "Bound") },
            { key: "capacity", label: "Capacity", mono: true },
            { key: "volume_name", label: "Bound PV", mono: true, truncate: true },
            { key: "storage_class", label: "StorageClass", mono: true, truncate: true },
            { label: "Access", render: (r) => (r.access_modes || []).join(", ") || "—" },
            { label: "Age", render: (r) => timeAgo(r.created_at) },
            {
              label: "Actions",
              render: (r) =>
                h("button", {
                  class: "btn btn--secondary btn--sm",
                  style: "padding:0.2rem 0.55rem;font-size:0.75rem;font-weight:600;",
                  onclick: () => openManifestModal("PersistentVolumeClaim", r.name, namespace),
                }, "YAML"),
            },
          ],
          data.pvcs,
          { emptyTitle: "No PersistentVolumeClaims in this namespace" }
        ),
    },
    {
      key: "serviceaccounts",
      label: "ServiceAccounts",
      count: data.serviceaccounts.length,
      render: () =>
        dataTable(
          [
            { key: "name", label: "Name", mono: true, truncate: true },
            { label: "Secrets", render: (r) => String((r.secrets || []).length) },
            {
              label: "Workload Identity",
              render: (r) => (Object.keys(r.workload_identity_annotations || {}).length ? pill("configured", "healthy") : pill("none", "unknown")),
            },
            { label: "Age", render: (r) => timeAgo(r.created_at) },
            {
              label: "Actions",
              render: (r) =>
                h("button", {
                  class: "btn btn--secondary btn--sm",
                  style: "padding:0.2rem 0.55rem;font-size:0.75rem;font-weight:600;",
                  onclick: () => openManifestModal("ServiceAccount", r.name, namespace),
                }, "YAML"),
            },
          ],
          data.serviceaccounts
        ),
    },
  ];

  const tabsRow = h("div", { class: "subtabs", role: "tablist" });
  const contentArea = h("div", {});

  const showSection = (key) => {
    const section = sections.find((s) => s.key === key);
    tabsRow.querySelectorAll(".subtabs__item").forEach((btn) => btn.setAttribute("aria-selected", String(btn.dataset.key === key)));
    if (section.isCards) {
      renderInto(contentArea, section.render());
      triggerGaugeAnimations(contentArea);
    } else {
      renderInto(contentArea, panel(null, section.render()));
    }
  };

  for (const section of sections) {
    const btn = h(
      "button",
      { class: "subtabs__item", role: "tab", "data-key": section.key, "aria-selected": "false", onClick: () => showSection(section.key) },
      [section.label, h("span", { class: "subtabs__count" }, section.count === null ? "0" : String(section.count))]
    );
    tabsRow.appendChild(btn);
  }

  showSection(sections[0].key);
  renderInto($("namespace-content"), h("div", {}, [tabsRow, contentArea]));
}

async function loadNamespaces(force = false) {
  const container = $("namespace-content");
  const namespace = $("namespace-select").value;
  if (!namespace) {
    renderInto(container, emptyState("No namespace selected", "Choose a namespace above."));
    return;
  }
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `namespaces:${clusterId}:${namespace}`,
    tokenView: "namespaces",
    container,
    force,
    fetchFn: () => api.getNamespaceResources(clusterId, namespace),
    renderFn: (data) => renderNamespaceResources(data, { clusterId, namespace }),
  });
}

let lastFocusedBeforeModal = null;

// -- resource detail modal (topology node click) --------------------------

function humanizeKey(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatDetailValue(key, value) {
  if (value === null || value === undefined || value === "") return "—";
  if (key === "created_at") return `${timeAgo(value)} (${value})`;
  if (Array.isArray(value)) {
    if (!value.length) return "—";
    // An array of plain scalars reads fine joined on one line; an array of
    // objects (e.g. a pod's containers) gets pretty-printed instead, since
    // .kv-row__value already preserves whitespace (white-space: pre-wrap).
    if (value.some((v) => v !== null && typeof v === "object")) return JSON.stringify(value, null, 2);
    return value.join(", ");
  }
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

function renderResourceDetails(node) {
  const meta = node.meta || {};
  const skipKeys = new Set(["name", "namespace"]); // already shown as the modal title
  const rows = Object.entries(meta)
    .filter(([k, v]) => !skipKeys.has(k) && v !== undefined)
    .map(([k, v]) => h("div", { class: "kv-row" }, [h("div", { class: "kv-row__key" }, humanizeKey(k)), h("div", { class: "kv-row__value" }, formatDetailValue(k, v))]));
  return h("div", {}, [
    h("div", { class: "actions-row", style: "margin-bottom:12px;" }, [pill((node.kind || "resource").toUpperCase(), "unknown"), statusPill(node.status)]),
    h("div", { class: "kv-list" }, rows.length ? rows : [emptyState("No additional details", "This resource has no extra metadata to show.")]),
  ]);
}

function openResourceModal(node) {
  lastFocusedBeforeModal = document.activeElement;
  $("resource-modal-title").textContent = node.label || node.id;
  $("resource-modal-body").replaceChildren(renderResourceDetails(node));
  $("resource-modal").hidden = false;
  $("resource-modal-close").focus();
}

function closeResourceModal() {
  $("resource-modal").hidden = true;
  if (lastFocusedBeforeModal) lastFocusedBeforeModal.focus();
}

// -- view: topology -------------------------------------------------------

async function loadNetworkTopology(force = false) {
  const container = $("network-topology-content");
  const namespace = $("network-namespace-select").value;
  if (!namespace) {
    renderInto(container, emptyState("No namespace selected", "Choose a namespace above."));
    return;
  }
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `network:${clusterId}:${namespace}`,
    tokenView: "network-topology",
    container,
    force,
    fetchFn: () => api.getNetworkTopology(clusterId, namespace),
    renderFn: (graph) => {
      const key = `${clusterId}:${namespace}`;
      const animate = state.lastTopologyKey.network !== key;
      state.lastTopologyKey.network = key;
      const wrap = h("div", {}, [
        h("div", { class: "topology-toolbar" }, topologyLegend()),
        !graph.gateway_api_available
          ? warningBanner("Gateway API CRDs are not installed on this cluster -- showing Load Balancer / Service / Pod relationships only.")
          : null,
        h("div", { class: "topology-container" }, renderTopologyGraph(graph, NETWORK_COLUMNS, animate, openResourceModal)),
      ]);
      renderInto(container, wrap);
    },
  });
}

async function loadStorageTopology(force = false) {
  const container = $("storage-topology-content");
  const namespace = $("storage-namespace-select").value;
  if (!namespace) {
    renderInto(container, emptyState("No namespace selected", "Choose a namespace above."));
    return;
  }
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `storage:${clusterId}:${namespace}`,
    tokenView: "storage-topology",
    container,
    force,
    fetchFn: () => api.getStorageTopology(clusterId, namespace),
    renderFn: (graph) => {
      const key = `${clusterId}:${namespace}`;
      const animate = state.lastTopologyKey.storage !== key;
      state.lastTopologyKey.storage = key;
      const wrap = h("div", {}, [
        h("div", { class: "topology-toolbar" }, topologyLegend()),
        h("div", { class: "topology-container" }, renderTopologyGraph(graph, STORAGE_COLUMNS, animate, openResourceModal)),
      ]);
      renderInto(container, wrap);
    },
  });
}

async function loadNamespaceMap(force = false) {
  const container = $("map-content");
  const namespace = $("map-namespace-select").value;
  if (!namespace) {
    renderInto(container, emptyState("No namespace selected", "Choose a namespace above."));
    return;
  }
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `nsmap:${clusterId}:${namespace}`,
    tokenView: "map",
    container,
    force,
    fetchFn: () => api.getNamespaceMap(clusterId, namespace),
    renderFn: (graph) => {
      const key = `${clusterId}:${namespace}`;
      const animate = state.lastTopologyKey.map !== key;
      state.lastTopologyKey.map = key;
      const banners = [];
      if (!graph.ingress_available && !graph.gateway_api_available) {
        banners.push(warningBanner("No Ingress resources or Gateway API CRDs found -- showing Service/Workload/Pod relationships only."));
      }
      const wrap = h("div", {}, [
        h("div", { class: "topology-toolbar" }, topologyLegend()),
        ...banners,
        h("div", { class: "topology-container" }, renderTopologyGraph(graph, NAMESPACE_MAP_COLUMNS, animate, openResourceModal)),
      ]);
      renderInto(container, wrap);
    },
  });
}

// -- view: events -----------------------------------------------------------

let eventsFilterState = "all";
let eventsSearchQuery = "";

function renderEvents(data) {
  const items = data.items || [];
  const warningCount = items.filter((e) => (e.type || "").toLowerCase() === "warning").length;
  const normalCount = items.length - warningCount;

  const stats = statGrid([
    statCard("Total Events", items.length, null, STAT_ICONS.pulse),
    statCard("Warnings", warningCount, warningCount ? [pill("Needs attention", "critical")] : [pill("None", "healthy")], STAT_ICONS.funnel),
    statCard("Normal Events", normalCount, null, STAT_ICONS.checkCircle),
  ]);

  const filterBar = h("div", { style: "display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;" }, [
    h("div", { class: "subtabs", role: "tablist", style: "margin:0;" }, [
      ["all", "All", items.length],
      ["warning", "Warnings", warningCount],
      ["normal", "Normal", normalCount],
    ].map(([key, label, count]) =>
      h("button", {
        class: "subtabs__item",
        role: "tab",
        "aria-selected": String(eventsFilterState === key),
        onClick: () => {
          eventsFilterState = key;
          renderEvents(data);
        },
      }, [label, h("span", { class: "subtabs__count" }, String(count))])
    )),
    h("input", {
      type: "text",
      class: "input",
      placeholder: "Filter by reason, object, or message...",
      value: eventsSearchQuery,
      style: "width:260px;",
      onInput: (e) => {
        eventsSearchQuery = e.target.value;
        renderEvents(data);
      },
    }),
  ]);

  const q = eventsSearchQuery.toLowerCase().trim();
  const filtered = items.filter((e) => {
    const isWarn = (e.type || "").toLowerCase() === "warning";
    if (eventsFilterState === "warning" && !isWarn) return false;
    if (eventsFilterState === "normal" && isWarn) return false;
    if (!q) return true;
    const hay = `${e.reason || ""} ${e.involved_kind || ""}/${e.involved_name || ""} ${e.message || ""} ${e.namespace || ""}`.toLowerCase();
    return hay.includes(q);
  });

  renderInto(
    $("events-content"),
    h("div", {}, [
      stats,
      panel(
        "Cluster Event Stream",
        dataTable(
          [
            { label: "Type", render: (r) => pill(r.type || "Normal", r.type === "Warning" ? "critical" : "healthy") },
            { key: "reason", label: "Reason", mono: true },
            {
              label: "Object",
              mono: true,
              render: (r) => {
                const objLabel = `${r.involved_kind || "Object"}/${r.involved_name || ""}`;
                if (r.involved_kind && r.involved_name) {
                  return h("button", {
                    type: "button",
                    class: "btn--unstyled",
                    style: "color:var(--accent);font-family:var(--font-mono);font-weight:600;cursor:pointer;text-decoration:underline;text-underline-offset:2px;",
                    title: "Inspect YAML manifest",
                    onclick: () => openManifestModal(r.involved_kind, r.involved_name, r.namespace || ""),
                  }, objLabel);
                }
                return objLabel;
              },
            },
            { key: "namespace", label: "Namespace", mono: true },
            { key: "message", label: "Message" },
            { key: "count", label: "Count" },
            { label: "Last Seen", render: (r) => timeAgo(r.last_timestamp) },
          ],
          filtered,
          { emptyTitle: "No matching events" }
        ),
        filterBar
      ),
    ])
  );
}

async function loadEvents(force = false) {
  const container = $("events-content");
  const clusterId = state.selectedClusterId;
  const namespace = $("events-namespace-select").value || undefined;
  await loadWithSWR({
    key: `events:${clusterId}:${namespace || "_all_"}`,
    tokenView: "events",
    container,
    force,
    fetchFn: () => api.getEvents(clusterId, namespace, 200),
    renderFn: (data) => renderEvents(data),
  });
}

// -- Pods, Deployments & Logs / Manifest Modals -----------------------------

let currentLogPod = null;
let currentLogNs = null;

async function openLogsModal(podName, namespace) {
  currentLogPod = podName;
  currentLogNs = namespace || "default";
  const modal = $("logs-modal");
  if (!modal) return;
  $("logs-modal-podname").textContent = podName;
  modal.hidden = false;
  await refreshLogs();
}

async function refreshLogs() {
  if (!currentLogPod || !state.selectedClusterId) return;
  const contentEl = $("logs-modal-content");
  contentEl.textContent = "Loading logs...";
  const lines = Number($("log-lines-select")?.value || 200);
  try {
    const data = await api.getPodLogs(state.selectedClusterId, currentLogNs, currentLogPod, lines);
    contentEl.textContent = data.logs || "No logs available for this pod.";
    contentEl.scrollTop = contentEl.scrollHeight;
  } catch (err) {
    contentEl.textContent = `Failed to load logs: ${err.message}`;
  }
}

function closeLogsModal() {
  const modal = $("logs-modal");
  if (modal) modal.hidden = true;
}

async function openManifestModal(kind, name, namespace) {
  const modal = $("manifest-modal");
  if (!modal) return;
  $("manifest-modal-resource").textContent = `${kind}/${name}`;
  modal.hidden = false;
  const contentEl = $("manifest-modal-content");
  const normalizedKind = String(kind || "").trim().toLowerCase().split("/").pop().split(".")[0];
  if (normalizedKind === "secret" || normalizedKind === "secrets") {
    contentEl.textContent = "# Kubernetes Secret value and YAML retrieval is disabled for security reasons.";
    return;
  }
  contentEl.textContent = "Loading manifest YAML...";
  try {
    const data = await api.getResourceYaml(state.selectedClusterId, namespace, kind, name);
    contentEl.textContent = data.yaml || "# Resource manifest not found.";
  } catch (err) {
    contentEl.textContent = `# Error fetching manifest: ${err.message}`;
  }
}

function closeManifestModal() {
  const modal = $("manifest-modal");
  if (modal) modal.hidden = true;
}

// -- workload catalog: inline listing opened from an Overview stat card -----
// Pods/Deployments/StatefulSets/DaemonSets/Jobs/CronJobs used to be 6
// separate nav items and pages (Pods on its own getNamespaceResources
// endpoint, the rest sharing getWorkloadsCatalog()), then briefly a
// standalone "Workload Catalog" page with its own type-filter chip
// row. Folded into the Overview page instead: clicking a stat card opens the
// matching type's list right there (see toggleWorkloadCatalogType() and
// renderCatalogPanelBody() below, and workloads-catalog-panel in index.html)
// -- one fetch, same row renderers, no second page to navigate to.

let catalogData = null;
let catalogDataNamespace; // which namespace catalogData was fetched for, so switching types doesn't re-fetch but switching namespaces does
let catalogTypeFilter = null; // null = no type currently expanded on Overview

// Pods used to have their own view backed by a different endpoint
// (getNamespaceResources, single-namespace only) with richer fields
// (qos/owner) that get_workloads_catalog's leaner _normalize_pod() doesn't
// return (name/namespace/status/reason/ready "N/M"/restarts/node/
// created_at only) -- this row is intentionally leaner to match what's
// actually available here, not a straight copy of the old one.
function podRows(items) {
  return [
    h("div", { class: "cmap-grid" }, items.map((pod) => renderNamespacePodCard(pod, pod.namespace))),
  ];
}

function deploymentRows(items) {
  return items.map((d) =>
    h("div", { className: "pod-card-row card", style: "display:flex;align-items:center;justify-content:space-between;padding:0.75rem 1rem;background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px;margin-bottom:0.5rem;box-shadow:var(--shadow-raised-sm);" }, [
      h("div", { style: "display:flex;align-items:center;gap:0.75rem;" }, [
        h("span", { style: "width:10px;height:10px;border-radius:50%;display:inline-block;background:var(--success);" }),
        h("span", { style: "font-weight:700;font-family:var(--font-mono, monospace);font-size:0.95rem;color:var(--fg);" }, d.name),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, d.namespace),
      ]),
      h("div", { style: "display:flex;align-items:center;gap:0.5rem;" }, [
        pill(`Replicas: ${d.ready_replicas || d.replicas || 1}/${d.replicas || 1}`, "success"),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, d.age || timeAgo(d.created_at)),
        h("button", { className: "btn btn--primary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;font-weight:600;", onclick: () => openRolloutModal("deployment", d.name, d.namespace) }, "Rollouts"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;font-weight:600;", onclick: () => openTimelineModal("Deployment", d.name, d.namespace) }, "Timeline"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;font-weight:600;", onclick: () => openDriftDiffModal("Deployment", d.name, d.namespace) }, "Drift Diff"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;font-weight:600;", onclick: () => openManifestModal("Deployment", d.name, d.namespace) }, "YAML"),
      ])
    ])
  );
}

function statefulsetRows(items) {
  return items.map(d => {
    const healthy = d.healthy !== false;
    return h("div", { className: "pod-card-row card", style: "display:flex;align-items:center;justify-content:space-between;padding:0.75rem 1rem;background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px;margin-bottom:0.5rem;" }, [
      h("div", { style: "display:flex;align-items:center;gap:0.75rem;" }, [
        h("span", { style: `width:10px;height:10px;border-radius:50%;display:inline-block;background:${healthy ? "var(--success)" : "var(--danger)"};` }),
        h("span", { style: "font-weight:700;font-family:var(--font-mono,monospace);font-size:0.95rem;color:var(--fg);" }, d.name),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, d.namespace),
      ]),
      h("div", { style: "display:flex;align-items:center;gap:0.5rem;" }, [
        pill(`${d.ready ?? 0}/${d.desired ?? 0} ready`, healthy ? "success" : "warning"),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, timeAgo(d.created_at)),
        h("button", { className: "btn btn--primary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;", onclick: () => openRolloutModal("statefulset", d.name, d.namespace) }, "Rollouts"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;", onclick: () => openTimelineModal("StatefulSet", d.name, d.namespace) }, "Timeline"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;", onclick: () => openDriftDiffModal("StatefulSet", d.name, d.namespace) }, "Drift Diff"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;", onclick: () => openManifestModal("StatefulSet", d.name, d.namespace) }, "YAML"),
      ])
    ]);
  });
}

function daemonsetRows(items) {
  return items.map(d => {
    const healthy = d.healthy !== false;
    return h("div", { className: "pod-card-row card", style: "display:flex;align-items:center;justify-content:space-between;padding:0.75rem 1rem;background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px;margin-bottom:0.5rem;" }, [
      h("div", { style: "display:flex;align-items:center;gap:0.75rem;" }, [
        h("span", { style: `width:10px;height:10px;border-radius:50%;display:inline-block;background:${healthy ? "var(--success)" : "var(--danger)"};` }),
        h("span", { style: "font-weight:700;font-family:var(--font-mono,monospace);font-size:0.95rem;color:var(--fg);" }, d.name),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, d.namespace),
      ]),
      h("div", { style: "display:flex;align-items:center;gap:0.5rem;" }, [
        pill(`${d.ready ?? d.available ?? 0}/${d.desired ?? 0} ready`, healthy ? "success" : "warning"),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, timeAgo(d.created_at)),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;", onclick: () => openTimelineModal("DaemonSet", d.name, d.namespace) }, "Timeline"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;", onclick: () => openDriftDiffModal("DaemonSet", d.name, d.namespace) }, "Drift Diff"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;", onclick: () => openManifestModal("DaemonSet", d.name, d.namespace) }, "YAML"),
      ])
    ]);
  });
}

function jobRows(items) {
  return items.map(j => {
    const done = j.succeeded >= (j.completions || 1);
    const failed = (j.failed || 0) > 0;
    const color = failed ? "var(--danger)" : done ? "var(--success)" : "var(--warning)";
    const statusLabel = failed ? "Failed" : done ? "Complete" : "Running";
    return h("div", { className: "pod-card-row card", style: "display:flex;align-items:center;justify-content:space-between;padding:0.75rem 1rem;background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px;margin-bottom:0.5rem;" }, [
      h("div", { style: "display:flex;align-items:center;gap:0.75rem;" }, [
        h("span", { style: `width:10px;height:10px;border-radius:50%;display:inline-block;background:${color};` }),
        h("span", { style: "font-weight:700;font-family:var(--font-mono,monospace);font-size:0.95rem;color:var(--fg);" }, j.name),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, j.namespace),
      ]),
      h("div", { style: "display:flex;align-items:center;gap:0.5rem;" }, [
        pill(statusLabel, failed ? "critical" : done ? "success" : "warning"),
        pill(`${j.succeeded ?? 0}/${j.completions ?? 1} succeeded`, "info"),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, timeAgo(j.created_at)),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;", onclick: () => openManifestModal("Job", j.name, j.namespace) }, "YAML"),
      ])
    ]);
  });
}

function cronjobRows(items) {
  return items.map(c => {
    const suspended = c.suspend;
    return h("div", { className: "pod-card-row card", style: "display:flex;align-items:center;justify-content:space-between;padding:0.75rem 1rem;background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px;margin-bottom:0.5rem;" }, [
      h("div", { style: "display:flex;align-items:center;gap:0.75rem;" }, [
        h("span", { style: `width:10px;height:10px;border-radius:50%;display:inline-block;background:${suspended ? "var(--unknown)" : "var(--success)"};` }),
        h("span", { style: "font-weight:700;font-family:var(--font-mono,monospace);font-size:0.95rem;color:var(--fg);" }, c.name),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, c.namespace),
      ]),
      h("div", { style: "display:flex;align-items:center;gap:0.5rem;" }, [
        h("span", { style: "font-family:var(--font-mono);font-size:0.8rem;background:var(--bg-inset);padding:2px 8px;border-radius:6px;color:var(--fg-muted);" }, c.schedule),
        pill(suspended ? "Suspended" : `${c.active_jobs ?? 0} active`, suspended ? "muted" : "success"),
        c.last_schedule_time ? h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, `Last: ${timeAgo(c.last_schedule_time)}`) : null,
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;", onclick: () => openManifestModal("CronJob", c.name, c.namespace) }, "YAML"),
      ].filter(Boolean))
    ]);
  });
}

const CATALOG_TYPES = [
  { key: "pods", label: "Pods", dataKey: "pods", rowFn: podRows },
  { key: "deployments", label: "Deployments", dataKey: "deployments", rowFn: deploymentRows },
  { key: "statefulsets", label: "StatefulSets", dataKey: "statefulsets", rowFn: statefulsetRows },
  { key: "daemonsets", label: "DaemonSets", dataKey: "daemonsets", rowFn: daemonsetRows },
  { key: "jobs", label: "Jobs", dataKey: "jobs", rowFn: jobRows },
  { key: "cronjobs", label: "CronJobs", dataKey: "cronjobs", rowFn: cronjobRows },
];

function filterCatalogItems(items) {
  const search = ($("workloads-catalog-search")?.value || "").toLowerCase();
  return (items || []).filter(
    (i) => (i.namespace || "") !== "kube-system" && (!search || i.name.toLowerCase().includes(search))
  );
}

// Fills workloads-catalog-panel's body for whichever type is currently
// expanded. Only touches the body -- never the search input beside it in
// the panel header -- so typing in that box doesn't lose focus per
// keystroke (see the panel's static markup in index.html).
function renderCatalogPanelBody() {
  const type = CATALOG_TYPES.find((t) => t.key === catalogTypeFilter);
  const titleEl = $("workloads-catalog-title");
  const bodyEl = $("workloads-catalog-body");
  if (!type || !catalogData || !bodyEl) return;
  const items = filterCatalogItems(catalogData[type.dataKey]);
  if (titleEl) titleEl.textContent = `${type.label} (${items.length})`;
  renderInto(
    bodyEl,
    items.length ? h("div", {}, type.rowFn(items)) : emptyState("No workloads found", `No matching ${type.label.toLowerCase()} in this namespace.`)
  );
  if (type.key === "pods") {
    triggerGaugeAnimations(bodyEl);
  }
}

// Click handler for an Overview stat card (Deployments/StatefulSets/
// DaemonSets/Jobs/CronJobs) -- opens/closes the inline catalog panel for
// that type, matching the toggle behavior the old catalog-chips had.
async function toggleWorkloadCatalogType(key) {
  const panelEl = $("workloads-catalog-panel");
  if (!panelEl) return;

  if (catalogTypeFilter === key) {
    catalogTypeFilter = null;
    panelEl.hidden = true;
    renderWorkloadStats(workloadsOverviewData);
    return;
  }

  catalogTypeFilter = key;
  panelEl.hidden = false;
  renderWorkloadStats(workloadsOverviewData); // reflect the newly-selected card immediately

  const ns = $("workloads-namespace-select")?.value || undefined;
  if (!catalogData || catalogDataNamespace !== ns) {
    renderInto($("workloads-catalog-body"), loadingSkeleton(3));
    const token = nextLoadToken("catalog");
    try {
      catalogData = await api.getWorkloadsCatalog(state.selectedClusterId, ns);
      catalogDataNamespace = ns;
    } catch (err) {
      if (isStaleToken("catalog", token)) return;
      renderInto($("workloads-catalog-body"), errorState(err, () => toggleWorkloadCatalogType(key)));
      return;
    }
    if (isStaleToken("catalog", token)) return;
  }
  renderCatalogPanelBody();
}

function formatMemory(memStr) {
  if (!memStr) return "—";
  if (typeof memStr === "string" && memStr.endsWith("Ki")) {
    const kib = parseInt(memStr, 10);
    if (!isNaN(kib)) return `${(kib / (1024 * 1024)).toFixed(1)} GB`;
  }
  if (typeof memStr === "string" && memStr.endsWith("Mi")) {
    const mib = parseInt(memStr, 10);
    if (!isNaN(mib)) return `${(mib / 1024).toFixed(1)} GB`;
  }
  return memStr;
}

function renderNodes(nodes = []) {
  const container = $("nodes-content");
  if (!container) return;

  if (!nodes.length) {
    renderInto(container, emptyState("No nodes found", "No active nodes reported for this cluster."));
    return;
  }

  const readyCount = nodes.filter((n) => n.status === "Ready").length;
  const controlPlaneCount = nodes.filter((n) => (n.roles || []).some((r) => r.includes("control-plane") || r.includes("master"))).length;
  const workerCount = Math.max(0, nodes.length - controlPlaneCount);

  const stats = statGrid([
    statCard("Total Nodes", nodes.length, null, STAT_ICONS.server),
    statCard("Ready Nodes", readyCount, null, STAT_ICONS.checkCircle),
    statCard("Control Plane", controlPlaneCount, null, STAT_ICONS.crown),
    statCard("Workers", workerCount, null, STAT_ICONS.server),
  ]);

  const cards = nodes.map((node) => {
    const isReady = node.status === "Ready";
    const roles = (node.roles && node.roles.length) ? node.roles : ["worker"];
    const conds = node.conditions || {};
    const pressureList = ["MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"];

    return h("div", {
      className: "node-card card",
      style: "display:flex;flex-direction:column;gap:0.85rem;padding:1.15rem 1.3rem;background:var(--grad-surface);border:1px solid var(--border);border-radius:14px;margin-bottom:0.85rem;box-shadow:var(--shadow-raised-md);"
    }, [
      h("div", { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem;" }, [
        h("div", { style: "display:flex;align-items:center;gap:0.65rem;flex-wrap:wrap;" }, [
          h("span", {
            class: `cmap-status-dot ${isReady ? "cmap-status-dot--ok" : "cmap-status-dot--bad"}`
          }),
          h("span", { style: "font-weight:700;font-family:var(--font-mono, monospace);font-size:1.02rem;color:var(--fg);" }, node.name),
          statusPill(node.status, isReady),
          ...roles.map((r) => h("span", { className: "badge", style: "font-size:0.72rem;background:var(--accent-soft);color:var(--accent);font-weight:700;text-transform:uppercase;padding:2px 8px;border-radius:99px;" }, r)),
          node.instance_type ? h("span", { class: "cmap-chip" }, node.instance_type) : null,
          node.node_pool ? h("span", { class: "cmap-chip" }, `pool: ${node.node_pool}`) : null,
          node.zone ? h("span", { class: "cmap-chip" }, node.zone) : null,
        ].filter(Boolean)),
        h("div", { style: "display:flex;align-items:center;gap:0.5rem;" }, [
          h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, node.age || (node.created_at ? timeAgo(node.created_at) : "—")),
          h("button", {
            className: "btn btn--secondary btn--sm",
            style: "padding:0.25rem 0.65rem;font-size:0.76rem;font-weight:600;",
            onclick: () => switchView("map")
          }, "Cluster Map"),
          h("button", {
            className: "btn btn--secondary btn--sm",
            style: "padding:0.25rem 0.65rem;font-size:0.76rem;font-weight:600;",
            onclick: () => openManifestModal("Node", node.name, "")
          }, "YAML"),
        ])
      ]),

      // Node specs grid
      h("div", { style: "display:grid;grid-template-columns:repeat(auto-fit, minmax(135px, 1fr));gap:0.75rem;padding-top:0.65rem;border-top:1px solid var(--border);font-size:0.82rem;" }, [
        h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "K8s Version"),
          h("div", { style: "font-family:var(--font-mono);font-weight:600;color:var(--fg);" }, node.kubernetes_version || "—")
        ]),
        h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "CPU / Memory / Pods"),
          h("div", { style: "font-weight:600;color:var(--fg);font-family:var(--font-mono);" }, `${node.cpu_capacity || "—"} vCPU \u00b7 ${formatMemory(node.memory_capacity)} \u00b7 ${node.pods_capacity || 110} pods`)
        ]),
        h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "Internal / External IP"),
          h("div", { style: "font-family:var(--font-mono);font-weight:600;color:var(--fg);font-size:0.78rem;" }, `${node.internal_ip || "—"} / ${node.external_ip || "none"}`)
        ]),
        node.container_runtime ? h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "Container Runtime"),
          h("div", { style: "font-family:var(--font-mono);font-weight:600;color:var(--fg);font-size:0.78rem;" }, node.container_runtime)
        ]) : null,
        h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "OS / Kernel"),
          h("div", { style: "font-weight:500;color:var(--fg-muted);font-size:0.76rem;word-break:break-word;" }, `${node.os_image || "—"}${node.kernel_version ? ` (${node.kernel_version})` : ""}`)
        ]),
      ].filter(Boolean)),

      // Node Health Conditions & Taints bar (KubeLens / Headlamp style)
      h("div", { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;padding-top:0.55rem;border-top:1px solid var(--border);font-size:0.74rem;" }, [
        h("div", { style: "display:flex;align-items:center;gap:6px;flex-wrap:wrap;" }, [
          h("span", { style: "font-weight:700;color:var(--fg-subtle);text-transform:uppercase;font-size:0.66rem;letter-spacing:0.04em;" }, "Conditions:"),
          ...pressureList.map((cName) => {
            const hasPressure = conds[cName] === true;
            return h("span", {
              class: "cmap-chip",
              style: hasPressure
                ? "background:var(--danger-soft);color:var(--danger);font-weight:700;"
                : "background:var(--bg-inset);color:var(--success);font-weight:600;",
            }, `${hasPressure ? "\u26a0 " : "\u2713 "}${cName}: ${hasPressure ? "True" : "OK"}`);
          }),
        ]),
        node.taints && node.taints.length
          ? h("div", { style: "display:flex;align-items:center;gap:4px;flex-wrap:wrap;" }, [
              h("span", { style: "font-weight:700;color:var(--fg-subtle);text-transform:uppercase;font-size:0.66rem;" }, "Taints:"),
              ...node.taints.map((t) => h("span", { class: "cmap-chip", style: "font-family:var(--font-mono);" }, t)),
            ])
          : h("span", { style: "color:var(--fg-subtle);font-size:0.72rem;" }, "No taints (schedulable)"),
      ]),
    ]);
  });

  renderInto(container, h("div", { style: "display:flex;flex-direction:column;gap:1.25rem;" }, [
    stats,
    h("div", { style: "display:flex;flex-direction:column;" }, cards)
  ]));
}

async function loadNodesView(force = false) {
  const container = $("nodes-content");
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `nodes:${clusterId}`,
    tokenView: "nodes",
    container,
    force,
    fetchFn: () => api.getNodes(clusterId),
    renderFn: (data) => {
      renderNodes(data.items || []);
      recordNodesSidebarCount(clusterId, data.count ?? data.items?.length);
    },
  });
}


// -- view routing -----------------------------------------------------------

async function loadClusterMap(force = false) {
  const container = $("map-content");
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `cluster-map:${clusterId}`,
    tokenView: "map",
    container,
    force,
    fetchFn: () => api.getClusterWorkloadMap(clusterId),
    renderFn: (data) => renderClusterMap(data),
  });
}

// Shared tooltip for the Cluster Map's gauges/pod-distribution bar -- built
// on the same .topo-tooltip markup/CSS topology.js's showNodeTooltip() uses,
// but as its own tiny instance rather than reusing that function directly:
// showNodeTooltip's row content is derived from a topology graph node shape
// (kind/label/edges), which doesn't fit a "namespace slice of a node's pod
// capacity" -- duplicating the ~15 lines of positioning logic here is cheaper
// and lower-risk than bending that function's data contract to also cover
// this unrelated caller.
let _cmapTooltipEl = null;
function _cmapTooltip() {
  if (!_cmapTooltipEl) {
    _cmapTooltipEl = document.createElement("div");
    _cmapTooltipEl.className = "topo-tooltip";
    _cmapTooltipEl.setAttribute("role", "tooltip");
    document.body.appendChild(_cmapTooltipEl);
  }
  return _cmapTooltipEl;
}
function _cmapPositionTooltip(x, y) {
  const el = _cmapTooltipEl;
  if (!el) return;
  const margin = 12;
  const rect = el.getBoundingClientRect();
  let left = x + margin;
  let top = y + margin;
  if (left + rect.width > window.innerWidth - margin) left = x - rect.width - margin;
  if (top + rect.height > window.innerHeight - margin) top = y - rect.height - margin;
  el.style.left = `${Math.max(margin, left)}px`;
  el.style.top = `${Math.max(margin, top)}px`;
}
function showCmapTooltip(x, y, kind, name, rows) {
  const el = _cmapTooltip();
  el.replaceChildren(
    h("div", { class: "topo-tooltip__kind" }, kind),
    h("div", { class: "topo-tooltip__name" }, name),
    h("div", { class: "topo-tooltip__rows" }, rows.map(([label, value]) =>
      h("div", { class: "topo-tooltip__row" }, [
        h("span", { class: "topo-tooltip__row-label" }, label),
        h("span", { class: "topo-tooltip__row-value" }, value),
      ])
    ))
  );
  el.classList.add("is-visible");
  _cmapPositionTooltip(x, y);
}
function moveCmapTooltip(x, y) { _cmapPositionTooltip(x, y); }
function hideCmapTooltip() { _cmapTooltipEl?.classList.remove("is-visible"); }

// Radial usage gauge (CPU/MEM/PODS): an SVG ring whose stroke-dashoffset is
// set to "empty" on creation and only moved to its real target after mount
// (see the requestAnimationFrame pass in renderClusterMap) so it animates
// filling in via the .cmap-gauge__fill CSS transition, rather than a percent
// number and a static bar appearing instantly.
function radialGauge(pct, label, sub, baseColor) {
  const clamped = Math.min(100, Math.max(0, Math.round(pct)));
  const r = 26;
  const c = 2 * Math.PI * r;
  const color = clamped >= 85 ? "var(--danger)" : clamped >= 65 ? "var(--warning)" : baseColor;
  const fill = h("circle", {
    class: "cmap-gauge__fill",
    cx: "32", cy: "32", r: String(r),
    stroke: color,
    style: `color:${color};`, // so the CSS glow filter's currentColor matches this ring's stroke
    "stroke-dasharray": String(c),
    "stroke-dashoffset": String(c),
    "data-target-offset": String(c * (1 - clamped / 100)),
  });
  return h("div", { class: "cmap-gauge" }, [
    h("div", { class: "cmap-gauge__ring-wrap" }, [
      h("svg", { viewBox: "0 0 64 64" }, [
        h("circle", { class: "cmap-gauge__track", cx: "32", cy: "32", r: String(r) }),
        fill,
      ]),
      h("span", { class: "cmap-gauge__pct" }, `${clamped}%`),
    ]),
    h("span", { class: "cmap-gauge__label" }, label),
    h("span", { class: "cmap-gauge__sub" }, sub),
  ]);
}

// Isometric cube glyph -- three rhombus faces (top/left/right) shaded via
// opacity to read as a lit 3D box at a glance, even filled flat white at
// ~12px. Drawn once and reused per pod tile rather than rebuilt each time.
const POD_CUBE_ICON = `<svg viewBox="0 0 24 24" fill="white"><path d="M12 2 20 6.5 12 11 4 6.5Z" opacity="0.95"/><path d="M4 6.5 12 11 12 19 4 14.5Z" opacity="0.68"/><path d="M20 6.5 12 11 12 19 20 14.5Z" opacity="0.45"/></svg>`;

// One directly-clickable tile per pod (click opens that pod's logs, same as
// the original dot grid) -- tried an aggregate bar, then a repeated flat
// shape (square, then diamond, then a bar-chart tick), but the ask was for
// an actual 3D/gradient pod glyph, not another flat shape. Each tile is a
// glossy gradient badge (like a provider badge) with the isometric cube
// icon above inside, one per pod, grouped per namespace.
function podChipsForNamespace(ns) {
  const podList = ns.pods && ns.pods.length ? ns.pods : null;
  const items = podList || Array.from({ length: ns.count || 0 });
  return items.map((pName, idx) => {
    const podName = typeof pName === "string" ? pName : null;
    return h("span", {
      class: "cmap-pod-chip",
      style: `--chip-color:${ns.color};animation-delay:${Math.min(idx * 30, 300)}ms;`,
      html: POD_CUBE_ICON,
      tabindex: podName ? "0" : "-1",
      onmouseenter: (e) => showCmapTooltip(e.clientX, e.clientY, "POD", podName || `${ns.namespace}-${idx + 1}`, [
        ["Namespace", ns.namespace],
        ...(podName ? [] : [["Name", "unavailable for this view"]]),
      ]),
      onmousemove: (e) => moveCmapTooltip(e.clientX, e.clientY),
      onmouseleave: hideCmapTooltip,
      onclick: podName ? () => openLogsModal(podName, ns.namespace) : null,
      onkeydown: podName ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openLogsModal(podName, ns.namespace); } } : null,
    });
  });
}

function buildNodeNamespaces(namespaces) {
  const rows = namespaces.map((ns) =>
    h("div", { class: "cmap-ns-row" }, [
      h("div", { class: "cmap-ns-row__label" }, [
        h("span", { class: "cmap-legend__swatch", style: `--chip-color:${ns.color};` }),
        h("span", {}, ns.namespace),
        h("span", { class: "cmap-podlegend__count" }, String(ns.count)),
      ]),
      h("div", { class: "cmap-pod-chips" }, podChipsForNamespace(ns)),
    ])
  );
  return h("div", { class: "cmap-ns-list" }, rows);
}

function renderClusterMap(data) {
  const container = $("map-content");
  const summary = data.summary || { nodes: 0, ready: 0, pods_scheduled: 0, namespaces: 0 };
  const namespaces = (data.namespaces || []).filter((ns) => ns.name !== "kube-system");
  const nodes = data.nodes || [];

  const stats = statGrid([
    statCard("Nodes", summary.nodes, null, STAT_ICONS.server),
    statCard("Ready", summary.ready, null, STAT_ICONS.checkCircle),
    statCard("Pods Scheduled", summary.pods_scheduled, null, kindIcon("pod")),
    statCard("Namespaces", namespaces.length, null, STAT_ICONS.folder),
  ]);

  const legendBar = h("div", { class: "cmap-legend" }, namespaces.map((ns) =>
    h("span", { class: "cmap-legend__item" }, [
      h("span", { class: "cmap-legend__swatch", style: `--chip-color:${ns.color};` }),
      h("span", {}, ns.name),
    ])
  ));

  const nodeCardsList = nodes.map((n) => {
    const isReady = n.status === "Ready";
    const cpuPct = n.cpu?.percent || 0;
    const memPct = n.memory?.percent || 0;
    const capacity = n.pods_capacity?.total || 110;
    const podsPct = Math.round(((n.pods_capacity?.used || 0) / capacity) * 100);
    const nodeNamespaces = (n.namespaces || []).filter((ns) => ns.namespace !== "kube-system");
    const freeSlots = Math.max(0, capacity - (n.pods_capacity?.used || 0));

    return h("div", { class: "cmap-card" }, [
      h("div", { class: "cmap-card__header" }, [
        h("div", { class: "cmap-card__name-row" }, [
          h("span", { class: `cmap-status-dot ${isReady ? "cmap-status-dot--ok" : "cmap-status-dot--bad"}` }),
          h("span", { class: "cmap-card__name", title: n.name }, n.short_name || n.name),
          statusPill(isReady ? "Ready" : (n.status || "NotReady"), isReady),
        ]),
        h("button", {
          class: "btn btn--secondary btn--sm",
          style: "padding:0.25rem 0.65rem;font-size:0.78rem;font-weight:600;",
          onclick: () => openManifestModal("Node", n.name, ""),
        }, "YAML"),
      ]),
      h("div", { class: "cmap-card__chips" }, [
        n.machine_type ? h("span", { class: "cmap-chip" }, n.machine_type) : null,
        n.zone ? h("span", { class: "cmap-chip" }, n.zone) : null,
        n.node_pool ? h("span", { class: "cmap-chip" }, n.node_pool) : null,
      ].filter(Boolean)),

      h("div", { class: "cmap-gauges" }, [
        radialGauge(cpuPct, "CPU", `${n.cpu?.used ?? "—"} / ${n.cpu?.total ?? "—"} cores`, "#3b82f6"),
        radialGauge(memPct, "MEM", `${n.memory?.used ?? "—"} / ${n.memory?.total ?? "—"}`, "#8b5cf6"),
        radialGauge(podsPct, "PODS", `${n.pods_capacity?.used ?? "—"} / ${capacity}`, "var(--success)"),
      ]),

      nodeNamespaces.length ? h("div", { style: "display:flex;flex-direction:column;gap:8px;" }, [
        h("div", { class: "cmap-podsection__header" }, [
          h("span", { class: "cmap-podsection__title" }, "Pod Distribution"),
          h("span", { class: "cmap-podsection__meta" }, `${n.pods_capacity?.used ?? 0} pods · ${freeSlots} slots free`),
        ]),
        buildNodeNamespaces(nodeNamespaces),
      ]) : null,
    ].filter(Boolean));
  });

  const nodesGrid = h("div", { class: "cmap-grid" }, nodeCardsList);

  renderInto(container, h("div", {}, [stats, legendBar, nodesGrid]));

  // Trigger the gauge-ring entrance animation one frame after mount --
  // setting the target value in the same synchronous pass as creation would
  // paint straight to the final state with nothing to transition from.
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      container.querySelectorAll(".cmap-gauge__fill").forEach((el) => {
        el.style.strokeDashoffset = el.dataset.targetOffset;
      });
    });
  });
}

async function loadClustersView() {
  const container = $("clusters-page-content");
  if (!container) return;

  renderInto(container, loadingSkeleton());

  let clusters = [];
  try {
    const res = await api.listClusters();
    clusters = res.clusters || [];
    state.allClusters = clusters;
  } catch (err) {
    renderInto(container, errorState(err, loadClustersView));
    return;
  }

  $("clusters-page-add-btn")?.addEventListener("click", () => {
    openClusterModal(null);
  });

  const totalCount = clusters.length;
  const cloudCount = clusters.filter((c) => ["gke", "eks", "aks"].includes(c.provider)).length;
  const localCount = clusters.filter((c) => c.provider === "local").length;
  const activeCluster = clusters.find((c) => c.id === state.selectedClusterId) || clusters[0];

  const statsRow = statGrid([
    statCard("Total Clusters", String(totalCount), `${cloudCount} Cloud, ${localCount} Local / Kind`, STAT_ICONS.grid),
    statCard("Active Cluster", activeCluster ? activeCluster.name : "None", activeCluster ? `Connected (${activeCluster.environment})` : "Disconnected", STAT_ICONS.pulse),
    statCard("Providers", String(new Set(clusters.map((c) => c.provider)).size), "GCP, AWS, Azure & Local", STAT_ICONS.cloud),
  ]);

  const searchInput = h("input", {
    type: "text",
    placeholder: "Filter clusters by name, provider, or environment...",
    style: "width:100%;padding:10px 14px 10px 38px;border:1.5px solid var(--border);border-radius:12px;background:var(--bg-elevated);color:var(--fg);font-size:0.88rem;outline:none;font-family:inherit;",
  });

  const searchContainer = h("div", { style: "position:relative;margin:1.25rem 0;" }, [
    h("svg", {
      width: "16", height: "16", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
      style: "position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--fg-subtle);"
    }, [
      h("circle", { cx: "11", cy: "11", r: "8" }),
      h("path", { d: "m21 21-4.3-4.3" }),
    ]),
    searchInput,
  ]);

  const cardsContainer = h("div", {
    style: "display:grid;grid-template-columns:repeat(auto-fill, minmax(340px, 1fr));gap:1rem;"
  });

  function renderCards(query = "") {
    cardsContainer.innerHTML = "";
    const q = query.toLowerCase().trim();
    const filtered = clusters.filter(
      (c) => c.name.toLowerCase().includes(q) || (c.environment || "").toLowerCase().includes(q) || (c.provider || "").toLowerCase().includes(q)
    );

    if (!filtered.length) {
      cardsContainer.appendChild(emptyState("No clusters found", "Try adjusting your search query."));
      return;
    }

    filtered.forEach((c) => {
      const isActive = c.id === state.selectedClusterId;
      const providerColor = PROVIDER_COLORS[c.provider] || "#64748b";
      const envColor = ENV_PILL_COLORS[c.environment] || ENV_PILL_COLORS.default;
      const icon = PROVIDER_ICONS[c.provider] || PROVIDER_ICONS.local;

      const card = h("div", {
        style: `
          display:flex;flex-direction:column;justify-content:space-between;
          padding:1.25rem;border-radius:16px;
          background:${isActive ? "var(--accent-soft)" : "var(--bg-elevated)"};
          border:1.5px solid ${isActive ? "var(--accent)" : "var(--border)"};
          box-shadow:0 4px 12px rgba(0,0,0,0.03);
          transition:all 180ms ease;
        `
      }, [
        h("div", { style: "display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:1rem;" }, [
          h("div", { style: "display:flex;align-items:center;gap:12px;" }, [
            h("div", {
              style: `width:42px;height:42px;border-radius:12px;background:${providerColor}18;color:${providerColor};display:flex;align-items:center;justify-content:center;flex-shrink:0;`,
              html: icon
            }),
            h("div", {}, [
              h("h3", { style: "font-size:0.98rem;font-weight:700;margin:0;color:var(--fg);" }, c.name),
              h("span", { style: "font-size:0.75rem;color:var(--fg-subtle);margin-top:2px;display:block;" }, `Provider: ${c.provider.toUpperCase()} \u2022 ID: ${c.id}`),
            ]),
          ]),
          h("span", {
            style: `font-size:0.68rem;font-weight:700;padding:3px 9px;border-radius:99px;background:${envColor}20;color:${envColor};text-transform:uppercase;letter-spacing:0.04em;`
          }, c.environment),
        ]),

        h("div", { style: "font-size:0.78rem;color:var(--fg-muted);margin-bottom:1.2rem;line-height:1.45;" }, [
          c.description || `Configured ${c.provider.toUpperCase()} cluster in ${c.environment}`
        ]),

        h("div", { style: "display:flex;align-items:center;justify-content:space-between;border-top:1px solid var(--border);padding-top:0.85rem;margin-top:auto;" }, [
          isActive
            ? h("span", { style: "font-size:0.82rem;font-weight:700;color:var(--accent);display:flex;align-items:center;gap:6px;" }, [
                h("span", { style: "width:8px;height:8px;border-radius:50%;background:var(--accent);" }),
                "Active Cluster"
              ])
            : h("button", {
                type: "button",
                className: "btn btn--secondary",
                style: "font-size:0.8rem;padding:6px 14px;border-radius:8px;",
                onClick: async () => {
                  await connectToCluster(c.id);
                  loadClustersView();
                }
              }, "Connect"),

          // Edit + Delete action container (Admin only)
          (() => {
            const deleteContainer = h("div", { style: "display:flex;align-items:center;gap:6px;" });
            if (!canAdmin()) {
              return deleteContainer;
            }

            const editBtn = h("button", {
              type: "button",
              title: `Edit cluster '${c.name}'`,
              style: "padding:5px 10px;height:34px;border-radius:8px;border:1.5px solid var(--border);background:transparent;color:var(--fg-muted);cursor:pointer;display:inline-flex;align-items:center;gap:5px;font-size:0.76rem;font-weight:600;font-family:inherit;transition:all 150ms;",
              onmouseenter: (e) => { e.currentTarget.style.background = "var(--bg-hover)"; e.currentTarget.style.color = "var(--accent)"; e.currentTarget.style.borderColor = "var(--accent)"; },
              onmouseleave: (e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--fg-muted)"; e.currentTarget.style.borderColor = "var(--border)"; },
              onClick: (e) => {
                e.stopPropagation();
                openClusterModal(c);
              }
            }, [
              h("svg", { width: "14", height: "14", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" }, [
                h("path", { d: "M12 20h9" }),
                h("path", { d: "M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" })
              ]),
              "Edit"
            ]);

            const trashBtn = h("button", {
              type: "button",
              title: `Remove cluster '${c.name}'`,
              style: "width:34px;height:34px;border-radius:8px;border:1.5px solid transparent;background:transparent;color:var(--fg-subtle);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all 150ms;",
              onmouseenter: (e) => { e.currentTarget.style.background = "rgba(239,68,68,0.12)"; e.currentTarget.style.color = "var(--danger)"; },
              onmouseleave: (e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--fg-subtle)"; },
              onClick: (e) => {
                e.stopPropagation();
                deleteContainer.innerHTML = "";

                const confirmBtn = h("button", {
                  type: "button",
                  style: "padding:5px 10px;border-radius:8px;border:none;background:var(--danger);color:#fff;font-size:0.75rem;font-weight:700;cursor:pointer;font-family:inherit;",
                  onClick: async (ev) => {
                    ev.stopPropagation();
                    try {
                      const res = await api.deleteCluster(c.id);
                      if (res.teams) state.teams = res.teams;
                      populateTeamSelect();
                      if (state.selectedClusterId === c.id) {
                        const nextId = restoreSelectionOrDefault();
                        if (nextId) await onClusterChange();
                      }
                      await loadClusterPanel();
                      await loadClustersView();
                    } catch (err) {
                      alert(`Failed to remove cluster: ${err.message}`);
                    }
                  }
                }, "Confirm Delete");

                const cancelBtn = h("button", {
                  type: "button",
                  style: "padding:5px 8px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--fg-muted);font-size:0.75rem;font-weight:600;cursor:pointer;font-family:inherit;",
                  onClick: (ev) => {
                    ev.stopPropagation();
                    deleteContainer.innerHTML = "";
                    deleteContainer.appendChild(editBtn);
                    deleteContainer.appendChild(trashBtn);
                  }
                }, "Cancel");

                deleteContainer.appendChild(confirmBtn);
                deleteContainer.appendChild(cancelBtn);
              }
            }, [
              h("svg", { width: "16", height: "16", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" }, [
                h("path", { d: "M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M10 11v6M14 11v6" })
              ])
            ]);

            deleteContainer.appendChild(editBtn);
            deleteContainer.appendChild(trashBtn);
            return deleteContainer;
          })()
        ])
      ]);

      cardsContainer.appendChild(card);
    });
  }

  searchInput.addEventListener("input", (e) => renderCards(e.target.value));
  renderCards();

  renderInto(container, h("div", {}, [statsRow, searchContainer, cardsContainer]));
}

// -- view: logical applications -----------------------------------------------

async function loadLogicalApplications(force = false) {
  const container = $("applications-content");
  const clusterId = state.selectedClusterId;
  const ns = $("applications-namespace-select")?.value || undefined;
  await loadWithSWR({
    key: `apps:${clusterId}:${ns || "_all_"}`,
    tokenView: "applications",
    container,
    force,
    fetchFn: () => api.getLogicalApplications(clusterId, ns),
    renderFn: (data) => renderLogicalApplications(data),
  });
}

function renderLogicalApplications(data) {
  const container = $("applications-content");
  const apps = data.items || data.applications || [];

  if (!apps.length) {
    renderInto(container, emptyState("No applications found", "No logical applications detected in this namespace. Workloads with `app.kubernetes.io/name` or `app` labels will appear here."));
    return;
  }

  const healthyCount = apps.filter((a) => a.healthy !== false).length;
  const degradedCount = apps.length - healthyCount;
  const totalServices = apps.reduce((s, a) => s + ((a.services || []).length), 0);
  const totalAppPods = apps.reduce((s, a) => s + ((a.pods || []).length), 0);

  const stats = statGrid([
    statCard("Logical Applications", apps.length, null, STAT_ICONS.grid),
    statCard("Healthy Stacks", healthyCount, degradedCount ? [pill(`${degradedCount} degraded`, "critical")] : [pill("All healthy", "healthy")], STAT_ICONS.checkCircle),
    statCard("Bound Services", totalServices, null, STAT_ICONS.layers),
    statCard("Application Pods", totalAppPods, null, kindIcon("pod")),
  ]);

  const MAX_CHIPS = 8;

  const appCards = apps.map((app) => {
    const isHealthy = app.healthy !== false;
    const statusColor = isHealthy ? "var(--success)" : "var(--danger)";
    const totalPods = (app.pods || []).length;
    const readyPods = (app.pods || []).filter(p => p.status === "Running" || p.status === "Succeeded").length;
    const firstPod = (app.pods || [])[0];

    const chips = [
      ...(app.deployments || []).map(d => ({
        label: d.name || d,
        prefix: "deploy",
        color: "var(--accent)",
        onClick: () => openManifestModal("Deployment", d.name || d, app.namespace),
      })),
      ...(app.statefulsets || []).map(s => ({
        label: s.name || s,
        prefix: "sts",
        color: "#8b5cf6",
        onClick: () => openManifestModal("StatefulSet", s.name || s, app.namespace),
      })),
      ...(app.daemonsets || []).map(ds => ({
        label: ds.name || ds,
        prefix: "ds",
        color: "var(--warning)",
        onClick: () => openManifestModal("DaemonSet", ds.name || ds, app.namespace),
      })),
      ...(app.services || []).map(svc => {
        const ports = (svc.ports || []).map(p => `${p.port}/${p.protocol || "TCP"}`).join(",");
        return {
          label: `${svc.name || svc}${ports ? ` (${ports})` : ""}`,
          prefix: "svc",
          color: "var(--success)",
          onClick: () => openManifestModal("Service", svc.name || svc, app.namespace),
        };
      }),
    ];
    const visibleChips = chips.slice(0, MAX_CHIPS);
    const hiddenCount = chips.length - visibleChips.length;

    return h("div", { className: "app-card app-card--sm", style: "justify-content:space-between;" }, [
      h("div", { style: "display:flex;flex-direction:column;gap:0.6rem;" }, [
        h("div", { style: "display:flex;align-items:center;gap:8px;" }, [
          h("div", { style: `width:30px;height:30px;border-radius:8px;background:${statusColor}18;color:${statusColor};display:flex;align-items:center;justify-content:center;flex-shrink:0;` }, [
            h("svg", { width: "15", height: "15", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2.2" }, [
              h("path", { d: "M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" })
            ])
          ]),
          h("div", { style: "min-width:0;flex:1;" }, [
            h("div", { style: "font-weight:700;font-size:0.92rem;color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }, app.name),
            h("div", { style: "font-size:0.72rem;color:var(--fg-subtle);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }, `ns: ${app.namespace}`)
          ]),
          statusPill(isHealthy ? "Healthy" : "Degraded", isHealthy),
        ]),

        h("div", { style: "display:flex;align-items:center;justify-content:space-between;font-size:0.75rem;color:var(--fg-muted);font-weight:600;" }, [
          h("span", {}, `${readyPods}/${totalPods} pods ready`),
          h("span", { style: "color:var(--fg-subtle);" }, `${(app.services || []).length} svc`),
        ]),

        h("div", { style: "display:flex;flex-wrap:wrap;gap:4px;" }, [
          ...visibleChips.map((c) => h("button", {
            type: "button",
            className: "app-child-pill app-child-pill--sm",
            style: "cursor:pointer;",
            title: "Click to view YAML manifest",
            onclick: c.onClick,
          }, [
            h("span", { style: `color:${c.color};font-weight:700;` }, `${c.prefix}:`),
            h("span", {}, c.label)
          ])),
          hiddenCount > 0 ? h("span", { className: "app-child-pill app-child-pill--sm", style: "color:var(--fg-subtle);" }, `+${hiddenCount} more`) : null,
        ].filter(Boolean)),
      ]),

      // Quick navigation actions row
      h("div", { style: "display:flex;align-items:center;justify-content:space-between;gap:6px;padding-top:8px;border-top:1px solid var(--border);margin-top:4px;" }, [
        h("button", {
          type: "button",
          className: "btn btn--secondary btn--sm",
          style: "padding:0.2rem 0.55rem;font-size:0.73rem;font-weight:600;",
          onclick: () => {
            const nsSelect = $("namespace-select");
            if (nsSelect && app.namespace) nsSelect.value = app.namespace;
            switchView("namespaces");
          },
        }, "Namespace \u2192"),
        h("div", { style: "display:flex;gap:5px;" }, [
          firstPod ? h("button", {
            type: "button",
            className: "btn btn--secondary btn--sm",
            style: "padding:0.2rem 0.55rem;font-size:0.73rem;font-weight:600;",
            onclick: () => openLogsModal(firstPod.name, app.namespace),
          }, "Logs") : null,
          h("button", {
            type: "button",
            className: "btn btn--secondary btn--sm",
            style: "padding:0.2rem 0.55rem;font-size:0.73rem;font-weight:600;",
            onclick: () => {
              const rmNs = $("resourcemap-namespace-select");
              if (rmNs && app.namespace) rmNs.value = app.namespace;
              switchView("resourcemap");
            },
          }, "Topology"),
        ].filter(Boolean)),
      ]),
    ]);
  });

  renderInto(container, h("div", {}, [
    stats,
    h("div", { className: "app-card-grid", style: "grid-template-columns:repeat(auto-fill, minmax(300px, 1fr));gap:14px;" }, appCards),
  ]));
}

// -- view: autonomous diagnostics & RCA --------------------------------------

let selectedPipelineStageId = null;
let selectedStepNumber = null;
let selectedK8sComponentId = null;
let selectedRcaKindFilter = "all"; // "all" | "active_failure" | "best_practice"
let rcaSearchQuery = "";
let _kmAnimTimer = null;

async function loadDiagnosticsRCA(force = false) {
  const container = $("diagnostics-content");
  const clusterId = state.selectedClusterId;
  const namespace = $("diagnostics-namespace-select")?.value || undefined;
  await loadWithSWR({
    key: `diagnostics:${clusterId}:${namespace || "_all_"}`,
    tokenView: "diagnostics",
    container,
    force,
    fetchFn: () => api.getDiagnosticsRCA(clusterId, namespace),
    renderFn: (data) => renderDiagnosticsRCA(data),
  });
}

function buildKubectlMachineInfographic(data) {
  if (_kmAnimTimer) {
    clearInterval(_kmAnimTimer);
    _kmAnimTimer = null;
  }

  const summary = data.summary || { overall_score: 100, active_root_causes: 0, workloads_healthy: 0, workloads_total: 0 };
  const allRootCauses = data.root_causes || [];
  const activeCauses = allRootCauses.filter((r) => r.kind === "active_failure");
  const stepCounts = data.step_counts || {};

  const countForStep = (stepNum) =>
    Number(stepCounts[stepNum] ?? stepCounts[String(stepNum)] ?? activeCauses.filter((r) => Number(r.step_number) === stepNum).length);

  const countByTag = (substr) =>
    activeCauses.filter((r) => (r.step_badge || "").toLowerCase().includes(substr.toLowerCase()));

  const step8Stalls = activeCauses.filter((r) => Number(r.step_number) === 8);
  const step9Stalls = activeCauses.filter((r) => Number(r.step_number) === 9);
  const step10aStalls = countByTag("10a");
  const step10bStalls = countByTag("10b");
  const step10cStalls = countByTag("10c");
  const step10dStalls = countByTag("10d");
  const step10AllStalls = activeCauses.filter((r) => Number(r.step_number) === 10);
  const step11Stalls = activeCauses.filter((r) => Number(r.step_number) === 11);
  const step12Stalls = activeCauses.filter((r) => Number(r.step_number) === 12);

  const clusterName = state.clusterBadge?.name || state.selectedClusterId || "active-cluster";
  const providerName = (state.clusterBadge?.provider || "k8s").toUpperCase();
  const activeNs = $("diagnostics-namespace-select")?.value || "";

  const integrations = data.integrations || {};
  const helmInfo = integrations.helm || { active: false, count: 0, releases: [], charts: [] };
  const argoInfo = integrations.argocd || { active: false, count: 0, apps: [] };
  const promInfo = integrations.prometheus || { active: false, count: 0, targets: [] };

  const activeDeliveryParts = [];
  if (helmInfo.active) {
    const label = helmInfo.releases?.length ? helmInfo.releases.slice(0, 2).join(", ") : `${helmInfo.count || 1} release(s)`;
    activeDeliveryParts.push(`Helm (${label})`);
  }
  if (argoInfo.active) {
    const label = argoInfo.apps?.length ? argoInfo.apps.slice(0, 2).join(", ") : `${argoInfo.count || 1} app(s)`;
    activeDeliveryParts.push(`Argo CD (${label})`);
  }
  if (!activeDeliveryParts.length) {
    activeDeliveryParts.push("kubectl");
  }
  const deliveryBadgeText = `Delivery: ${activeDeliveryParts.join(" + ")}`;

  // Generate 52 golden-angle points for the etcd cluster-state particle sphere at (200, 118)
  const etcdDots = [];
  for (let i = 0; i < 52; i++) {
    const r = 36 * Math.sqrt((i + 0.5) / 52);
    const theta = i * 2.39996323;
    const dx = (200 + r * Math.cos(theta)).toFixed(1);
    const dy = (118 + r * Math.sin(theta)).toFixed(1);
    const dotR = (i % 5 === 0 ? 1.8 : 1.25);
    const op = (0.45 + 0.5 * (1 - r / 40)).toFixed(2);
    etcdDots.push(`<circle cx="${dx}" cy="${dy}" r="${dotR}" class="km-etcd-dot" opacity="${op}"/>`);
  }

  // 12 canonical animated paths & step definitions
  const stepMeta = [
    { num: 1, compId: "kubectl-client", title: "kubectl apply", sub: "sends manifest to kube-apiserver", x: 540, y: 46, stalled: countForStep(1) },
    { num: 2, compId: "kube-apiserver", title: "{authn}", sub: "who are you?", x: 612, y: 242, stalled: countForStep(2) },
    { num: 3, compId: "kube-apiserver", title: "{rbac-authz}", sub: "are you allowed?", x: 672, y: 266, stalled: countForStep(3) },
    { num: 4, compId: "kube-apiserver", title: "{mutating-admission}", sub: "defaults & injected fields", x: 684, y: 320, stalled: countForStep(4) },
    { num: 5, compId: "kube-apiserver", title: "{schema-validation}", sub: "is it valid as written?", x: 632, y: 372, stalled: countForStep(5) },
    { num: 6, compId: "etcd", title: "etcd · cluster state", sub: "the Deployment is stored", x: 152, y: 92, stalled: countForStep(6) },
    { num: 7, compId: "kube-controller-manager", title: "{deployment-controller}", sub: "creates a ReplicaSet", x: 74, y: 230, stalled: countForStep(7) },
    { num: 8, compId: "kube-controller-manager", title: "{replicaset-controller}", sub: "creates Pods (no node yet)", x: 62, y: 278, stalled: countForStep(8) },
    { num: 9, compId: "kube-scheduler", title: "kube-scheduler", sub: "filter → score → bind (Pod gets node)", x: 878, y: 215, stalled: countForStep(9) },
    { num: 10, compId: "kubelet-runtime", title: "kubelet · worker node", sub: "CSI → CNI → image pull → containerd", x: 292, y: 506, stalled: countForStep(10) },
    { num: 11, compId: "kubelet-probes", title: "{readiness-probe}", sub: "Pod is Ready", x: 432, y: 546, stalled: countForStep(11) },
    { num: 12, compId: "datapath-routing", title: "{endpointslice-controller}", sub: "adds Pod IP → kube-proxy routes traffic", x: 66, y: 382, stalled: countForStep(12) },
  ];

  // Bezier curves matching the exact radial flow of "The kubectl apply Machine" GIF
  const stepPaths = [
    { num: 1, compId: "kubectl-client", d: "M 540 46 Q 558 175 540 310", stalled: countForStep(1) > 0 },
    { num: 2, compId: "kube-apiserver", d: "M 540 310 Q 576 268 612 242", stalled: countForStep(2) > 0 },
    { num: 3, compId: "kube-apiserver", d: "M 612 242 Q 646 250 672 266", stalled: countForStep(3) > 0 },
    { num: 4, compId: "kube-apiserver", d: "M 672 266 Q 684 292 684 320", stalled: countForStep(4) > 0 },
    { num: 5, compId: "kube-apiserver", d: "M 684 320 Q 664 352 632 372 L 540 310", stalled: countForStep(5) > 0 },
    { num: 6, compId: "etcd", d: "M 540 310 Q 370 230 200 118", stalled: countForStep(6) > 0 },
    { num: 7, compId: "kube-controller-manager", d: "M 540 310 Q 320 262 74 230", stalled: countForStep(7) > 0 },
    { num: 8, compId: "kube-controller-manager", d: "M 540 310 Q 310 296 62 278", stalled: countForStep(8) > 0 },
    { num: 9, compId: "kube-scheduler", d: "M 540 310 Q 705 236 878 215", stalled: countForStep(9) > 0 },
    { num: 10, compId: "kubelet-runtime", d: "M 540 310 Q 430 432 292 506", stalled: countForStep(10) > 0 },
    { num: 11, compId: "kubelet-probes", d: "M 292 506 Q 362 536 432 546 Q 468 424 540 310", stalled: countForStep(11) > 0 },
    { num: 12, compId: "datapath-routing", d: "M 540 310 Q 310 348 66 382 M 540 310 Q 685 430 788 552", stalled: countForStep(12) > 0 },
  ];

  const isStepHighlighted = (num, compId) =>
    selectedStepNumber === num || (selectedK8sComponentId && selectedK8sComponentId === compId);

  const narratorEl = h("div", { className: "km-narrator" }, [
    h("div", { id: "km-narrator-text", style: "display:flex;align-items:center;gap:10px;flex-wrap:wrap;" }, [
      h("span", { style: "font-weight:800;color:var(--km-accent-red);font-family:var(--font-mono);" },
        selectedStepNumber ? `FILTERED: STEP ${selectedStepNumber}` : (selectedK8sComponentId ? `COMPONENT: ${selectedK8sComponentId.toUpperCase()}` : "LIVE 12-STEP CLUSTER TELEMETRY")),
      h("span", {},
        activeCauses.length
          ? `${activeCauses.length} active pipeline stall(s) detected on ${clusterName}${activeNs ? ` / ${activeNs}` : ""} (${providerName}) — click any numbered step (1–12) or K8s component chip to filter.`
          : `All 12 steps from kubectl apply → EndpointSlice traffic are healthy on ${clusterName}${activeNs ? ` / ${activeNs}` : ""}.`)
    ]),
    h("span", { style: "font-size:0.74rem;font-family:var(--font-mono);color:var(--km-muted-fg);white-space:nowrap;" },
      `${summary.workloads_healthy}/${summary.workloads_total} Workloads Ready`)
  ]);

  const svgContainer = h("div", { className: "km-canvas-wrap" });

  const edgesHtml = stepPaths.map((p) => {
    const pNum = Number(p.num);
    const cls = p.stalled ? "is-stalled-edge" : "is-healthy-edge";
    const selCls = isStepHighlighted(pNum, p.compId) ? " is-active-step" : "";
    return `
      <path id="km-glow-${pNum}" class="km-wire-glow ${cls}${selCls}" d="${escapeHtml(p.d)}" />
      <path id="km-flow-${pNum}" class="km-wire-flow ${cls}${selCls}" d="${escapeHtml(p.d)}" />
    `;
  }).join("");

  const stepBadgesHtml = stepMeta.map((s) => {
    const sNum = Number(s.num);
    const sX = Number(s.x);
    const sY = Number(s.y);
    const sStalled = Number(s.stalled || 0);
    const hasStall = sStalled > 0;
    const isSel = isStepHighlighted(sNum, s.compId);
    const badgeColor = isSel ? "#f59e0b" : (hasStall ? "#e11d48" : "#10b981");
    const pulseHtml = (hasStall || isSel)
      ? `<circle cx="${sX}" cy="${sY}" r="12" class="km-pulse-circle" />`
      : "";
    return `
      <g class="km-step-node" data-step-num="${sNum}" data-comp-id="${escapeHtml(s.compId)}">
        <title>Step ${sNum}: ${escapeHtml(s.title)} (${sStalled} stalled) — Click to filter</title>
        ${pulseHtml}
        <circle cx="${sX}" cy="${sY}" r="11.5" fill="${escapeHtml(badgeColor)}" stroke="#ffffff" stroke-width="2" filter="url(#km-neon-glow)" />
        <text x="${sX}" y="${sY + 3.8}" text-anchor="middle" fill="#ffffff" font-size="10.5" font-weight="800" font-family="monospace">${sNum}</text>
      </g>
    `;
  }).join("");

  const hasAny10Stall = step10AllStalls.length > 0;

  const activeIntegrationWires = [
    argoInfo.active ? `
      <path class="km-wire-glow is-argocd-active" d="M 405 128 Q 470 215 540 310" />
      <path class="km-wire-flow is-argocd-active" d="M 405 128 Q 470 215 540 310" />` : "",
    helmInfo.active ? `
      <path class="km-wire-glow is-helm-active" d="M 772 152 Q 655 228 540 310" />
      <path class="km-wire-flow is-helm-active" d="M 772 152 Q 655 228 540 310" />` : "",
    promInfo.active ? `
      <path class="km-wire-glow is-prom-active" d="M 940 112 Q 740 205 540 310" />
      <path class="km-wire-flow is-prom-active" d="M 940 112 Q 740 205 540 310" />` : "",
  ].join("");

  const helmTextLabel = helmInfo.releases?.length
    ? `◈ Helm · ${helmInfo.releases[0]}${helmInfo.releases.length > 1 ? ` +${helmInfo.releases.length - 1}` : ""}`
    : `◈ Helm (${helmInfo.count || 1})`;
  const helmBadgeWidth = Math.max(108, helmTextLabel.length * 6.9 + 20);
  const argoTextLabel = argoInfo.apps?.length
    ? `◈ Argo CD · ${argoInfo.apps[0]}${argoInfo.apps.length > 1 ? ` +${argoInfo.apps.length - 1}` : ""}`
    : `◈ Argo CD (${argoInfo.count || 1})`;
  const argoBadgeWidth = Math.max(114, argoTextLabel.length * 6.9 + 20);
  const promTextLabel = `◈ Prometheus (${promInfo.count || 1})`;
  const promBadgeWidth = Math.max(122, promTextLabel.length * 6.9 + 20);

  const activeIntegrationNodes = [
    argoInfo.active ? `
      <g class="km-integration-node km-integration-node--argocd" data-nav-view="ecosystem">
        <title>Detected Argo CD apps: ${escapeHtml((argoInfo.apps || []).join(", ") || "active")} — Click to inspect Ecosystem</title>
        <rect x="${395 - Math.round(argoBadgeWidth / 2)}" y="114" width="${argoBadgeWidth}" height="24" rx="12" class="km-integration-pill km-integration-pill--argocd" filter="url(#km-neon-glow)" />
        <text x="395" y="130" text-anchor="middle" class="km-integration-text km-integration-text--argocd">${escapeHtml(argoTextLabel)}</text>
      </g>` : "",
    helmInfo.active ? `
      <g class="km-integration-node km-integration-node--helm" data-nav-view="helm">
        <title>Detected Helm releases: ${escapeHtml((helmInfo.releases || []).join(", ") || "active")}${helmInfo.charts?.length ? ` (${escapeHtml(helmInfo.charts.join(", "))})` : ""} — Click to open Helm view</title>
        <rect x="708" y="138" width="${helmBadgeWidth}" height="24" rx="12" class="km-integration-pill km-integration-pill--helm" filter="url(#km-neon-glow)" />
        <text x="${708 + Math.round(helmBadgeWidth / 2)}" y="154" text-anchor="middle" class="km-integration-text km-integration-text--helm">${escapeHtml(helmTextLabel)}</text>
      </g>` : "",
    promInfo.active ? `
      <g class="km-integration-node km-integration-node--prom">
        <title>Prometheus metrics/scrape targets detected: ${escapeHtml((promInfo.targets || []).join(", ") || "active")}</title>
        <rect x="${940 - Math.round(promBadgeWidth / 2)}" y="96" width="${promBadgeWidth}" height="24" rx="12" class="km-integration-pill km-integration-pill--prom" filter="url(#km-neon-glow)" />
        <text x="940" y="112" text-anchor="middle" class="km-integration-text km-integration-text--prom">${escapeHtml(promTextLabel)}</text>
      </g>` : "",
  ].join("");

  svgContainer.innerHTML = `
    <svg class="km-svg" viewBox="0 0 1080 615" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <filter id="km-neon-glow" x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="3.2" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
        <radialGradient id="km-hub-grad" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.22" />
          <stop offset="65%" stop-color="#ff4d2a" stop-opacity="0.09" />
          <stop offset="100%" stop-color="#090d16" stop-opacity="0" />
        </radialGradient>
      </defs>

      <!-- Subtle background structural web lines (satellites to kube-apiserver) -->
      <g class="km-wire-bg">
        <path d="M 540 46 Q 470 55 425 78" />
        <path d="M 540 46 Q 610 55 660 78" />
        <path d="M 878 215 L 945 176 M 878 215 L 966 212 M 878 215 L 938 258" />
        <path d="M 335 295 L 185 332 M 335 295 L 195 418 M 335 295 L 155 450" />
        <path d="M 292 506 L 165 480 M 292 506 L 145 516 M 292 506 L 185 552 M 292 506 L 312 562 M 292 506 L 420 476 M 292 506 L 442 506" />
        <path d="M 540 310 Q 740 350 935 370" />
        <path d="M 540 310 Q 720 400 845 435" />
        <path d="M 540 310 Q 600 410 635 455" />
        <path d="M 540 310 Q 740 465 895 495" />
        <path d="M 540 310 Q 700 495 855 530" />
      </g>

      <!-- Center Hub Ambient Glow -->
      <circle cx="540" cy="310" r="105" fill="url(#km-hub-grad)" />

      <!-- Active 12-Step Animated Neon Bezier Paths -->
      <g id="km-active-paths">${edgesHtml}${activeIntegrationWires}</g>

      <!-- Step 6: etcd · cluster state particle sphere (Top-Left, clear above {deployment-controller}) -->
      <g>
        <circle cx="200" cy="118" r="40" fill="none" stroke="var(--km-wire-bg)" stroke-dasharray="3 3" />
        ${etcdDots.join("")}
        <text x="200" y="174" text-anchor="middle" class="km-svg-title" font-size="13">etcd · cluster state</text>
        <text x="200" y="189" text-anchor="middle" class="${countForStep(6) ? "km-svg-alert" : "km-svg-sub"}" font-size="10.5">
          the Deployment is stored (${Number(summary.workloads_total || 0)} workloads)
        </text>
      </g>

      <!-- Satellite context nodes & labels (theme-aware via CSS classes) -->
      <g font-size="10.5">
        <text x="415" y="81" text-anchor="end" class="km-svg-sat">{~/.kube/config} ●</text>
        <text x="666" y="81" class="km-svg-sat">● {server-side-apply}</text>
        ${activeIntegrationNodes}

        <!-- kube-scheduler satellites (Step 9) -->
        <text x="952" y="179" class="km-svg-sat">● {filter}</text>
        <text x="972" y="215" class="km-svg-sat">● {score}</text>
        <text x="945" y="261" class="km-svg-sat">● {bind}</text>
        <text x="878" y="245" text-anchor="middle" class="km-svg-title" font-size="13">kube-scheduler</text>
        <text x="878" y="261" text-anchor="middle" class="${step9Stalls.length ? "km-svg-alert" : "km-svg-sub"}" font-size="10.5">
          filter → score → bind ${step9Stalls.length ? `(${Number(step9Stalls.length)} Pending!)` : "(Pod gets a node)"}
        </text>

        <!-- kube-controller-manager & satellites (Mid-Left) -->
        <circle cx="335" cy="295" r="9" class="km-svg-hub-inner" />
        <text x="335" y="317" text-anchor="middle" class="km-svg-label" font-size="12">kube-controller-manager</text>
        <text x="88" y="227" class="${countForStep(7) ? "km-svg-alert" : "km-svg-label"}">{deployment-controller}</text>
        <text x="88" y="241" class="km-svg-sub">creates a ReplicaSet</text>
        <text x="76" y="275" class="${step8Stalls.length ? "km-svg-alert" : "km-svg-label"}">{replicaset-controller}</text>
        <text x="76" y="289" class="km-svg-sub">creates Pods (no node yet)</text>
        <text x="178" y="335" text-anchor="end" class="km-svg-sat">● {namespace-controller}</text>
        <text x="458" y="352" text-anchor="end" class="km-svg-sat">{pod-security} ●</text>
        <text x="458" y="365" text-anchor="end" class="km-svg-note" font-size="9.5">enforced at step 8</text>
        <text x="80" y="380" class="${step12Stalls.length ? "km-svg-alert" : "km-svg-label"}">{endpointslice-controller}</text>
        <text x="80" y="394" class="${step12Stalls.length ? "km-svg-alert" : "km-svg-sub"}">adds Pod IP → kube-proxy routes traffic</text>
        <text x="80" y="407" class="km-svg-note" font-size="9.5">(only if a Service selects the Pod)</text>
        <text x="188" y="426" text-anchor="end" class="km-svg-sat">● {node-controller}</text>
        <text x="148" y="453" text-anchor="end" class="km-svg-sat">● {job-controller}</text>

        <!-- Step 1-5 labels around kube-apiserver -->
        <text x="540" y="26" text-anchor="middle" class="km-svg-title" font-size="13.5">kubectl</text>
        <text x="552" y="132" class="km-svg-sub">sends the manifest to kube-apiserver</text>
        <text x="626" y="238" class="${countForStep(2) ? "km-svg-alert" : "km-svg-label"}">{authn}</text>
        <text x="626" y="251" class="km-svg-sub">who are you?</text>
        <text x="688" y="264" class="${countForStep(3) ? "km-svg-alert" : "km-svg-label"}">{rbac-authz}</text>
        <text x="688" y="277" class="km-svg-sub">are you allowed?</text>
        <text x="700" y="317" class="${countForStep(4) ? "km-svg-alert" : "km-svg-label"}">{mutating-admission}</text>
        <text x="700" y="330" class="km-svg-sub">defaults and injected fields</text>
        <text x="646" y="369" class="${countForStep(5) ? "km-svg-alert" : "km-svg-label"}">{schema-validation}</text>
        <text x="646" y="382" class="km-svg-sub">is it allowed as written?</text>
        <text x="530" y="398" text-anchor="middle" class="km-svg-sat">{validating-admission}</text>

        <!-- Bottom-Left: kubelet · worker node (Step 10 & Step 11) -->
        <text x="160" y="478" text-anchor="end" class="${(step10aStalls.length || hasAny10Stall) ? "km-svg-alert" : "km-svg-sat"}">● {csi-mount}${step10aStalls.length ? " [!]" : ""}</text>
        <text x="140" y="518" text-anchor="end" class="${step10bStalls.length ? "km-svg-alert" : "km-svg-sat"}">● {cni-sandbox}</text>
        <text x="180" y="555" text-anchor="end" class="${(step10cStalls.length || hasAny10Stall) ? "km-svg-alert" : "km-svg-sat"}">● {image-pull}${step10cStalls.length ? " [!]" : ""}</text>
        <text x="312" y="576" text-anchor="middle" class="${(step10dStalls.length || hasAny10Stall) ? "km-svg-alert" : "km-svg-sat"}">● {containerd}${step10dStalls.length ? " [!]" : ""}</text>
        <text x="425" y="476" class="km-svg-sat">● {init-containers}</text>
        <text x="448" y="508" class="km-svg-sat">● {startup-probe}</text>
        <text x="448" y="545" class="${step11Stalls.length ? "km-svg-alert" : "km-svg-label"}">{readiness-probe}</text>
        <text x="448" y="558" class="${step11Stalls.length ? "km-svg-alert" : "km-svg-sub"}">${step11Stalls.length ? "0/1 Ready (Probe Failing!)" : "Pod is Ready"}</text>

        <text x="292" y="534" text-anchor="middle" class="km-svg-title" font-size="13">kubelet · worker node</text>
        <text x="292" y="598" text-anchor="middle" class="km-svg-sub" font-size="10.5">mount volumes (CSI) → sandbox + network (CNI) → pull image → start containers</text>

        <!-- Bottom-Right: Service, EndpointSlice, kube-proxy, HPA, Karpenter, CoreDNS -->
        <text x="942" y="373" class="km-svg-sat">HPA ◈</text>
        <text x="748" y="435" class="km-svg-sat">Karpenter / cluster-autoscaler ◈</text>
        <text x="612" y="458" class="km-svg-sat">◈ cert-manager</text>
        <text x="745" y="495" class="${step12Stalls.length ? "km-svg-alert" : "km-svg-sat"}">● EndpointSlice</text>
        <text x="898" y="495" class="km-svg-sat">CoreDNS ●</text>
        <text x="845" y="528" class="${step12Stalls.length ? "km-svg-alert" : "km-svg-sat"}">● Service</text>
        <circle cx="788" cy="552" r="6" fill="${step12Stalls.length ? "#e11d48" : "#10b981"}" filter="url(#km-neon-glow)" />
        <text x="802" y="556" class="km-svg-label">kube-proxy (or eBPF CNI)</text>
      </g>

      <!-- CENTER HUB: kube-apiserver -->
      <g>
        <circle cx="540" cy="310" r="32" class="km-svg-hub-outer" />
        <circle cx="540" cy="310" r="24" class="km-svg-hub-inner" filter="url(#km-neon-glow)" />
        <text x="540" y="356" text-anchor="middle" class="km-svg-title" font-size="14.5">kube-apiserver</text>
      </g>

      <!-- Numbered Step Badges (1..12) -->
      ${stepBadgesHtml}
    </svg>
  `;

  // Wire up interactive click on SVG step nodes (filters by exact step number 1..12)
  svgContainer.querySelectorAll(".km-step-node").forEach((nodeEl) => {
    nodeEl.addEventListener("click", () => {
      const sNum = Number(nodeEl.getAttribute("data-step-num"));
      selectedStepNumber = selectedStepNumber === sNum ? null : sNum;
      selectedPipelineStageId = null;
      renderDiagnosticsRCA(data);
    });
  });

  // Wire up clickable detected integration badges (e.g., Helm / Argo CD)
  svgContainer.querySelectorAll(".km-integration-node[data-nav-view]").forEach((nodeEl) => {
    nodeEl.addEventListener("click", () => {
      const navView = nodeEl.getAttribute("data-nav-view");
      if (navView) switchView(navView);
    });
  });

  // Animated 12-Step Sequencer ("▶ Replay 12-Step Flow")
  const playBtn = h("button", {
    type: "button",
    className: "km-btn km-btn--primary",
    onclick: () => {
      if (_kmAnimTimer) {
        clearInterval(_kmAnimTimer);
        _kmAnimTimer = null;
        playBtn.textContent = "▶ Replay 12-Step Flow";
        svgContainer.querySelectorAll(".km-wire-flow, .km-wire-glow").forEach((el) => el.classList.remove("is-active-step"));
        return;
      }
      let currentStep = 1;
      playBtn.textContent = "⏹ Stop Animation";
      const highlightStep = (stepNum) => {
        svgContainer.querySelectorAll(".km-wire-flow, .km-wire-glow").forEach((el) => el.classList.remove("is-active-step"));
        const flowEl = svgContainer.querySelector(`#km-flow-${stepNum}`);
        const glowEl = svgContainer.querySelector(`#km-glow-${stepNum}`);
        if (flowEl) flowEl.classList.add("is-active-step");
        if (glowEl) glowEl.classList.add("is-active-step");
        const meta = stepMeta.find((m) => m.num === stepNum);
        const narText = $("km-narrator-text");
        if (meta && narText) {
          const metaNum = Number(meta.num);
          const metaStalled = Number(meta.stalled || 0);
          narText.innerHTML = `
            <span style="font-weight:800;color:var(--km-accent-amber);font-family:var(--font-mono);">STEP ${metaNum} OF 12 · ${escapeHtml(meta.title.toUpperCase())}</span>
            <span>${escapeHtml(meta.sub)}</span>
            <span style="font-family:var(--font-mono);font-size:0.75rem;padding:2px 8px;border-radius:99px;background:${metaStalled ? "rgba(225,29,72,0.16)" : "rgba(16,185,129,0.16)"};color:${metaStalled ? "var(--km-accent-red)" : "var(--km-accent-green)"};">
              ${metaStalled ? `⚠ ${metaStalled} STALLED` : "✓ PASSING"}
            </span>
          `;
        }
      };
      highlightStep(currentStep);
      _kmAnimTimer = setInterval(() => {
        currentStep++;
        if (currentStep > 12) {
          clearInterval(_kmAnimTimer);
          _kmAnimTimer = null;
          playBtn.textContent = "▶ Replay 12-Step Flow";
          svgContainer.querySelectorAll(".km-wire-flow, .km-wire-glow").forEach((el) => el.classList.remove("is-active-step"));
          return;
        }
        highlightStep(currentStep);
      }, 1200);
    }
  }, "▶ Replay 12-Step Flow");

  const hasAnyFilter = Boolean(selectedPipelineStageId || selectedStepNumber || selectedK8sComponentId || selectedRcaKindFilter !== "all" || rcaSearchQuery);
  const resetFilterBtn = hasAnyFilter ? h("button", {
    type: "button",
    className: "km-btn",
    onclick: () => {
      selectedPipelineStageId = null;
      selectedStepNumber = null;
      selectedK8sComponentId = null;
      selectedRcaKindFilter = "all";
      rcaSearchQuery = "";
      renderDiagnosticsRCA(data);
    }
  }, "✕ Reset Filters") : null;

  // Bottom-Right "Where it stalls" items bound to cluster counts
  const stallCategories = [
    {
      icon: "◷",
      label: "Pending",
      stepText: "→ scheduler (step 9)",
      stepNum: 9,
      matches: step9Stalls,
    },
    {
      icon: "⬡",
      label: "ContainerCreating",
      stepText: "→ CSI / CNI (step 10)",
      stepNum: 10,
      matches: [...step10aStalls, ...step10bStalls, ...(step10aStalls.length === 0 ? step10AllStalls : [])],
    },
    {
      icon: "↓",
      label: "ImagePullBackOff",
      stepText: "→ image pull (step 10)",
      stepNum: 10,
      matches: step10cStalls.length ? step10cStalls : step10AllStalls,
    },
    {
      icon: "∿",
      label: "0/1 Ready",
      stepText: "→ readiness probe (step 11)",
      stepNum: 11,
      matches: step11Stalls,
    },
    {
      icon: "⊘",
      label: "503 from Ingress",
      stepText: "→ no ready endpoints (step 12)",
      stepNum: 12,
      matches: step12Stalls,
    },
    {
      icon: "🔒",
      label: "Forbidden",
      stepText: "→ RBAC (3) or PodSecurity (8)",
      stepNum: countForStep(3) > 0 ? 3 : 8,
      matches: [...activeCauses.filter((r) => Number(r.step_number) === 3), ...step8Stalls],
    },
    {
      icon: "⊗",
      label: "Admission Webhook Denied",
      stepText: "→ mutating/validating (step 4–5)",
      stepNum: countForStep(5) > 0 ? 5 : 4,
      matches: activeCauses.filter((r) => Number(r.step_number) === 4 || Number(r.step_number) === 5),
    },
    {
      icon: "⚡",
      label: "CrashLoop / OOMKilled",
      stepText: "→ containerd runtime (step 10)",
      stepNum: 10,
      matches: step10dStalls.length ? step10dStalls : step10AllStalls,
    },
  ];

  const bottomGrid = h("div", { className: "km-bottom-grid" }, [
    // Left Box: The machine in numbers
    h("div", { className: "km-box" }, [
      h("div", { className: "km-box__title" }, [
        h("span", {}, "The machine in numbers"),
        h("span", { style: "font-size:0.72rem;font-family:var(--font-mono);color:var(--km-accent-cyan);" }, clusterName)
      ]),
      h("div", { className: "km-num-hero" }, [
        h("span", { className: "km-num-hero__big" }, "12"),
        h("span", { className: "km-num-hero__label" }, "steps from kubectl to traffic")
      ]),
      h("div", { className: "km-num-row" }, [h("span", {}, "Components that talk to etcd"), h("strong", {}, "1 (kube-apiserver)")]),
      h("div", { className: "km-num-row" }, [h("span", {}, "Controllers on the path"), h("strong", {}, "3 (Deploy → RS → EndpointSlice)")]),
      h("div", { className: "km-num-row" }, [h("span", {}, "Checks before storage"), h("strong", {}, "4 (Authn, RBAC, Mutating, Validating)")]),
      h("div", { className: "km-num-row" }, [h("span", {}, "Things that watch the API server"), h("strong", {}, "all of them")]),
      h("div", { className: "km-num-row" }, [
        h("span", {}, "Active Pipeline Stalls"),
        h("strong", { style: `color:${activeCauses.length ? "var(--km-accent-red)" : "var(--km-accent-green)"};` }, `${activeCauses.length} active`)
      ]),
    ]),

    // Right Box: Where it stalls
    h("div", { className: "km-box" }, [
      h("div", { className: "km-box__title" }, [
        h("span", {}, "Where it stalls"),
        h("span", { style: "font-size:0.7rem;font-family:var(--font-mono);color:var(--km-muted-fg);" }, "Click any stall point to filter RCA cards")
      ]),
      h("div", { className: "km-stalls-grid" }, stallCategories.map((sc) => {
        const count = sc.matches.length;
        const isSel = selectedStepNumber === sc.stepNum;
        const sampleNames = sc.matches.flatMap((m) => m.folded_failures || []).slice(0, 2).join(", ");
        return h("button", {
          type: "button",
          className: `km-stall-item ${count > 0 ? "has-stall" : ""} ${isSel ? "is-active" : ""}`,
          onclick: () => {
            selectedStepNumber = isSel ? null : sc.stepNum;
            selectedPipelineStageId = null;
            renderDiagnosticsRCA(data);
          }
        }, [
          h("div", { style: "display:flex;align-items:center;justify-content:space-between;width:100%;gap:6px;" }, [
            h("span", { style: "font-size:0.79rem;font-weight:800;color:var(--km-title-fg);display:flex;align-items:center;gap:6px;" }, [
              h("span", { style: `color:${count > 0 ? "var(--km-accent-red)" : "var(--km-accent-green)"};` }, sc.icon),
              sc.label
            ]),
            h("span", {
              style: `font-size:0.67rem;font-weight:800;padding:1px 7px;border-radius:99px;background:${count > 0 ? "rgba(225,29,72,0.16)" : "rgba(16,185,129,0.14)"};color:${count > 0 ? "var(--km-accent-red)" : "var(--km-accent-green)"};`
            }, count > 0 ? `${count} stalled` : "✓ OK")
          ]),
          h("div", { style: "font-size:0.72rem;color:var(--km-muted-fg);" }, sc.stepText),
          count > 0 && sampleNames
            ? h("div", { style: "font-size:0.67rem;color:var(--km-stall-sample-fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%;" }, `↳ ${sampleNames}`)
            : null,
        ].filter(Boolean));
      }))
    ])
  ]);

  return h("div", { className: "km-shell" }, [
    h("div", { className: "km-shell__header" }, [
      h("div", {}, [
        h("div", { className: "km-shell__tag" }, [
          h("span", {}, "KUBEORBIT LIVE PIPELINE"),
          h("span", { style: "opacity:0.6;" }, "·"),
          h("span", {}, `LIVE ${providerName} CONTROL & DATA PLANE`)
        ]),
        h("h2", { className: "km-shell__title" }, [
          "The ",
          h("code", { style: "font-family:var(--font-mono);font-size:1.5rem;" }, "kubectl apply"),
          " Machine",
          h("span", {}, ".")
        ]),
        h("div", { className: "km-shell__subtitle" },
          `Cluster: ${clusterName}${activeNs ? ` · Namespace: ${activeNs}` : " · All Namespaces"} · ${deliveryBadgeText} · Click any numbered node (1–12), stall card, or K8s component chip below to filter`)
      ]),
      h("div", { className: "km-controls" }, [
        resetFilterBtn,
        playBtn,
      ].filter(Boolean))
    ]),
    narratorEl,
    svgContainer,
    bottomGrid,
    h("div", { className: "km-footer-caption" }, [
      h("span", {}, [
        "KUBEORBIT 12-STEP RUNBOOK ENGINE · VISUAL CONCEPT INSPIRED BY ",
        h("a", {
          href: "https://infra360.io",
          target: "_blank",
          rel: "noopener noreferrer",
          style: "color:inherit;text-decoration:underline;text-underline-offset:2px;",
        }, "DEEPAK AGRAWAL (INFRA360.IO)"),
      ]),
      h("span", {}, "NOTHING TALKS TO ANYTHING. EVERYTHING TALKS TO THE API SERVER.")
    ])
  ]);
}

function buildRCAFilterPanel(data) {
  const allRootCauses = data.root_causes || [];
  const components = data.components || [
    { component_id: "kubectl-client", label: "kubectl · Client", step_range: "Step 1" },
    { component_id: "kube-apiserver", label: "kube-apiserver", step_range: "Steps 2–5" },
    { component_id: "etcd", label: "etcd · State", step_range: "Step 6" },
    { component_id: "kube-controller-manager", label: "kube-controller-manager", step_range: "Steps 7–8" },
    { component_id: "kube-scheduler", label: "kube-scheduler", step_range: "Step 9" },
    { component_id: "kubelet-runtime", label: "kubelet · CSI / CNI / Runtime", step_range: "Step 10" },
    { component_id: "kubelet-probes", label: "kubelet · Probes", step_range: "Step 11" },
    { component_id: "datapath-routing", label: "EndpointSlice & kube-proxy", step_range: "Step 12" },
  ];

  // Row 1: Filter by Kubernetes Component
  const allCompBtn = h("button", {
    type: "button",
    className: `rca-comp-chip ${!selectedK8sComponentId ? "is-active" : ""}`,
    onclick: () => {
      selectedK8sComponentId = null;
      renderDiagnosticsRCA(data);
    }
  }, [
    h("span", {}, "All K8s Components"),
    h("span", {
      className: "rca-comp-chip__badge",
      style: "background:var(--bg-elevated);color:var(--fg);"
    }, String(allRootCauses.length))
  ]);

  const compButtons = components.map((c) => {
    const cid = c.component_id;
    const matching = allRootCauses.filter((r) => r.component_id === cid);
    const activeFailCount = matching.filter((r) => r.kind === "active_failure").length;
    const isSel = selectedK8sComponentId === cid;

    return h("button", {
      type: "button",
      className: `rca-comp-chip ${isSel ? "is-active" : ""}`,
      title: `${c.label} (${c.step_range})`,
      onclick: () => {
        selectedK8sComponentId = isSel ? null : cid;
        selectedStepNumber = null;
        renderDiagnosticsRCA(data);
      }
    }, [
      h("span", {}, c.label),
      h("span", { style: "font-size:0.66rem;opacity:0.75;" }, `(${c.step_range})`),
      h("span", {
        className: "rca-comp-chip__badge",
        style: activeFailCount > 0
          ? "background:rgba(217,48,37,0.16);color:var(--danger);"
          : (matching.length > 0 ? "background:rgba(245,158,11,0.16);color:var(--warning);" : "background:rgba(16,185,129,0.14);color:var(--success);")
      }, activeFailCount > 0 ? `${activeFailCount} stalled` : (matching.length > 0 ? `${matching.length}` : "✓"))
    ]);
  });

  // Row 2: Filter by Individual Step (1..12) + Kind Filter + Search Input
  const stepPills = [
    h("button", {
      type: "button",
      className: `rca-step-pill ${!selectedStepNumber ? "is-active" : ""}`,
      onclick: () => {
        selectedStepNumber = null;
        renderDiagnosticsRCA(data);
      }
    }, "All 12 Steps"),
    ...Array.from({ length: 12 }, (_, idx) => {
      const sNum = idx + 1;
      const sCount = allRootCauses.filter((r) => Number(r.step_number) === sNum && r.kind === "active_failure").length;
      const isSel = selectedStepNumber === sNum;
      return h("button", {
        type: "button",
        className: `rca-step-pill ${sCount > 0 ? "has-stall" : ""} ${isSel ? "is-active" : ""}`,
        onclick: () => {
          selectedStepNumber = isSel ? null : sNum;
          renderDiagnosticsRCA(data);
        }
      }, `Step ${sNum}${sCount > 0 ? ` (${sCount})` : ""}`);
    })
  ];

  const kindTabs = [
    ["all", `All Findings (${allRootCauses.length})`],
    ["active_failure", `Active Stalls (${allRootCauses.filter((r) => r.kind === "active_failure").length})`],
    ["best_practice", `Best Practices (${allRootCauses.filter((r) => r.kind === "best_practice").length})`],
  ].map(([k, label]) =>
    h("button", {
      type: "button",
      className: `rca-step-pill ${selectedRcaKindFilter === k ? "is-active" : ""}`,
      onclick: () => {
        selectedRcaKindFilter = k;
        renderDiagnosticsRCA(data);
      }
    }, label)
  );

  const searchInput = h("input", {
    type: "text",
    className: "input",
    placeholder: "Search findings by pod, service, step, or component…",
    value: rcaSearchQuery,
    style: "min-width:240px;padding:6px 11px;font-size:0.78rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-inset);color:var(--fg);",
    onInput: (e) => {
      rcaSearchQuery = e.target.value;
      renderDiagnosticsRCA(data);
    }
  });

  return h("div", { className: "rca-filter-panel" }, [
    h("div", { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;" }, [
      h("span", { style: "font-size:0.74rem;font-weight:800;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.05em;" },
        "Filter by Kubernetes Control / Data Plane Component"),
      searchInput,
    ]),
    h("div", { className: "rca-comp-chips" }, [allCompBtn, ...compButtons]),
    h("div", { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding-top:6px;border-top:1px solid var(--border);" }, [
      h("div", { className: "rca-step-strip" }, stepPills),
      h("div", { className: "rca-step-strip" }, kindTabs),
    ])
  ]);
}

function renderDiagnosticsRCA(data) {
  const container = $("diagnostics-content");
  const summary = data.summary || { overall_score: 100, active_root_causes: 0, workloads_healthy: 0, workloads_total: 0 };
  const allRootCauses = data.root_causes || [];

  const stats = statGrid([
    statCard("Cluster Health Score", `${summary.overall_score}%`, [pill(summary.overall_score >= 80 ? "Healthy" : "Anomalies Detected", summary.overall_score >= 80 ? "healthy" : "critical")], STAT_ICONS.pulse),
    statCard("Folded Root Causes", summary.active_root_causes, [pill(`${allRootCauses.reduce((acc, r) => acc + (r.folded_failures?.length || 1), 0)} Raw Symptoms Folded`, "info")], STAT_ICONS.funnel),
    statCard("Workload Health", `${summary.workloads_healthy}/${summary.workloads_total} Healthy`, null, STAT_ICONS.shieldCheck),
  ]);

  const machineInfographic = buildKubectlMachineInfographic(data);
  const filterPanel = buildRCAFilterPanel(data);

  const q = rcaSearchQuery.toLowerCase().trim();
  const rootCauses = allRootCauses.filter((r) => {
    if (selectedPipelineStageId && r.stage_id !== selectedPipelineStageId) return false;
    if (selectedStepNumber && Number(r.step_number) !== selectedStepNumber) return false;
    if (selectedK8sComponentId && r.component_id !== selectedK8sComponentId) return false;
    if (selectedRcaKindFilter !== "all" && r.kind !== selectedRcaKindFilter) return false;
    if (q) {
      const hay = `${r.title || ""} ${r.summary || ""} ${r.step_badge || ""} ${r.component || ""} ${r.namespace || ""} ${(r.folded_failures || []).join(" ")}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (!rootCauses.length) {
    const hasFilter = Boolean(selectedPipelineStageId || selectedStepNumber || selectedK8sComponentId || selectedRcaKindFilter !== "all" || q);
    const cleanState = h("div", {
      className: "card",
      style: "padding:2.5rem;text-align:center;background:var(--bg-elevated);border:1.5px solid var(--success);border-radius:16px;margin-top:0.25rem;"
    }, [
      h("div", { style: "width:54px;height:54px;border-radius:50%;background:rgba(16,185,129,0.12);color:var(--success);display:inline-flex;align-items:center;justify-content:center;margin-bottom:1rem;" }, [
        h("svg", { width: "28", height: "28", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2.5" }, [
          h("path", { d: "M20 6L9 17l-5-5" })
        ])
      ]),
      h("h3", { style: "font-size:1.2rem;font-weight:700;color:var(--fg);margin:0 0 0.5rem;" },
        hasFilter ? "Zero Findings Match Selected Filter" : "Zero Anomalies Detected"),
      h("p", { style: "font-size:0.85rem;color:var(--fg-muted);max-width:520px;margin:0 auto;" },
        hasFilter
          ? "No active stalls or advisories matched your current K8s component / step filter. Click 'Reset Filters' to view all findings."
          : "AI Diagnostic Engine verified all 12 pipeline steps across controllers, scheduler, kubelet CSI/CNI/runtime, readiness probes, and EndpointSlices.")
    ]);
    renderInto(container, h("div", { style: "display:flex;flex-direction:column;gap:1.25rem;" }, [stats, machineInfographic, filterPanel, cleanState].filter(Boolean)));
    return;
  }

  const rcaCards = rootCauses.map((rc) => {
    const isCrit = rc.severity === "critical";

    return h("div", { className: `rca-card ${isCrit ? "rca-critical" : "rca-warning"}` }, [
      h("div", { style: "display:flex;align-items:flex-start;justify-content:space-between;gap:10px;flex-wrap:wrap;" }, [
        h("div", { style: "flex:1;min-width:0;" }, [
          h("div", { style: "display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;" }, [
            h("span", { style: `font-size:0.72rem;font-weight:700;padding:2px 8px;border-radius:6px;background:${isCrit ? "rgba(217,48,37,0.15)" : "rgba(245,158,11,0.15)"};color:${isCrit ? "var(--danger)" : "var(--warning)"};text-transform:uppercase;` }, rc.severity),
            rc.step_badge ? h("span", {
              style: "font-size:0.72rem;font-weight:700;font-family:var(--font-mono);padding:2px 9px;border-radius:6px;background:var(--accent-soft);color:var(--accent);border:1px solid var(--border);"
            }, rc.step_badge) : null,
            rc.component ? h("button", {
              type: "button",
              title: "Click to filter by this Kubernetes component",
              onclick: () => {
                selectedK8sComponentId = selectedK8sComponentId === rc.component_id ? null : rc.component_id;
                renderDiagnosticsRCA(data);
              },
              style: "font-size:0.72rem;font-family:var(--font-mono);color:var(--fg-muted);background:var(--bg-inset);padding:2px 8px;border-radius:6px;border:1px solid var(--border);cursor:pointer;"
            }, `⚙ ${rc.component}`) : null,
            h("span", { style: "font-size:0.75rem;font-weight:600;color:var(--accent);" }, `Confidence: ${Math.round((rc.confidence || 0.95) * 100)}%`),
            h("span", { style: "font-size:0.75rem;color:var(--fg-subtle);" }, `Namespace: ${rc.namespace}`)
          ].filter(Boolean)),
          h("h3", { style: "font-size:1.05rem;font-weight:700;color:var(--fg);margin:0;" }, rc.title),
          h("p", { style: "font-size:0.82rem;color:var(--fg-muted);margin:4px 0 0;line-height:1.45;" }, rc.summary)
        ])
      ]),

      // Blast Radius Section
      rc.blast_radius && (rc.blast_radius.impacted_services?.length || rc.blast_radius.impacted_namespaces?.length) ? h("div", { style: "display:flex;flex-direction:column;gap:6px;padding:10px 12px;border-radius:10px;background:var(--bg-inset);" }, [
        h("div", { style: "font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.04em;" }, "Blast Radius Impact Analysis"),
        h("div", { style: "display:flex;flex-wrap:wrap;gap:6px;align-items:center;" }, [
          ...(rc.blast_radius.impacted_services || []).map((s) => h("span", { className: "blast-tag" }, [
            h("svg", { width: "12", height: "12", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2" }, [
              h("circle", { cx: "12", cy: "12", r: "10" }),
              h("line", { x1: "12", y1: "8", x2: "12", y2: "12" })
            ]),
            `svc/${s}`
          ])),
          ...(rc.blast_radius.impacted_namespaces || []).map((ns) => h("span", { className: "blast-tag" }, `ns/${ns}`)),
          h("span", { style: "font-size:0.75rem;color:var(--fg-subtle);margin-left:4px;" }, `Estimated blast radius: ${rc.blast_radius.blast_scope || "isolated"}`)
        ])
      ]) : null,

      // Folded Failures Symptom List
      rc.folded_failures && rc.folded_failures.length ? h("div", { style: "display:flex;flex-direction:column;gap:4px;" }, [
        h("div", { style: "font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;" }, `Folded Symptoms (${rc.folded_failures.length})`),
        h("div", { style: "display:flex;flex-direction:column;gap:4px;" }, rc.folded_failures.map((f) => h("div", { style: "font-size:0.78rem;font-family:var(--font-mono);color:var(--fg-muted);background:var(--bg-inset);padding:4px 8px;border-radius:6px;" }, `• ${f}`)))
      ]) : null,

      // Remediation Command
      rc.remediation ? h("div", { style: "display:flex;flex-direction:column;gap:6px;" }, [
        h("div", { style: "font-size:0.72rem;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:0.04em;" }, "Suggested 12-Step Runbook Action"),
        h("div", { className: "code-action-box" }, [
          h("code", { style: "word-break:break-all;" }, rc.remediation.command || rc.remediation.action),
          h("button", {
            type: "button",
            className: "btn btn--primary btn--sm",
            style: "padding:4px 10px;font-size:0.75rem;white-space:nowrap;",
            onclick: (e) => {
              navigator.clipboard?.writeText(rc.remediation.command || rc.remediation.action);
              e.target.textContent = "Copied!";
              setTimeout(() => { e.target.textContent = "Copy Command"; }, 2000);
            }
          }, "Copy Command")
        ])
      ]) : null,

      h("button", {
        type: "button",
        className: "btn btn--secondary btn--sm",
        style: "align-self:flex-start;",
        onclick: () => openAIDiagnoseModal({
          namespace: rc.namespace,
          question: `Using the 12-Step kubectl apply Machine runbook, investigate this finding at ${rc.step_badge || "this stage"} and suggest concrete next steps: "${rc.title}" — ${rc.summary}. `
            + `Affected: ${(rc.folded_failures || []).join(", ")}.`,
          title: `Diagnose with AI — ${rc.title}`,
        }),
      }, "✨ Diagnose with AI"),
    ].filter(Boolean));
  });

  renderInto(container, h("div", { style: "display:flex;flex-direction:column;gap:1.25rem;" }, [stats, machineInfographic, filterPanel, ...rcaCards].filter(Boolean)));
}

// -- view: audit logs & timeline ----------------------------------------------

async function loadAuditLogsView(force = false) {
  const container = $("audit-content");
  const clusterId = state.selectedClusterId;
  await loadWithSWR({
    key: `audit:${clusterId}`,
    tokenView: "audit",
    container,
    force,
    fetchFn: async () => {
      const [eventsRes, auditRes] = await Promise.all([
        api.getExtendedEvents(clusterId),
        api.getAuditLogs(clusterId),
      ]);
      return { events: eventsRes.items || [], audit: auditRes.logs || [] };
    },
    renderFn: (data) => renderAuditAndEvents(data),
  });
}

function renderAuditAndEvents(data) {
  const container = $("audit-content");
  const filter = $("audit-type-select")?.value || "all";

  const allItems = [];
  (data.audit || []).forEach((a) => {
    allItems.push({
      type: "audit",
      timestamp: a.timestamp,
      actor: a.actor || "system",
      action: a.action,
      target: a.target,
      details: a.details,
      status: a.status || "success"
    });
  });

  (data.events || []).forEach((e) => {
    const kind = e.involved_kind || e.involved_object_kind || "Object";
    const name = e.involved_name || e.involved_object_name || "unknown";
    allItems.push({
      type: "event",
      timestamp: e.last_timestamp || e.first_timestamp,
      actor: e.source_component || "k8s-controller",
      action: e.reason,
      target: `${kind}/${name}`,
      involved_kind: kind,
      involved_name: name,
      namespace: e.namespace || "",
      details: e.message,
      event_type: e.type,
      count: e.count
    });
  });

  allItems.sort((a, b) => new Date(b.timestamp || 0) - new Date(a.timestamp || 0));

  const filtered = allItems.filter((item) => {
    if (filter === "audit") return item.type === "audit";
    if (filter === "events") return item.type === "event";
    if (filter === "warning") return item.event_type === "Warning" || item.status === "failed";
    return true;
  });

  if (!filtered.length) {
    renderInto(container, emptyState("No audit records or events found", "No entries match the selected filter."));
    return;
  }

  const rows = filtered.map((item) => {
    const isAudit = item.type === "audit";
    const isWarning = item.event_type === "Warning" || item.status === "failed";

    return h("div", { className: "audit-timeline-item" }, [
      h("div", { className: "audit-icon-bubble", style: `background:${isAudit ? "var(--accent-soft)" : isWarning ? "rgba(217,48,37,0.12)" : "var(--bg-inset)"};color:${isAudit ? "var(--accent)" : isWarning ? "var(--danger)" : "var(--fg-muted)"};` }, [
        h("svg", { width: "16", height: "16", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2" }, [
          isAudit
            ? h("path", { d: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" })
            : h("circle", { cx: "12", cy: "12", r: "10" })
        ])
      ]),
      h("div", { style: "flex:1;min-width:0;" }, [
        h("div", { style: "display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;" }, [
          h("div", { style: "display:flex;align-items:center;gap:6px;" }, [
            h("span", { style: `font-size:0.68rem;font-weight:700;padding:2px 6px;border-radius:4px;background:${isAudit ? "var(--accent-soft)" : "var(--bg-inset)"};color:${isAudit ? "var(--accent)" : "var(--fg-muted)"};text-transform:uppercase;` }, isAudit ? "AUDIT" : "EVENT"),
            h("span", { style: "font-weight:700;font-size:0.88rem;color:var(--fg);" }, item.action),
            item.involved_kind && item.involved_name
              ? h("button", {
                  type: "button",
                  class: "btn--unstyled",
                  style: "font-family:var(--font-mono);font-size:0.8rem;color:var(--accent);font-weight:600;cursor:pointer;text-decoration:underline;text-underline-offset:2px;",
                  title: "Click to view YAML manifest",
                  onclick: () => openManifestModal(item.involved_kind, item.involved_name, item.namespace || ""),
                }, item.target)
              : h("span", { style: "font-family:var(--font-mono);font-size:0.8rem;color:var(--accent);" }, item.target),
            item.namespace ? h("span", { class: "cmap-chip" }, item.namespace) : null,
          ]),
          h("span", { style: "font-size:0.75rem;color:var(--fg-subtle);" }, timeAgo(item.timestamp))
        ]),
        h("div", { style: "font-size:0.8rem;color:var(--fg-muted);margin-top:4px;line-height:1.4;" }, item.details || JSON.stringify(item.details || "")),
        h("div", { style: "font-size:0.72rem;color:var(--fg-subtle);margin-top:2px;" }, `Actor: ${item.actor} · ${item.timestamp ? new Date(item.timestamp).toLocaleString() : ""}`)
      ])
    ]);
  });

  renderInto(container, h("div", { className: "card", style: "padding:1.25rem;background:var(--bg-elevated);border:1px solid var(--border);border-radius:12px;" }, rows));
}

// -- Rollout History & Rollback Modal -----------------------------------------

async function openRolloutModal(workloadType, name, namespace) {
  const modal = $("rollout-modal");
  const title = $("rollout-modal-title");
  const subtitle = $("rollout-modal-subtitle");
  const body = $("rollout-modal-body");

  if (!modal || !body) return;
  modal.hidden = false;
  title.textContent = `Rollout History — ${workloadType}/${name}`;
  subtitle.textContent = `Namespace: ${namespace || "default"} · Revisions & Version Skew`;
  renderInto(body, loadingSkeleton());

  try {
    const data = await api.getRolloutHistory(state.selectedClusterId, workloadType, name, namespace);
    renderRolloutModal(data, workloadType, name, namespace);
  } catch (err) {
    renderInto(body, errorState(err, () => openRolloutModal(workloadType, name, namespace)));
  }
}

function renderRolloutModal(data, workloadType, name, namespace) {
  const body = $("rollout-modal-body");
  const revisions = data.revisions || [];
  const hasSkew = data.has_skew || false;

  const skewAlert = hasSkew ? h("div", {
    style: "padding:10px 14px;border-radius:10px;background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.3);color:#b45309;font-size:0.82rem;font-weight:600;display:flex;align-items:center;gap:8px;margin-bottom:1rem;"
  }, [
    h("svg", { width: "16", height: "16", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2" }, [
      h("path", { d: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" })
    ]),
    "Version Skew Detected: Pods are currently running on different revisions during active rollout."
  ]) : null;

  if (!revisions.length) {
    renderInto(body, h("div", {}, [skewAlert, emptyState("No revision history found", "ReplicaSets history may have been trimmed.")]));
    return;
  }

  const rows = revisions.map((rev) => {
    const isActive = rev.is_active || rev.revision === data.active_revision;

    return h("div", {
      style: `display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-radius:10px;background:${isActive ? "var(--accent-soft)" : "var(--bg-inset)"};border:1.5px solid ${isActive ? "var(--accent)" : "var(--border)"};margin-bottom:8px;`
    }, [
      h("div", { style: "display:flex;align-items:center;gap:12px;" }, [
        h("div", { style: `width:32px;height:32px;border-radius:8px;background:${isActive ? "var(--accent)" : "var(--bg-elevated)"};color:${isActive ? "#fff" : "var(--fg)"};display:flex;align-items:center;justify-content:center;font-weight:700;font-size:0.88rem;` }, String(rev.revision)),
        h("div", {}, [
          h("div", { style: "display:flex;align-items:center;gap:8px;" }, [
            h("span", { style: "font-weight:700;font-size:0.9rem;color:var(--fg);" }, `Revision ${rev.revision}`),
            isActive ? h("span", { style: "font-size:0.68rem;font-weight:700;padding:2px 8px;border-radius:99px;background:var(--success);color:#fff;text-transform:uppercase;" }, "CURRENT") : null,
          ]),
          h("div", { style: "font-size:0.75rem;font-family:var(--font-mono);color:var(--fg-muted);margin-top:2px;" }, (rev.images || []).join(", ") || "No image recorded"),
          rev.cause ? h("div", { style: "font-size:0.75rem;color:var(--fg-subtle);margin-top:2px;" }, `Cause: ${rev.cause}`) : null
        ])
      ]),

      h("div", { style: "display:flex;align-items:center;gap:8px;" }, [
        h("span", { style: "font-size:0.75rem;color:var(--fg-subtle);" }, rev.created_at ? timeAgo(rev.created_at) : ""),
        !isActive && canOperate() ? h("button", {
          className: "btn btn--primary btn--sm",
          style: "padding:5px 12px;font-size:0.78rem;font-weight:600;",
          onclick: async () => {
            if (confirm(`Rollback ${workloadType} "${name}" to Revision ${rev.revision}?`)) {
              try {
                await api.rollbackWorkload(state.selectedClusterId, workloadType, name, namespace, rev.revision);
                alert(`✓ Rollback to Revision ${rev.revision} initiated!`);
                await openRolloutModal(workloadType, name, namespace);
              } catch (err) {
                alert(`Failed to rollback: ${err.message}`);
              }
            }
          }
        }, "Rollback to this") : null
      ])
    ]);
  });

  renderInto(body, h("div", {}, [skewAlert, ...rows]));
}

function closeRolloutModal() {
  const modal = $("rollout-modal");
  if (modal) modal.hidden = true;
}

// -- Workload Change Timeline Modal ------------------------------------------

function closeTimelineModal() {
  const modal = $("timeline-modal");
  if (modal) modal.hidden = true;
}

async function openTimelineModal(workloadType, name, namespace) {
  const modal = $("timeline-modal");
  const title = $("timeline-modal-title");
  const subtitle = $("timeline-modal-subtitle");
  const body = $("timeline-modal-body");
  if (!modal || !body) return;

  modal.hidden = false;
  title.textContent = `Change Timeline — ${workloadType}/${name}`;
  subtitle.textContent = `Cluster: ${state.selectedClusterId} · Namespace: ${namespace || "default"}`;
  renderInto(body, loadingSkeleton());

  try {
    const data = await api.getWorkloadTimeline(state.selectedClusterId, namespace || "default", workloadType, name, 60);
    renderTimelineModal(data);
  } catch (err) {
    renderInto(body, errorState(err, () => openTimelineModal(workloadType, name, namespace)));
  }
}

function renderTimelineModal(data) {
  const body = $("timeline-modal-body");
  const items = data.items || [];
  const summary = data.summary || {};
  let activeFilter = "all";

  const summaryBar = h("div", { style: "display:flex;gap:0.5rem;flex-wrap:wrap;margin-bottom:0.85rem;" }, [
    pill(`Rollouts: ${summary.rollouts ?? 0}`, "healthy"),
    pill(`Helm: ${summary.helm_revisions ?? 0}`, "unknown"),
    pill(`Config/Secret Refs: ${summary.config_refs ?? 0}`, "unknown"),
    pill(`Events: ${summary.events ?? 0}`, summary.warnings ? "warning" : "healthy"),
  ]);

  const listBox = h("div", { style: "display:flex;flex-direction:column;gap:0.55rem;" });
  const renderRows = () => {
    const filtered = activeFilter === "all" ? items : items.filter((it) => it.kind === activeFilter);
    if (!filtered.length) {
      renderInto(listBox, emptyState("No changes recorded", "Try expanding filter scope or checking live events."));
      return;
    }
    renderInto(
      listBox,
      h("div", { style: "display:flex;flex-direction:column;gap:0.5rem;" }, filtered.map((entry) => {
        const badgeKind = entry.severity === "warning" ? "warning" : entry.kind === "rollout" ? "healthy" : "unknown";
        return h("div", { style: "padding:0.75rem 0.9rem;border:1px solid var(--border);border-radius:10px;background:var(--bg-inset);" }, [
          h("div", { style: "display:flex;align-items:center;justify-content:space-between;gap:0.5rem;flex-wrap:wrap;" }, [
            h("div", { style: "display:flex;align-items:center;gap:0.45rem;flex-wrap:wrap;" }, [
              pill((entry.kind || "change").toUpperCase(), badgeKind),
              h("span", { style: "font-weight:700;font-size:0.86rem;color:var(--fg);" }, entry.title || ""),
            ]),
            h("span", { style: "font-size:0.75rem;color:var(--fg-subtle);font-family:var(--font-mono);" }, entry.timestamp ? timeAgo(entry.timestamp) : "no timestamp"),
          ]),
          h("div", { style: "font-size:0.8rem;color:var(--fg-muted);margin-top:0.3rem;" }, entry.detail || ""),
        ]);
      }))
    );
  };

  const filters = h("div", { style: "display:flex;gap:0.4rem;flex-wrap:wrap;margin-bottom:0.9rem;" },
    [["all", "All"], ["rollout", "Rollouts"], ["helm", "Helm"], ["config", "Config/Secrets"], ["event", "Events"]].map(([key, label]) =>
      h("button", {
        type: "button",
        className: "btn btn--secondary btn--sm",
        onclick: () => {
          activeFilter = key;
          renderRows();
        },
      }, label)
    )
  );

  renderRows();
  renderInto(body, h("div", {}, [summaryBar, filters, listBox]));
}

// -- Cross-Cluster Drift Diff Modal ------------------------------------------

function closeDriftDiffModal() {
  const modal = $("drift-modal");
  if (modal) modal.hidden = true;
}

function openDriftDiffModal(workloadType, name, sourceNamespace) {
  const modal = $("drift-modal");
  const title = $("drift-modal-title");
  const subtitle = $("drift-modal-subtitle");
  const body = $("drift-modal-body");
  if (!modal || !body) return;

  modal.hidden = false;
  title.textContent = `Drift Diff — ${workloadType}/${name}`;
  subtitle.textContent = `Compare ${state.selectedClusterId} (${sourceNamespace || "default"}) against another registered cluster (Secret values masked)`;

  const allClusters = [];
  for (const team of state.teams || []) {
    for (const env of team.environments || []) {
      for (const cl of env.clusters || []) {
        allClusters.push(cl);
      }
    }
  }
  const otherClusters = allClusters.filter((c) => c.id !== state.selectedClusterId);
  const targetChoices = otherClusters.length ? otherClusters : allClusters;

  const targetSelect = h("select", { class: "select", style: "width:100%;font-size:0.82rem;" },
    targetChoices.map((c) => h("option", { value: c.id }, `${c.name || c.id} (${c.environment || c.provider || ""})`))
  );
  const targetNsInput = h("input", {
    type: "text",
    value: sourceNamespace || "default",
    class: "select",
    style: "width:100%;font-family:var(--font-mono);font-size:0.82rem;",
  });
  const resultHost = h("div", { style: "margin-top:0.9rem;" });

  const runCompare = async () => {
    const targetClusterId = targetSelect.value;
    if (!targetClusterId) {
      renderInto(resultHost, emptyState("No target cluster available", "Add another cluster in Clusters to compare drift."));
      return;
    }
    renderInto(resultHost, loadingSkeleton());
    try {
      const diff = await api.getWorkloadDriftDiff({
        source_cluster_id: state.selectedClusterId,
        target_cluster_id: targetClusterId,
        workload_type: workloadType,
        name,
        source_namespace: sourceNamespace || "default",
        target_namespace: targetNsInput.value.trim() || sourceNamespace || "default",
      });
      renderDriftResult(resultHost, diff);
    } catch (err) {
      renderInto(resultHost, errorState(err, runCompare));
    }
  };

  const controls = h("div", { style: "display:grid;grid-template-columns:1fr 1fr auto;gap:0.65rem;align-items:end;padding-bottom:0.75rem;border-bottom:1px solid var(--border);" }, [
    h("label", { style: "display:flex;flex-direction:column;gap:4px;font-size:0.75rem;font-weight:600;color:var(--fg-muted);" }, ["Target Cluster", targetSelect]),
    h("label", { style: "display:flex;flex-direction:column;gap:4px;font-size:0.75rem;font-weight:600;color:var(--fg-muted);" }, ["Target Namespace", targetNsInput]),
    h("button", { type: "button", className: "btn btn--primary", style: "height:36px;white-space:nowrap;", onClick: runCompare }, "Compare Clusters"),
  ]);

  renderInto(body, h("div", {}, [controls, resultHost]));
  if (targetChoices.length) runCompare();
}

function renderDriftResult(host, diff) {
  const sections = diff.sections || [];
  const header = h("div", { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem;margin-bottom:0.75rem;" }, [
    h("div", { style: "display:flex;align-items:center;gap:0.5rem;" }, [
      pill(diff.has_drift ? `${diff.drift_count} Drift(s)` : "In Sync", diff.has_drift ? (diff.severity === "critical" || diff.severity === "high" ? "critical" : "warning") : "healthy"),
      pill("Secrets SHA-256 Masked", "healthy"),
    ]),
    h("span", { style: "font-size:0.8rem;color:var(--fg-muted);" }, diff.summary || ""),
  ]);

  if (!sections.length) {
    renderInto(host, h("div", {}, [header, emptyState("Zero configuration drift", `${diff.source?.label || "Source"} and ${diff.target?.label || "Target"} match across replicas, images, limits, env vars, probes, ConfigMaps, and NetworkPolicies.`)]));
    return;
  }

  const rows = sections.map((s) =>
    h("tr", {}, [
      h("td", { style: "padding:0.45rem 0.6rem;border-bottom:1px solid var(--border);" }, pill(s.category, "unknown")),
      h("td", { style: "padding:0.45rem 0.6rem;border-bottom:1px solid var(--border);font-family:var(--font-mono);font-size:0.78rem;" }, s.field),
      h("td", { style: "padding:0.45rem 0.6rem;border-bottom:1px solid var(--border);font-family:var(--font-mono);font-size:0.78rem;" }, s.source_value),
      h("td", { style: "padding:0.45rem 0.6rem;border-bottom:1px solid var(--border);font-family:var(--font-mono);font-size:0.78rem;" }, s.target_value),
      h("td", { style: "padding:0.45rem 0.6rem;border-bottom:1px solid var(--border);" }, pill(s.status, s.status === "changed" ? "warning" : "critical")),
    ])
  );

  const table = h("table", { style: "width:100%;border-collapse:collapse;text-align:left;" }, [
    h("thead", {}, h("tr", { style: "font-size:0.74rem;color:var(--fg-subtle);border-bottom:1.5px solid var(--border);" }, [
      h("th", { style: "padding:0.45rem 0.6rem;" }, "Category"),
      h("th", { style: "padding:0.45rem 0.6rem;" }, "Field"),
      h("th", { style: "padding:0.45rem 0.6rem;" }, diff.source?.label || "Source"),
      h("th", { style: "padding:0.45rem 0.6rem;" }, diff.target?.label || "Target"),
      h("th", { style: "padding:0.45rem 0.6rem;" }, "Status"),
    ])),
    h("tbody", {}, rows),
  ]);

  renderInto(host, h("div", { style: "overflow-x:auto;" }, [header, table]));
}

// -- Pod & Container Inspector Modal -----------------------------------------

async function openPodInspectorModal(podName, namespace) {
  const modal = $("pod-inspector-modal");
  const title = $("pod-inspector-title");
  const subtitle = $("pod-inspector-subtitle");
  const body = $("pod-inspector-body");

  if (!modal || !body) return;
  modal.hidden = false;
  title.textContent = `Pod Inspector — ${podName}`;
  subtitle.textContent = `Namespace: ${namespace || "default"}`;
  renderInto(body, loadingSkeleton());

  try {
    const data = await api.getPodDetails(state.selectedClusterId, namespace || "default", podName);
    renderPodInspectorModal(data, podName, namespace || "default");
  } catch (err) {
    renderInto(body, errorState(err, () => openPodInspectorModal(podName, namespace)));
  }
}

function renderPodInspectorModal(data, podName, namespace) {
  const body = $("pod-inspector-body");
  const containers = data.containers || [];
  const logs = data.recent_logs || "";

  // Containers Specs Cards -- probes are rendered per-container (that's
  // where the backend's get_pod_inspector() actually attaches them, as
  // containers[].probes = {livenessProbe/readinessProbe/startupProbe: {...}}
  // -- rather than a separate top-level list, which only ever read an
  // undefined `data.probes` and so always rendered empty).
  const containerCards = containers.map((c) => {
    const isReady = c.ready || false;
    const probeEntries = Object.entries(c.probes || {});

    return h("div", {
      style: "padding:12px;border-radius:10px;background:var(--bg-inset);border:1px solid var(--border);display:flex;flex-direction:column;gap:8px;"
    }, [
      h("div", { style: "display:flex;align-items:center;justify-content:space-between;" }, [
        h("div", { style: "display:flex;align-items:center;gap:8px;" }, [
          h("span", { style: `width:8px;height:8px;border-radius:50%;background:${isReady ? "var(--success)" : "var(--danger)"};` }),
          h("span", { style: "font-weight:700;font-size:0.92rem;color:var(--fg);" }, c.name),
          h("span", { style: "font-size:0.72rem;font-family:var(--font-mono);color:var(--fg-muted);" }, c.image)
        ]),
        pill(`Restarts: ${c.restart_count || 0}`, (c.restart_count || 0) > 0 ? "warning" : "healthy")
      ]),

      h("div", { style: "display:grid;grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));gap:8px;font-size:0.78rem;" }, [
        h("div", {}, [
          h("span", { style: "color:var(--fg-subtle);font-weight:600;" }, "CPU Req/Limit: "),
          h("span", { style: "font-family:var(--font-mono);color:var(--fg);" }, `${c.resources?.requests?.cpu || "—"} / ${c.resources?.limits?.cpu || "—"}`)
        ]),
        h("div", {}, [
          h("span", { style: "color:var(--fg-subtle);font-weight:600;" }, "Mem Req/Limit: "),
          h("span", { style: "font-family:var(--font-mono);color:var(--fg);" }, `${c.resources?.requests?.memory || "—"} / ${c.resources?.limits?.memory || "—"}`)
        ]),
        c.last_termination_reason ? h("div", { style: "color:var(--danger);font-weight:600;" }, [
          `Last Exit: ${c.last_termination_reason} (code ${c.last_exit_code ?? "?"})`
        ]) : null,
        c.waiting_reason ? h("div", { style: "color:var(--warning);font-weight:600;" }, [
          `Waiting: ${c.waiting_reason}${c.waiting_message ? ` — ${c.waiting_message}` : ""}`
        ]) : null,
      ]),

      h("div", { style: "display:flex;flex-direction:column;gap:4px;" }, [
        h("span", { style: "font-size:0.7rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.04em;" }, "Probes"),
        probeEntries.length
          ? h("div", { style: "display:flex;flex-direction:column;gap:3px;" }, probeEntries.map(([type, p]) =>
              h("div", { style: "display:flex;align-items:center;gap:8px;font-size:0.78rem;" }, [
                h("span", { style: "width:7px;height:7px;border-radius:50%;background:var(--success);flex-shrink:0;" }),
                h("span", { style: "font-weight:600;color:var(--fg);" }, type.replace("Probe", "")),
                h("span", { style: "font-family:var(--font-mono);color:var(--fg-muted);" }, p.description || "configured")
              ])
            ))
          : h("div", { style: "font-size:0.78rem;color:var(--fg-subtle);font-style:italic;" }, "No liveness/readiness/startup probes configured")
      ])
    ]);
  });

  // Interactive Live Logs / Web Exec Simulator Box
  const terminalBox = h("div", { style: "display:flex;flex-direction:column;gap:8px;" }, [
    h("div", { style: "display:flex;align-items:center;justify-content:space-between;" }, [
      h("span", { style: "font-size:0.75rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.04em;" }, "Live Container Log Stream"),
      h("span", { style: "font-size:0.72rem;color:var(--fg-subtle);" }, "Auto-tailing last 100 lines")
    ]),
    h("pre", { className: "terminal-output" }, logs || "No container logs available.")
  ]);

  const aiButton = h("button", {
    type: "button",
    className: "btn btn--secondary btn--sm",
    style: "align-self:flex-start;",
    onclick: () => openAIDiagnoseModal({
      namespace, resourceType: "Pod", resourceName: podName,
      title: `Diagnose with AI — ${podName}`,
    }),
  }, "✨ Diagnose with AI");

  const pipelineSteps = data.pipeline_steps || [];
  const pipelineChecklist = pipelineSteps.length ? h("div", {
    style: "display:flex;flex-direction:column;gap:6px;"
  }, [
    h("span", { style: "font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.04em;" },
      "12-Step Delivery Pipeline Status (kubectl apply → Traffic)"),
    h("div", {
      style: "display:grid;grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));gap:8px;"
    }, pipelineSteps.map((s) => {
      const isPass = s.status === "passed";
      const isFail = s.status === "failed";
      const color = isPass ? "var(--success)" : isFail ? "var(--danger)" : "var(--warning)";
      const iconText = isPass ? "✓" : isFail ? "✗" : "⋯";
      return h("div", {
        style: `padding:9px 11px;border-radius:9px;background:var(--bg-inset);border:1px solid ${isFail ? "var(--danger)" : "var(--border)"};display:flex;flex-direction:column;gap:3px;`
      }, [
        h("div", { style: "display:flex;align-items:center;justify-content:space-between;gap:6px;" }, [
          h("span", { style: "font-size:0.7rem;font-weight:800;font-family:var(--font-mono);color:var(--accent);" }, s.step),
          h("span", { style: `font-size:0.7rem;font-weight:700;color:${color};` }, `${iconText} ${s.status.toUpperCase()}`)
        ]),
        h("div", { style: "font-size:0.8rem;font-weight:700;color:var(--fg);" }, s.title),
        h("div", { style: "font-size:0.73rem;color:var(--fg-muted);line-height:1.35;word-break:break-word;" }, s.detail || "")
      ]);
    }))
  ]) : null;

  renderInto(body, h("div", { style: "display:flex;flex-direction:column;gap:1.25rem;" }, [
    aiButton,
    pipelineChecklist,
    h("div", { style: "display:flex;flex-direction:column;gap:6px;" }, [
      h("span", { style: "font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.04em;" }, "Container Specifications & Resource Allocation"),
      ...containerCards
    ]),
    terminalBox
  ].filter(Boolean)));
}

function closePodInspectorModal() {
  const modal = $("pod-inspector-modal");
  if (modal) modal.hidden = true;
}

// -- Safe Markdown Renderer for AI Responses ---------------------------------
// Converts agent Markdown (headings, bold, italic, inline code, fenced code
// blocks with copy button, bullet/numbered lists, blockquotes, and tables) into
// sanitized DOM nodes inside a `.ai-markdown` wrapper. HTML entities are
// escaped first so untrusted pod logs or resource names cannot inject scripts.

function formatInlineMarkdown(rawLine) {
  // 1. Extract inline code spans `...` first so bold/italic regexes don't touch them
  const codeTokens = [];
  let text = String(rawLine || "").replace(/`([^`\n]+)`/g, (_, code) => {
    const idx = codeTokens.length;
    codeTokens.push(`<code class="ai-md-inline-code">${escapeHtml(code)}</code>`);
    return `\x00CODE_${idx}\x00`;
  });

  // 2. Escape remaining HTML
  text = escapeHtml(text);

  // 3. Bold (**...** or __...__) and Italic (*...*)
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  text = text.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>");

  // 4. Restore inline code tokens
  text = text.replace(/\x00CODE_(\d+)\x00/g, (_, idx) => codeTokens[Number(idx)] || "");
  return text;
}

function renderMarkdownNode(rawText) {
  const wrapper = h("div", { className: "ai-markdown" });
  const text = String(rawText || "").replace(/\r\n/g, "\n").trim();
  if (!text) {
    wrapper.textContent = "No response.";
    return wrapper;
  }

  // Split out fenced code blocks ```lang ... ```
  const segments = [];
  const fenceRegex = /```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```/g;
  let lastIdx = 0;
  let match;
  while ((match = fenceRegex.exec(text)) !== null) {
    if (match.index > lastIdx) {
      segments.push({ type: "prose", content: text.slice(lastIdx, match.index) });
    }
    segments.push({ type: "code", lang: (match[1] || "text").trim(), content: match[2].replace(/\n$/, "") });
    lastIdx = fenceRegex.lastIndex;
  }
  if (lastIdx < text.length) {
    segments.push({ type: "prose", content: text.slice(lastIdx) });
  }

  for (const seg of segments) {
    if (seg.type === "code") {
      const copyBtn = h("button", {
        type: "button",
        className: "ai-md-codeblock__copy",
        onclick: () => {
          navigator.clipboard?.writeText(seg.content);
          copyBtn.textContent = "Copied!";
          setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500);
        }
      }, "Copy");
      const block = h("div", { className: "ai-md-codeblock" }, [
        h("div", { className: "ai-md-codeblock__header" }, [
          h("span", {}, seg.lang || "code"),
          copyBtn,
        ]),
        h("pre", {}, [h("code", {}, seg.content)]),
      ]);
      wrapper.appendChild(block);
      continue;
    }

    // Parse line-oriented blocks (headings, lists, tables, hr, blockquotes, paragraphs)
    const lines = seg.content.split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      if (!trimmed) {
        i++;
        continue;
      }

      // Horizontal rule
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
        wrapper.appendChild(h("hr"));
        i++;
        continue;
      }

      // Heading (# to ####)
      const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
      if (headingMatch) {
        const level = Math.min(4, headingMatch[1].length);
        const headingText = headingMatch[2].replace(/^\*\*(.+)\*\*$/, "$1");
        const el = document.createElement(`h${level}`);
        el.innerHTML = formatInlineMarkdown(headingText);
        wrapper.appendChild(el);
        i++;
        continue;
      }

      // Markdown Table (| col | col | followed by | --- | --- |)
      if (trimmed.startsWith("|") && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
        const parseCells = (rowStr) =>
          rowStr.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const headers = parseCells(trimmed);
        i += 2; // skip header + separator
        const rows = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) {
          rows.push(parseCells(lines[i]));
          i++;
        }
        const table = document.createElement("table");
        const thead = document.createElement("thead");
        const trHead = document.createElement("tr");
        headers.forEach((hdr) => {
          const th = document.createElement("th");
          th.innerHTML = formatInlineMarkdown(hdr);
          trHead.appendChild(th);
        });
        thead.appendChild(trHead);
        table.appendChild(thead);

        const tbody = document.createElement("tbody");
        rows.forEach((r) => {
          const tr = document.createElement("tr");
          headers.forEach((_, colIdx) => {
            const td = document.createElement("td");
            td.innerHTML = formatInlineMarkdown(r[colIdx] || "");
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        const tableWrap = h("div", { className: "ai-md-table-wrap" }, [table]);
        wrapper.appendChild(tableWrap);
        continue;
      }

      // Unordered list (*, -, •)
      if (/^(\*|-|•)\s+/.test(trimmed)) {
        const ul = document.createElement("ul");
        while (i < lines.length && /^(\*|-|•)\s+/.test(lines[i].trim())) {
          const itemText = lines[i].trim().replace(/^(\*|-|•)\s+/, "");
          const li = document.createElement("li");
          li.innerHTML = formatInlineMarkdown(itemText);
          ul.appendChild(li);
          i++;
        }
        wrapper.appendChild(ul);
        continue;
      }

      // Ordered list (1. 2. ...)
      if (/^\d+\.\s+/.test(trimmed)) {
        const ol = document.createElement("ol");
        while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
          const itemText = lines[i].trim().replace(/^\d+\.\s+/, "");
          const li = document.createElement("li");
          li.innerHTML = formatInlineMarkdown(itemText);
          ol.appendChild(li);
          i++;
        }
        wrapper.appendChild(ol);
        continue;
      }

      // Blockquote (> ...)
      if (trimmed.startsWith(">")) {
        const quoteLines = [];
        while (i < lines.length && lines[i].trim().startsWith(">")) {
          quoteLines.push(lines[i].trim().replace(/^>\s?/, ""));
          i++;
        }
        const bq = document.createElement("blockquote");
        bq.innerHTML = quoteLines.map(formatInlineMarkdown).join("<br>");
        wrapper.appendChild(bq);
        continue;
      }

      // Regular paragraph (group consecutive plain lines)
      const paraLines = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^(#{1,4}\s+|(\*|-|•)\s+|\d+\.\s+|>\s*|(-{3,}|\*{3,}|_{3,})$|\|)/.test(lines[i].trim())
      ) {
        paraLines.push(lines[i].trim());
        i++;
      }
      if (paraLines.length) {
        const p = document.createElement("p");
        p.innerHTML = paraLines.map(formatInlineMarkdown).join("<br>");
        wrapper.appendChild(p);
      } else {
        i++;
      }
    }
  }

  return wrapper;
}

// -- AI Diagnose Modal --------------------------------------------------------
// Thin UI around POST /api/clusters/{id}/ai/diagnose (see ai_agent.py): a
// single-turn, read-only agent call. Never mutates the cluster -- worst case
// it returns an error (e.g. no model credentials configured).

const aguiClient = new AGUIStreamClient();
let aguiAbortController = null;
let aguiDiagnoseAbortController = null;

function getActivePortalState() {
  const nsMap = {
    workloads: "workloads-namespace-select",
    applications: "applications-namespace-select",
    diagnostics: "diagnostics-namespace-select",
    resourcemap: "resourcemap-namespace-select",
    events: "events-namespace-select",
    network: "network-namespace-select",
    storage: "storage-namespace-select",
    map: "map-namespace-select",
    namespaces: "namespace-select",
  };
  const selectId = nsMap[state.activeView];
  const nsEl = selectId ? $(selectId) : null;
  const providerVal = $("ai-provider-select")?.value || "vertex_ai";
  return {
    clusterId: state.selectedClusterId || "",
    activeView: state.activeView || "overview",
    namespace: nsEl ? nsEl.value || "" : "",
    selectedStep: selectedStepNumber || null,
    aiProvider: providerVal,
    userRole: state.userRole || "viewer",
    userSubject: state.userSubject || "anonymous",
  };
}

function applyNamespaceFilterForView(view, namespace) {
  if (namespace === undefined || namespace === null) return;
  const nsMap = {
    workloads: "workloads-namespace-select",
    applications: "applications-namespace-select",
    diagnostics: "diagnostics-namespace-select",
    resourcemap: "resourcemap-namespace-select",
    events: "events-namespace-select",
    network: "network-namespace-select",
    storage: "storage-namespace-select",
    map: "map-namespace-select",
    namespaces: "namespace-select",
  };
  const selectId = nsMap[view];
  const el = selectId ? $(selectId) : null;
  if (!el) return;
  const target = String(namespace);
  if (target === "" || Array.from(el.options).some((opt) => opt.value === target)) {
    el.value = target;
  } else if (target) {
    const opt = document.createElement("option");
    opt.value = target;
    opt.textContent = target;
    el.appendChild(opt);
    el.value = target;
  }
}

function executePortalUIAction(payload) {
  if (!payload || !payload.action) return false;
  if (payload.action === "navigate" && payload.view) {
    applyNamespaceFilterForView(payload.view, payload.namespace);
    switchView(payload.view);
    return true;
  }
  if (payload.action === "filter_12step") {
    applyNamespaceFilterForView("diagnostics", payload.namespace);
    selectedStepNumber = payload.step_number ? Number(payload.step_number) : null;
    if (state.activeView === "diagnostics") {
      renderAGUIQuickPrompts();
      loadDiagnosticsRCA();
    } else {
      switchView("diagnostics");
    }
    return true;
  }
  if (payload.action === "inspect_pod" && payload.pod_name) {
    openPodInspectorModal(payload.pod_name, payload.namespace || "default");
    return true;
  }
  if (payload.action === "open_logs" && payload.pod_name) {
    openLogsModal(payload.pod_name, payload.namespace || "default");
    return true;
  }
  if (payload.action === "open_manifest" && payload.kind && payload.name) {
    openManifestModal(payload.kind, payload.name, payload.namespace || "default");
    return true;
  }
  return false;
}

function summarizeUIAction(payload) {
  if (!payload) return { title: "Portal Action", meta: "" };
  switch (payload.action) {
    case "navigate":
      return {
        title: `Navigate -> ${payload.view}`,
        meta: payload.namespace ? `namespace: ${payload.namespace}` : "all namespaces",
      };
    case "filter_12step":
      return {
        title: "Filter 12-Step kubectl apply Machine",
        meta: [
          payload.namespace ? `ns: ${payload.namespace}` : "ns: all",
          payload.step_number ? `step: #${payload.step_number}` : "all steps",
        ].join(" · "),
      };
    case "inspect_pod":
      return {
        title: `Inspect Pod ${payload.pod_name}`,
        meta: `namespace: ${payload.namespace || "default"}`,
      };
    case "open_logs":
      return {
        title: `Open Pod Logs ${payload.pod_name}`,
        meta: `namespace: ${payload.namespace || "default"}`,
      };
    case "open_manifest":
      return {
        title: `Open Manifest ${payload.kind}/${payload.name}`,
        meta: `namespace: ${payload.namespace || "default"}`,
      };
    default:
      return { title: payload.action || "Portal Action", meta: "" };
  }
}

function getAGUIToolCallbacks() {
  return {
    getActiveView: () => state.activeView,
    onNavigate: (view, ns) => executePortalUIAction({ action: "navigate", view, namespace: ns ?? "" }),
    onInspectPod: (podName, ns) => openPodInspectorModal(podName, ns || "default"),
    onOpenLogs: (podName, ns) => openLogsModal(podName, ns || "default"),
    onOpenManifest: (kind, name, ns) => openManifestModal(kind, name, ns || "default"),
    onFilter12Step: (ns, step) =>
      executePortalUIAction({ action: "filter_12step", namespace: ns ?? "", step_number: step || null }),
    onOpenHelmHistory: (ns, name) => helmController.navigateToHistory(ns || "default", name),
    onOpenHelmDiff: (ns, name, fromRev, toRev, mode = "values") =>
      helmController.navigateToDiff(ns || "default", name, fromRev, toRev, mode),
    onAskAI: (prompt) => {
      if (!prompt) return;
      openAIChatPanel();
      sendAIChatMessage(prompt);
    },
  };
}

async function openAIDiagnoseModal({ namespace, resourceType, resourceName, question, title } = {}) {
  const modal = $("ai-diagnose-modal");
  const titleEl = $("ai-diagnose-title");
  const subtitleEl = $("ai-diagnose-subtitle");
  const body = $("ai-diagnose-body");
  if (!modal || !body) return;

  modal.hidden = false;
  titleEl.textContent = title || "Diagnose with AI (AG-UI Live Stream)";
  subtitleEl.textContent = [state.selectedClusterId, namespace, resourceType && resourceName ? `${resourceType}/${resourceName}` : null]
    .filter(Boolean).join(" · ");

  const toolTimeline = h("div", { className: "agui-tool-timeline" });
  const streamStatus = h("div", { className: "agui-stream-pulse" }, [
    h("span", { className: "agui-stream-dot" }),
    "Streaming ADK + MCP diagnosis via AG-UI SSE...",
  ]);
  const markdownHost = h("div", {});
  renderInto(body, h("div", { style: "display:flex;flex-direction:column;gap:10px;" }, [
    streamStatus,
    toolTimeline,
    markdownHost,
  ]));

  const defaultQuestion = [
    question || "Diagnose this resource, identify the root cause, and suggest exact next checks or remediations.",
    namespace ? `Target namespace: ${namespace}` : null,
    resourceType && resourceName ? `Target resource: ${resourceType}/${resourceName}` : null,
  ].filter(Boolean).join("\n");

  let accText = "";
  const toolChips = new Map();
  if (aguiDiagnoseAbortController) {
    aguiDiagnoseAbortController.abort();
  }
  aguiDiagnoseAbortController = new AbortController();

  try {
    await aguiClient.run({
      clusterId: state.selectedClusterId,
      state: getActivePortalState(),
      messages: [{ role: "user", content: defaultQuestion }],
      signal: aguiDiagnoseAbortController.signal,
      onEvent: (evt) => {
        if (evt.type === "TOOL_CALL_START") {
          const toolName = evt.toolCallName || "tool";
          const isUi = String(toolName).startsWith("ui_");
          const tc = { id: evt.toolCallId, name: toolName, protocol: isUi ? "ag-ui" : "mcp", argsText: "", resultText: "", status: "running", uiState: {} };
          const statusSpan = h("span", { className: "agui-tool-chip__status is-running" }, "running...");
          const scopeSpan = h("span", { className: "agui-tool-chip__scope" }, "");
          const bodyHost = h("div", { className: "agui-iv-host" });
          bodyHost.replaceChildren(renderInteractiveToolBody(tc, getAGUIToolCallbacks()));
          const chip = h("details", { className: "agui-tool-chip" }, [
            h("summary", { className: "agui-tool-chip__summary" }, [
              h("span", { className: "agui-tool-chip__left" }, [
                h("span", { className: `agui-tool-chip__tag agui-tool-chip__tag--${isUi ? "agui" : "mcp"}` }, isUi ? "AG-UI" : "MCP"),
                h("span", { className: "agui-tool-chip__name" }, toolName),
                scopeSpan,
              ]),
              statusSpan,
            ]),
            bodyHost,
          ]);
          toolChips.set(evt.toolCallId, { tc, statusSpan, scopeSpan, bodyHost });
          toolTimeline.appendChild(chip);
        } else if (evt.type === "TOOL_CALL_ARGS") {
          const rec = toolChips.get(evt.toolCallId);
          if (rec) {
            rec.tc.argsText += evt.delta || "";
            const scope = extractToolScopeLabel(safeJsonParse(rec.tc.argsText));
            if (scope) rec.scopeSpan.textContent = scope;
            rec.bodyHost.replaceChildren(renderInteractiveToolBody(rec.tc, getAGUIToolCallbacks()));
          }
        } else if (evt.type === "TOOL_CALL_RESULT") {
          const rec = toolChips.get(evt.toolCallId);
          if (rec) {
            rec.tc.resultText = evt.content || "";
            const resObj = safeJsonParse(rec.tc.resultText);
            const argsObj = safeJsonParse(rec.tc.argsText);
            const isErr = Boolean(resObj && (resObj.ok === false || resObj.error));
            rec.tc.status = isErr ? "error" : "done";
            rec.statusSpan.className = `agui-tool-chip__status ${isErr ? "is-error" : "is-done"}`;
            rec.statusSpan.textContent = summarizeToolResult(rec.tc.name, argsObj, resObj, rec.tc.status);
            rec.bodyHost.replaceChildren(renderInteractiveToolBody(rec.tc, getAGUIToolCallbacks()));
          }
        } else if (evt.type === "TEXT_MESSAGE_CONTENT") {
          accText += evt.delta || "";
          renderInto(markdownHost, renderMarkdownNode(accText));
        } else if (evt.type === "RUN_FINISHED") {
          streamStatus.remove();
          if (!accText.trim()) renderInto(markdownHost, renderMarkdownNode("Diagnosis completed."));
        } else if (evt.type === "RUN_ERROR") {
          streamStatus.remove();
          throw new Error(evt.message || "AG-UI diagnose stream failed.");
        }
      },
    });
  } catch (err) {
    if (err.name === "AbortError") return;
    renderInto(body, errorState(err, () => openAIDiagnoseModal({ namespace, resourceType, resourceName, question, title })));
  } finally {
    aguiDiagnoseAbortController = null;
  }
}

function closeAIDiagnoseModal() {
  if (aguiDiagnoseAbortController) {
    aguiDiagnoseAbortController.abort();
    aguiDiagnoseAbortController = null;
  }
  const modal = $("ai-diagnose-modal");
  if (modal) modal.hidden = true;
}

// -- Ask AI interactive AG-UI chat panel (ADK <-> AG-UI <-> MCP) -----------

function renderToolCallChip(tc) {
  const isUi = tc.protocol === "ag-ui";
  const argsObj = safeJsonParse(tc.argsText);
  const resObj = safeJsonParse(tc.resultText);
  const scopeLabel = extractToolScopeLabel(argsObj);
  const statusClass = tc.status === "error" ? "is-error" : tc.status === "done" ? "is-done" : "is-running";
  const statusLabel = summarizeToolResult(tc.name, argsObj, resObj, tc.status);

  const detailsNode = h("details", {
    className: "agui-tool-chip",
    open: Boolean(tc.expanded),
    ontoggle: (e) => {
      tc.expanded = e.target.open;
    },
  }, [
    h("summary", { className: "agui-tool-chip__summary" }, [
      h("span", { className: "agui-tool-chip__left" }, [
        h("span", { className: `agui-tool-chip__tag agui-tool-chip__tag--${isUi ? "agui" : "mcp"}` }, isUi ? "AG-UI" : "MCP"),
        h("span", { className: "agui-tool-chip__name" }, tc.name || "tool"),
        scopeLabel ? h("span", { className: "agui-tool-chip__scope" }, scopeLabel) : null,
      ].filter(Boolean)),
      h("span", { className: `agui-tool-chip__status ${statusClass}` }, statusLabel),
    ]),
    h("div", { className: "agui-iv-host" }, renderInteractiveToolBody(tc, getAGUIToolCallbacks())),
  ]);
  return detailsNode;
}

function renderUIActionCard(act) {
  const info = summarizeUIAction(act.payload);
  return h("div", { className: "agui-action-card" }, [
    h("div", { className: "agui-action-card__info" }, [
      h("div", { className: "agui-action-card__title" }, info.title),
      info.meta ? h("div", { className: "agui-action-card__meta" }, info.meta) : null,
    ]),
    h("button", {
      type: "button",
      className: `btn btn--sm ${act.applied ? "btn--ghost" : "btn--primary"}`,
      onclick: () => {
        executePortalUIAction(act.payload);
        act.applied = true;
        renderAIChatMessages();
      },
    }, act.applied ? "Re-run UI Action" : "Apply UI Action"),
  ]);
}

function renderHitlApprovalCard(hitl) {
  const targetText = `${hitl.workload_type}/${hitl.name} (ns: ${hitl.namespace}${hitl.to_revision ? `, rev: ${hitl.to_revision}` : ", previous revision"})`;
  const pending = hitl.status === "pending";
  const executing = hitl.status === "executing";
  const operatorAllowed = canOperate();

  let actionArea;
  if (pending || executing) {
    if (!operatorAllowed) {
      actionArea = h("div", { className: "agui-hitl-card__status", style: "color:var(--danger);" },
        `Viewer role (${state.userRole}) cannot execute rollback. Operator or Admin role required.`);
    } else {
      actionArea = h("div", { className: "agui-hitl-card__actions" }, [
        h("button", {
          type: "button",
          className: "btn btn--primary btn--sm",
          disabled: executing,
          onclick: () => handleHitlDecision(hitl, true),
        }, executing ? "Rolling back..." : "Approve Rollback"),
        h("button", {
          type: "button",
          className: "btn btn--ghost btn--sm",
          disabled: executing,
          onclick: () => handleHitlDecision(hitl, false),
        }, "Reject"),
      ]);
    }
  } else {
    const color = hitl.status === "approved" ? "#16a34a" : "var(--danger)";
    const prefix = hitl.status === "approved" ? "Approved & executed" : hitl.status === "rejected" ? "Rejected" : "Failed";
    actionArea = h("div", { className: "agui-hitl-card__status", style: `color:${color};` },
      `${prefix}${hitl.outcomeMessage ? ` — ${hitl.outcomeMessage}` : ""}`);
  }

  return h("div", { className: "agui-hitl-card" }, [
    h("div", { className: "agui-hitl-card__header" }, [
      h("span", {}, "Human-in-the-Loop Rollback Confirmation"),
      h("span", { className: "agui-badge agui-badge--state" }, `RBAC: ${(state.userRole || "viewer").toUpperCase()}`),
    ]),
    h("div", { className: "agui-hitl-card__target" }, targetText),
    hitl.reason ? h("div", { className: "agui-hitl-card__reason" }, hitl.reason) : null,
    actionArea,
  ]);
}

function renderAIChatMessages() {
  const container = $("ai-chat-messages");
  if (!container) return;
  if (state.aiChatMessages.length === 0) {
    renderInto(container, h("div", { className: "ai-chat-panel__empty", id: "ai-chat-empty" },
      "Ask about the active cluster — streams live via AG-UI (ADK <-> MCP) and can navigate views, open inspectors, or stage guarded rollbacks."));
    return;
  }

  container.replaceChildren(...state.aiChatMessages.map((m) => {
    if (m.role === "assistant") {
      const children = [];
      if (m.streaming) {
        children.push(h("div", { className: "agui-stream-pulse", style: "margin-bottom:6px;" }, [
          h("span", { className: "agui-stream-dot" }),
          m.stepName ? `Streaming (${m.stepName})...` : "Streaming response...",
        ]));
      }
      if (m.toolCalls && m.toolCalls.length > 0) {
        children.push(h("div", { className: "agui-tool-timeline" }, m.toolCalls.map(renderToolCallChip)));
      }
      const mdHost = h("div", {}, m.text ? [renderMarkdownNode(m.text)] : []);
      m._mdHost = mdHost;
      children.push(mdHost);
      if (m.uiActions && m.uiActions.length > 0) {
        children.push(...m.uiActions.map(renderUIActionCard));
      }
      if (m.hitlRequests && m.hitlRequests.length > 0) {
        children.push(...m.hitlRequests.map(renderHitlApprovalCard));
      }
      return h("div", { className: "ai-chat-msg ai-chat-msg--assistant" }, children);
    }
    return h("div", { className: `ai-chat-msg ai-chat-msg--${m.role}` }, m.text);
  }));
  container.scrollTop = container.scrollHeight;
}

function buildPortalQuickPrompts() {
  const { activeView, namespace } = getActivePortalState();
  const nsClause = namespace ? ` in namespace '${namespace}'` : "";

  const catalog = {
    namespaces: {
      id: "namespaces",
      label: "List Namespaces",
      prompt: "List all active namespaces and workload counts in this cluster, then open the Namespaces view.",
    },
    services: {
      id: "services",
      label: "Services & Ingress",
      prompt: `List services and ingress routes${nsClause}, flag any service with 0 ready endpoints, and switch to Network Topology.`,
    },
    helm: {
      id: "helm",
      label: "Helm Releases",
      prompt: "List all Helm releases, charts, and deployment status in this cluster, and switch to the Helm view.",
    },
    diagnostics: {
      id: "diagnostics",
      label: "12-Step Diagnostics",
      prompt: `Summarize any unhealthy pods or stalled workloads across the 12-Step kubectl apply Machine${nsClause}, and open the Diagnostics view.`,
    },
    workloads: {
      id: "workloads",
      label: "Unhealthy Workloads",
      prompt: `List Deployments, StatefulSets, DaemonSets, and unhealthy pods${nsClause}, and switch to Workloads.`,
    },
    events: {
      id: "events",
      label: "Recent Events",
      prompt: `Summarize recent Warning events${nsClause} and switch to the Events view.`,
    },
    nodes: {
      id: "nodes",
      label: "Node Capacity",
      prompt: "Summarize node readiness, taints, and CPU/memory pressure, and switch to Nodes.",
    },
    storage: {
      id: "storage",
      label: "Storage & PVCs",
      prompt: `Check PersistentVolumeClaims and StorageClasses${nsClause}, flag any Pending PVCs, and switch to Storage Topology.`,
    },
    cost: {
      id: "cost",
      label: "Cost Estimate",
      prompt: "Summarize estimated cluster compute/storage cost and highlight optimization opportunities, then switch to Cost.",
    },
    ecosystem: {
      id: "ecosystem",
      label: "Ecosystem CRDs",
      prompt: "List detected platform ecosystem operators/CRDs (cert-manager, Argo CD, KEDA, etc.) and switch to Ecosystem.",
    },
  };

  const viewPriorities = {
    helm: ["helm", "services", "workloads", "namespaces", "events", "diagnostics"],
    "helm-history": ["helm", "workloads", "events", "services", "namespaces", "diagnostics"],
    "helm-diff": ["helm", "workloads", "diagnostics", "services", "events", "namespaces"],
    network: ["services", "diagnostics", "namespaces", "workloads", "events", "helm"],
    diagnostics: ["diagnostics", "workloads", "services", "events", "helm", "namespaces"],
    workloads: ["workloads", "diagnostics", "services", "namespaces", "events", "helm"],
    applications: ["services", "workloads", "helm", "namespaces", "diagnostics", "events"],
    namespaces: ["namespaces", "services", "helm", "workloads", "diagnostics", "events"],
    events: ["events", "diagnostics", "workloads", "services", "namespaces", "helm"],
    nodes: ["nodes", "workloads", "cost", "diagnostics", "namespaces", "events"],
    storage: ["storage", "workloads", "diagnostics", "namespaces", "events", "cost"],
    cost: ["cost", "nodes", "workloads", "namespaces", "helm", "diagnostics"],
    ecosystem: ["ecosystem", "helm", "services", "namespaces", "diagnostics", "events"],
  };

  const defaultOrder = ["namespaces", "services", "helm", "diagnostics", "events", "workloads", "nodes", "storage", "cost", "ecosystem"];
  const orderedIds = Array.from(new Set([...(viewPriorities[activeView] || []), ...defaultOrder]));

  const prompts = [];
  if (namespace) {
    prompts.push({
      id: "active-ns",
      label: `Inspect ${namespace}`,
      prompt: `Summarize pods, services, and any warning events in namespace '${namespace}', and filter the 12-Step kubectl apply Machine to '${namespace}'.`,
    });
  }
  for (const id of orderedIds) {
    if (catalog[id]) prompts.push(catalog[id]);
    if (prompts.length >= 6) break;
  }
  return prompts;
}

function renderAGUIQuickPrompts() {
  const container = $("agui-quick-prompts");
  if (!container) return;
  const items = buildPortalQuickPrompts();
  container.replaceChildren(
    ...items.map((item) =>
      h(
        "button",
        {
          type: "button",
          className: "agui-quick-chip",
          "data-prompt": item.prompt,
          title: item.prompt,
        },
        item.label
      )
    )
  );
}

function updateAIChatSubtitle() {
  const el = $("ai-chat-subtitle");
  if (el) el.textContent = state.selectedClusterId ? `Scoped to ${state.selectedClusterId} · AG-UI` : "No cluster selected";
}

const AI_PANEL_WIDTH_KEY = "kubeorbit_ai_panel_width";
const AI_PANEL_DEFAULT_WIDTH = 440;
const AI_PANEL_WIDE_WIDTH = 640;

function clampAIPanelWidth(px) {
  const maxW = Math.max(360, Math.min(760, Math.round(window.innerWidth * 0.58)));
  return Math.max(320, Math.min(maxW, Math.round(Number(px) || AI_PANEL_DEFAULT_WIDTH)));
}

function setAIPanelWidth(px, { persist = true } = {}) {
  const w = clampAIPanelWidth(px);
  document.documentElement.style.setProperty("--ai-panel-width", `${w}px`);
  const panel = $("ai-chat-panel");
  if (panel) panel.classList.toggle("ai-chat-panel--wide", w >= 560);
  if (persist) localStorage.setItem(AI_PANEL_WIDTH_KEY, String(w));
  return w;
}

function notifyLayoutReflow() {
  moveNavIndicator(state.activeView);
  window.dispatchEvent(new Event("resize"));
  setTimeout(() => {
    moveNavIndicator(state.activeView);
    window.dispatchEvent(new Event("resize"));
  }, 260);
}

function openAIChatPanel() {
  const panel = $("ai-chat-panel");
  if (!panel) return;
  panel.classList.add("is-open");
  panel.setAttribute("aria-hidden", "false");
  document.body.classList.add("ai-chat-open");
  updateAIChatSubtitle();
  renderAGUIQuickPrompts();
  renderAIChatMessages();
  notifyLayoutReflow();
  $("ai-chat-input")?.focus();
}

function closeAIChatPanel() {
  const panel = $("ai-chat-panel");
  if (!panel) return;
  panel.classList.remove("is-open");
  panel.setAttribute("aria-hidden", "true");
  document.body.classList.remove("ai-chat-open");
  notifyLayoutReflow();
}

function toggleAIChatPanel() {
  const panel = $("ai-chat-panel");
  if (panel && panel.classList.contains("is-open")) closeAIChatPanel();
  else openAIChatPanel();
}

function resetAIChat() {
  if (aguiAbortController) {
    aguiAbortController.abort();
    aguiAbortController = null;
  }
  state.aiChatSessionId = null;
  state.aiChatMessages = [];
  const stopBtn = $("ai-chat-stop");
  const sendBtn = $("ai-chat-send");
  if (stopBtn) stopBtn.hidden = true;
  if (sendBtn) sendBtn.disabled = false;
  if ($("ai-chat-panel")?.classList.contains("is-open")) {
    updateAIChatSubtitle();
    renderAGUIQuickPrompts();
    renderAIChatMessages();
  }
}

async function runAGUITurn(messagesPayload) {
  const sendBtn = $("ai-chat-send");
  const stopBtn = $("ai-chat-stop");
  if (!state.selectedClusterId) return;

  if (aguiAbortController) {
    aguiAbortController.abort();
  }
  aguiAbortController = new AbortController();
  if (sendBtn) sendBtn.disabled = true;
  if (stopBtn) stopBtn.hidden = false;

  const assistantMsg = {
    role: "assistant",
    text: "",
    streaming: true,
    stepName: "connecting",
    toolCalls: [],
    uiActions: [],
    hitlRequests: [],
    _mdHost: null,
  };
  state.aiChatMessages.push(assistantMsg);
  renderAIChatMessages();

  const autoSyncEnabled = () => Boolean($("ai-chat-autosync")?.checked);
  const findToolCall = (id) => assistantMsg.toolCalls.find((tc) => tc.id === id);
  const dropIfEmptyAssistant = () => {
    const empty =
      !assistantMsg.text &&
      assistantMsg.toolCalls.length === 0 &&
      assistantMsg.uiActions.length === 0 &&
      assistantMsg.hitlRequests.length === 0;
    if (empty) {
      const idx = state.aiChatMessages.indexOf(assistantMsg);
      if (idx !== -1) state.aiChatMessages.splice(idx, 1);
    }
  };

  try {
    await aguiClient.run({
      clusterId: state.selectedClusterId,
      threadId: state.aiChatSessionId || "",
      state: getActivePortalState(),
      messages: messagesPayload,
      tools: PORTAL_FRONTEND_TOOLS,
      signal: aguiAbortController.signal,
      onEvent: (evt) => {
        const container = $("ai-chat-messages");
        switch (evt.type) {
          case "RUN_STARTED":
            if (evt.threadId) state.aiChatSessionId = evt.threadId;
            assistantMsg.stepName = "ADK + MCP";
            renderAIChatMessages();
            break;
          case "STEP_STARTED":
            assistantMsg.stepName = evt.stepName || "reasoning";
            renderAIChatMessages();
            break;
          case "TOOL_CALL_START": {
            const name = evt.toolCallName || "tool";
            assistantMsg.toolCalls.push({
              id: evt.toolCallId,
              name,
              protocol: name.startsWith("ui_") ? "ag-ui" : "mcp",
              argsText: "",
              resultText: "",
              status: "running",
              expanded: false,
              uiState: {},
            });
            renderAIChatMessages();
            break;
          }
          case "TOOL_CALL_ARGS": {
            const tc = findToolCall(evt.toolCallId);
            if (tc) {
              tc.argsText += evt.delta || "";
              renderAIChatMessages();
            }
            break;
          }
          case "TOOL_CALL_END": {
            const tc = findToolCall(evt.toolCallId);
            if (tc && tc.status === "running") tc.status = "done";
            renderAIChatMessages();
            break;
          }
          case "TOOL_CALL_RESULT": {
            const tc = findToolCall(evt.toolCallId);
            if (tc) {
              tc.resultText = evt.content || "";
              try {
                const parsed = JSON.parse(tc.resultText);
                tc.status = parsed && (parsed.ok === false || parsed.error) ? "error" : "done";
              } catch {
                tc.status = "done";
              }
            }
            renderAIChatMessages();
            break;
          }
          case "CUSTOM":
            if (evt.name === "kubeorbit:ui_action" && evt.value) {
              const applied = autoSyncEnabled() ? executePortalUIAction(evt.value) : false;
              assistantMsg.uiActions.push({ payload: evt.value, applied });
              renderAIChatMessages();
            } else if (evt.name === "kubeorbit:hitl_approval" && evt.value) {
              assistantMsg.hitlRequests.push({
                ...evt.value,
                status: "pending",
                outcomeMessage: "",
              });
              renderAIChatMessages();
            }
            break;
          case "STATE_DELTA": {
            const badge = $("agui-state-badge");
            if (badge && Array.isArray(evt.delta) && evt.delta.length > 0) {
              const viewOp = evt.delta.find((op) => op.path === "/activeView");
              if (viewOp) badge.textContent = `UI Synced -> ${viewOp.value}`;
            }
            break;
          }
          case "TEXT_MESSAGE_CONTENT":
            assistantMsg.text += evt.delta || "";
            if (assistantMsg._mdHost) {
              renderInto(assistantMsg._mdHost, renderMarkdownNode(assistantMsg.text));
              if (container) container.scrollTop = container.scrollHeight;
            } else {
              renderAIChatMessages();
            }
            break;
          case "RUN_FINISHED":
            if (evt.threadId) state.aiChatSessionId = evt.threadId;
            assistantMsg.streaming = false;
            if (!assistantMsg.text && assistantMsg.toolCalls.length === 0) {
              assistantMsg.text = "Done.";
            }
            renderAIChatMessages();
            break;
          case "RUN_ERROR":
            assistantMsg.streaming = false;
            dropIfEmptyAssistant();
            state.aiChatMessages.push({ role: "error", text: evt.message || "AG-UI run error." });
            renderAIChatMessages();
            break;
          default:
            break;
        }
      },
    });
  } catch (err) {
    assistantMsg.streaming = false;
    if (err.name === "AbortError") {
      if (!assistantMsg.text) assistantMsg.text = "_Stream stopped by user._";
    } else {
      dropIfEmptyAssistant();
      state.aiChatMessages.push({ role: "error", text: err.message || "AG-UI stream failed." });
    }
  } finally {
    assistantMsg.streaming = false;
    aguiAbortController = null;
    if (sendBtn) sendBtn.disabled = false;
    if (stopBtn) stopBtn.hidden = true;
    renderAIChatMessages();
    $("ai-chat-input")?.focus();
  }
}

async function handleHitlDecision(hitl, approved) {
  if (!state.selectedClusterId) return;
  if (!canOperate()) {
    hitl.status = "failed";
    hitl.outcomeMessage = `Role '${state.userRole}' lacks Operator/Admin permission.`;
    renderAIChatMessages();
    return;
  }

  if (!approved) {
    hitl.status = "rejected";
    hitl.outcomeMessage = "Operator declined rollback.";
    renderAIChatMessages();
    await runAGUITurn([
      {
        role: "tool",
        toolCallId: hitl.toolCallId || "hitl-rollback",
        content: JSON.stringify({
          ok: false,
          approved: false,
          message: `Operator (${state.userSubject}) rejected rollback of ${hitl.workload_type}/${hitl.name} in namespace ${hitl.namespace}.`,
        }),
      },
    ]);
    return;
  }

  hitl.status = "executing";
  renderAIChatMessages();
  try {
    const res = await api.rollbackWorkload(
      state.selectedClusterId,
      hitl.namespace,
      hitl.workload_type,
      hitl.name,
      hitl.to_revision || null
    );
    hitl.status = "approved";
    hitl.outcomeMessage = res.message || `Rolled back ${hitl.workload_type}/${hitl.name}`;
    renderAIChatMessages();
    if (state.activeView === "workloads" || state.activeView === "diagnostics" || state.activeView === "overview") {
      loadActiveView();
    }
    await runAGUITurn([
      {
        role: "tool",
        toolCallId: hitl.toolCallId || "hitl-rollback",
        content: JSON.stringify({ ok: true, approved: true, rollback_result: res }),
      },
    ]);
  } catch (err) {
    hitl.status = "failed";
    hitl.outcomeMessage = err.message || "Rollback failed.";
    renderAIChatMessages();
  }
}

async function sendAIChatMessage(overridePrompt = null) {
  const input = $("ai-chat-input");
  const sendBtn = $("ai-chat-send");
  const message = (overridePrompt !== null ? String(overridePrompt) : (input?.value || "")).trim();
  if (!message || !state.selectedClusterId || sendBtn?.disabled) return;

  state.aiChatMessages.push({ role: "user", text: message });
  if (overridePrompt === null && input) {
    input.value = "";
    input.style.height = "auto";
  }
  renderAIChatMessages();
  await runAGUITurn([{ role: "user", content: message }]);
}

function initAIChatPanel() {
  const savedWidth = localStorage.getItem(AI_PANEL_WIDTH_KEY);
  setAIPanelWidth(savedWidth ? Number(savedWidth) : AI_PANEL_DEFAULT_WIDTH, { persist: false });

  const providerSelect = $("ai-provider-select");
  const providerNotice = $("ai-provider-placeholder-note");
  const syncProviderNotice = () => {
    if (!providerSelect || !providerNotice) return;
    const val = providerSelect.value;
    if (val === "claude") {
      providerNotice.innerHTML =
        "<strong>Anthropic Claude (Placeholder):</strong> Wired via Google ADK LiteLLM bridge. Install <code>litellm</code> and set <code>KUBEORBIT_AI_PROVIDER=claude</code> + <code>ANTHROPIC_API_KEY</code> to activate.";
      providerNotice.hidden = false;
    } else if (val === "openai") {
      providerNotice.innerHTML =
        "<strong>OpenAI (Placeholder):</strong> Wired via Google ADK LiteLLM bridge. Install <code>litellm</code> and set <code>KUBEORBIT_AI_PROVIDER=openai</code> + <code>OPENAI_API_KEY</code> to activate.";
      providerNotice.hidden = false;
    } else {
      providerNotice.hidden = true;
      providerNotice.textContent = "";
    }
  };
  providerSelect?.addEventListener("change", syncProviderNotice);
  syncProviderNotice();

  $("ai-chat-fab")?.addEventListener("click", toggleAIChatPanel);
  $("ai-chat-close")?.addEventListener("click", closeAIChatPanel);
  $("ai-chat-send")?.addEventListener("click", () => sendAIChatMessage());
  $("ai-chat-stop")?.addEventListener("click", () => {
    if (aguiAbortController) aguiAbortController.abort();
  });
  $("ai-chat-clear")?.addEventListener("click", () => {
    resetAIChat();
    $("ai-chat-input")?.focus();
  });
  $("ai-chat-expand")?.addEventListener("click", () => {
    const panel = $("ai-chat-panel");
    const isWide = panel?.classList.contains("ai-chat-panel--wide");
    setAIPanelWidth(isWide ? AI_PANEL_DEFAULT_WIDTH : AI_PANEL_WIDE_WIDTH);
    notifyLayoutReflow();
  });

  const resizer = $("ai-chat-resizer");
  if (resizer) {
    let dragging = false;
    resizer.addEventListener("pointerdown", (e) => {
      if (window.innerWidth < 1025) return;
      dragging = true;
      document.body.classList.add("ai-chat-resizing");
      try { resizer.setPointerCapture(e.pointerId); } catch {}
      e.preventDefault();
    });
    const stopDrag = (e) => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("ai-chat-resizing");
      const current = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--ai-panel-width"), 10);
      setAIPanelWidth(current || AI_PANEL_DEFAULT_WIDTH, { persist: true });
      notifyLayoutReflow();
      try { if (e && resizer.hasPointerCapture?.(e.pointerId)) resizer.releasePointerCapture(e.pointerId); } catch {}
    };
    window.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      setAIPanelWidth(window.innerWidth - e.clientX, { persist: false });
    });
    window.addEventListener("pointerup", stopDrag);
    window.addEventListener("pointercancel", stopDrag);
  }

  renderAGUIQuickPrompts();
  $("agui-quick-prompts")?.addEventListener("click", (e) => {
    const chip = e.target.closest(".agui-quick-chip");
    if (!chip) return;
    const prompt = chip.dataset.prompt;
    if (prompt) sendAIChatMessage(prompt);
  });

  const input = $("ai-chat-input");
  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendAIChatMessage();
    }
  });
  input?.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("ai-chat-panel")?.classList.contains("is-open")) closeAIChatPanel();
  });
}

let resourceMapController = null;

function getResourceMapController() {
  if (!resourceMapController) {
    resourceMapController = new ResourceMapController({
      container: $("resourcemap-canvas-container"),
      svg: $("resourcemap-svg"),
      drawer: $("resourcemap-drawer"),
      onNodeAction: (action, node) => {
        const meta = node.meta || {};
        const ns = meta.namespace || $("resourcemap-namespace-select")?.value || "default";
        if (action === "yaml") {
          openManifestModal(node.kind || node.group || "Resource", node.label, ns);
        } else if (action === "logs") {
          openLogsModal(node.label, ns);
        } else if (action === "inspect") {
          openPodInspectorModal(node.label, ns);
        }
      }
    });

    $("resourcemap-type-select")?.addEventListener("change", () => {
      resourceMapController.updateResourceSelectors();
    });

    $("resourcemap-analyze-btn")?.addEventListener("click", () => {
      const rootId = $("resourcemap-resource-select")?.value || "";
      const depth = Number($("resourcemap-depth-select")?.value || 3);
      resourceMapController.filterAndRender(rootId, depth);
    });

    $("resourcemap-namespace-select")?.addEventListener("change", () => {
      renderAGUIQuickPrompts();
      if (state.activeView === "resourcemap") loadResourceMap();
    });
  }
  return resourceMapController;
}

async function loadResourceMap(force = false) {
  const controller = getResourceMapController();
  const clusterId = state.selectedClusterId;
  const ns = $("resourcemap-namespace-select")?.value || "default";
  await loadWithSWR({
    key: `nsmap:${clusterId}:${ns}`,
    tokenView: "resourcemap",
    container: null,
    force,
    showSkeleton: false,
    fetchFn: () => api.getNamespaceMap(clusterId, ns),
    renderFn: (graph) => {
      controller.setGraphData(graph);
      const rootId = $("resourcemap-resource-select")?.value || "";
      const depth = Number($("resourcemap-depth-select")?.value || 3);
      const key = `${clusterId}:${ns}:${rootId}:${depth}`;
      const animate = state.lastTopologyKey.resourcemap !== key;
      state.lastTopologyKey.resourcemap = key;
      controller.filterAndRender(rootId, depth, animate);
      controller.resetView();
    },
    onError: (err) => {
      console.error("Resource Map load error:", err);
    },
  });
}

const viewLoaders = {
  overview: loadOverview,
  clusters: loadClustersView,
  applications: loadLogicalApplications,
  workloads: loadWorkloads,
  diagnostics: loadDiagnosticsRCA,
  audit: loadAuditLogsView,
  nodes: loadNodesView,
  namespaces: loadNamespaces,
  resourcemap: loadResourceMap,
  map: loadClusterMap,
  network: loadNetworkTopology,
  storage: loadStorageTopology,
  events: loadEvents,
  helm: (force) => helmController.loadHelmView(force),
  "helm-history": (force) => helmController.loadHelmHistoryView(force),
  "helm-diff": (force) => helmController.loadHelmDiffView(force),
  ecosystem: loadEcosystemView,
  cost: loadCostView,
};


async function loadActiveView(opts = {}) {
  const loader = viewLoaders[state.activeView];
  if (!loader || !state.selectedClusterId) return;
  const force = Boolean(opts && opts.force);
  await loader(force);
  updateLastUpdated();
  loadSidebarCounts(force); // fire-and-forget -- throttled to once per 60s unless forced
}

// Live resource counts next to Nodes/Namespaces/Pods/Workload Catalog in the
// sidebar (Radar-style), reusing endpoints already fetched elsewhere in the
// app rather than adding new ones: get_workloads_overview already returns
// pod + all-workload-type totals in one call, get_nodes for node count.
// Namespace count is populated separately by populateNamespaceSelects(),
// which already fetches the namespace list for the selectors.
function setSidebarCount(id, value) {
  const el = $(id);
  if (el) el.textContent = value == null ? "" : String(value);
}

async function loadSidebarCounts(force = false) {
  if (!state.selectedClusterId) return;
  const clusterId = state.selectedClusterId;
  if (
    !force &&
    sidebarCountsCache.clusterId === clusterId &&
    Date.now() - sidebarCountsCache.ts < SIDEBAR_COUNTS_TTL_MS
  ) {
    return;
  }
  try {
    const nodes = await api.getNodes(clusterId);
    if (state.selectedClusterId !== clusterId) return; // cluster changed mid-flight
    recordNodesSidebarCount(clusterId, nodes.count ?? nodes.items?.length);
  } catch (_) {
    // Sidebar counts are a nice-to-have, not worth surfacing an error banner for.
  }
}

// -- sidebar nav-group accordion --------------------------------------------

function setNavGroupCollapsed(itemsEl, collapsed) {
  itemsEl.classList.toggle("is-collapsed", collapsed);
  const caret = itemsEl.previousElementSibling?.querySelector(".nav-group__caret");
  if (caret) caret.style.transform = collapsed ? "rotate(-90deg)" : "rotate(0deg)";
}

function collapseOtherNavGroups(exceptItemsEl) {
  document.querySelectorAll(".nav-group__items").forEach((el) => {
    if (el !== exceptItemsEl) setNavGroupCollapsed(el, true);
  });
}

// Whichever group contains the view being switched to must be visible --
// otherwise the accordion could hide the user's own current location (e.g.
// the group auto-collapsed itself while its item was still the active one).
function expandNavGroupForActiveView(view) {
  const activeBtn = document.querySelector(`.nav-group__items .sidebar-item[data-view="${view}"]`);
  const itemsEl = activeBtn?.closest(".nav-group__items");
  if (!itemsEl) return;
  collapseOtherNavGroups(itemsEl);
  setNavGroupCollapsed(itemsEl, false);
}

function moveNavIndicator(view) {
  // Legacy indicator check
  const group = $("nav-tabs-group");
  if (!group) return;
  const indicator = $("nav-tabs-indicator");
  const btn = group.querySelector(`.nav-tabs__item[data-view="${view}"]`);
  if (!btn || !indicator) return;
  indicator.style.width = `${btn.offsetWidth}px`;
  indicator.style.transform = `translateX(${btn.offsetLeft - 4}px)`;
}

function switchView(view) {
  if (!viewLoaders[view]) return;
  hideNodeTooltip(); // a topology view's hover tooltip would otherwise float, stuck, over an unrelated page
  state.activeView = view;
  renderAGUIQuickPrompts();
  if (!aguiAbortController && state.aiChatMessages.length > 0 && $("ai-chat-panel")?.classList.contains("is-open")) {
    renderAIChatMessages();
  }
  document.querySelectorAll(".nav-tabs__item, .sidebar-item").forEach((btn) => btn.setAttribute("aria-selected", String(btn.dataset.view === view)));
  const viewAllBtn = $("view-all-clusters-btn");
  if (viewAllBtn) {
    if (view === "clusters") {
      viewAllBtn.style.background = "var(--accent-soft)";
      viewAllBtn.style.borderColor = "var(--accent)";
      viewAllBtn.style.color = "var(--accent)";
    } else {
      viewAllBtn.style.background = "var(--bg-inset)";
      viewAllBtn.style.borderColor = "var(--border)";
      viewAllBtn.style.color = "var(--fg-muted)";
    }
  }
  document.querySelectorAll(".view").forEach((section) => (section.hidden = section.dataset.view !== view));
  // The resource map wants every pixel it can get; #main's own 1.25rem
  // padding otherwise shrinks the canvas on all four sides on top of the
  // drawer/sidebar already competing for width.
  const mainEl = $("main");
  if (mainEl) mainEl.style.padding = view === "resourcemap" ? "0" : "1.25rem";
  moveNavIndicator(view);
  expandNavGroupForActiveView(view);
  loadActiveView();
}

// -- auto refresh -----------------------------------------------------------

function restoreRefreshInterval() {
  const stored = localStorage.getItem("kubeorbit_refresh_interval");
  state.refreshIntervalSec = stored !== null ? Number(stored) : 30;
  $("refresh-select").value = String(state.refreshIntervalSec);
}

function startAutoRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  if (!state.refreshIntervalSec) return;
  state.refreshTimer = setInterval(() => {
    if (document.visibilityState === "visible" && state.selectedClusterId) loadActiveView({ force: true });
  }, state.refreshIntervalSec * 1000);
}

// -- bootstrap ---------------------------------------------------------------

async function fetchStatusWithApiKeyPrompt() {
  try {
    return await api.getStatus();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401 && !getStoredApiKey()) {
      const key = window.prompt("This KubeOrbit deployment requires an API key. Enter it now:");
      if (key) {
        setStoredApiKey(key.trim());
        return await api.getStatus();
      }
    }
    throw err;
  }
}

function wireStaticControls() {
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("refresh-select").addEventListener("change", (e) => {
    state.refreshIntervalSec = Number(e.target.value);
    localStorage.setItem("kubeorbit_refresh_interval", String(state.refreshIntervalSec));
    startAutoRefresh();
  });
  $("refresh-now").addEventListener("click", async () => {
    const btn = $("refresh-now");
    if (btn) btn.disabled = true;
    try {
      if (state.selectedClusterId) {
        await api.refreshClusterCache(state.selectedClusterId).catch(() => {});
      }
      clearViewCache();
      helmController.resetHelmState();
      await loadActiveView({ force: true });
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  // Sidebar navigation click handler
  const sidebarNav = $("sidebar-nav");
  if (sidebarNav) {
    sidebarNav.addEventListener("click", (e) => {
      const btn = e.target.closest(".nav-tabs__item");
      if (btn && btn.dataset.view) switchView(btn.dataset.view);
      
      const groupHeader = e.target.closest(".nav-group__header");
      if (groupHeader) {
        const items = groupHeader.nextElementSibling;
        if (items) {
          const wasCollapsed = items.classList.contains("is-collapsed");
          if (wasCollapsed) collapseOtherNavGroups(items);
          setNavGroupCollapsed(items, !wasCollapsed);
          if (wasCollapsed && groupHeader.dataset.defaultView) {
            switchView(groupHeader.dataset.defaultView);
          }
        }
      }
    });
  }

  $("team-select")?.addEventListener("change", () => {
    populateEnvSelect();
    populateClusterSelect();
    onClusterChange();
  });
  $("env-select")?.addEventListener("change", () => {
    populateClusterSelect();
    onClusterChange();
  });
  $("cluster-select")?.addEventListener("change", onClusterChange);

  $("workloads-namespace-select")?.addEventListener("change", () => { renderAGUIQuickPrompts(); if (state.activeView === "workloads") loadWorkloads(); });
  // Search filters the already-fetched catalogData client-side -- no refetch needed.
  $("workloads-catalog-search")?.addEventListener("input", () => state.activeView === "workloads" && catalogTypeFilter && renderCatalogPanelBody());
  $("workloads-catalog-close")?.addEventListener("click", () => catalogTypeFilter && toggleWorkloadCatalogType(catalogTypeFilter));
  $("applications-namespace-select")?.addEventListener("change", () => { renderAGUIQuickPrompts(); if (state.activeView === "applications") loadLogicalApplications(); });
  $("diagnostics-namespace-select")?.addEventListener("change", () => {
    selectedPipelineStageId = null;
    selectedStepNumber = null;
    selectedK8sComponentId = null;
    renderAGUIQuickPrompts();
    if (state.activeView === "diagnostics") loadDiagnosticsRCA();
  });
  $("audit-type-select")?.addEventListener("change", () => state.activeView === "audit" && loadAuditLogsView());

  $("namespace-select")?.addEventListener("change", () => { renderAGUIQuickPrompts(); if (state.activeView === "namespaces") loadNamespaces(); });
  $("namespaces-include-system")?.addEventListener("change", async () => {
    await populateNamespaceSelects(state.selectedClusterId);
    renderAGUIQuickPrompts();
    if (state.activeView === "namespaces") loadNamespaces();
  });
  $("map-namespace-select")?.addEventListener("change", () => { renderAGUIQuickPrompts(); if (state.activeView === "map") loadNamespaceMap(); });
  $("network-namespace-select")?.addEventListener("change", () => { renderAGUIQuickPrompts(); if (state.activeView === "network") loadNetworkTopology(); });
  $("storage-namespace-select")?.addEventListener("change", () => { renderAGUIQuickPrompts(); if (state.activeView === "storage") loadStorageTopology(); });
  $("events-namespace-select")?.addEventListener("change", () => { renderAGUIQuickPrompts(); if (state.activeView === "events") loadEvents(); });

  $("resource-modal-close")?.addEventListener("click", closeResourceModal);
  $("resource-modal-cancel")?.addEventListener("click", closeResourceModal);
  $("resource-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "resource-modal") closeResourceModal();
  });

  // Logs & Manifest Modal event bindings
  $("logs-modal-close")?.addEventListener("click", closeLogsModal);
  $("log-refresh-btn")?.addEventListener("click", refreshLogs);
  $("log-lines-select")?.addEventListener("change", refreshLogs);
  $("manifest-modal-close")?.addEventListener("click", closeManifestModal);

  // Rollout, Timeline, Drift Diff & Pod Inspector Modal event bindings
  $("rollout-modal-close")?.addEventListener("click", closeRolloutModal);
  $("rollout-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "rollout-modal") closeRolloutModal();
  });
  $("timeline-modal-close")?.addEventListener("click", closeTimelineModal);
  $("timeline-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "timeline-modal") closeTimelineModal();
  });
  $("drift-modal-close")?.addEventListener("click", closeDriftDiffModal);
  $("drift-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "drift-modal") closeDriftDiffModal();
  });
  $("pod-inspector-close")?.addEventListener("click", closePodInspectorModal);
  $("pod-inspector-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "pod-inspector-modal") closePodInspectorModal();
  });

  $("ai-diagnose-close")?.addEventListener("click", closeAIDiagnoseModal);
  $("ai-diagnose-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "ai-diagnose-modal") closeAIDiagnoseModal();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("resource-modal").hidden) closeResourceModal();
    if (!$("onboard-modal").hidden) $("onboard-modal").hidden = true;
    if (!$("logs-modal").hidden) closeLogsModal();
    if (!$("manifest-modal").hidden) closeManifestModal();
    if (!$("rollout-modal").hidden) closeRolloutModal();
    if ($("timeline-modal") && !$("timeline-modal").hidden) closeTimelineModal();
    if ($("drift-modal") && !$("drift-modal").hidden) closeDriftDiffModal();
    if (!$("pod-inspector-modal").hidden) closePodInspectorModal();
    if (!$("ai-diagnose-modal").hidden) closeAIDiagnoseModal();
    if ($("helm-details-modal") && !$("helm-details-modal").hidden) helmController.closeDetailsModal();
  });

  initOnboardModal();
  initClustersModal();
  initAIChatPanel();
  helmController.wireHelmStaticControls();

  // Wire sidebar "Add" button (same modal as header button)
  $("onboard-trigger-sidebar")?.addEventListener("click", () => {
    const modal = $("onboard-modal");
    if (modal) {
      if (typeof modal.showModal === "function") modal.showModal();
      else modal.hidden = false;
    }
  });

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => moveNavIndicator(state.activeView), 120);
  });
}

// ------------------------------------------------------------------ //
// Cluster Connections Sidebar Panel
// ------------------------------------------------------------------ //
// Same official brand marks as PROVIDER_GLYPHS in components.js (Google's
// Cloud glyph, the AWS smile cropped from its wordmark, Microsoft's Azure
// glyph) -- this is a separate, smaller (14px, currentColor fill) rendering
// used in the sidebar's active-cluster card and the "All Clusters" list, so
// it's kept as its own object rather than forcing those call sites onto
// components.js's gradient-circle badge layout.
const PROVIDER_ICONS = {
  gke: `<svg viewBox="0 0 24 24" style="width:14px;height:14px;fill:currentColor;"><path d="M12.19 2.38a9.344 9.344 0 0 0-9.234 6.893c.053-.02-.055.013 0 0-3.875 2.551-3.922 8.11-.247 10.941l.006-.007-.007.03a6.717 6.717 0 0 0 4.077 1.356h5.173l.03.03h5.192c6.687.053 9.376-8.605 3.835-12.35a9.365 9.365 0 0 0-2.821-4.552l-.043.043.006-.05A9.344 9.344 0 0 0 12.19 2.38zm-.358 4.146c1.244-.04 2.518.368 3.486 1.15a5.186 5.186 0 0 1 1.862 4.078v.518c3.53-.07 3.53 5.262 0 5.193h-5.193l-.008.009v-.04H6.785a2.59 2.59 0 0 1-1.067-.23h.001a2.597 2.597 0 1 1 3.437-3.437l3.013-3.012A6.747 6.747 0 0 0 8.11 8.24c.018-.01.04-.026.054-.023a5.186 5.186 0 0 1 3.67-1.69z"/></svg>`,
  eks: `<svg viewBox="-0.5 13 25.3 6.8" style="width:16px;height:14px;fill:currentColor;"><path d="M21.698 16.207c-2.626 1.94-6.442 2.969-9.722 2.969-4.598 0-8.74-1.7-11.87-4.526-.247-.223-.024-.527.272-.351 3.384 1.963 7.559 3.153 11.877 3.153 2.914 0 6.114-.607 9.06-1.852.439-.2.814.287.383.607zM22.792 14.961c-.336-.43-2.22-.207-3.074-.103-.255.032-.295-.192-.063-.36 1.5-1.053 3.967-.75 4.254-.399.287.36-.08 2.826-1.485 4.007-.215.184-.423.088-.327-.151.32-.79 1.03-2.57.695-2.994z"/></svg>`,
  aks: `<svg viewBox="0 0 24 24" style="width:14px;height:14px;fill:currentColor;"><path d="M22.379 23.343a1.62 1.62 0 0 0 1.536-2.14v.002L17.35 1.76A1.62 1.62 0 0 0 15.816.657H8.184A1.62 1.62 0 0 0 6.65 1.76L.086 21.204a1.62 1.62 0 0 0 1.536 2.139h4.741a1.62 1.62 0 0 0 1.535-1.103l.977-2.892 4.947 3.675c.28.208.618.32.966.32m-3.084-12.531 3.624 10.739a.54.54 0 0 1-.51.713v-.001h-.03a.54.54 0 0 1-.322-.106l-9.287-6.9h4.853m6.313 7.006c.116-.326.13-.694.007-1.058L9.79 1.76a1.722 1.722 0 0 0-.007-.02h6.034a.54.54 0 0 1 .512.366l6.562 19.445a.54.54 0 0 1-.338.684"/></svg>`,
  local: `<svg viewBox="0 0 24 24" style="width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>`,
};
const PROVIDER_COLORS = { gke: "var(--provider-gke)", eks: "var(--provider-eks)", aks: "var(--provider-aks)", local: "var(--provider-local)" };
const ENV_PILL_COLORS = { prod: "var(--danger)", nonprod: "var(--warning)", sandbox: "var(--info)", default: "var(--unknown)" };

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderClusterPanel(clusters) {
  state.allClusters = clusters;
  const container = $("sidebar-active-cluster-card");
  const viewAllBtnText = $("view-all-clusters-text");
  
  if (viewAllBtnText) {
    viewAllBtnText.textContent = `Cluster Fleet (${clusters ? clusters.length : 0})`;
  }

  if (!container) return;

  if (!clusters || !clusters.length) {
    container.innerHTML = `<div style="text-align:center;padding:0.8rem 0.5rem;color:var(--color-fg-muted);font-size:0.75rem;opacity:0.6;">No cluster selected.<br><span style="opacity:0.8;">Click <b>Add</b> to register.</span></div>`;
    return;
  }

  // Find currently active cluster
  const activeCluster = clusters.find((c) => c.id === state.selectedClusterId) || clusters[0];
  const providerColor = PROVIDER_COLORS[activeCluster.provider] || "#64748b";
  const envColor = ENV_PILL_COLORS[activeCluster.environment] || ENV_PILL_COLORS.default;
  const icon = PROVIDER_ICONS[activeCluster.provider] || PROVIDER_ICONS.local;
  const safeName = escapeHtml(activeCluster.name);
  const safeEnv = escapeHtml(activeCluster.environment);

  container.innerHTML = `
    <div class="cluster-card" style="
      display:flex;align-items:center;gap:10px;
      padding:8px 10px;border-radius:12px;
      background:var(--accent-soft, rgba(66,133,244,0.1));
      border:1.5px solid var(--accent, #4285f4);
      position:relative;box-shadow:0 2px 6px rgba(0,0,0,0.04);
    ">
      <div style="width:28px;height:28px;border-radius:8px;background:${providerColor}20;color:${providerColor};display:flex;align-items:center;justify-content:center;flex-shrink:0;">
        ${icon}
      </div>
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:0.82rem;font-weight:700;color:var(--color-fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${safeName}">
            ${safeName}
          </span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;margin-top:2px;">
          <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--success);box-shadow:0 0 0 2px rgba(16,185,129,0.2);"></span>
          <span style="font-size:0.65rem;font-weight:600;padding:1px 6px;border-radius:99px;background:${envColor}20;color:${envColor};text-transform:uppercase;letter-spacing:0.04em;">${safeEnv}</span>
        </div>
      </div>
      ${canAdmin() ? `<button type="button" id="sidebar-edit-active-cluster-btn" title="Edit cluster details" style="
        width:26px;height:26px;border-radius:7px;border:1px solid transparent;
        background:transparent;color:var(--fg-muted);display:inline-flex;align-items:center;
        justify-content:center;cursor:pointer;flex-shrink:0;transition:all 150ms;
      " onmouseenter="this.style.background='var(--bg-elevated)';this.style.color='var(--accent)';this.style.borderColor='var(--border)'" onmouseleave="this.style.background='transparent';this.style.color='var(--fg-muted)';this.style.borderColor='transparent'">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
      </button>` : ""}
    </div>
  `;

  $("sidebar-edit-active-cluster-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    openClusterModal(activeCluster);
  });
}

function initClustersModal() {
  const modal = $("clusters-modal");
  const openBtn = $("view-all-clusters-btn");
  const closeBtn = $("clusters-modal-close");
  const addBtn = $("clusters-modal-add-btn");
  const searchInput = $("clusters-search-input");
  const listEl = $("clusters-modal-list");

  if (!modal) return;

  function renderList(filter = "") {
    if (!listEl) return;
    const clusters = state.allClusters || [];
    const query = filter.toLowerCase().trim();
    const filtered = clusters.filter(
      (c) => c.name.toLowerCase().includes(query) || (c.environment || "").toLowerCase().includes(query) || (c.provider || "").toLowerCase().includes(query)
    );

    if (!filtered.length) {
      listEl.innerHTML = `<div style="text-align:center;padding:2rem;color:var(--fg-subtle);font-size:0.85rem;">No matching clusters found</div>`;
      return;
    }

    listEl.innerHTML = "";
    filtered.forEach((c) => {
      const isActive = c.id === state.selectedClusterId;
      const providerColor = PROVIDER_COLORS[c.provider] || "#64748b";
      const envColor = ENV_PILL_COLORS[c.environment] || ENV_PILL_COLORS.default;
      const icon = PROVIDER_ICONS[c.provider] || PROVIDER_ICONS.local;
      const safeName = escapeHtml(c.name);
      const safeEnv = escapeHtml(c.environment);
      const safeProv = escapeHtml((c.provider || "").toUpperCase());
      const safeId = escapeHtml(c.id);

      const item = document.createElement("div");
      item.style.cssText = `
        display:flex;align-items:center;justify-content:space-between;
        padding:10px 14px;border-radius:12px;
        background:${isActive ? "var(--accent-soft)" : "var(--bg-inset)"};
        border:1.5px solid ${isActive ? "var(--accent)" : "var(--border)"};
        transition:all 150ms;
      `;

      item.innerHTML = `
        <div style="display:flex;align-items:center;gap:12px;flex:1;min-width:0;">
          <div style="width:34px;height:34px;border-radius:10px;background:${providerColor}18;color:${providerColor};display:flex;align-items:center;justify-content:center;flex-shrink:0;">
            ${icon}
          </div>
          <div style="flex:1;min-width:0;">
            <div style="display:flex;align-items:center;gap:8px;">
              <span style="font-size:0.88rem;font-weight:700;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${safeName}</span>
              <span style="font-size:0.65rem;font-weight:600;padding:2px 7px;border-radius:99px;background:${envColor}20;color:${envColor};text-transform:uppercase;letter-spacing:0.04em;">${safeEnv}</span>
            </div>
            <div style="font-size:0.75rem;color:var(--fg-subtle);margin-top:2px;">Provider: ${safeProv} &bull; ID: ${safeId}</div>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;">
          ${
            isActive
              ? `<span style="font-size:0.78rem;font-weight:700;color:var(--accent);padding:6px 12px;border-radius:8px;background:var(--accent-soft);display:inline-flex;align-items:center;gap:4px;">
                  <span style="width:6px;height:6px;border-radius:50%;background:var(--accent);"></span> Active
                 </span>`
              : `<button type="button" class="connect-cluster-action-btn" data-id="${safeId}" style="
                  padding:6px 14px;border-radius:8px;border:1.5px solid var(--border);
                  background:transparent;color:var(--fg);font-size:0.78rem;font-weight:600;
                  cursor:pointer;font-family:inherit;transition:all 150ms;
                 " onmouseenter="this.style.background='var(--bg-hover)';this.style.borderColor='var(--accent)'" onmouseleave="this.style.background='transparent';this.style.borderColor='var(--border)'">Connect</button>`
          }
          ${canAdmin() ? `<button type="button" class="edit-cluster-action-btn" data-id="${safeId}" title="Edit cluster '${safeName}'" style="
            width:32px;height:32px;border-radius:8px;border:1.5px solid transparent;
            background:transparent;color:var(--fg-subtle);display:inline-flex;align-items:center;
            justify-content:center;cursor:pointer;transition:all 150ms;
          " onmouseenter="this.style.background='var(--bg-hover)';this.style.color='var(--accent)'" onmouseleave="this.style.background='transparent';this.style.color='var(--fg-subtle)'">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
          </button>
          <button type="button" class="delete-cluster-action-btn" data-id="${safeId}" title="Remove cluster '${safeName}'" style="
            width:32px;height:32px;border-radius:8px;border:1.5px solid transparent;
            background:transparent;color:var(--fg-subtle);display:inline-flex;align-items:center;
            justify-content:center;cursor:pointer;transition:all 150ms;
          " onmouseenter="this.style.background='rgba(239,68,68,0.12)';this.style.color='var(--danger)'" onmouseleave="this.style.background='transparent';this.style.color='var(--fg-subtle)'">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M10 11v6M14 11v6"/></svg>
          </button>` : ""}
        </div>
      `;

      const connectBtn = item.querySelector(".connect-cluster-action-btn");
      connectBtn?.addEventListener("click", async () => {
        await connectToCluster(c.id);
        renderList(searchInput?.value || "");
        if (typeof modal.close === "function") modal.close();
      });

      const editBtn = item.querySelector(".edit-cluster-action-btn");
      editBtn?.addEventListener("click", (e) => {
        e.stopPropagation();
        if (typeof modal.close === "function") modal.close();
        openClusterModal(c);
      });

      const deleteBtn = item.querySelector(".delete-cluster-action-btn");
      deleteBtn?.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (confirm(`Are you sure you want to remove cluster "${c.name}"?`)) {
          try {
            const res = await api.deleteCluster(c.id);
            if (res.teams) state.teams = res.teams;
            populateTeamSelect();
            if (state.selectedClusterId === c.id) {
              const nextClusterId = restoreSelectionOrDefault();
              if (nextClusterId) await onClusterChange();
            }
            await loadClusterPanel();
            renderList(searchInput?.value || "");
          } catch (err) {
            alert(`Failed to remove cluster: ${err.message}`);
          }
        }
      });

      listEl.appendChild(item);
    });
  }

  function openModal() {
    renderList();
    if (typeof modal.showModal === "function") {
      modal.showModal();
    } else {
      modal.hidden = false;
    }
  }

  function closeModal() {
    if (typeof modal.close === "function") {
      modal.close();
    } else {
      modal.hidden = true;
    }
  }

  openBtn?.addEventListener("click", () => switchView("clusters"));
  closeBtn?.addEventListener("click", closeModal);
  searchInput?.addEventListener("input", (e) => renderList(e.target.value));

  addBtn?.addEventListener("click", () => {
    closeModal();
    openClusterModal(null);
  });
}

async function connectToCluster(clusterId) {
  await switchActiveCluster(clusterId); // already refreshes the cluster list panel on success
}

async function loadClusterPanel() {
  try {
    const res = await api.listClusters();
    renderClusterPanel(res.clusters || []);
  } catch (err) {
    const container = $("cluster-list-items");
    if (container) container.innerHTML = `<div style="padding:0.5rem;color:var(--danger);font-size:0.73rem;">Failed to load clusters</div>`;
  }
}

let _editingCluster = null;
let _openClusterModalImpl = null;

function openClusterModal(cluster = null) {
  if (_openClusterModalImpl) {
    _openClusterModalImpl(cluster);
  }
}

function initOnboardModal() {
  const modal = $("onboard-modal");
  const trigger = $("onboard-trigger");
  const closeBtn = $("onboard-modal-close");
  const cancelBtn = $("onboard-modal-cancel");
  const saveBtn = $("onboard-save-btn");
  const statusEl = $("onboard-status");
  const titleEl = $("onboard-modal-title");
  const subtitleEl = $("onboard-modal-subtitle");
  const idHintEl = $("onboard-id-hint");

  if (!modal) return;

  const providerTabs = document.querySelectorAll(".provider-tab");
  const colors = { gke: "#4285F4", eks: "#FF9900", aks: "#0078D4", local: "#188038" };
  const softBgs = {
    gke: "color-mix(in srgb, #4285F4 12%, transparent)",
    eks: "color-mix(in srgb, #FF9900 12%, transparent)",
    aks: "color-mix(in srgb, #0078D4 12%, transparent)",
    local: "color-mix(in srgb, #188038 12%, transparent)",
  };

  function showStatus(text, type = "info") {
    if (!statusEl) return;
    statusEl.hidden = false;
    statusEl.style.display = "block";
    if (type === "error") {
      statusEl.style.background = "rgba(239, 68, 68, 0.12)";
      statusEl.style.color = "var(--danger)";
      statusEl.style.border = "1px solid rgba(239, 68, 68, 0.25)";
    } else if (type === "success") {
      statusEl.style.background = "rgba(24, 128, 56, 0.12)";
      statusEl.style.color = "#188038";
      statusEl.style.border = "1px solid rgba(24, 128, 56, 0.25)";
    } else {
      statusEl.style.background = "rgba(66, 133, 244, 0.12)";
      statusEl.style.color = "#4285f4";
      statusEl.style.border = "1px solid rgba(66, 133, 244, 0.25)";
    }
    statusEl.textContent = text;
  }

  function hideStatus() {
    if (!statusEl) return;
    statusEl.hidden = true;
    statusEl.style.display = "none";
  }

  function selectProviderTab(provider) {
    const pVal = provider || "gke";
    if ($("onboard-provider")) $("onboard-provider").value = pVal;
    providerTabs.forEach((t) => {
      const p = t.dataset.provider;
      const isSelected = p === pVal;
      t.setAttribute("aria-pressed", isSelected ? "true" : "false");
      t.style.borderColor = isSelected ? colors[p] : "var(--border)";
      t.style.background = isSelected ? softBgs[p] : "transparent";
      const span = t.querySelector("span");
      if (span) span.style.color = isSelected ? colors[p] : "var(--fg-muted)";
    });
    document.querySelectorAll(".provider-fields").forEach((pf) => (pf.style.display = "none"));
    const pfEl = $(`provider-fields-${pVal}`);
    if (pfEl) pfEl.style.display = "flex";
  }

  function selectEnvironmentPill(env) {
    const val = env || "sandbox";
    if ($("onboard-environment")) $("onboard-environment").value = val;
    document.querySelectorAll('input[name="onboard-env"]').forEach((inp) => {
      inp.checked = inp.value === val;
    });
    document.querySelectorAll(".env-pill-opt").forEach((pill) => {
      const isMatch = pill.dataset.val === val;
      pill.style.background = isMatch ? "var(--grad-pill-selected)" : "transparent";
      pill.style.borderColor = isMatch ? "var(--accent)" : "var(--border)";
      pill.style.color = isMatch ? "var(--accent)" : "var(--fg-muted)";
    });
  }

  function resetFormFields() {
    if ($("onboard-name")) $("onboard-name").value = "";
    if ($("onboard-description")) $("onboard-description").value = "";
    if ($("gke-project")) $("gke-project").value = "";
    if ($("gke-region")) $("gke-region").value = "";
    if ($("gke-cluster-name")) $("gke-cluster-name").value = "";
    if ($("gke-impersonate-sa")) $("gke-impersonate-sa").value = "";
    if ($("eks-region")) $("eks-region").value = "";
    if ($("eks-cluster-name")) $("eks-cluster-name").value = "";
    if ($("eks-role-arn")) $("eks-role-arn").value = "";
    if ($("eks-log-group")) $("eks-log-group").value = "";
    if ($("aks-resource-group")) $("aks-resource-group").value = "";
    if ($("aks-cluster-name")) $("aks-cluster-name").value = "";
    if ($("aks-subscription-id")) $("aks-subscription-id").value = "";
    if ($("aks-tenant-id")) $("aks-tenant-id").value = "";
    if ($("aks-client-id")) $("aks-client-id").value = "";
    if ($("aks-log-analytics-workspace")) $("aks-log-analytics-workspace").value = "";
    if ($("local-kubeconfig-path")) $("local-kubeconfig-path").value = "~/.kube/config";
    if ($("local-context-select")) $("local-context-select").innerHTML = "";
    if ($("local-context-wrap")) $("local-context-wrap").style.display = "none";
  }

  function openModal(cluster = null) {
    hideStatus();
    _editingCluster = cluster || null;
    resetFormFields();

    if (_editingCluster) {
      const cfg = _editingCluster.config || {};
      if (titleEl) titleEl.textContent = "Edit Cluster";
      if (subtitleEl) subtitleEl.textContent = `Update display name, environment, or connection settings`;
      if (saveBtn) saveBtn.textContent = "Save Changes";
      if (idHintEl) idHintEl.innerHTML = `Cluster ID: <code style="font-family:var(--font-mono);color:var(--fg);">${escapeHtml(_editingCluster.id)}</code> (preserved when renaming)`;

      if ($("onboard-name")) $("onboard-name").value = _editingCluster.name || "";
      if ($("onboard-description")) $("onboard-description").value = _editingCluster.description || "";
      selectProviderTab(_editingCluster.provider || "gke");
      selectEnvironmentPill(_editingCluster.environment || "sandbox");

      if (_editingCluster.provider === "gke") {
        if ($("gke-project")) $("gke-project").value = cfg.project || "";
        if ($("gke-region")) $("gke-region").value = cfg.zone || cfg.region || "";
        if ($("gke-cluster-name")) $("gke-cluster-name").value = cfg.cluster_name || "";
        if ($("gke-impersonate-sa")) $("gke-impersonate-sa").value = cfg.impersonate_service_account || "";
      } else if (_editingCluster.provider === "eks") {
        if ($("eks-region")) $("eks-region").value = cfg.region || "";
        if ($("eks-cluster-name")) $("eks-cluster-name").value = cfg.cluster_name || "";
        if ($("eks-role-arn")) $("eks-role-arn").value = cfg.role_arn || "";
        if ($("eks-log-group")) $("eks-log-group").value = cfg.cloudwatch_log_group || "";
      } else if (_editingCluster.provider === "aks") {
        if ($("aks-resource-group")) $("aks-resource-group").value = cfg.resource_group || "";
        if ($("aks-cluster-name")) $("aks-cluster-name").value = cfg.cluster_name || "";
        if ($("aks-subscription-id")) $("aks-subscription-id").value = cfg.subscription_id || "";
        if ($("aks-tenant-id")) $("aks-tenant-id").value = cfg.tenant_id || "";
        if ($("aks-client-id")) $("aks-client-id").value = cfg.client_id || cfg.managed_identity_client_id || "";
        if ($("aks-log-analytics-workspace")) $("aks-log-analytics-workspace").value = cfg.log_analytics_workspace_id || "";
      } else if (_editingCluster.provider === "local") {
        if ($("local-kubeconfig-path")) $("local-kubeconfig-path").value = cfg.kubeconfig_path || "~/.kube/config";
        if (cfg.context_name && $("local-context-select")) {
          const select = $("local-context-select");
          select.innerHTML = "";
          const opt = document.createElement("option");
          opt.value = cfg.context_name;
          opt.textContent = cfg.context_name;
          opt.selected = true;
          select.appendChild(opt);
          if ($("local-context-wrap")) $("local-context-wrap").style.display = "block";
        }
      }
    } else {
      if (titleEl) titleEl.textContent = "Add Cluster";
      if (subtitleEl) subtitleEl.textContent = "Connect a Kubernetes cluster to monitor";
      if (saveBtn) saveBtn.textContent = "Add Cluster";
      if (idHintEl) idHintEl.textContent = "ID is auto-generated from the name";
      selectProviderTab("gke");
      selectEnvironmentPill("sandbox");
    }

    if (typeof modal.showModal === "function") {
      modal.showModal();
    } else {
      modal.hidden = false;
    }
    setTimeout(() => $("onboard-name")?.focus(), 50);
  }

  _openClusterModalImpl = openModal;

  function closeModal() {
    hideStatus();
    _editingCluster = null;
    if (typeof modal.close === "function") {
      modal.close();
    } else {
      modal.hidden = true;
    }
  }

  trigger?.addEventListener("click", () => openModal(null));
  closeBtn?.addEventListener("click", closeModal);
  cancelBtn?.addEventListener("click", closeModal);

  providerTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      selectProviderTab(tab.dataset.provider);
    });
  });

  // Environment Pill Selector
  const envInputs = document.querySelectorAll('input[name="onboard-env"]');
  envInputs.forEach((input) => {
    input.addEventListener("change", () => {
      selectEnvironmentPill(input.value);
    });
  });

  function getFormPayload() {
    const provider = $("onboard-provider").value;
    const name = $("onboard-name").value.trim();
    const description = $("onboard-description")?.value?.trim() || undefined;
    const id = _editingCluster
      ? _editingCluster.id
      : (name.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "") || "cluster-" + Date.now());
    const team_id = (_editingCluster?.team_id || state.selectedTeamId || "platform").toLowerCase();
    const team_name = _editingCluster?.team_name || team_id.toUpperCase();
    const environment = $("onboard-environment").value;

    let config = {};
    if (provider === "gke") {
      const regionVal = $("gke-region").value.trim();
      const prevCfg = (_editingCluster && _editingCluster.provider === "gke" && _editingCluster.config) || {};
      // Preserve zone vs region key if editing an existing zonal GKE cluster
      const isZone = /^[a-z]+-[a-z0-9]+-[a-z]$/.test(regionVal);
      config = {
        ...prevCfg,
        project: $("gke-project").value.trim(),
        ...(isZone ? { zone: regionVal } : { region: regionVal }),
        cluster_name: $("gke-cluster-name").value.trim() || name,
        impersonate_service_account: $("gke-impersonate-sa")?.value?.trim() || undefined,
      };
      if (isZone) delete config.region;
      else delete config.zone;
    } else if (provider === "eks") {
      config = {
        region: $("eks-region").value.trim(),
        cluster_name: $("eks-cluster-name").value.trim() || name,
        role_arn: $("eks-role-arn").value.trim() || undefined,
        cloudwatch_log_group: $("eks-log-group")?.value?.trim() || undefined,
      };
    } else if (provider === "aks") {
      config = {
        resource_group: $("aks-resource-group").value.trim(),
        cluster_name: $("aks-cluster-name").value.trim() || name,
        subscription_id: $("aks-subscription-id")?.value?.trim() || undefined,
        tenant_id: $("aks-tenant-id")?.value?.trim() || undefined,
        client_id: $("aks-client-id")?.value?.trim() || undefined,
        log_analytics_workspace_id: $("aks-log-analytics-workspace")?.value?.trim() || undefined,
      };
    } else if (provider === "local") {
      config = {
        kubeconfig_path: $("local-kubeconfig-path").value.trim(),
        context_name: $("local-context-select")?.value || (_editingCluster?.config?.context_name) || undefined,
      };
    }

    return { id, name: name || id, provider, environment, description, team_id, team_name, config };
  }

  function applyParsedKubeconfig(res) {
    if (res.kubeconfig_path && $("local-kubeconfig-path")) {
      $("local-kubeconfig-path").value = res.kubeconfig_path;
    }
    const select = $("local-context-select");
    const wrap = $("local-context-wrap");
    if (select) {
      select.innerHTML = "";
      (res.contexts || []).forEach((ctx) => {
        const opt = document.createElement("option");
        opt.value = ctx.name;
        opt.textContent = `${ctx.name} (${ctx.cluster})`;
        if (ctx.name === res.current_context) opt.selected = true;
        select.appendChild(opt);
      });
      if (wrap) wrap.style.display = "block";
    }
    if (res.suggested_id && !$("onboard-name").value) {
      $("onboard-name").value = res.suggested_id;
    }
    showStatus(`✓ Found ${res.contexts?.length || 0} context(s) in Kubeconfig`, "success");
  }

  const uploadLocalBtn = $("local-upload-btn");
  const uploadLocalInput = $("local-file-input");
  uploadLocalBtn?.addEventListener("click", () => uploadLocalInput?.click());
  uploadLocalInput?.addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    showStatus(`Uploading and scanning ${file.name}...`, "info");
    try {
      const content = await file.text();
      const res = await api.parseKubeconfig(file.name, content, file.name);
      applyParsedKubeconfig(res);
    } catch (err) {
      showStatus(`✗ Failed to parse Kubeconfig: ${err.message}`, "error");
    } finally {
      uploadLocalInput.value = "";
    }
  });

  const parseLocalBtn = $("local-parse-btn");
  parseLocalBtn?.addEventListener("click", async () => {
    const path = $("local-kubeconfig-path")?.value?.trim();
    if (!path) {
      showStatus("Please enter a Kubeconfig path (e.g. ~/.kube/config)", "error");
      return;
    }
    showStatus("Scanning local Kubeconfig file...", "info");
    try {
      const res = await api.parseKubeconfig(path);
      applyParsedKubeconfig(res);
    } catch (err) {
      showStatus(`✗ Failed to parse Kubeconfig: ${err.message}`, "error");
    }
  });

  const localContextSelect = $("local-context-select");
  localContextSelect?.addEventListener("change", () => {
    const selected = localContextSelect.value;
    if (selected && !$("onboard-name").value) {
      $("onboard-name").value = selected;
    }
  });

  saveBtn?.addEventListener("click", async (e) => {
    e.preventDefault();
    const isEditing = Boolean(_editingCluster);
    const payload = getFormPayload();
    if (!payload.name) {
      showStatus("Please enter a Cluster Display Name", "error");
      return;
    }
    showStatus(isEditing ? "Saving cluster changes..." : "Registering cluster...", "info");
    try {
      const res = isEditing
        ? await api.updateCluster(payload.id, payload)
        : await api.onboardCluster(payload);
      state.teams = res.teams;
      populateTeamSelect();
      const clusterSelect = $("cluster-select");
      if (clusterSelect) {
        clusterSelect.value = payload.id;
        if (!isEditing || state.selectedClusterId === payload.id) {
          await onClusterChange();
        }
      }
      showStatus(isEditing ? "✓ Cluster updated successfully!" : "✓ Cluster registered successfully!", "success");
      await loadClusterPanel();
      if (state.activeView === "clusters") {
        await loadClustersView();
      }
      setTimeout(closeModal, 700);
    } catch (err) {
      showStatus(`✗ ${isEditing ? "Update" : "Registration"} Failed: ${err.message}`, "error");
    }
  });
}


async function bootstrap() {
  initTheme();
  restoreRefreshInterval();
  wireStaticControls();

  let status;
  try {
    status = await fetchStatusWithApiKeyPrompt();
  } catch (err) {
    renderInto($("main"), errorState(err, () => window.location.reload()));
    return;
  }
  state.userRole = status.user?.role || "admin";
  state.userSubject = status.user?.subject || "anonymous";
  state.readOnlyMode = status.read_only_mode !== false;
  applyRoleVisibility();
  const roleSuffix = status.user?.role ? ` (${status.user.role})` : "";
  $("auth-mode-text").textContent = `auth: ${status.auth_mode}${roleSuffix}`;
  if (status.protocols?.mcp?.tool_count && $("agui-state-badge")) {
    $("agui-state-badge").textContent = `${status.protocols.mcp.tool_count} MCP Tools · Live Sync`;
  }
  if (status.disable_local_provider) {
    const localTab = document.querySelector('.provider-tab[data-provider="local"]');
    if (localTab) localTab.style.display = "none";
  }

  try {
    state.teams = (await api.getTeams()).teams;
  } catch (err) {
    renderInto($("main"), errorState(err, () => window.location.reload()));
    return;
  }

  if (!state.teams.length) {
    renderInto($("main"), emptyState("No clusters configured", "Add teams and clusters to config/clusters.yaml to get started."));
    return;
  }

  populateTeamSelect();
  const clusterId = restoreSelectionOrDefault();
  if (clusterId) await onClusterChange({ skipViewLoad: true, skipClusterPanel: true });
  switchView(state.activeView || "overview");
  startAutoRefresh();
  loadClusterPanel(); // populate sidebar cluster connections panel

  // Fonts (Plus Jakarta Sans) may still be swapping in, which shifts tab
  // widths -- position the indicator once now and once more after they load.
  moveNavIndicator(state.activeView);
  document.fonts?.ready.then(() => moveNavIndicator(state.activeView));
}

document.addEventListener("DOMContentLoaded", bootstrap);
