// Thin fetch wrapper around the KubeOrbit REST API. Every function here
// returns parsed JSON or throws an ApiError with a human-readable message --
// callers (app.js) never touch `fetch` or Response objects directly.

const API_KEY_STORAGE_KEY = "kubeorbit_api_key";

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function getStoredApiKey() {
  return localStorage.getItem(API_KEY_STORAGE_KEY) || "";
}

export function setStoredApiKey(key) {
  if (key) localStorage.setItem(API_KEY_STORAGE_KEY, key);
  else localStorage.removeItem(API_KEY_STORAGE_KEY);
}

async function apiFetch(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  const apiKey = getStoredApiKey();
  if (apiKey) headers["X-API-Key"] = apiKey;
  if (options.body) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (networkErr) {
    throw new ApiError(0, "NetworkError", "Could not reach the KubeOrbit API (network error).");
  }

  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const message = (payload && (payload.message || payload.detail)) || `Request failed (${response.status})`;
    const code = (payload && payload.error) || `HTTP_${response.status}`;
    throw new ApiError(response.status, code, message);
  }
  return payload;
}

function qs(params) {
  const usable = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (!usable.length) return "";
  return "?" + new URLSearchParams(usable).toString();
}

export const api = {
  getHealth: () => apiFetch("/api/health"),
  getStatus: () => apiFetch("/api/status"),
  getTeams: () => apiFetch("/api/teams"),

  switchCluster: (clusterId) => apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/switch`, { method: "POST" }),
  getClusterInfo: (clusterId) => apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/info`),
  getNodes: (clusterId) => apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/nodes`),

  getNamespaces: (clusterId, includeSystem = false) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/namespaces${qs({ include_system: includeSystem })}`),

  getWorkloads: (clusterId, namespace) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/workloads${qs({ namespace })}`),

  getNamespaceResources: (clusterId, namespace) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/resources`),

  revealSecret: (clusterId, namespace, name) =>
    apiFetch(
      `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/secrets/${encodeURIComponent(name)}/reveal`,
      { method: "POST" }
    ),

  getNetworkTopology: (clusterId, namespace) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/topology/network${qs({ namespace })}`),

  getStorageTopology: (clusterId, namespace) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/topology/storage${qs({ namespace })}`),

  getNamespaceMap: (clusterId, namespace) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/topology/map${qs({ namespace })}`),

  getClusterWorkloadMap: (clusterId) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/topology/clustermap`),

  getEvents: (clusterId, namespace, limit = 100) =>

    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/events${qs({ namespace, limit })}`),

  getHelm: (clusterId) => apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/helm`),

  getEcosystemOverview: (clusterId) => apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/ecosystem`),

  getCostEstimate: (clusterId) => apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/cost`),

  testOnboardCluster: (data) =>
    apiFetch("/api/admin/clusters/test-connection", { method: "POST", body: JSON.stringify(data) }),

  parseKubeconfig: (kubeconfig_path) =>
    apiFetch("/api/admin/clusters/parse-kubeconfig", { method: "POST", body: JSON.stringify({ kubeconfig_path }) }),

  listClusters: () =>
    apiFetch("/api/admin/clusters"),

  onboardCluster: (data) =>
    apiFetch("/api/admin/clusters", { method: "POST", body: JSON.stringify(data) }),

  updateCluster: (clusterId, data) =>
    apiFetch(`/api/admin/clusters/${encodeURIComponent(clusterId)}`, { method: "PUT", body: JSON.stringify(data) }),

  deleteCluster: (clusterId) =>
    apiFetch(`/api/admin/clusters/${encodeURIComponent(clusterId)}`, { method: "DELETE" }),

  getPodLogs: (clusterId, namespace, podName, tailLines = 200, container = null) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/pods/${encodeURIComponent(podName)}/logs${qs({ tail_lines: tailLines, container })}`),

  getResourceYaml: (clusterId, namespace, kind, name) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/manifest${qs({ namespace, kind, name })}`),

  // Enterprise Extensions
  getWorkloadsCatalog: (clusterId, namespace) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/workloads/catalog${qs({ namespace })}`),

  getRolloutHistory: (clusterId, namespace, workloadType, name) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/workloads/${encodeURIComponent(workloadType)}/${encodeURIComponent(name)}/rollout/history${qs({ namespace })}`),

  rollbackWorkload: (clusterId, namespace, workloadType, name, toRevision = null) =>
    apiFetch(
      `/api/clusters/${encodeURIComponent(clusterId)}/workloads/${encodeURIComponent(workloadType)}/${encodeURIComponent(name)}/rollback${qs({ namespace })}`,
      { method: "POST", body: JSON.stringify({ to_revision: toRevision }) }
    ),

  getLogicalApplications: (clusterId, namespace) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/applications${qs({ namespace })}`),

  getPodDetails: (clusterId, namespace, podName) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/pods/${encodeURIComponent(podName)}/details`),

  getExtendedEvents: (clusterId, namespace, limit = 200) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/events/extended${qs({ namespace, limit })}`),

  getAuditLogs: (clusterId = null, limit = 50) =>
    apiFetch(`/api/audit/logs${qs({ cluster_id: clusterId, limit })}`),

  getDiagnosticsRCA: (clusterId, namespace) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/diagnostics/rca${qs({ namespace })}`),

  aiDiagnose: (clusterId, { namespace, resourceType, resourceName, question } = {}) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/ai/diagnose`, {
      method: "POST",
      body: JSON.stringify({ namespace, resource_type: resourceType, resource_name: resourceName, question }),
    }),

  aiChat: (clusterId, { message, sessionId } = {}) =>
    apiFetch(`/api/clusters/${encodeURIComponent(clusterId)}/ai/chat`, {
      method: "POST",
      body: JSON.stringify({ message, session_id: sessionId }),
    }),
};

