// Layered SVG graph renderer shared by the Network, Storage, and Resource
// Map topology views. No charting library -- nodes are grouped into columns
// by `kind` (ILB | Gateway | HTTPRoute | Service | Pod, StorageClass | PV |
// PVC | Pod, or the fuller namespace map's Ingress..PVC set) and edges are
// drawn as cubic-bezier connectors between them. Node/edge DOM references
// are kept in closures (not re-queried via selectors) so highlighting never
// has to worry about escaping resource-name-derived ids in a CSS selector.
//
// Hovering (or keyboard-focusing) a node previews its direct connections;
// clicking calls the caller-supplied `onNodeClick` instead of toggling
// anything itself, so the caller decides what "see this resource's details"
// means (KubeOrbit opens a modal).
//
// Each column renders as a distinct "lane" (a soft background band behind
// its nodes) so each stage reads as a clearly separated zone rather than a
// loose scatter of boxes.

import { h, emptyState } from "./components.js";

const COLUMN_LABELS = {
  ilb: "ILB",
  ingress: "Ingress",
  gateway: "Gateway",
  httproute: "HTTPRoute",
  service: "Service",
  workload: "Workload",
  pod: "Pod",
  configmap: "ConfigMap",
  secret: "Secret",
  serviceaccount: "ServiceAccount",
  storageclass: "StorageClass",
  pv: "PersistentVolume",
  pvc: "PVC",
};

const NODE_W = 176;
const NODE_H = 52;
const COL_GAP = 96;
const ROW_GAP = 20;
const MARGIN = 32;
const COL_LABEL_H = 34;
const LANE_PAD_X = 16;
const LANE_PAD_TOP = 12;

export const NETWORK_COLUMNS = ["ilb", "ingress", "gateway", "httproute", "service", "pod"];
export const STORAGE_COLUMNS = ["storageclass", "pv", "pvc", "pod"];
// Namespace map: routing in on the left, the workload that owns each pod,
// the pod itself, then everything that pod actually mounts/references
// fanning out to the right -- an ArgoCD-app-tree-style "everything here and
// how it's wired" view rather than a single request path.
export const NAMESPACE_MAP_COLUMNS = [
  "ingress", "gateway", "httproute", "service", "workload", "pod",
  "configmap", "secret", "serviceaccount", "pvc",
];

// One tooltip element, shared across every topology graph on the page (only
// one can ever be hovered at a time) -- created lazily on first use and
// appended to <body> so `position: fixed` coordinates are always relative to
// the viewport, regardless of which view's container is currently showing
// the graph.
let _tooltipEl = null;
function _getTooltipEl() {
  if (!_tooltipEl) {
    _tooltipEl = document.createElement("div");
    _tooltipEl.className = "topo-tooltip";
    _tooltipEl.setAttribute("role", "tooltip");
    document.body.appendChild(_tooltipEl);
  }
  return _tooltipEl;
}

function _positionTooltip(el, x, y) {
  // Offset from the cursor/element so the tooltip never sits directly under
  // it, then clamped to the viewport so a node near the right/bottom edge
  // doesn't push the tooltip off-screen and out of view.
  const margin = 12;
  const rect = el.getBoundingClientRect();
  let left = x + margin;
  let top = y + margin;
  if (left + rect.width > window.innerWidth - margin) left = x - rect.width - margin;
  if (top + rect.height > window.innerHeight - margin) top = y - rect.height - margin;
  el.style.left = `${Math.max(margin, left)}px`;
  el.style.top = `${Math.max(margin, top)}px`;
}

function _formatPorts(ports) {
  if (!ports || !ports.length) return undefined;
  return ports
    .map((p) => (p.target_port && p.target_port !== p.port ? `${p.port}:${p.target_port}` : `${p.port}`))
    .join(", ");
}

// The full set of fields the backend already fetches per resource kind (see
// kube_client/normalize.py) -- node.meta carries the whole normalized
// object, this just picks which of its fields are worth a row and in what
// order, per kind, mirroring how the KubeOrbit UI's own detail panels
// already summarize each resource type.
function tooltipRows(n) {
  const m = n.meta || {};
  const rows = [];
  const push = (label, value) => {
    if (value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length)) return;
    rows.push([label, Array.isArray(value) ? value.join(", ") : String(value)]);
  };

  switch (n.kind) {
    case "ilb":
      push("Service", m.service);
      push("External IP", n.label);
      break;
    case "service":
      push("Type", m.type);
      push("Namespace", m.namespace);
      push("Cluster IP", m.cluster_ip);
      push("External IP", m.external_ips);
      push("Ports", _formatPorts(m.ports));
      break;
    case "gateway":
      push("Namespace", m.namespace);
      push("Gateway Class", m.gateway_class);
      push("Addresses", m.addresses);
      push("Listeners", (m.listeners || []).map((l) => `${l.name}:${l.port}/${l.protocol}`));
      break;
    case "httproute":
      push("Namespace", m.namespace);
      push("Hostnames", m.hostnames);
      push("Routes To", (m.rules || []).flatMap((r) => r.backend_refs || []));
      break;
    case "ingress":
      push("Namespace", m.namespace);
      push("Hosts", (m.rules || []).map((r) => r.host).filter(Boolean));
      break;
    case "pod":
      push("Namespace", m.namespace);
      push("Status", m.status);
      push("Ready", m.ready);
      push("Restarts", m.restarts);
      push("Node", m.node);
      break;
    case "storageclass":
      push("Provisioner", m.provisioner);
      push("Reclaim Policy", m.reclaim_policy);
      push("Volume Binding", m.volume_binding_mode);
      push("Default Class", m.is_default ? "Yes" : undefined);
      break;
    case "pv":
      push("Status", m.status);
      push("Capacity", m.capacity);
      push("Access Modes", m.access_modes);
      push("Storage Class", m.storage_class);
      push("Reclaim Policy", m.reclaim_policy);
      push("Claim", m.claim_name ? `${m.claim_namespace}/${m.claim_name}` : undefined);
      break;
    case "pvc":
      push("Namespace", m.namespace);
      push("Status", m.status);
      push("Capacity", m.capacity);
      push("Access Modes", m.access_modes);
      push("Storage Class", m.storage_class);
      break;
    case "configmap":
      push("Namespace", m.namespace);
      push("Keys", m.key_count);
      break;
    case "secret":
      push("Namespace", m.namespace);
      push("Type", m.type);
      push("Keys", m.key_count);
      break;
    case "serviceaccount":
      push("Namespace", m.namespace);
      push("Secrets", m.secrets);
      break;
    default: // workload (deployment/statefulset/daemonset/job/cronjob) and anything else
      push("Namespace", m.namespace);
      push("Status", n.status);
      push("Ready", m.ready);
  }
  return rows;
}

export function showNodeTooltip(n, x, y) {
  const el = _getTooltipEl();
  el.replaceChildren(
    h("div", { class: "topo-tooltip__kind" }, (n.kind || "").toUpperCase()),
    h("div", { class: "topo-tooltip__name" }, n.label || n.id),
    h(
      "div",
      { class: "topo-tooltip__rows" },
      tooltipRows(n).map(([label, value]) =>
        h("div", { class: "topo-tooltip__row" }, [
          h("span", { class: "topo-tooltip__row-label" }, label),
          h("span", { class: "topo-tooltip__row-value" }, value),
        ])
      )
    )
  );
  el.classList.add("is-visible");
  _positionTooltip(el, x, y);
}

export function hideNodeTooltip() {
  _tooltipEl?.classList.remove("is-visible");
}

// For a mousemove while the tooltip is already showing -- repositions
// without rebuilding its content (showNodeTooltip does both, which is
// unnecessary and flicker-prone to repeat on every pixel of pointer travel).
export function moveNodeTooltip(x, y) {
  if (_tooltipEl) _positionTooltip(_tooltipEl, x, y);
}

function gradientDefs() {
  const specs = [
    ["topo-grad-healthy", "var(--accent-soft)"],
    ["topo-grad-warning", "var(--warning-soft)"],
    ["topo-grad-unhealthy", "var(--danger-soft)"],
    ["topo-grad-unknown", "var(--unknown-soft)"],
  ];
  return h(
    "defs",
    {},
    specs.map(([id, tint]) =>
      h("linearGradient", { id, x1: "0%", y1: "0%", x2: "100%", y2: "100%" }, [
        h("stop", { offset: "0%", "stop-color": tint }),
        h("stop", { offset: "100%", "stop-color": "var(--bg-elevated)" }),
      ])
    )
  );
}

export function renderTopologyGraph(graph, columnOrder, animate = true, onNodeClick = null) {
  // A fresh render (switching views, or an auto-refresh redraw while a node
  // was hovered) discards the old node <g> elements outright rather than
  // moving the mouse off them, so no mouseleave ever fires to hide a
  // tooltip left open from the previous graph -- hide it unconditionally
  // whenever a new graph is about to replace the old one.
  hideNodeTooltip();

  const nodes = graph.nodes || [];
  const edges = graph.edges || [];

  if (!nodes.length) {
    return emptyState(
      "No topology data",
      "No resources were found to build this graph for the selected namespace."
    );
  }

  // `group` decides which column/lane a node sits in; `kind` is only for its
  // label. They're the same value for every existing graph (Network/Storage
  // Topology don't set `group`), but the namespace map needs them to differ:
  // Deployment/StatefulSet/DaemonSet/Job/CronJob nodes are all distinct
  // `kind`s (for an accurate label) that share one "workload" `group`/column.
  const groupOf = (n) => n.group || n.kind;
  const kindsPresent = [...new Set(nodes.map(groupOf))];
  const columns = columnOrder.filter((k) => kindsPresent.includes(k));
  for (const k of kindsPresent) if (!columns.includes(k)) columns.push(k);

  const nodesByColumn = columns.map((k) => nodes.filter((n) => groupOf(n) === k));
  const maxRows = Math.max(1, ...nodesByColumn.map((list) => list.length));

  const width = columns.length * (NODE_W + COL_GAP) - COL_GAP + MARGIN * 2;
  const contentHeight = maxRows * (NODE_H + ROW_GAP) - ROW_GAP;
  const height = contentHeight + MARGIN * 2 + COL_LABEL_H;

  const positions = new Map();
  nodesByColumn.forEach((list, colIdx) => {
    const colX = MARGIN + colIdx * (NODE_W + COL_GAP);
    const totalH = list.length * (NODE_H + ROW_GAP) - ROW_GAP;
    const startY = MARGIN + COL_LABEL_H + Math.max(0, (contentHeight - totalH) / 2);
    list.forEach((n, i) => positions.set(n.id, { x: colX, y: startY + i * (NODE_H + ROW_GAP) }));
  });

  const svg = h("svg", {
    class: "topology-svg",
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    role: "img",
    "aria-label": "Resource topology graph",
  });
  svg.appendChild(gradientDefs());

  // Lanes: one soft background band per column, drawn first so everything
  // else sits on top -- this is what makes each stage read as its own zone.
  const laneGroup = h("g", { class: "topo-lanes" });
  columns.forEach((k, i) => {
    const x = MARGIN + i * (NODE_W + COL_GAP) - LANE_PAD_X;
    laneGroup.appendChild(
      h("rect", {
        class: "topo-lane",
        x,
        y: MARGIN + COL_LABEL_H - LANE_PAD_TOP,
        width: NODE_W + LANE_PAD_X * 2,
        height: contentHeight + LANE_PAD_TOP * 2,
        rx: 18,
      })
    );
  });
  svg.appendChild(laneGroup);

  columns.forEach((k, i) => {
    const x = MARGIN + i * (NODE_W + COL_GAP) + NODE_W / 2;
    svg.appendChild(h("text", { class: "topo-col-label", x, y: MARGIN + 16, "text-anchor": "middle" }, COLUMN_LABELS[k] || k));
  });

  const nodeElements = new Map(); // id -> <g>
  const edgeRefs = []; // { source, target, el, flowEl }

  // A fixed per-item delay reads as a nice cascade for a handful of nodes
  // but turns into a multi-second crawl once a namespace map pulls in
  // dozens of pods/configmaps/secrets -- cap the *total* spread instead so
  // a 95-node graph still finishes settling in well under a second.
  const nodeDelayStep = Math.min(22, 480 / Math.max(1, nodes.length));
  const edgeDelayStep = Math.min(16, 300 / Math.max(1, edges.length));

  const edgeGroup = h("g", { class: "topo-edges" });
  let edgeIdx = 0;
  for (const edge of edges) {
    const from = positions.get(edge.source);
    const to = positions.get(edge.target);
    if (!from || !to) continue; // dangling edge (partial data) -- skip rather than crash the render
    const x1 = from.x + NODE_W;
    const y1 = from.y + NODE_H / 2;
    const x2 = to.x;
    const y2 = to.y + NODE_H / 2;
    const midX = (x1 + x2) / 2;
    const d = `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`;
    // Two paths per connector: a static base line (fades in once on a real
    // load, same as nodes -- see the `animate` gating) plus an overlaid
    // dashed line whose dash offset animates continuously and never stops,
    // giving a subtle "traffic moving downstream" flow indicator along the
    // same curve. A varied negative delay (offset by index) starts each
    // edge's flow at a different phase so parallel edges don't all pulse in
    // visual lockstep.
    const path = h("path", {
      class: animate ? "topo-edge is-entering" : "topo-edge",
      d,
      style: animate ? `animation-delay:${120 + edgeIdx * edgeDelayStep}ms;` : undefined,
    });
    const flow = h("path", { class: "topo-edge-flow", d, style: `animation-delay:${-(edgeIdx * 260) % 1600}ms;` });
    edgeIdx += 1;
    edgeGroup.appendChild(path);
    edgeGroup.appendChild(flow);
    edgeRefs.push({ source: edge.source, target: edge.target, el: path, flowEl: flow });
  }
  svg.appendChild(edgeGroup);

  const nodeGroup = h("g", { class: "topo-nodes" });
  let nodeIdx = 0;
  for (const n of nodes) {
    const pos = positions.get(n.id);
    if (!pos) continue;
    const label = n.label && n.label.length > 22 ? n.label.slice(0, 21) + "…" : n.label || n.id;
    // Positioning lives on this outer <g> as a plain SVG `transform` attribute
    // and must never be touched by a CSS animation/transition -- a CSS
    // `transform` (from an animation, say) replaces an element's presentation
    // `transform` attribute outright rather than composing with it. The
    // entrance animation instead runs on the inner <g>, which has no
    // positioning transform of its own to clobber.
    const inner = h(
      "g",
      { class: animate ? "topo-node__inner is-entering" : "topo-node__inner", style: animate ? `animation-delay:${nodeIdx * nodeDelayStep}ms;` : undefined },
      [
        h("rect", { class: "topo-node__box", width: NODE_W, height: NODE_H, rx: 10 }),
        h("rect", { class: "topo-node__accent", x: 1.5, y: 6, width: 4, height: NODE_H - 12, rx: 2 }),
        h("text", { class: "topo-node__kind", x: 14, y: 18 }, (n.kind || "").toUpperCase()),
        h("text", { x: 14, y: 37 }, label),
      ]
    );
    const g = h(
      "g",
      {
        class: `topo-node topo-node--${n.status || "unknown"} topo-node--kind-${n.kind || "unknown"}`,
        transform: `translate(${pos.x}, ${pos.y})`,
        tabindex: "0",
        role: "button",
        "aria-label": `${n.kind}: ${n.label || n.id}, status ${n.status || "unknown"}`,
      },
      [inner]
    );
    nodeIdx += 1;
    // Hover (and keyboard focus, for the same experience without a mouse)
    // previews the node's direct connections live and shows its full
    // type + name in a tooltip (the box itself truncates long names); it
    // never "sticks" -- move to another node or off the graph and the
    // preview/tooltip follows/clears. Click instead opens the full detail
    // view, so the two interactions don't fight over what a click vs a
    // hover should do.
    g.addEventListener("mouseenter", (e) => {
      applyHighlight(n.id, nodeElements, edgeRefs);
      showNodeTooltip(n, e.clientX, e.clientY);
    });
    g.addEventListener("mousemove", (e) => _positionTooltip(_getTooltipEl(), e.clientX, e.clientY));
    g.addEventListener("mouseleave", () => {
      clearHighlight(nodeElements, edgeRefs);
      hideNodeTooltip();
    });
    g.addEventListener("focus", () => {
      applyHighlight(n.id, nodeElements, edgeRefs);
      const rect = g.getBoundingClientRect();
      showNodeTooltip(n, rect.left, rect.bottom);
    });
    g.addEventListener("blur", () => {
      clearHighlight(nodeElements, edgeRefs);
      hideNodeTooltip();
    });
    g.addEventListener("click", () => onNodeClick && onNodeClick(n));
    g.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onNodeClick && onNodeClick(n);
      }
    });
    nodeElements.set(n.id, g);
    nodeGroup.appendChild(g);
  }
  svg.appendChild(nodeGroup);

  return svg;
}

function applyHighlight(nodeId, nodeElements, edgeRefs) {
  const connected = new Set([nodeId]);
  for (const { source, target, el, flowEl } of edgeRefs) {
    if (source === nodeId || target === nodeId) {
      el.classList.add("is-highlighted");
      flowEl.classList.add("is-highlighted");
      connected.add(source);
      connected.add(target);
    }
  }
  connected.forEach((id) => nodeElements.get(id)?.classList.add("is-highlighted"));

  // Dim everything not part of this node's own chain -- with a large,
  // densely cross-linked namespace map, "highlight one thing" barely reads
  // against 90+ other full-brightness nodes unless the rest recede.
  nodeElements.forEach((el, id) => {
    if (!connected.has(id)) el.classList.add("is-dimmed");
  });
  edgeRefs.forEach(({ source, target, el, flowEl }) => {
    if (source !== nodeId && target !== nodeId) {
      el.classList.add("is-dimmed");
      flowEl.classList.add("is-dimmed");
    }
  });
}

function clearHighlight(nodeElements, edgeRefs) {
  nodeElements.forEach((el) => el.classList.remove("is-highlighted", "is-dimmed"));
  edgeRefs.forEach(({ el, flowEl }) => {
    el.classList.remove("is-highlighted", "is-dimmed");
    flowEl.classList.remove("is-highlighted", "is-dimmed");
  });
}

export function topologyLegend() {
  const items = [
    ["healthy", "var(--accent)"],
    ["pending / warning", "var(--warning)"],
    ["unhealthy", "var(--danger)"],
    ["unknown", "var(--border-strong)"],
  ];
  return h(
    "div",
    { class: "topology-legend" },
    items.map(([label, color]) =>
      h("span", { class: "topology-legend__item" }, [h("span", { class: "topology-legend__swatch", style: `background:${color};` }), label])
    )
  );
}
