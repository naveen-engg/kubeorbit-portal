// Interactive MCP & AG-UI Tool Result Renderers for KubeOrbit Portal.
// Converts raw MCP JSON responses into structured, filterable, drill-down UI cards
// backed by real portal actions (navigate, inspect pod, open logs, view YAML, 12-step filter, Ask AI).

import { h } from "./components.js";

const SYSTEM_NS_PREFIXES = [
  "kube-",
  "gke-",
  "gmp-",
  "istio-",
  "asm-",
  "cert-manager",
  "gatekeeper-",
  "config-management-",
  "cnrm-",
];

export function safeJsonParse(raw) {
  if (raw === null || raw === undefined || raw === "") return null;
  if (typeof raw === "object") return raw;
  try {
    return JSON.parse(String(raw));
  } catch {
    return null;
  }
}

function isSystemNamespace(name) {
  const n = String(name || "").toLowerCase();
  if (!n) return false;
  if (n === "default") return false;
  return SYSTEM_NS_PREFIXES.some((p) => n.startsWith(p));
}

function miniBtn(label, onClick, variant = "default") {
  const cls =
    variant === "primary"
      ? "agui-iv-mini-btn agui-iv-mini-btn--primary"
      : variant === "ai"
      ? "agui-iv-mini-btn agui-iv-mini-btn--ai"
      : "agui-iv-mini-btn";
  return h(
    "button",
    {
      type: "button",
      className: cls,
      onclick: (e) => {
        e.stopPropagation();
        onClick(e);
      },
    },
    label
  );
}

function badge(text, tone = "info") {
  return h("span", { className: `agui-iv-badge agui-iv-badge--${tone}` }, String(text));
}

function statusTone(statusText) {
  const s = String(statusText || "").toLowerCase();
  if (!s) return "info";
  if (/(running|ready|deployed|succeeded|active|healthy|bound|normal|ok|true)/.test(s)) return "ok";
  if (/(pending|progress|warning|degraded|terminating)/.test(s)) return "warn";
  if (/(fail|error|crash|backoff|oom|notready|unhealthy|critical|false|0\/)/.test(s)) return "err";
  return "info";
}

function truncatePreview(val, max = 96) {
  const s = typeof val === "string" ? val : JSON.stringify(val);
  if (!s) return "—";
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

export function extractToolScopeLabel(argsObj) {
  if (!argsObj || typeof argsObj !== "object") return "";
  const parts = [];
  if (argsObj.namespace) parts.push(argsObj.namespace);
  if (argsObj.pod_name) parts.push(argsObj.pod_name);
  else if (argsObj.name && argsObj.kind) parts.push(`${argsObj.kind}/${argsObj.name}`);
  else if (argsObj.name) parts.push(argsObj.name);
  if (argsObj.view) parts.push(argsObj.view);
  return parts.join(" · ");
}

export function summarizeToolResult(toolName, argsObj, resultObj, fallbackStatus = "done") {
  if (!resultObj || typeof resultObj !== "object") {
    return fallbackStatus === "running" ? "running..." : fallbackStatus === "error" ? "failed" : "done";
  }
  if (resultObj.ok === false || resultObj.error) {
    return resultObj.error ? `error: ${truncatePreview(resultObj.error, 34)}` : "failed";
  }

  switch (toolName) {
    case "get_namespaces": {
      const items = Array.isArray(resultObj.items) ? resultObj.items : [];
      return `${resultObj.count ?? items.length} namespaces`;
    }
    case "get_namespace_resources": {
      const pods = (resultObj.pods || []).length;
      const svcs = (resultObj.services || []).length;
      const cms = (resultObj.configmaps || []).length;
      const secs = (resultObj.secrets || []).length;
      const bits = [];
      if (pods) bits.push(`${pods} pods`);
      if (svcs) bits.push(`${svcs} svc`);
      if (cms) bits.push(`${cms} cm`);
      if (secs) bits.push(`${secs} sec`);
      return bits.length ? bits.join(" · ") : "0 resources";
    }
    case "get_helm_releases": {
      const items = Array.isArray(resultObj.items) ? resultObj.items : [];
      return `${items.length} release${items.length === 1 ? "" : "s"}`;
    }
    case "get_events":
    case "get_extended_events": {
      const items = Array.isArray(resultObj.items) ? resultObj.items : [];
      const warns = resultObj.warning_count ?? items.filter((e) => String(e.type).toLowerCase() === "warning").length;
      return warns ? `${warns} warn · ${items.length} events` : `${items.length} events`;
    }
    case "get_nodes": {
      const items = Array.isArray(resultObj.items) ? resultObj.items : [];
      const ready = items.filter((n) => String(n.status).toLowerCase() === "ready" || n.ready === true).length;
      return `${ready}/${items.length} Ready`;
    }
    case "diagnose_cluster_issues": {
      const sum = resultObj.summary || {};
      const score = sum.overall_score ?? resultObj.health_score;
      const stalls = sum.active_failures_count ?? sum.active_root_causes;
      if (score !== undefined) {
        return stalls ? `Score ${score}% · ${stalls} stall${stalls === 1 ? "" : "s"}` : `Score ${score}% · Healthy`;
      }
      return "diagnostics";
    }
    case "get_workloads_overview": {
      const ov = resultObj.overview || resultObj;
      const pods = ov.pods;
      if (pods && pods.total !== undefined) return `${pods.healthy ?? 0}/${pods.total} pods ok`;
      return "workloads";
    }
    case "get_workloads_catalog": {
      const sum = resultObj.summary || {};
      if (sum.total !== undefined) return `${sum.healthy ?? 0}/${sum.total} workloads`;
      const items = Array.isArray(resultObj.items) ? resultObj.items : [];
      return items.length ? `${items.length} workloads` : "catalog";
    }
    case "get_pod_details": {
      const st = resultObj.status || resultObj.phase;
      return st ? String(st) : "pod inspected";
    }
    case "get_pod_logs": {
      const lines = String(resultObj.logs || "").split("\n").filter(Boolean).length;
      return `${lines} log lines`;
    }
    case "get_manifest":
      return "YAML loaded";
    case "get_cost_estimate": {
      const total = resultObj.monthly_estimate_usd ?? resultObj.total_monthly_usd ?? resultObj.total_monthly_cost;
      return total !== undefined ? `~$${Number(total).toFixed(0)}/mo` : "cost estimate";
    }
    case "get_ecosystem_overview": {
      const detected = (resultObj.items || resultObj.detected || []).filter((i) => i.detected !== false);
      return `${detected.length} detected`;
    }
    default: {
      if (toolName && toolName.startsWith("ui_")) {
        return resultObj.action ? `UI: ${resultObj.action}` : "UI synced";
      }
      if (Array.isArray(resultObj.items)) return `${resultObj.items.length} items`;
      if (Array.isArray(resultObj.nodes)) return `${resultObj.nodes.length} nodes`;
      return "done";
    }
  }
}

function renderNamespacesView(argsObj, data, uiState, rerender, cb) {
  const rawItems = Array.isArray(data?.items) ? data.items : [];
  const names = rawItems.map((it) => (typeof it === "string" ? it : it?.name || "")).filter(Boolean);
  const appNs = names.filter((n) => !isSystemNamespace(n));
  const sysNs = names.filter((n) => isSystemNamespace(n));

  if (uiState.selectedNs === undefined) {
    uiState.selectedNs = appNs[0] || names[0] || "";
  }
  const selected = uiState.selectedNs;

  const renderChip = (nsName, isSys = false) =>
    h(
      "button",
      {
        type: "button",
        className: `agui-iv-pill ${selected === nsName ? "is-selected" : ""} ${isSys ? "is-system" : ""}`,
        onclick: (e) => {
          e.stopPropagation();
          uiState.selectedNs = nsName;
          rerender();
        },
      },
      [
        h("span", { className: "agui-iv-pill__dot" }),
        nsName,
      ]
    );

  const sections = [
    h("div", { className: "agui-iv-header" }, [
      h("span", { className: "agui-iv-title" }, `Cluster Namespaces (${names.length})`),
      h("div", { className: "agui-iv-actions" }, [
        miniBtn("Open Namespaces View", () => cb.onNavigate?.("namespaces", selected || ""), "primary"),
      ]),
    ]),
  ];

  if (appNs.length) {
    sections.push(
      h("div", { className: "agui-iv-section-label" }, `Application Namespaces (${appNs.length})`),
      h("div", { className: "agui-iv-pills" }, appNs.map((n) => renderChip(n, false)))
    );
  }

  if (sysNs.length) {
    sections.push(
      h("div", { className: "agui-iv-sys-toggle" }, [
        miniBtn(
          uiState.showSystem ? `Hide System Namespaces (${sysNs.length})` : `Show System Namespaces (${sysNs.length})`,
          () => {
            uiState.showSystem = !uiState.showSystem;
            rerender();
          }
        ),
      ])
    );
    if (uiState.showSystem) {
      sections.push(h("div", { className: "agui-iv-pills" }, sysNs.map((n) => renderChip(n, true))));
    }
  }

  if (selected) {
    sections.push(
      h("div", { className: "agui-iv-actionbar" }, [
        h("span", { className: "agui-iv-actionbar__label" }, [
          "Selected: ",
          h("strong", {}, selected),
        ]),
        h("div", { className: "agui-iv-actions" }, [
          miniBtn("Resources", () => cb.onNavigate?.("namespaces", selected), "primary"),
          miniBtn("Workloads", () => cb.onNavigate?.("workloads", selected)),
          miniBtn("Network", () => cb.onNavigate?.("network", selected)),
          miniBtn("12-Step", () => cb.onFilter12Step?.(selected, null)),
          miniBtn("Ask AI", () => cb.onAskAI?.(`Inspect namespace '${selected}': list pods, services, and any warnings or unhealthy workloads.`), "ai"),
        ]),
      ])
    );
  }

  return h("div", { className: "agui-iv" }, sections);
}

function asArray(maybeArrOrWrapped) {
  if (Array.isArray(maybeArrOrWrapped)) return maybeArrOrWrapped;
  if (maybeArrOrWrapped && Array.isArray(maybeArrOrWrapped.items)) return maybeArrOrWrapped.items;
  return [];
}

function renderNamespaceResourcesView(argsObj, data, uiState, rerender, cb) {
  const ns = data?.namespace || argsObj?.namespace || "default";
  const pods = asArray(data?.pods);
  const services = asArray(data?.services);
  const configmaps = asArray(data?.configmaps);
  const secrets = asArray(data?.secrets);
  const pvcs = asArray(data?.pvcs);
  const gateways = asArray(data?.gateways);
  const httproutes = asArray(data?.httproutes);
  const sas = asArray(data?.serviceaccounts);

  const tabs = [
    { id: "pods", label: "Pods", items: pods },
    { id: "services", label: "Services", items: services },
    { id: "configmaps", label: "ConfigMaps", items: configmaps },
    { id: "secrets", label: "Secrets", items: secrets },
    { id: "pvcs", label: "PVCs", items: pvcs },
    { id: "routes", label: "Routes", items: [...gateways, ...httproutes] },
    { id: "sas", label: "ServiceAccts", items: sas },
  ];

  const nonEmptyFirst = tabs.find((t) => t.items.length > 0)?.id || "pods";
  if (!uiState.tab || !tabs.some((t) => t.id === uiState.tab)) {
    uiState.tab = nonEmptyFirst;
  }

  const activeTab = tabs.find((t) => t.id === uiState.tab) || tabs[0];
  let listNode;

  if (!activeTab.items.length) {
    listNode = h("div", { className: "agui-iv-empty" }, `No ${activeTab.label.toLowerCase()} in namespace '${ns}'.`);
  } else if (activeTab.id === "pods") {
    listNode = h(
      "div",
      { className: "agui-iv-list" },
      pods.map((p) => {
        const st = p.status || p.phase || (p.ready ? "Running" : "Unknown");
        const meta = [
          p.node ? `node: ${p.node}` : null,
          p.restarts ? `restarts: ${p.restarts}` : null,
        ]
          .filter(Boolean)
          .join(" · ");
        return h("div", { className: "agui-iv-row" }, [
          h("div", { className: "agui-iv-row__main" }, [
            h("div", { className: "agui-iv-row__title" }, [
              badge(st, statusTone(p.ready === false && st === "Running" ? "warn" : st)),
              h("span", { className: "agui-iv-mono" }, p.name || "pod"),
            ]),
            meta ? h("div", { className: "agui-iv-row__sub" }, meta) : null,
          ]),
          h("div", { className: "agui-iv-actions" }, [
            miniBtn("Inspect", () => cb.onInspectPod?.(p.name, ns), "primary"),
            miniBtn("Logs", () => cb.onOpenLogs?.(p.name, ns)),
            miniBtn("YAML", () => cb.onOpenManifest?.("Pod", p.name, ns)),
          ]),
        ]);
      })
    );
  } else if (activeTab.id === "services") {
    listNode = h(
      "div",
      { className: "agui-iv-list" },
      services.map((s) => {
        const portsStr = Array.isArray(s.ports)
          ? s.ports.map((pt) => (typeof pt === "object" ? `${pt.port}${pt.target_port ? `→${pt.target_port}` : ""}` : String(pt))).join(", ")
          : "";
        const sub = [s.type || "ClusterIP", s.cluster_ip || s.clusterIP, portsStr ? `ports: ${portsStr}` : null]
          .filter(Boolean)
          .join(" · ");
        return h("div", { className: "agui-iv-row" }, [
          h("div", { className: "agui-iv-row__main" }, [
            h("div", { className: "agui-iv-row__title" }, [
              badge(s.type || "Service", "info"),
              h("span", { className: "agui-iv-mono" }, s.name || "service"),
            ]),
            sub ? h("div", { className: "agui-iv-row__sub" }, sub) : null,
          ]),
          h("div", { className: "agui-iv-actions" }, [
            miniBtn("Network", () => cb.onNavigate?.("network", ns), "primary"),
            miniBtn("YAML", () => cb.onOpenManifest?.("Service", s.name, ns)),
          ]),
        ]);
      })
    );
  } else if (activeTab.id === "configmaps" || activeTab.id === "secrets") {
    const kind = activeTab.id === "configmaps" ? "ConfigMap" : "Secret";
    listNode = h(
      "div",
      { className: "agui-iv-list" },
      activeTab.items.map((item) => {
        const keys = Array.isArray(item.keys) ? item.keys : Object.keys(item.data || {});
        const sub = [
          item.type || null,
          keys.length ? `${keys.length} key(s): ${keys.slice(0, 4).join(", ")}${keys.length > 4 ? "…" : ""}` : "0 keys",
        ]
          .filter(Boolean)
          .join(" · ");
        return h("div", { className: "agui-iv-row" }, [
          h("div", { className: "agui-iv-row__main" }, [
            h("div", { className: "agui-iv-row__title" }, [
              badge(kind, activeTab.id === "secrets" ? "warn" : "info"),
              h("span", { className: "agui-iv-mono" }, item.name || kind),
            ]),
            h("div", { className: "agui-iv-row__sub" }, sub),
          ]),
          h("div", { className: "agui-iv-actions" }, [
            miniBtn("YAML", () => cb.onOpenManifest?.(kind, item.name, ns)),
          ]),
        ]);
      })
    );
  } else {
    const kindGuess = activeTab.id === "pvcs" ? "PersistentVolumeClaim" : activeTab.id === "sas" ? "ServiceAccount" : null;
    listNode = h(
      "div",
      { className: "agui-iv-list" },
      activeTab.items.map((item) => {
        const kind = item.kind || kindGuess || "Resource";
        const sub = [item.status, item.storage_class, item.capacity, item.created_at].filter(Boolean).join(" · ");
        return h("div", { className: "agui-iv-row" }, [
          h("div", { className: "agui-iv-row__main" }, [
            h("div", { className: "agui-iv-row__title" }, [
              badge(item.status || kind, statusTone(item.status || "info")),
              h("span", { className: "agui-iv-mono" }, item.name || "item"),
            ]),
            sub ? h("div", { className: "agui-iv-row__sub" }, sub) : null,
          ]),
          h("div", { className: "agui-iv-actions" }, [
            item.name ? miniBtn("YAML", () => cb.onOpenManifest?.(kind, item.name, ns)) : null,
          ].filter(Boolean)),
        ]);
      })
    );
  }

  return h("div", { className: "agui-iv" }, [
    h("div", { className: "agui-iv-header" }, [
      h("span", { className: "agui-iv-title" }, [
        "Namespace: ",
        h("span", { className: "agui-iv-mono" }, ns),
      ]),
      h("div", { className: "agui-iv-actions" }, [
        miniBtn("Open Namespace", () => cb.onNavigate?.("namespaces", ns), "primary"),
        miniBtn("Network", () => cb.onNavigate?.("network", ns)),
        miniBtn("12-Step", () => cb.onFilter12Step?.(ns, null)),
      ]),
    ]),
    h(
      "div",
      { className: "agui-iv-tabs" },
      tabs.map((t) =>
        h(
          "button",
          {
            type: "button",
            className: `agui-iv-tab ${uiState.tab === t.id ? "is-active" : ""}`,
            onclick: (e) => {
              e.stopPropagation();
              uiState.tab = t.id;
              rerender();
            },
          },
          `${t.label} (${t.items.length})`
        )
      )
    ),
    listNode,
  ]);
}

function renderHelmReleasesView(argsObj, data, uiState, rerender, cb) {
  const items = asArray(data?.items);
  return h("div", { className: "agui-iv" }, [
    h("div", { className: "agui-iv-header" }, [
      h("span", { className: "agui-iv-title" }, `Helm Releases (${items.length})`),
      h("div", { className: "agui-iv-actions" }, [
        miniBtn("Open Helm View", () => cb.onNavigate?.("helm", ""), "primary"),
      ]),
    ]),
    items.length
      ? h(
          "div",
          { className: "agui-iv-list" },
          items.map((rel) =>
            h("div", { className: "agui-iv-row" }, [
              h("div", { className: "agui-iv-row__main" }, [
                h("div", { className: "agui-iv-row__title" }, [
                  badge(rel.status || "deployed", statusTone(rel.status || "deployed")),
                  h("span", { className: "agui-iv-mono" }, rel.name || "release"),
                  rel.namespace ? badge(`ns: ${rel.namespace}`, "info") : null,
                ].filter(Boolean)),
                h(
                  "div",
                  { className: "agui-iv-row__sub" },
                  [rel.chart, rel.app_version ? `app v${rel.app_version}` : null, rel.revision ? `rev ${rel.revision}` : null]
                    .filter(Boolean)
                    .join(" · ")
                ),
              ]),
              h("div", { className: "agui-iv-actions" }, [
                rel.namespace ? miniBtn("Namespace", () => cb.onNavigate?.("namespaces", rel.namespace), "primary") : null,
                rel.namespace ? miniBtn("12-Step", () => cb.onFilter12Step?.(rel.namespace, null)) : null,
                miniBtn("Ask AI", () => cb.onAskAI?.(`Inspect Helm release '${rel.name}' in namespace '${rel.namespace || "default"}': show pods, service endpoints, and health.`), "ai"),
              ].filter(Boolean)),
            ])
          )
        )
      : h("div", { className: "agui-iv-empty" }, "No Helm releases found in this cluster."),
  ]);
}

function renderEventsView(argsObj, data, uiState, rerender, cb) {
  const items = asArray(data?.items);
  if (!uiState.filter) uiState.filter = "all";
  const warns = items.filter((e) => String(e.type || "").toLowerCase() === "warning");
  const visible = (uiState.filter === "warning" ? warns : items).slice(0, 25);

  return h("div", { className: "agui-iv" }, [
    h("div", { className: "agui-iv-header" }, [
      h("span", { className: "agui-iv-title" }, `Events (${items.length} · ${warns.length} warnings)`),
      h("div", { className: "agui-iv-actions" }, [
        miniBtn(`All (${items.length})`, () => { uiState.filter = "all"; rerender(); }, uiState.filter === "all" ? "primary" : "default"),
        miniBtn(`Warnings (${warns.length})`, () => { uiState.filter = "warning"; rerender(); }, uiState.filter === "warning" ? "primary" : "default"),
        miniBtn("Open Events View", () => cb.onNavigate?.("events", argsObj?.namespace || ""), "primary"),
      ]),
    ]),
    visible.length
      ? h(
          "div",
          { className: "agui-iv-list" },
          visible.map((ev) => {
            const isPod = String(ev.involved_kind || "").toLowerCase() === "pod" && ev.involved_name;
            const kindObj = [ev.involved_kind, ev.involved_name].filter(Boolean).join("/");
            return h("div", { className: "agui-iv-row" }, [
              h("div", { className: "agui-iv-row__main" }, [
                h("div", { className: "agui-iv-row__title" }, [
                  badge(ev.reason || ev.type || "Event", String(ev.type).toLowerCase() === "warning" ? "warn" : "ok"),
                  kindObj ? h("span", { className: "agui-iv-mono" }, kindObj) : null,
                  ev.namespace ? badge(ev.namespace, "info") : null,
                ].filter(Boolean)),
                h("div", { className: "agui-iv-row__sub" }, truncatePreview(ev.message || "", 150)),
              ]),
              h("div", { className: "agui-iv-actions" }, [
                isPod ? miniBtn("Inspect Pod", () => cb.onInspectPod?.(ev.involved_name, ev.namespace || "default"), "primary") : null,
                ev.involved_kind && ev.involved_name ? miniBtn("YAML", () => cb.onOpenManifest?.(ev.involved_kind, ev.involved_name, ev.namespace || "default")) : null,
              ].filter(Boolean)),
            ]);
          })
        )
      : h("div", { className: "agui-iv-empty" }, "No matching events."),
  ]);
}

function renderNodesView(argsObj, data, uiState, rerender, cb) {
  const items = asArray(data?.items);
  return h("div", { className: "agui-iv" }, [
    h("div", { className: "agui-iv-header" }, [
      h("span", { className: "agui-iv-title" }, `Nodes (${items.length})`),
      h("div", { className: "agui-iv-actions" }, [
        miniBtn("Open Nodes View", () => cb.onNavigate?.("nodes", ""), "primary"),
      ]),
    ]),
    items.length
      ? h(
          "div",
          { className: "agui-iv-list" },
          items.map((n) => {
            const st = n.status || (n.ready ? "Ready" : "NotReady");
            const sub = [
              n.node_pool ? `pool: ${n.node_pool}` : null,
              n.zone ? `zone: ${n.zone}` : null,
              n.cpu_capacity ? `CPU: ${n.cpu_capacity}` : null,
              n.memory_capacity ? `Mem: ${n.memory_capacity}` : null,
            ]
              .filter(Boolean)
              .join(" · ");
            return h("div", { className: "agui-iv-row" }, [
              h("div", { className: "agui-iv-row__main" }, [
                h("div", { className: "agui-iv-row__title" }, [
                  badge(st, statusTone(st)),
                  h("span", { className: "agui-iv-mono" }, n.name || "node"),
                ]),
                sub ? h("div", { className: "agui-iv-row__sub" }, sub) : null,
              ]),
              h("div", { className: "agui-iv-actions" }, [
                n.name ? miniBtn("YAML", () => cb.onOpenManifest?.("Node", n.name, "")) : null,
              ].filter(Boolean)),
            ]);
          })
        )
      : h("div", { className: "agui-iv-empty" }, "No nodes returned."),
  ]);
}

function renderWorkloadsView(toolName, argsObj, data, uiState, rerender, cb) {
  const ns = data?.namespace || argsObj?.namespace || "";
  if (toolName === "get_workloads_overview") {
    const ov = data?.overview || data || {};
    const kinds = Object.entries(ov).filter(([, v]) => v && typeof v === "object" && ("total" in v || "healthy" in v));
    return h("div", { className: "agui-iv" }, [
      h("div", { className: "agui-iv-header" }, [
        h("span", { className: "agui-iv-title" }, `Workloads Health${ns ? ` · ${ns}` : ""}`),
        h("div", { className: "agui-iv-actions" }, [
          miniBtn("Open Workloads", () => cb.onNavigate?.("workloads", ns), "primary"),
          miniBtn("12-Step", () => cb.onFilter12Step?.(ns, null)),
        ]),
      ]),
      h(
        "div",
        { className: "agui-iv-kv-grid" },
        kinds.map(([k, v]) => {
          const total = v.total ?? 0;
          const healthy = v.healthy ?? 0;
          const tone = total > 0 && healthy < total ? "err" : "ok";
          return h("div", { className: "agui-iv-kv" }, [
            h("span", { className: "agui-iv-kv__label" }, k),
            badge(`${healthy}/${total} ok`, tone),
          ]);
        })
      ),
    ]);
  }

  // get_workloads_catalog or get_applications
  let items = asArray(data?.items || data?.applications);
  if (!items.length && data && typeof data === "object") {
    for (const k of ["deployments", "statefulsets", "daemonsets", "jobs", "cronjobs"]) {
      if (Array.isArray(data[k])) {
        items = items.concat(data[k].map((w) => ({ ...w, _kind: k.slice(0, -1) })));
      }
    }
  }

  return h("div", { className: "agui-iv" }, [
    h("div", { className: "agui-iv-header" }, [
      h("span", { className: "agui-iv-title" }, `${toolName === "get_applications" ? "Applications" : "Workloads"} (${items.length})`),
      h("div", { className: "agui-iv-actions" }, [
        miniBtn(toolName === "get_applications" ? "Open Applications" : "Open Workloads", () => cb.onNavigate?.(toolName === "get_applications" ? "applications" : "workloads", ns), "primary"),
      ]),
    ]),
    items.length
      ? h(
          "div",
          { className: "agui-iv-list" },
          items.slice(0, 20).map((w) => {
            const wNs = w.namespace || ns || "default";
            const kind = w.kind || w._kind || "Deployment";
            const healthyStr = w.healthy === false ? "Unhealthy" : w.status || (w.ready !== undefined ? `${w.ready}/${w.desired ?? w.replicas ?? w.ready} Ready` : "Healthy");
            return h("div", { className: "agui-iv-row" }, [
              h("div", { className: "agui-iv-row__main" }, [
                h("div", { className: "agui-iv-row__title" }, [
                  badge(healthyStr, statusTone(healthyStr)),
                  h("span", { className: "agui-iv-mono" }, w.name || "workload"),
                  wNs ? badge(wNs, "info") : null,
                ].filter(Boolean)),
              ]),
              h("div", { className: "agui-iv-actions" }, [
                w.name ? miniBtn("YAML", () => cb.onOpenManifest?.(kind, w.name, wNs)) : null,
                w.name ? miniBtn("Ask AI", () => cb.onAskAI?.(`Check rollout history, pods, and logs for ${kind}/${w.name} in namespace '${wNs}'.`), "ai") : null,
              ].filter(Boolean)),
            ]);
          })
        )
      : h("div", { className: "agui-iv-empty" }, "No items found."),
  ]);
}

function renderDiagnosticsRCAView(argsObj, data, uiState, rerender, cb) {
  const sum = data?.summary || {};
  const ns = argsObj?.namespace || "";
  const causes = Array.isArray(data?.root_causes) ? data.root_causes : [];
  const active = causes.filter((c) => c.kind === "active_failure");
  const integrations = data?.integrations || {};
  const activeIntegrations = [];
  if (integrations.helm?.active) activeIntegrations.push(`Helm (${(integrations.helm.releases || []).join(", ") || integrations.helm.count})`);
  if (integrations.argocd?.active) activeIntegrations.push(`Argo CD (${(integrations.argocd.apps || []).join(", ") || integrations.argocd.count})`);
  if (integrations.prometheus?.active) activeIntegrations.push(`Prometheus (${integrations.prometheus.count})`);
  if (!activeIntegrations.length) activeIntegrations.push("kubectl");

  return h("div", { className: "agui-iv" }, [
    h("div", { className: "agui-iv-header" }, [
      h("span", { className: "agui-iv-title" }, [
        `12-Step Diagnostics `,
        badge(`Score ${sum.overall_score ?? 100}%`, (sum.overall_score ?? 100) < 80 ? "warn" : "ok"),
      ]),
      h("div", { className: "agui-iv-actions" }, [
        miniBtn("Open 12-Step Machine", () => cb.onFilter12Step?.(ns, null), "primary"),
      ]),
    ]),
    h("div", { className: "agui-iv-kv-grid" }, [
      h("div", { className: "agui-iv-kv" }, [h("span", { className: "agui-iv-kv__label" }, "Active Stalls"), badge(active.length, active.length ? "err" : "ok")]),
      h("div", { className: "agui-iv-kv" }, [h("span", { className: "agui-iv-kv__label" }, "Workloads"), badge(`${sum.workloads_healthy ?? 0}/${sum.workloads_total ?? 0}`, "info")]),
      h("div", { className: "agui-iv-kv" }, [h("span", { className: "agui-iv-kv__label" }, "Delivery"), badge(activeIntegrations.join(" + "), "info")]),
    ]),
    causes.length
      ? h(
          "div",
          { className: "agui-iv-list" },
          causes.slice(0, 12).map((rc) =>
            h("div", { className: "agui-iv-row" }, [
              h("div", { className: "agui-iv-row__main" }, [
                h("div", { className: "agui-iv-row__title" }, [
                  badge(`Step ${rc.step_number}`, rc.kind === "active_failure" ? "err" : "info"),
                  rc.namespace ? badge(rc.namespace, "info") : null,
                  h("span", { className: "agui-iv-mono" }, rc.title || rc.component || "Finding"),
                ].filter(Boolean)),
                rc.summary ? h("div", { className: "agui-iv-row__sub" }, truncatePreview(rc.summary, 140)) : null,
              ]),
              h("div", { className: "agui-iv-actions" }, [
                miniBtn(`Step ${rc.step_number}`, () => cb.onFilter12Step?.(rc.namespace || ns, rc.step_number), "primary"),
                miniBtn("Ask AI", () => cb.onAskAI?.(`Explain Step ${rc.step_number} finding '${rc.title}' in namespace '${rc.namespace || ns || "default"}' and show exact fix steps.`), "ai"),
              ]),
            ])
          )
        )
      : h("div", { className: "agui-iv-empty" }, "All 12 kubectl apply steps are passing with 0 stalls."),
  ]);
}

function renderTopologyOrSingleResourceView(toolName, argsObj, data, uiState, rerender, cb) {
  const ns = argsObj?.namespace || data?.namespace || "default";
  if (toolName === "get_pod_details") {
    const podName = data?.name || argsObj?.pod_name || "";
    const containers = Array.isArray(data?.containers) ? data.containers : [];
    return h("div", { className: "agui-iv" }, [
      h("div", { className: "agui-iv-header" }, [
        h("span", { className: "agui-iv-title" }, [
          `Pod: `,
          h("span", { className: "agui-iv-mono" }, podName),
          badge(data?.status || data?.phase || "Pod", statusTone(data?.status || data?.phase)),
        ]),
        h("div", { className: "agui-iv-actions" }, [
          podName ? miniBtn("Open Inspector", () => cb.onInspectPod?.(podName, ns), "primary") : null,
          podName ? miniBtn("Logs", () => cb.onOpenLogs?.(podName, ns)) : null,
          podName ? miniBtn("YAML", () => cb.onOpenManifest?.("Pod", podName, ns)) : null,
        ].filter(Boolean)),
      ]),
      containers.length
        ? h(
            "div",
            { className: "agui-iv-list" },
            containers.map((c) =>
              h("div", { className: "agui-iv-row" }, [
                h("div", { className: "agui-iv-row__main" }, [
                  h("div", { className: "agui-iv-row__title" }, [
                    badge(c.ready ? "Ready" : c.state || "NotReady", statusTone(c.ready ? "ready" : c.state)),
                    h("span", { className: "agui-iv-mono" }, c.name || "container"),
                  ]),
                  h("div", { className: "agui-iv-row__sub" }, truncatePreview(c.image || "", 100)),
                ]),
              ])
            )
          )
        : null,
    ].filter(Boolean));
  }

  if (toolName === "get_pod_logs") {
    const podName = data?.pod_name || argsObj?.pod_name || "";
    const lines = String(data?.logs || "").split("\n").filter(Boolean);
    return h("div", { className: "agui-iv" }, [
      h("div", { className: "agui-iv-header" }, [
        h("span", { className: "agui-iv-title" }, `Pod Logs · ${podName} (${lines.length} lines)`),
        h("div", { className: "agui-iv-actions" }, [
          podName ? miniBtn("Open Full Logs", () => cb.onOpenLogs?.(podName, ns), "primary") : null,
        ].filter(Boolean)),
      ]),
      h("pre", { className: "agui-tool-chip__details" }, lines.slice(-18).join("\n") || "No log output."),
    ]);
  }

  if (toolName === "get_manifest") {
    const kind = argsObj?.kind || "Resource";
    const name = argsObj?.name || "";
    return h("div", { className: "agui-iv" }, [
      h("div", { className: "agui-iv-header" }, [
        h("span", { className: "agui-iv-title" }, `Manifest · ${kind}/${name}`),
        h("div", { className: "agui-iv-actions" }, [
          name ? miniBtn("Open Full YAML", () => cb.onOpenManifest?.(kind, name, argsObj?.namespace || ""), "primary") : null,
        ].filter(Boolean)),
      ]),
      h("pre", { className: "agui-tool-chip__details" }, String(data?.yaml || "").split("\n").slice(0, 24).join("\n") || "Empty YAML"),
    ]);
  }

  const viewMap = {
    get_network_topology: "network",
    get_storage_topology: "storage",
    get_namespace_map: "resourcemap",
    get_cluster_workload_map: "map",
    get_cost_estimate: "cost",
    get_ecosystem_overview: "ecosystem",
    get_cluster_info: "overview",
    get_fleet_overview: "clusters",
    list_teams_and_clusters: "clusters",
    get_audit_logs: "audit",
  };
  const targetView = viewMap[toolName];
  const nodes = Array.isArray(data?.nodes) ? data.nodes : [];
  const edges = Array.isArray(data?.edges) ? data.edges : [];
  const entries = Object.entries(data || {}).filter(([, v]) => typeof v !== "object" || Array.isArray(v)).slice(0, 8);

  return h("div", { className: "agui-iv" }, [
    h("div", { className: "agui-iv-header" }, [
      h("span", { className: "agui-iv-title" }, toolName.replace(/^get_/, "").replace(/_/g, " ")),
      h("div", { className: "agui-iv-actions" }, [
        targetView ? miniBtn(`Open ${targetView} View`, () => cb.onNavigate?.(targetView, argsObj?.namespace || ""), "primary") : null,
      ].filter(Boolean)),
    ]),
    nodes.length || edges.length
      ? h("div", { className: "agui-iv-kv-grid" }, [
          h("div", { className: "agui-iv-kv" }, [h("span", { className: "agui-iv-kv__label" }, "Nodes"), badge(nodes.length, "info")]),
          h("div", { className: "agui-iv-kv" }, [h("span", { className: "agui-iv-kv__label" }, "Connections"), badge(edges.length, "info")]),
        ])
      : h(
          "div",
          { className: "agui-iv-kv-grid" },
          entries.map(([k, v]) =>
            h("div", { className: "agui-iv-kv" }, [
              h("span", { className: "agui-iv-kv__label" }, k),
              h("span", { className: "agui-iv-mono" }, Array.isArray(v) ? `${v.length} item(s)` : truncatePreview(v, 48)),
            ])
          )
        ),
  ]);
}

function renderGenericFallback(tc, argsObj, data, cb) {
  if (!data || typeof data !== "object") {
    return h("div", { className: "agui-iv" }, [
      h("div", { className: "agui-iv-empty" }, tc.status === "running" ? "Calling tool..." : "Tool finished."),
    ]);
  }
  const entries = Object.entries(data).filter(([k]) => !k.startsWith("__")).slice(0, 10);
  return h("div", { className: "agui-iv" }, [
    entries.length
      ? h(
          "div",
          { className: "agui-iv-kv-grid" },
          entries.map(([k, v]) =>
            h("div", { className: "agui-iv-kv" }, [
              h("span", { className: "agui-iv-kv__label" }, k),
              h(
                "span",
                { className: "agui-iv-mono" },
                Array.isArray(v) ? `${v.length} item(s)` : typeof v === "object" && v !== null ? `${Object.keys(v).length} field(s)` : truncatePreview(v, 52)
              ),
            ])
          )
        )
      : h("div", { className: "agui-iv-empty" }, "Done."),
  ]);
}

export function renderInteractiveToolBody(tc, callbacks = {}) {
  if (!tc.uiState) tc.uiState = {};
  const wrapper = h("div", { className: "agui-iv-wrap" });

  const rerender = () => {
    const argsObj = safeJsonParse(tc.argsText) || {};
    const data = safeJsonParse(tc.resultText);
    const uiState = tc.uiState;

    let bodyNode;
    if (!data && tc.status === "running") {
      bodyNode = h("div", { className: "agui-iv" }, [
        h("div", { className: "agui-iv-empty" }, "Running tool query against cluster..."),
      ]);
    } else if (data && data.error) {
      bodyNode = h("div", { className: "agui-iv" }, [
        h("div", { className: "agui-iv-error" }, String(data.error)),
      ]);
    } else if (tc.name === "get_namespaces") {
      bodyNode = renderNamespacesView(argsObj, data, uiState, rerender, callbacks);
    } else if (tc.name === "get_namespace_resources") {
      bodyNode = renderNamespaceResourcesView(argsObj, data, uiState, rerender, callbacks);
    } else if (tc.name === "get_helm_releases") {
      bodyNode = renderHelmReleasesView(argsObj, data, uiState, rerender, callbacks);
    } else if (tc.name === "get_events" || tc.name === "get_extended_events") {
      bodyNode = renderEventsView(argsObj, data, uiState, rerender, callbacks);
    } else if (tc.name === "get_nodes") {
      bodyNode = renderNodesView(argsObj, data, uiState, rerender, callbacks);
    } else if (
      tc.name === "get_workloads_overview" ||
      tc.name === "get_workloads_catalog" ||
      tc.name === "get_applications"
    ) {
      bodyNode = renderWorkloadsView(tc.name, argsObj, data, uiState, rerender, callbacks);
    } else if (tc.name === "diagnose_cluster_issues") {
      bodyNode = renderDiagnosticsRCAView(argsObj, data, uiState, rerender, callbacks);
    } else if (
      data &&
      [
        "get_pod_details",
        "get_pod_logs",
        "get_manifest",
        "get_network_topology",
        "get_storage_topology",
        "get_namespace_map",
        "get_cluster_workload_map",
        "get_cost_estimate",
        "get_ecosystem_overview",
        "get_cluster_info",
        "get_fleet_overview",
        "list_teams_and_clusters",
        "get_audit_logs",
      ].includes(tc.name)
    ) {
      bodyNode = renderTopologyOrSingleResourceView(tc.name, argsObj, data, uiState, rerender, callbacks);
    } else {
      bodyNode = renderGenericFallback(tc, argsObj, data, callbacks);
    }

    const rawArgs = tc.argsText || "{}";
    const rawRes = tc.resultText || "";
    const rawSection =
      rawArgs || rawRes
        ? h(
            "details",
            {
              className: "agui-raw-json",
              open: Boolean(uiState.showRaw),
              ontoggle: (e) => {
                e.stopPropagation();
                uiState.showRaw = e.target.open;
              },
            },
            [
              h("summary", { className: "agui-raw-json__summary" }, "Raw JSON & Args"),
              h(
                "pre",
                { className: "agui-tool-chip__details" },
                `Args:\n${rawArgs}${rawRes ? `\n\nResult:\n${rawRes}` : ""}`
              ),
            ]
          )
        : null;

    wrapper.replaceChildren(...[bodyNode, rawSection].filter(Boolean));
  };

  rerender();
  return wrapper;
}
