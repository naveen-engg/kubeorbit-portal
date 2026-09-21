// Application controller: state, view routing, auto-refresh, theme, and the
// per-view render functions that combine api.js data with components.js /
// topology.js widgets. No provider-specific branching lives here -- every
// cloud badge/label comes pre-resolved from the backend.

import { api, ApiError, getStoredApiKey, setStoredApiKey } from "./api.js";
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
  revealButton,
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
  lastTopologyKey: { network: null, storage: null, map: null },
  loadTokens: {},
  aiChatSessionId: null,
  aiChatMessages: [],
};

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
  const general = (await api.getNamespaces(clusterId, false)).items;
  setSidebarCount("count-namespaces", general.length);

  fillSelect($("workloads-namespace-select"), general, { placeholder: "All namespaces (excl. system)" });
  if ($("applications-namespace-select")) fillSelect($("applications-namespace-select"), general, { placeholder: "All namespaces" });
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
async function switchActiveCluster(clusterId) {
  if (!clusterId) return;
  if (clusterId !== state.selectedClusterId) resetAIChat(); // stale thread would otherwise answer for the wrong cluster
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
    await loadActiveView();
    loadClusterPanel(); // sync sidebar active state
  } catch (err) {
    setConnStatus("error", err.message);
    const container = document.querySelector(`.view[data-view="${state.activeView}"] [id$="-content"]`);
    if (container) renderInto(container, errorState(err, () => switchActiveCluster(clusterId)));
  } finally {
    setSelectorsDisabled(false);
  }
}

async function onClusterChange() {
  const clusterSelect = $("cluster-select");
  const clusterId = clusterSelect ? clusterSelect.value : state.selectedClusterId;
  await switchActiveCluster(clusterId);
}

// -- view: overview -----------------------------------------------------

function renderOverview(data) {
  const { badge, meta, node_count, node_fetch_error, helm } = data;
  $("overview-meta").textContent = `${badge.team_name} · ${capitalize(badge.environment)} · ${badge.cloud}`;

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

  children.push(
    panel(
      "Node Pools",
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
      )
    )
  );

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

  children.push(
    panel(
      "Helm Releases",
      helm.available
        ? dataTable(
            [
              { key: "name", label: "Release", mono: true },
              { key: "namespace", label: "Namespace", mono: true },
              { key: "chart", label: "Chart", mono: true },
              { key: "app_version", label: "App Version" },
              { key: "revision", label: "Revision" },
              { key: "status", label: "Status" },
            ],
            helm.items,
            { emptyTitle: "No Helm releases found" }
          )
        : emptyState("Helm not available", "The `helm` CLI is not installed on the server running KubeOrbit.")
    )
  );

  children.push(panel("Estimated Monthly Cost", h("div", { id: "cost-panel-content" }, loadingSkeleton(2)), null, true));
  children.push(panel("Ecosystem & Integrations", h("div", { id: "ecosystem-panel-content" }, loadingSkeleton(2)), null, true));

  if (meta.console_url || meta.logs_url) {
    children.push(h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;" }, [
      meta.console_url ? h("a", { href: meta.console_url, target: "_blank", rel: "noopener noreferrer", class: "btn btn--ghost" }, `Open in ${badge.cloud} Console ↗`) : null,
      meta.logs_url ? h("a", { href: meta.logs_url, target: "_blank", rel: "noopener noreferrer", class: "btn btn--ghost" }, `View Cloud Logs ↗`) : null,
    ].filter(Boolean)));
  }

  renderInto($("overview-content"), h("div", {}, children));
}

// Cost estimation shells out to a cloud pricing API (slow on first call for
// GKE especially -- see providers/pricing.py) -- loaded separately, after
// the main overview, same as the ecosystem panel below.
async function loadCostPanel() {
  const target = $("cost-panel-content");
  if (!target) return;
  const token = nextLoadToken("cost-panel");
  try {
    const data = await api.getCostEstimate(state.selectedClusterId);
    if (isStaleToken("cost-panel", token)) return;

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
  } catch (err) {
    if (isStaleToken("cost-panel", token)) return;
    renderInto(target, h("div", { style: "font-size:0.82rem;color:var(--fg-subtle);" }, "Could not load cost estimate."));
  }
}

// Ecosystem CRD detection takes several seconds on first load (it probes ~18
// resource types) -- loaded separately and after the main overview so it
// never blocks the rest of the page from appearing.
async function loadEcosystemPanel() {
  const target = $("ecosystem-panel-content");
  if (!target) return;
  const token = nextLoadToken("ecosystem-panel");
  try {
    const data = await api.getEcosystemOverview(state.selectedClusterId);
    if (isStaleToken("ecosystem-panel", token)) return;
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
  } catch (err) {
    if (isStaleToken("ecosystem-panel", token)) return;
    renderInto(target, h("div", { style: "font-size:0.82rem;color:var(--fg-subtle);" }, "Could not load ecosystem detection."));
  }
}

async function loadOverview() {
  const container = $("overview-content");
  const token = nextLoadToken("overview");
  renderInto(container, loadingSkeleton());
  try {
    const data = await api.getClusterInfo(state.selectedClusterId);
    if (isStaleToken("overview", token)) return;
    renderOverview(data);
    loadCostPanel();
    loadEcosystemPanel();
  } catch (err) {
    if (isStaleToken("overview", token)) return;
    renderInto(container, errorState(err, loadOverview));
  }
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

async function loadWorkloads() {
  const statsContainer = $("workloads-stats");
  const token = nextLoadToken("workloads");
  renderInto(statsContainer, loadingSkeleton());
  renderInto($("workloads-issues"), h("div", {}));
  // Data is for a (possibly new) namespace -- collapse any open catalog
  // panel rather than leave it showing a stale, now-mismatched type list.
  catalogTypeFilter = null;
  const catalogPanel = $("workloads-catalog-panel");
  if (catalogPanel) catalogPanel.hidden = true;
  try {
    const namespace = $("workloads-namespace-select").value || undefined;
    const data = await api.getWorkloads(state.selectedClusterId, namespace);
    if (isStaleToken("workloads", token)) return;
    renderWorkloads(data);
  } catch (err) {
    if (isStaleToken("workloads", token)) return;
    renderInto(statsContainer, errorState(err, loadWorkloads));
  }
}

// -- view: namespace resources ----------------------------------------

function renderNamespaceResources(data, { clusterId, namespace }) {
  const gatewayUnavailable = () =>
    emptyState("Gateway API not installed", "This cluster does not have the Gateway API CRDs registered.");

  // One full-width table at a time (switched via the subtabs row below)
  // instead of eight cramped side-by-side mini-tables -- at typical panel
  // widths, several small columns squeezed into a ~300px card forced pod
  // names and status text to wrap into an illegible stack of fragments.
  const sections = [
    {
      key: "pods",
      label: "Pods",
      count: data.pods.length,
      render: () =>
        dataTable(
          [
            { key: "name", label: "Name", mono: true, truncate: true },
            { label: "Status", render: (r) => statusPill(r.status) },
            { key: "ready", label: "Ready", mono: true },
            { key: "restarts", label: "Restarts" },
            { key: "node", label: "Node", mono: true, truncate: true },
            { label: "Age", render: (r) => timeAgo(r.created_at) },
          ],
          data.pods
        ),
    },
    {
      key: "services",
      label: "Services",
      count: data.services.length,
      render: () =>
        dataTable(
          [
            { key: "name", label: "Name", mono: true, truncate: true },
            { key: "type", label: "Type" },
            { key: "cluster_ip", label: "Cluster IP", mono: true },
            { label: "External", render: (r) => (r.external_ips || []).join(", ") || "—" },
            { label: "Ports", render: (r) => (r.ports || []).map((p) => `${p.port}/${p.protocol}`).join(", ") || "—" },
          ],
          data.services
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
            { label: "Age", render: (r) => timeAgo(r.created_at) },
          ],
          data.configmaps
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
            { label: "Age", render: (r) => timeAgo(r.created_at) },
            { label: "", render: (r) => revealButton(() => openSecretModal(r, clusterId, namespace)) },
          ],
          data.secrets,
          { emptyTitle: "No secrets in this namespace" }
        ),
    },
    {
      key: "gateways",
      label: "Gateways",
      count: data.gateways.available ? data.gateways.items.length : null,
      render: () =>
        data.gateways.available
          ? dataTable(
              [
                { key: "name", label: "Name", mono: true, truncate: true },
                { key: "gateway_class", label: "Class" },
                { label: "Addresses", render: (r) => (r.addresses || []).join(", ") || "—" },
                { label: "Listeners", render: (r) => String((r.listeners || []).length) },
              ],
              data.gateways.items
            )
          : gatewayUnavailable(),
    },
    {
      key: "httproutes",
      label: "HTTPRoutes",
      count: data.httproutes.available ? data.httproutes.items.length : null,
      render: () =>
        data.httproutes.available
          ? dataTable(
              [
                { key: "name", label: "Name", mono: true, truncate: true },
                { label: "Hostnames", render: (r) => (r.hostnames || []).join(", ") || "—" },
                { label: "Parents", render: (r) => (r.parent_refs || []).join(", ") || "—" },
                { label: "Rules", render: (r) => String((r.rules || []).length) },
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
            { label: "Status", render: (r) => statusPill(r.status) },
            { key: "capacity", label: "Capacity" },
            { key: "storage_class", label: "StorageClass", mono: true, truncate: true },
            { label: "Access", render: (r) => (r.access_modes || []).join(", ") || "—" },
          ],
          data.pvcs
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
          ],
          data.serviceaccounts
        ),
    },
  ];

  const tabsRow = h("div", { class: "subtabs", role: "tablist" });
  const panelBody = h("div", {});

  const showSection = (key) => {
    const section = sections.find((s) => s.key === key);
    tabsRow.querySelectorAll(".subtabs__item").forEach((btn) => btn.setAttribute("aria-selected", String(btn.dataset.key === key)));
    renderInto(panelBody, section.render());
  };

  for (const section of sections) {
    const btn = h(
      "button",
      { class: "subtabs__item", role: "tab", "data-key": section.key, "aria-selected": "false", onClick: () => showSection(section.key) },
      [section.label, h("span", { class: "subtabs__count" }, section.count === null ? "—" : String(section.count))]
    );
    tabsRow.appendChild(btn);
  }

  showSection(sections[0].key);
  renderInto($("namespace-content"), h("div", {}, [tabsRow, panel(null, panelBody)]));
}

async function loadNamespaces() {
  const container = $("namespace-content");
  const namespace = $("namespace-select").value;
  if (!namespace) {
    renderInto(container, emptyState("No namespace selected", "Choose a namespace above."));
    return;
  }
  const token = nextLoadToken("namespaces");
  renderInto(container, loadingSkeleton());
  try {
    const data = await api.getNamespaceResources(state.selectedClusterId, namespace);
    if (isStaleToken("namespaces", token)) return;
    renderNamespaceResources(data, { clusterId: state.selectedClusterId, namespace });
  } catch (err) {
    if (isStaleToken("namespaces", token)) return;
    renderInto(container, errorState(err, loadNamespaces));
  }
}

// -- secret reveal modal --------------------------------------------------

let lastFocusedBeforeModal = null;

function openSecretModal(secretMeta, clusterId, namespace) {
  lastFocusedBeforeModal = document.activeElement;
  $("secret-modal-title").textContent = secretMeta.name;
  const body = $("secret-modal-body");
  body.replaceChildren(
    warningBanner("Revealing decodes this Secret's values in your browser. Only do this if you're authorized to view them -- this action is logged."),
    h("div", { class: "kv-list", style: "margin:12px 0;" }, [
      h("div", { class: "kv-row" }, [h("div", { class: "kv-row__key" }, "Type"), h("div", { class: "kv-row__value" }, secretMeta.type || "Opaque")]),
      h("div", { class: "kv-row" }, [h("div", { class: "kv-row__key" }, "Keys"), h("div", { class: "kv-row__value" }, (secretMeta.keys || []).join(", ") || "—")]),
    ]),
    revealButton(async (evt) => {
      const btn = evt.currentTarget;
      btn.disabled = true;
      try {
        renderRevealedSecret(await api.revealSecret(clusterId, namespace, secretMeta.name));
      } catch (err) {
        body.appendChild(errorState(err));
      } finally {
        btn.disabled = false;
      }
    })
  );
  $("secret-modal").hidden = false;
  $("secret-modal-close").focus();
}

function renderRevealedSecret(result) {
  const body = $("secret-modal-body");
  const entries = Object.entries(result.values || {});
  const rows = entries.map(([k, v]) => h("div", { class: "kv-row" }, [h("div", { class: "kv-row__key" }, k), h("div", { class: "kv-row__value" }, v)]));
  body.appendChild(h("div", { class: "kv-list", style: "margin-top:12px;" }, rows.length ? rows : [emptyState("No data", "This secret has no keys.")]));
}

function closeSecretModal() {
  $("secret-modal").hidden = true;
  if (lastFocusedBeforeModal) lastFocusedBeforeModal.focus();
}

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

async function loadNetworkTopology() {
  const container = $("network-topology-content");
  const namespace = $("network-namespace-select").value;
  if (!namespace) {
    renderInto(container, emptyState("No namespace selected", "Choose a namespace above."));
    return;
  }
  const token = nextLoadToken("network-topology");
  renderInto(container, loadingSkeleton());
  try {
    const graph = await api.getNetworkTopology(state.selectedClusterId, namespace);
    if (isStaleToken("network-topology", token)) return; // superseded by a newer request -- drop this result
    const key = `${state.selectedClusterId}:${namespace}`;
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
  } catch (err) {
    if (isStaleToken("network-topology", token)) return;
    renderInto(container, errorState(err, loadNetworkTopology));
  }
}

async function loadStorageTopology() {
  const container = $("storage-topology-content");
  const namespace = $("storage-namespace-select").value;
  if (!namespace) {
    renderInto(container, emptyState("No namespace selected", "Choose a namespace above."));
    return;
  }
  const token = nextLoadToken("storage-topology");
  renderInto(container, loadingSkeleton());
  try {
    const graph = await api.getStorageTopology(state.selectedClusterId, namespace);
    if (isStaleToken("storage-topology", token)) return;
    const key = `${state.selectedClusterId}:${namespace}`;
    const animate = state.lastTopologyKey.storage !== key;
    state.lastTopologyKey.storage = key;
    const wrap = h("div", {}, [
      h("div", { class: "topology-toolbar" }, topologyLegend()),
      h("div", { class: "topology-container" }, renderTopologyGraph(graph, STORAGE_COLUMNS, animate, openResourceModal)),
    ]);
    renderInto(container, wrap);
  } catch (err) {
    if (isStaleToken("storage-topology", token)) return;
    renderInto(container, errorState(err, loadStorageTopology));
  }
}

async function loadNamespaceMap() {
  const container = $("map-content");
  const namespace = $("map-namespace-select").value;
  if (!namespace) {
    renderInto(container, emptyState("No namespace selected", "Choose a namespace above."));
    return;
  }
  const token = nextLoadToken("map");
  renderInto(container, loadingSkeleton());
  try {
    const graph = await api.getNamespaceMap(state.selectedClusterId, namespace);
    if (isStaleToken("map", token)) return;
    const key = `${state.selectedClusterId}:${namespace}`;
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
  } catch (err) {
    if (isStaleToken("map", token)) return;
    renderInto(container, errorState(err, loadNamespaceMap));
  }
}

// -- view: events -----------------------------------------------------------

function renderEvents(data) {
  renderInto(
    $("events-content"),
    panel(
      "Recent Events",
      dataTable(
        [
          { label: "Type", render: (r) => pill(r.type || "Unknown", r.type === "Warning" ? "critical" : "healthy") },
          { key: "reason", label: "Reason", mono: true },
          { label: "Object", render: (r) => `${r.involved_kind || ""}/${r.involved_name || ""}` },
          { key: "message", label: "Message" },
          { key: "count", label: "Count" },
          { label: "Last Seen", render: (r) => timeAgo(r.last_timestamp) },
        ],
        data.items,
        { emptyTitle: "No events" }
      )
    )
  );
}

async function loadEvents() {
  const container = $("events-content");
  const token = nextLoadToken("events");
  renderInto(container, loadingSkeleton());
  try {
    const namespace = $("events-namespace-select").value || undefined;
    const data = await api.getEvents(state.selectedClusterId, namespace, 200);
    if (isStaleToken("events", token)) return;
    renderEvents(data);
  } catch (err) {
    if (isStaleToken("events", token)) return;
    renderInto(container, errorState(err, loadEvents));
  }
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
  return items.map((pod) => {
    const isRunning = pod.status === "Running";
    return h("div", { className: "pod-card-row card", style: "display:flex;align-items:center;justify-content:space-between;padding:0.75rem 1rem;background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px;margin-bottom:0.5rem;box-shadow:var(--shadow-raised-sm);" }, [
      h("div", { style: "display:flex;align-items:center;gap:0.75rem;" }, [
        h("span", { style: `width:10px;height:10px;border-radius:50%;display:inline-block;background:${isRunning ? "var(--success)" : "var(--danger)"};` }),
        h("span", { style: "font-weight:700;font-family:var(--font-mono, monospace);font-size:0.95rem;color:var(--fg);" }, pod.name),
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, pod.namespace),
      ]),
      h("div", { style: "display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;" }, [
        pill(pod.reason || pod.status || "Unknown", isRunning ? "success" : "warning"),
        pill(pod.ready || "0/0", "info"),
        pod.restarts ? pill(`↺ ${pod.restarts}`, "muted") : null,
        pod.node ? h("span", { className: "badge", style: "font-size:0.75rem;background:var(--info-soft);color:var(--info);" }, pod.node) : null,
        h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, timeAgo(pod.created_at)),
        h("button", { className: "btn btn--primary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;font-weight:600;", onclick: () => openPodInspectorModal(pod.name, pod.namespace) }, "Inspect"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;font-weight:600;color:var(--info);border-color:color-mix(in srgb, var(--info) 40%, transparent);", onclick: () => openLogsModal(pod.name, pod.namespace) }, "Logs"),
        h("button", { className: "btn btn--secondary btn--sm", style: "padding:0.2rem 0.55rem;font-size:0.78rem;font-weight:600;", onclick: () => openManifestModal("Pod", pod.name, pod.namespace) }, "YAML"),
      ].filter(Boolean))
    ]);
  });
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

    return h("div", {
      className: "node-card card",
      style: "display:flex;flex-direction:column;gap:0.75rem;padding:1.1rem 1.25rem;background:var(--bg-elevated);border:1px solid var(--border);border-radius:12px;margin-bottom:0.75rem;box-shadow:var(--shadow-raised-sm);"
    }, [
      h("div", { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem;" }, [
        h("div", { style: "display:flex;align-items:center;gap:0.75rem;flex-wrap:wrap;" }, [
          h("span", {
            style: `width:10px;height:10px;border-radius:50%;display:inline-block;background:${isReady ? "var(--success)" : "var(--danger)"};`
          }),
          h("span", { style: "font-weight:700;font-family:var(--font-mono, monospace);font-size:1.05rem;color:var(--fg);" }, node.name),
          statusPill(node.status, isReady),
          ...roles.map((r) => h("span", { className: "badge", style: "font-size:0.75rem;background:var(--accent-soft);color:var(--accent);font-weight:600;text-transform:uppercase;padding:2px 8px;border-radius:99px;" }, r)),
        ]),
        h("div", { style: "display:flex;align-items:center;gap:0.5rem;" }, [
          h("span", { style: "font-size:0.78rem;color:var(--fg-muted);" }, node.age || (node.created_at ? timeAgo(node.created_at) : "—")),
          h("button", {
            className: "btn btn--secondary btn--sm",
            style: "padding:0.25rem 0.65rem;font-size:0.78rem;font-weight:600;",
            onclick: () => openManifestModal("Node", node.name, "")
          }, "YAML"),
        ])
      ]),
      h("div", { style: "display:grid;grid-template-columns:repeat(auto-fit, minmax(130px, 1fr));gap:0.75rem;padding-top:0.6rem;border-top:1px solid var(--border);font-size:0.82rem;" }, [
        h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "K8s Version"),
          h("div", { style: "font-family:var(--font-mono);font-weight:600;color:var(--fg);" }, node.kubernetes_version || "—")
        ]),
        h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "CPU Capacity"),
          h("div", { style: "font-weight:600;color:var(--fg);" }, node.cpu_capacity ? `${node.cpu_capacity} cores` : "—")
        ]),
        h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "Memory"),
          h("div", { style: "font-weight:600;color:var(--fg);" }, formatMemory(node.memory_capacity))
        ]),
        h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "OS Image"),
          h("div", { style: "font-weight:500;color:var(--fg-muted);font-size:0.78rem;word-break:break-word;" }, node.os_image || "—")
        ]),
        node.zone ? h("div", {}, [
          h("div", { style: "font-size:0.68rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;margin-bottom:2px;" }, "Zone"),
          h("div", { style: "font-weight:600;color:var(--fg);" }, node.zone)
        ]) : null,
      ].filter(Boolean))
    ]);
  });

  renderInto(container, h("div", { style: "display:flex;flex-direction:column;gap:1.25rem;" }, [
    stats,
    h("div", { style: "display:flex;flex-direction:column;" }, cards)
  ]));
}

async function loadNodesView() {
  const container = $("nodes-content");
  const token = nextLoadToken("nodes");
  renderInto(container, loadingSkeleton());
  try {
    const data = await api.getNodes(state.selectedClusterId);
    if (isStaleToken("nodes", token)) return;
    renderNodes(data.items || []);
  } catch (err) {
    if (isStaleToken("nodes", token)) return;
    renderInto(container, errorState(err, loadNodesView));
  }
}


// -- view routing -----------------------------------------------------------

async function loadClusterMap() {
  const container = $("map-content");
  const token = nextLoadToken("map");
  renderInto(container, loadingSkeleton());
  try {
    const data = await api.getClusterWorkloadMap(state.selectedClusterId);
    if (isStaleToken("map", token)) return;
    renderClusterMap(data);
  } catch (err) {
    if (isStaleToken("map", token)) return;
    renderInto(container, errorState(err, loadClusterMap));
  }
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
    const onboardModal = $("onboard-modal");
    if (onboardModal && typeof onboardModal.showModal === "function") {
      onboardModal.showModal();
    }
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

          // Delete action container
          (() => {
            const deleteContainer = h("div", { style: "display:flex;align-items:center;gap:6px;" });

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

async function loadLogicalApplications() {
  const container = $("applications-content");
  const token = nextLoadToken("applications");
  renderInto(container, loadingSkeleton());
  try {
    const ns = $("applications-namespace-select")?.value || undefined;
    const data = await api.getLogicalApplications(state.selectedClusterId, ns);
    if (isStaleToken("applications", token)) return;
    renderLogicalApplications(data);
  } catch (err) {
    if (isStaleToken("applications", token)) return;
    renderInto(container, errorState(err, loadLogicalApplications));
  }
}

function renderLogicalApplications(data) {
  const container = $("applications-content");
  const apps = data.items || data.applications || [];

  if (!apps.length) {
    renderInto(container, emptyState("No applications found", "No logical applications detected in this namespace. Workloads with `app.kubernetes.io/name` or `app` labels will appear here."));
    return;
  }

  // Small cards in a wrapping grid, not one full-width row per app -- a
  // namespace with a dozen small apps used to mean a dozen screens of mostly
  // empty horizontal space to scroll through for a couple of chips each.
  const MAX_CHIPS = 6;

  const appCards = apps.map((app) => {
    const isHealthy = app.healthy !== false;
    const statusColor = isHealthy ? "var(--success)" : "var(--danger)";
    const totalPods = (app.pods || []).length;
    const readyPods = (app.pods || []).filter(p => p.status === "Running" || p.ready === p.total_containers).length;

    const chips = [
      ...(app.deployments || []).map(d => ({ label: d.name || d, prefix: "deploy", color: "var(--accent)" })),
      ...(app.statefulsets || []).map(s => ({ label: s.name || s, prefix: "sts", color: "#8b5cf6" })),
      ...(app.daemonsets || []).map(ds => ({ label: ds.name || ds, prefix: "ds", color: "var(--warning)" })),
      ...(app.services || []).map(svc => ({ label: svc.name || svc, prefix: "svc", color: "var(--success)" })),
    ];
    const visibleChips = chips.slice(0, MAX_CHIPS);
    const hiddenCount = chips.length - visibleChips.length;

    return h("div", { className: "app-card app-card--sm" }, [
      h("div", { style: "display:flex;align-items:center;gap:8px;" }, [
        h("div", { style: `width:26px;height:26px;border-radius:7px;background:${statusColor}18;color:${statusColor};display:flex;align-items:center;justify-content:center;flex-shrink:0;` }, [
          h("svg", { width: "13", height: "13", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2.2" }, [
            h("path", { d: "M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" })
          ])
        ]),
        h("div", { style: "min-width:0;flex:1;" }, [
          h("div", { style: "font-weight:700;font-size:0.88rem;color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }, app.name),
          h("div", { style: "font-size:0.7rem;color:var(--fg-subtle);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }, app.namespace)
        ]),
        h("span", {
          style: `width:7px;height:7px;border-radius:50%;background:${statusColor};flex-shrink:0;`,
          title: isHealthy ? "Healthy" : "Degraded",
        }),
      ]),

      h("div", { style: "font-size:0.72rem;color:var(--fg-muted);font-weight:600;" }, `${readyPods}/${totalPods} pods ready`),

      h("div", { style: "display:flex;flex-wrap:wrap;gap:4px;" }, [
        ...visibleChips.map((c) => h("span", { className: "app-child-pill app-child-pill--sm" }, [
          h("span", { style: `color:${c.color};font-weight:700;` }, `${c.prefix}:`),
          h("span", {}, c.label)
        ])),
        hiddenCount > 0 ? h("span", { className: "app-child-pill app-child-pill--sm", style: "color:var(--fg-subtle);" }, `+${hiddenCount} more`) : null,
      ].filter(Boolean))
    ]);
  });

  renderInto(container, h("div", { className: "app-card-grid" }, appCards));
}

// -- view: autonomous diagnostics & RCA --------------------------------------

async function loadDiagnosticsRCA() {
  const container = $("diagnostics-content");
  const token = nextLoadToken("diagnostics");
  renderInto(container, loadingSkeleton());
  try {
    const data = await api.getDiagnosticsRCA(state.selectedClusterId);
    if (isStaleToken("diagnostics", token)) return;
    renderDiagnosticsRCA(data);
  } catch (err) {
    if (isStaleToken("diagnostics", token)) return;
    renderInto(container, errorState(err, loadDiagnosticsRCA));
  }
}

function renderDiagnosticsRCA(data) {
  const container = $("diagnostics-content");
  const summary = data.summary || { overall_score: 100, active_root_causes: 0, workloads_healthy: 0, workloads_total: 0 };
  const rootCauses = data.root_causes || [];

  const stats = statGrid([
    statCard("Cluster Health Score", `${summary.overall_score}%`, [pill(summary.overall_score >= 80 ? "Healthy" : "Anomalies Detected", summary.overall_score >= 80 ? "healthy" : "critical")], STAT_ICONS.pulse),
    statCard("Folded Root Causes", summary.active_root_causes, [pill(`${rootCauses.reduce((acc, r) => acc + (r.folded_failures?.length || 1), 0)} Raw Symptoms Folded`, "info")], STAT_ICONS.funnel),
    statCard("Workload Health", `${summary.workloads_healthy}/${summary.workloads_total} Healthy`, null, STAT_ICONS.shieldCheck),
  ]);

  if (!rootCauses.length) {
    const cleanState = h("div", {
      className: "card",
      style: "padding:2.5rem;text-align:center;background:var(--bg-elevated);border:1.5px solid var(--success);border-radius:16px;margin-top:1.5rem;"
    }, [
      h("div", { style: "width:54px;height:54px;border-radius:50%;background:rgba(16,185,129,0.12);color:var(--success);display:inline-flex;align-items:center;justify-content:center;margin-bottom:1rem;" }, [
        h("svg", { width: "28", height: "28", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2.5" }, [
          h("path", { d: "M20 6L9 17l-5-5" })
        ])
      ]),
      h("h3", { style: "font-size:1.2rem;font-weight:700;color:var(--fg);margin:0 0 0.5rem;" }, "Zero Anomalies Detected"),
      h("p", { style: "font-size:0.85rem;color:var(--fg-muted);max-width:480px;margin:0 auto;" }, "AI Diagnostic Engine continuously verified pod liveness, probe health, memory limits, and rollout skew. All cluster workloads are running within optimal thresholds.")
    ]);
    renderInto(container, h("div", { style: "display:flex;flex-direction:column;gap:1.25rem;" }, [stats, cleanState]));
    return;
  }

  const rcaCards = rootCauses.map((rc) => {
    const isCrit = rc.severity === "critical";

    return h("div", { className: `rca-card ${isCrit ? "rca-critical" : "rca-warning"}` }, [
      h("div", { style: "display:flex;align-items:flex-start;justify-content:space-between;gap:10px;flex-wrap:wrap;" }, [
        h("div", { style: "flex:1;min-width:0;" }, [
          h("div", { style: "display:flex;align-items:center;gap:8px;margin-bottom:4px;" }, [
            h("span", { style: `font-size:0.72rem;font-weight:700;padding:2px 8px;border-radius:6px;background:${isCrit ? "rgba(217,48,37,0.15)" : "rgba(245,158,11,0.15)"};color:${isCrit ? "var(--danger)" : "var(--warning)"};text-transform:uppercase;` }, rc.severity),
            h("span", { style: "font-size:0.75rem;font-weight:600;color:var(--accent);" }, `Confidence: ${Math.round((rc.confidence || 0.95) * 100)}%`),
            h("span", { style: "font-size:0.75rem;color:var(--fg-subtle);" }, `Namespace: ${rc.namespace}`)
          ]),
          h("h3", { style: "font-size:1.05rem;font-weight:700;color:var(--fg);margin:0;" }, rc.title),
          h("p", { style: "font-size:0.82rem;color:var(--fg-muted);margin:4px 0 0;line-height:1.4;" }, rc.summary)
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
        h("div", { style: "font-size:0.72rem;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:0.04em;" }, "Suggested AI Remediation Action"),
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
          question: `Investigate this finding and suggest concrete next steps: "${rc.title}" — ${rc.summary}. `
            + `Affected: ${(rc.folded_failures || []).join(", ")}.`,
          title: `Diagnose with AI — ${rc.title}`,
        }),
      }, "✨ Diagnose with AI"),
    ].filter(Boolean));
  });

  renderInto(container, h("div", { style: "display:flex;flex-direction:column;gap:1.25rem;" }, [stats, ...rcaCards]));
}

// -- view: audit logs & timeline ----------------------------------------------

async function loadAuditLogsView() {
  const container = $("audit-content");
  const token = nextLoadToken("audit");
  renderInto(container, loadingSkeleton());
  try {
    const [eventsRes, auditRes] = await Promise.all([
      api.getExtendedEvents(state.selectedClusterId),
      api.getAuditLogs(state.selectedClusterId),
    ]);
    if (isStaleToken("audit", token)) return;
    renderAuditAndEvents({ events: eventsRes.items || [], audit: auditRes.logs || [] });
  } catch (err) {
    if (isStaleToken("audit", token)) return;
    renderInto(container, errorState(err, loadAuditLogsView));
  }
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
    allItems.push({
      type: "event",
      timestamp: e.last_timestamp || e.first_timestamp,
      actor: e.source_component || "k8s-controller",
      action: e.reason,
      target: `${e.involved_object_kind}/${e.involved_object_name}`,
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
            h("span", { style: "font-family:var(--font-mono);font-size:0.8rem;color:var(--accent);" }, item.target)
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
        !isActive ? h("button", {
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

  renderInto(body, h("div", { style: "display:flex;flex-direction:column;gap:1.25rem;" }, [
    aiButton,
    h("div", { style: "display:flex;flex-direction:column;gap:6px;" }, [
      h("span", { style: "font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.04em;" }, "Container Specifications & Resource Allocation"),
      ...containerCards
    ]),
    terminalBox
  ]));
}

function closePodInspectorModal() {
  const modal = $("pod-inspector-modal");
  if (modal) modal.hidden = true;
}

// -- AI Diagnose Modal --------------------------------------------------------
// Thin UI around POST /api/clusters/{id}/ai/diagnose (see ai_agent.py): a
// single-turn, read-only agent call. Never mutates the cluster -- worst case
// it returns an error (e.g. no model credentials configured).

async function openAIDiagnoseModal({ namespace, resourceType, resourceName, question, title } = {}) {
  const modal = $("ai-diagnose-modal");
  const titleEl = $("ai-diagnose-title");
  const subtitleEl = $("ai-diagnose-subtitle");
  const body = $("ai-diagnose-body");
  if (!modal || !body) return;

  modal.hidden = false;
  titleEl.textContent = title || "Diagnose with AI";
  subtitleEl.textContent = [state.selectedClusterId, namespace, resourceType && resourceName ? `${resourceType}/${resourceName}` : null]
    .filter(Boolean).join(" · ");
  renderInto(body, h("div", { style: "display:flex;flex-direction:column;gap:8px;" }, [
    h("div", { style: "font-size:0.82rem;color:var(--fg-muted);" }, "Asking the agent — this calls a live LLM and may take a few seconds…"),
    loadingSkeleton(3),
  ]));

  try {
    const result = await api.aiDiagnose(state.selectedClusterId, { namespace, resourceType, resourceName, question });
    if (result.error) {
      renderInto(body, h("div", {
        style: "padding:12px 14px;border-radius:10px;background:var(--danger-soft, rgba(217,48,37,0.08));border:1px solid var(--danger);color:var(--danger);font-size:0.85rem;line-height:1.5;"
      }, result.error));
      return;
    }
    renderInto(body, h("div", {
      style: "font-size:0.88rem;line-height:1.6;color:var(--fg);white-space:pre-wrap;"
    }, result.answer || "No response."));
  } catch (err) {
    renderInto(body, errorState(err, () => openAIDiagnoseModal({ namespace, resourceType, resourceName, question, title })));
  }
}

function closeAIDiagnoseModal() {
  const modal = $("ai-diagnose-modal");
  if (modal) modal.hidden = true;
}

// -- Ask AI chat panel ----------------------------------------------------
// A global floating chat, distinct from the one-shot AI Diagnose modal above.
// Multi-turn: state.aiChatSessionId is handed back by every /ai/chat reply
// and resent on the next message so the agent keeps the conversation's
// context (see ai_agent.chat()). Always scoped to state.selectedClusterId --
// the backend resolves cluster_id from the URL, not from anything in the
// chat request body, so switching the active cluster below resets the
// thread rather than let old messages silently answer for a new cluster.

function renderAIChatMessages() {
  const container = $("ai-chat-messages");
  if (!container) return;
  if (state.aiChatMessages.length === 0) {
    renderInto(container, h("div", { className: "ai-chat-panel__empty", id: "ai-chat-empty" },
      "Ask about the active cluster — pods, events, costs, root causes, anything the read-only tools can see."));
    return;
  }
  // Not renderInto(): messages must be direct siblings of the flex
  // container for its `gap` spacing to apply between bubbles, and
  // renderInto() only accepts a single node, not a list.
  container.replaceChildren(...state.aiChatMessages.map((m) =>
    h("div", { className: `ai-chat-msg ai-chat-msg--${m.role}` }, m.text)
  ));
  container.scrollTop = container.scrollHeight;
}

function updateAIChatSubtitle() {
  const el = $("ai-chat-subtitle");
  if (el) el.textContent = state.selectedClusterId ? `Scoped to ${state.selectedClusterId}` : "No cluster selected";
}

function openAIChatPanel() {
  const panel = $("ai-chat-panel");
  if (!panel) return;
  panel.classList.add("is-open");
  panel.setAttribute("aria-hidden", "false");
  updateAIChatSubtitle();
  renderAIChatMessages();
  $("ai-chat-input")?.focus();
}

function closeAIChatPanel() {
  const panel = $("ai-chat-panel");
  if (!panel) return;
  panel.classList.remove("is-open");
  panel.setAttribute("aria-hidden", "true");
}

function toggleAIChatPanel() {
  const panel = $("ai-chat-panel");
  if (panel && panel.classList.contains("is-open")) closeAIChatPanel();
  else openAIChatPanel();
}

function resetAIChat() {
  state.aiChatSessionId = null;
  state.aiChatMessages = [];
  if ($("ai-chat-panel")?.classList.contains("is-open")) {
    updateAIChatSubtitle();
    renderAIChatMessages();
  }
}

async function sendAIChatMessage() {
  const input = $("ai-chat-input");
  const sendBtn = $("ai-chat-send");
  const message = (input?.value || "").trim();
  if (!message || !state.selectedClusterId || sendBtn?.disabled) return;

  state.aiChatMessages.push({ role: "user", text: message });
  input.value = "";
  input.style.height = "auto";
  if (sendBtn) sendBtn.disabled = true;
  renderAIChatMessages();

  const pending = { role: "pending", text: "Thinking…" };
  state.aiChatMessages.push(pending);
  renderAIChatMessages();

  try {
    const result = await api.aiChat(state.selectedClusterId, { message, sessionId: state.aiChatSessionId });
    state.aiChatMessages.pop(); // drop the pending placeholder
    if (result.error) {
      state.aiChatMessages.push({ role: "error", text: result.error });
    } else {
      if (result.session_id) state.aiChatSessionId = result.session_id;
      state.aiChatMessages.push({ role: "assistant", text: result.answer || "No response." });
    }
  } catch (err) {
    state.aiChatMessages.pop();
    state.aiChatMessages.push({ role: "error", text: err.message || "Request failed." });
  } finally {
    if (sendBtn) sendBtn.disabled = false;
    renderAIChatMessages();
    input?.focus();
  }
}

function initAIChatPanel() {
  $("ai-chat-fab")?.addEventListener("click", toggleAIChatPanel);
  $("ai-chat-close")?.addEventListener("click", closeAIChatPanel);
  $("ai-chat-send")?.addEventListener("click", sendAIChatMessage);

  const input = $("ai-chat-input");
  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendAIChatMessage();
    }
  });
  // Auto-grow up to the CSS max-height, then the textarea's own scrollbar takes over.
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
      if (state.activeView === "resourcemap") loadResourceMap();
    });
  }
  return resourceMapController;
}

async function loadResourceMap() {
  const token = nextLoadToken("resourcemap");
  const controller = getResourceMapController();
  try {
    const ns = $("resourcemap-namespace-select")?.value || "default";
    const graph = await api.getNamespaceMap(state.selectedClusterId, ns);
    if (isStaleToken("resourcemap", token)) return;
    controller.setGraphData(graph);
    const rootId = $("resourcemap-resource-select")?.value || "";
    const depth = Number($("resourcemap-depth-select")?.value || 3);
    controller.filterAndRender(rootId, depth);
    controller.resetView();
  } catch (err) {
    if (isStaleToken("resourcemap", token)) return;
    console.error("Resource Map load error:", err);
  }
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
};


async function loadActiveView() {
  const loader = viewLoaders[state.activeView];
  if (!loader || !state.selectedClusterId) return;
  await loader();
  updateLastUpdated();
  loadSidebarCounts(); // fire-and-forget -- never blocks the view that was actually requested
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

async function loadSidebarCounts() {
  if (!state.selectedClusterId) return;
  const clusterId = state.selectedClusterId;
  try {
    // Used to also fetch getWorkloads() here for a Pods sidebar badge --
    // Pods no longer has its own nav item (folded into Overview's stat
    // cards, same as the other workload types), so that fetch is gone too.
    const nodes = await api.getNodes(clusterId);
    if (state.selectedClusterId !== clusterId) return; // cluster changed mid-flight
    setSidebarCount("count-nodes", nodes.count ?? nodes.items?.length);
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
    if (document.visibilityState === "visible" && state.selectedClusterId) loadActiveView();
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
  $("refresh-now").addEventListener("click", () => loadActiveView());

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
          // Accordion: opening one group closes the others, so the sidebar
          // doesn't grow into one long always-expanded list again as more
          // groups/items get added over time. Closing the open group (click
          // to collapse it) doesn't touch its siblings.
          if (wasCollapsed) collapseOtherNavGroups(items);
          setNavGroupCollapsed(items, !wasCollapsed);
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

  $("workloads-namespace-select")?.addEventListener("change", () => state.activeView === "workloads" && loadWorkloads());
  // Search filters the already-fetched catalogData client-side -- no refetch needed.
  $("workloads-catalog-search")?.addEventListener("input", () => state.activeView === "workloads" && catalogTypeFilter && renderCatalogPanelBody());
  $("workloads-catalog-close")?.addEventListener("click", () => catalogTypeFilter && toggleWorkloadCatalogType(catalogTypeFilter));
  $("applications-namespace-select")?.addEventListener("change", () => state.activeView === "applications" && loadLogicalApplications());
  $("diagnostics-reanalyze-btn")?.addEventListener("click", () => state.activeView === "diagnostics" && loadDiagnosticsRCA());
  $("audit-type-select")?.addEventListener("change", () => state.activeView === "audit" && loadAuditLogsView());

  $("namespace-select")?.addEventListener("change", () => state.activeView === "namespaces" && loadNamespaces());
  $("namespaces-include-system")?.addEventListener("change", async () => {
    await populateNamespaceSelects(state.selectedClusterId);
    if (state.activeView === "namespaces") loadNamespaces();
  });
  $("map-namespace-select")?.addEventListener("change", () => state.activeView === "map" && loadNamespaceMap());
  $("network-namespace-select")?.addEventListener("change", () => state.activeView === "network" && loadNetworkTopology());
  $("storage-namespace-select")?.addEventListener("change", () => state.activeView === "storage" && loadStorageTopology());
  $("events-namespace-select")?.addEventListener("change", () => state.activeView === "events" && loadEvents());

  $("secret-modal-close")?.addEventListener("click", closeSecretModal);
  $("secret-modal-cancel")?.addEventListener("click", closeSecretModal);
  $("secret-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "secret-modal") closeSecretModal();
  });
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

  // Rollout & Pod Inspector Modal event bindings
  $("rollout-modal-close")?.addEventListener("click", closeRolloutModal);
  $("rollout-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "rollout-modal") closeRolloutModal();
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
    if (!$("secret-modal").hidden) closeSecretModal();
    if (!$("resource-modal").hidden) closeResourceModal();
    if (!$("onboard-modal").hidden) $("onboard-modal").hidden = true;
    if (!$("logs-modal").hidden) closeLogsModal();
    if (!$("manifest-modal").hidden) closeManifestModal();
    if (!$("rollout-modal").hidden) closeRolloutModal();
    if (!$("pod-inspector-modal").hidden) closePodInspectorModal();
    if (!$("ai-diagnose-modal").hidden) closeAIDiagnoseModal();
  });

  initOnboardModal();
  initClustersModal();
  initAIChatPanel();

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

function renderClusterPanel(clusters) {
  state.allClusters = clusters;
  const container = $("sidebar-active-cluster-card");
  const viewAllBtnText = $("view-all-clusters-text");
  
  if (viewAllBtnText) {
    viewAllBtnText.textContent = `View All Clusters (${clusters ? clusters.length : 0})`;
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
          <span style="font-size:0.82rem;font-weight:700;color:var(--color-fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${activeCluster.name}">
            ${activeCluster.name}
          </span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;margin-top:2px;">
          <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--success);box-shadow:0 0 0 2px rgba(16,185,129,0.2);"></span>
          <span style="font-size:0.65rem;font-weight:600;padding:1px 6px;border-radius:99px;background:${envColor}20;color:${envColor};text-transform:uppercase;letter-spacing:0.04em;">${activeCluster.environment}</span>
        </div>
      </div>
    </div>
  `;
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
              <span style="font-size:0.88rem;font-weight:700;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${c.name}</span>
              <span style="font-size:0.65rem;font-weight:600;padding:2px 7px;border-radius:99px;background:${envColor}20;color:${envColor};text-transform:uppercase;letter-spacing:0.04em;">${c.environment}</span>
            </div>
            <div style="font-size:0.75rem;color:var(--fg-subtle);margin-top:2px;">Provider: ${c.provider.toUpperCase()} &bull; ID: ${c.id}</div>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;">
          ${
            isActive
              ? `<span style="font-size:0.78rem;font-weight:700;color:var(--accent);padding:6px 12px;border-radius:8px;background:var(--accent-soft);display:inline-flex;align-items:center;gap:4px;">
                  <span style="width:6px;height:6px;border-radius:50%;background:var(--accent);"></span> Active
                 </span>`
              : `<button type="button" class="connect-cluster-action-btn" data-id="${c.id}" style="
                  padding:6px 14px;border-radius:8px;border:1.5px solid var(--border);
                  background:transparent;color:var(--fg);font-size:0.78rem;font-weight:600;
                  cursor:pointer;font-family:inherit;transition:all 150ms;
                 " onmouseenter="this.style.background='var(--bg-hover)';this.style.borderColor='var(--accent)'" onmouseleave="this.style.background='transparent';this.style.borderColor='var(--border)'">Connect</button>`
          }
          <button type="button" class="delete-cluster-action-btn" data-id="${c.id}" title="Remove cluster '${c.name}'" style="
            width:32px;height:32px;border-radius:8px;border:1.5px solid transparent;
            background:transparent;color:var(--fg-subtle);display:inline-flex;align-items:center;
            justify-content:center;cursor:pointer;transition:all 150ms;
          " onmouseenter="this.style.background='rgba(239,68,68,0.12)';this.style.color='var(--danger)'" onmouseleave="this.style.background='transparent';this.style.color='var(--fg-subtle)'">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M10 11v6M14 11v6"/></svg>
          </button>
        </div>
      `;

      const connectBtn = item.querySelector(".connect-cluster-action-btn");
      connectBtn?.addEventListener("click", async () => {
        await connectToCluster(c.id);
        renderList(searchInput?.value || "");
        if (typeof modal.close === "function") modal.close();
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
    const onboardModal = $("onboard-modal");
    if (onboardModal && typeof onboardModal.showModal === "function") {
      onboardModal.showModal();
    }
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

function initOnboardModal() {
  const modal = $("onboard-modal");
  const trigger = $("onboard-trigger");
  const closeBtn = $("onboard-modal-close");
  const cancelBtn = $("onboard-modal-cancel");
  const saveBtn = $("onboard-save-btn");
  const statusEl = $("onboard-status");

  if (!modal || !trigger) return;

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

  function openModal() {
    hideStatus();
    if (typeof modal.showModal === "function") {
      modal.showModal();
    } else {
      modal.hidden = false;
    }
  }

  function closeModal() {
    hideStatus();
    if (typeof modal.close === "function") {
      modal.close();
    } else {
      modal.hidden = true;
    }
  }

  trigger.addEventListener("click", openModal);
  closeBtn?.addEventListener("click", closeModal);
  cancelBtn?.addEventListener("click", closeModal);

  // Provider Tab Switching
  const providerTabs = document.querySelectorAll(".provider-tab");
  const colors = { gke: "#4285F4", eks: "#FF9900", aks: "#0078D4", local: "#188038" };
  const softBgs = {
    gke: "color-mix(in srgb, #4285F4 12%, transparent)",
    eks: "color-mix(in srgb, #FF9900 12%, transparent)",
    aks: "color-mix(in srgb, #0078D4 12%, transparent)",
    local: "color-mix(in srgb, #188038 12%, transparent)",
  };

  providerTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const provider = tab.dataset.provider;
      $("onboard-provider").value = provider;

      providerTabs.forEach((t) => {
        const p = t.dataset.provider;
        const isSelected = p === provider;
        t.setAttribute("aria-pressed", isSelected ? "true" : "false");
        t.style.borderColor = isSelected ? colors[p] : "var(--border)";
        t.style.background = isSelected ? softBgs[p] : "transparent";
        const span = t.querySelector("span");
        if (span) span.style.color = isSelected ? colors[p] : "var(--fg-muted)";
      });

      document.querySelectorAll(".provider-fields").forEach((pf) => (pf.style.display = "none"));
      const pfEl = $(`provider-fields-${provider}`);
      if (pfEl) pfEl.style.display = "flex";
    });
  });

  // Environment Pill Selector
  const envInputs = document.querySelectorAll('input[name="onboard-env"]');
  envInputs.forEach((input) => {
    input.addEventListener("change", () => {
      const val = input.value;
      $("onboard-environment").value = val;
      document.querySelectorAll(".env-pill-opt").forEach((pill) => {
        const isMatch = pill.dataset.val === val;
        pill.style.background = isMatch ? "var(--grad-pill-selected)" : "transparent";
        pill.style.borderColor = isMatch ? "var(--accent)" : "var(--border)";
        pill.style.color = isMatch ? "var(--accent)" : "var(--fg-muted)";
      });
    });
  });

  function getFormPayload() {
    const provider = $("onboard-provider").value;
    const name = $("onboard-name").value.trim();
    const id = name.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "") || "cluster-" + Date.now();
    const team_id = (state.selectedTeamId || "platform").toLowerCase();
    const environment = $("onboard-environment").value;

    let config = {};
    if (provider === "gke") {
      config = {
        project: $("gke-project").value.trim(),
        region: $("gke-region").value.trim(),
        cluster_name: $("gke-cluster-name").value.trim() || name,
        impersonate_service_account: $("gke-impersonate-sa")?.value?.trim() || undefined,
      };
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
        log_analytics_workspace_id: $("aks-log-analytics-workspace")?.value?.trim() || undefined,
      };
    } else if (provider === "local") {
      config = {
        kubeconfig_path: $("local-kubeconfig-path").value.trim(),
        context_name: $("local-context-select")?.value || undefined,
      };
    }

    return { id, name: name || id, provider, environment, team_id, team_name: team_id.toUpperCase(), config };
  }

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
    const payload = getFormPayload();
    if (!payload.name) {
      showStatus("Please enter a Cluster Name", "error");
      return;
    }
    showStatus("Registering cluster...", "info");
    try {
      const res = await api.onboardCluster(payload);
      state.teams = res.teams;
      populateTeamSelect();
      const clusterSelect = $("cluster-select");
      if (clusterSelect) {
        clusterSelect.value = payload.id;
        await onClusterChange();
      }
      showStatus("✓ Cluster registered successfully!", "success");
      loadClusterPanel();
      setTimeout(closeModal, 1000);
    } catch (err) {
      showStatus(`✗ Registration Failed: ${err.message}`, "error");
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
  $("auth-mode-text").textContent = `auth: ${status.auth_mode}`;

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
  if (clusterId) await onClusterChange();
  switchView(state.activeView || "overview");
  startAutoRefresh();
  loadClusterPanel(); // populate sidebar cluster connections panel

  // Fonts (Plus Jakarta Sans) may still be swapping in, which shifts tab
  // widths -- position the indicator once now and once more after they load.
  moveNavIndicator(state.activeView);
  document.fonts?.ready.then(() => moveNavIndicator(state.activeView));
}

document.addEventListener("DOMContentLoaded", bootstrap);
