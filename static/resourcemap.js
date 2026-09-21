// Interactive Resource Map with Pan/Zoom, Dynamic Depth, Card Nodes & Inspector Drawer

import { api } from "./api.js";
import { h, renderInto, pill, statusPill } from "./components.js";
import { showNodeTooltip, hideNodeTooltip, moveNodeTooltip } from "./topology.js";

// Node cards are large enough to show name + type directly on the card face
// (unlike the small circles this replaced, which needed a tooltip just for
// that). Hovering still shows a tooltip for everything that doesn't fit on
// the card -- see renderSVG()'s node-building loop.
const NODE_CARD_W = 152;
const NODE_CARD_H = 46;

// Resource type theme definitions
export const RESOURCE_TYPE_THEME = {
  deployment:   { color: "#3b82f6", bg: "rgba(59,130,246,0.12)", label: "DEPLOYMENT", icon: `<polygon points="5 3 19 12 5 21 5 3" fill="currentColor"/>` },
  service:      { color: "#10b981", bg: "rgba(16,185,129,0.12)", label: "SERVICE", icon: `<polygon points="12 2 22 8.5 22 15.5 12 22 2 15.5 2 8.5 12 2" fill="none" stroke="currentColor" stroke-width="2"/>` },
  pod:          { color: "#06b6d4", bg: "rgba(6,182,212,0.12)", label: "POD", icon: `<rect x="5" y="5" width="14" height="14" rx="2" transform="rotate(45 12 12)" fill="currentColor"/>` },
  configmap:    { color: "#6b7280", bg: "rgba(107,114,128,0.12)", label: "CONFIGMAP", icon: `<rect x="4" y="4" width="16" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><line x1="8" y1="9" x2="16" y2="9" stroke="currentColor" stroke-width="2"/><line x1="8" y1="13" x2="14" y2="13" stroke="currentColor" stroke-width="2"/>` },
  secret:       { color: "#ec4899", bg: "rgba(236,72,153,0.12)", label: "SECRET", icon: `<rect x="4" y="10" width="16" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3" fill="none" stroke="currentColor" stroke-width="2"/>` },
  pvc:          { color: "#f97316", bg: "rgba(249,115,22,0.12)", label: "PVC", icon: `<ellipse cx="12" cy="5" rx="9" ry="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" fill="none" stroke="currentColor" stroke-width="2"/>` },
  httproute:    { color: "#8b5cf6", bg: "rgba(139,92,246,0.12)", label: "HTTPROUTE", icon: `<path d="M17 3v4M17 7l-4-4M17 7l4-4M7 21v-4M7 17l-4 4M7 17l4 4" stroke="currentColor" stroke-width="2"/><path d="M7 17h6a4 4 0 0 0 4-4V7" fill="none" stroke="currentColor" stroke-width="2"/>` },
  gateway:      { color: "#a855f7", bg: "rgba(168,85,247,0.12)", label: "GATEWAY", icon: `<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 3a14.5 14.5 0 0 0 0 18M2 12a14.5 14.5 0 0 0 20 0" fill="none" stroke="currentColor" stroke-width="2"/>` },
  ingress:      { color: "#8b5cf6", bg: "rgba(139,92,246,0.12)", label: "INGRESS", icon: `<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" stroke="currentColor" stroke-width="2"/>` },
  statefulset:  { color: "#3b82f6", bg: "rgba(59,130,246,0.12)", label: "STATEFULSET", icon: `<rect x="3" y="3" width="18" height="18" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><line x1="3" y1="9" x2="21" y2="9" stroke="currentColor" stroke-width="2"/><line x1="9" y1="21" x2="9" y2="9" stroke="currentColor" stroke-width="2"/>` },
  daemonset:    { color: "#3b82f6", bg: "rgba(59,130,246,0.12)", label: "DAEMONSET", icon: `<rect x="2" y="2" width="20" height="8" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><rect x="2" y="14" width="20" height="8" rx="2" fill="none" stroke="currentColor" stroke-width="2"/>` },
  job:          { color: "#eab308", bg: "rgba(234,179,8,0.12)", label: "JOB", icon: `<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><polyline points="12 6 12 12 16 14" fill="none" stroke="currentColor" stroke-width="2"/>` },
  cronjob:      { color: "#eab308", bg: "rgba(234,179,8,0.12)", label: "CRONJOB", icon: `<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><polyline points="12 6 12 12 16 14" fill="none" stroke="currentColor" stroke-width="2"/>` },
  serviceaccount:{ color: "#64748b", bg: "rgba(100,116,139,0.12)", label: "SERVICEACCOUNT", icon: `<circle cx="12" cy="8" r="4" fill="none" stroke="currentColor" stroke-width="2"/><path d="M6 20v-2a6 6 0 0 1 12 0v2" fill="none" stroke="currentColor" stroke-width="2"/>` }
};

function timeAgo(dateStr) {
  if (!dateStr) return "—";
  try {
    const diff = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
    if (isNaN(diff)) return String(dateStr);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  } catch (_) {
    return String(dateStr);
  }
}

export class ResourceMapController {
  constructor(options = {}) {
    this.container = options.container || document.getElementById("resourcemap-canvas-container");
    this.svg = options.svg || document.getElementById("resourcemap-svg");
    this.drawer = options.drawer || document.getElementById("resourcemap-drawer");
    this.onNodeAction = options.onNodeAction || null;

    this.rawGraph = { nodes: [], edges: [] };
    this.activeNodes = [];
    this.activeEdges = [];
    this.selectedNodeId = null;
    this.hoveredNodeId = null;

    // Viewport transform
    this.zoom = 1.0;
    this.panX = 0;
    this.panY = 0;
    this.isDragging = false;
    this.startX = 0;
    this.startY = 0;

    this.initControls();
    this.initDragAndZoom();
    this.initResizeRefit();
  }

  // Re-fits the graph whenever the canvas's own size actually changes --
  // window resize, sidebar collapse, or (below) the inspector drawer
  // sliding open/closed -- instead of leaving the last zoom/pan computed
  // against a since-stale container size, which is what made the map look
  // small/off-center after any layout change.
  initResizeRefit() {
    if (!this.container || typeof ResizeObserver === "undefined") return;
    let raf = null;
    let lastW = 0, lastH = 0;
    this._resizeObserver = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (!box) return;
      // Ignore sub-pixel/intermediate reflow noise; only re-fit once the
      // canvas has actually grown or shrunk meaningfully (drawer open/close,
      // sidebar toggle, real window resize) so we don't fight a user who is
      // mid-pan/zoom over a trivial layout jitter.
      if (Math.abs(box.width - lastW) < 24 && Math.abs(box.height - lastH) < 24) return;
      lastW = box.width;
      lastH = box.height;
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        if (this.activeNodes.length) this.resetView();
      });
    });
    this._resizeObserver.observe(this.container);
  }

  initControls() {
    document.getElementById("resourcemap-zoom-in-btn")?.addEventListener("click", () => this.setZoom(this.zoom * 1.25));
    document.getElementById("resourcemap-zoom-out-btn")?.addEventListener("click", () => this.setZoom(this.zoom / 1.25));
    document.getElementById("resourcemap-zoom-reset-btn")?.addEventListener("click", () => this.resetView());
    document.getElementById("resourcemap-drawer-close")?.addEventListener("click", () => this.deselectNode());
  }

  initDragAndZoom() {
    if (!this.container) return;

    this.container.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = this.container.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const mouseY = e.clientY - rect.top;

      const factor = e.deltaY < 0 ? 1.15 : 0.85;
      const newZoom = Math.min(4.0, Math.max(0.25, this.zoom * factor));

      // Zoom towards mouse pointer
      this.panX = mouseX - (mouseX - this.panX) * (newZoom / this.zoom);
      this.panY = mouseY - (mouseY - this.panY) * (newZoom / this.zoom);
      this.zoom = newZoom;

      this.updateTransform();
    }, { passive: false });

    this.container.addEventListener("mousedown", (e) => {
      if (e.target.closest(".resourcemap-node")) return;
      this.isDragging = true;
      this.startX = e.clientX - this.panX;
      this.startY = e.clientY - this.panY;
      this.container.style.cursor = "grabbing";
    });

    window.addEventListener("mousemove", (e) => {
      if (!this.isDragging) return;
      this.panX = e.clientX - this.startX;
      this.panY = e.clientY - this.startY;
      this.updateTransform();
    });

    window.addEventListener("mouseup", () => {
      if (this.isDragging) {
        this.isDragging = false;
        if (this.container) this.container.style.cursor = "grab";
      }
    });
  }

  setZoom(newZoom) {
    const rect = this.container.getBoundingClientRect();
    const cx = rect.width / 2;
    const cy = rect.height / 2;

    const clampedZoom = Math.min(4.0, Math.max(0.25, newZoom));
    this.panX = cx - (cx - this.panX) * (clampedZoom / this.zoom);
    this.panY = cy - (cy - this.panY) * (clampedZoom / this.zoom);
    this.zoom = clampedZoom;
    this.updateTransform(true);
  }

  resetView() {
    this.zoom = 1.0;
    if (this.container && this.activeNodes.length) {
      const rect = this.container.getBoundingClientRect();
      // Calculate bounding box of all nodes
      const xs = this.activeNodes.map(n => n.x);
      const ys = this.activeNodes.map(n => n.y);
      const minX = Math.min(...xs), maxX = Math.max(...xs);
      const minY = Math.min(...ys), maxY = Math.max(...ys);
      const graphW = Math.max(100, maxX - minX + 160);
      const graphH = Math.max(100, maxY - minY + 160);

      const scaleX = rect.width / graphW;
      const scaleY = rect.height / graphH;
      this.zoom = Math.min(1.4, Math.max(0.4, Math.min(scaleX, scaleY)));

      const centerX = (minX + maxX) / 2;
      const centerY = (minY + maxY) / 2;
      this.panX = rect.width / 2 - centerX * this.zoom;
      this.panY = rect.height / 2 - centerY * this.zoom;
    } else {
      this.panX = 0;
      this.panY = 0;
    }
    this.updateTransform(true);
  }

  // `animated` smooths button-driven zoom/reset with a CSS transition (see
  // .resourcemap-root-g-animated in styles.css); wheel-zoom and drag-pan
  // call this every pointer tick with animated=false (the default) so they
  // stay 1:1 with the input -- a transition fighting continuous input reads
  // as laggy, not smooth, which is why only the discrete, one-shot actions
  // (buttons, reset, refit) opt into it.
  updateTransform(animated = false) {
    const rootG = document.getElementById("resourcemap-root-g");
    if (rootG) {
      if (animated) {
        rootG.classList.add("is-animated-transform");
        clearTimeout(this._transformAnimTimeout);
        this._transformAnimTimeout = setTimeout(() => rootG.classList.remove("is-animated-transform"), 320);
      } else {
        rootG.classList.remove("is-animated-transform");
      }
      rootG.setAttribute("transform", `translate(${this.panX}, ${this.panY}) scale(${this.zoom})`);
    }
    const pctEl = document.getElementById("resourcemap-zoom-pct");
    if (pctEl) {
      pctEl.textContent = `${Math.round(this.zoom * 100)}%`;
    }
  }

  setGraphData(graph) {
    this.rawGraph = graph || { nodes: [], edges: [] };
    this.updateResourceSelectors();
  }

  updateResourceSelectors() {
    const typeSelect = document.getElementById("resourcemap-type-select");
    const resourceSelect = document.getElementById("resourcemap-resource-select");
    if (!resourceSelect) return;

    const selectedType = typeSelect?.value?.toLowerCase() || "";
    resourceSelect.innerHTML = `<option value="">Select Resource</option>`;

    const nodes = this.rawGraph.nodes || [];
    const filteredNodes = selectedType
      ? nodes.filter(n => (n.kind || "").toLowerCase() === selectedType || (n.group || "").toLowerCase() === selectedType)
      : nodes;

    // Sort alphabetically
    filteredNodes.sort((a, b) => a.label.localeCompare(b.label));

    filteredNodes.forEach(n => {
      const opt = document.createElement("option");
      opt.value = n.id;
      opt.textContent = `${n.label} (${(n.kind || n.group || "").toUpperCase()})`;
      resourceSelect.appendChild(opt);
    });
  }

  filterAndRender(rootId = "", depth = 3, animate = true) {
    const allNodes = this.rawGraph.nodes || [];
    const allEdges = this.rawGraph.edges || [];

    if (!allNodes.length) {
      this.svg.innerHTML = `
        <text x="50%" y="50%" text-anchor="middle" fill="var(--fg-subtle)" font-size="14" font-family="var(--font-ui)">
          No resources found in this namespace.
        </text>
      `;
      return;
    }

    if (!rootId) {
      // Show full namespace graph
      this.activeNodes = [...allNodes];
      this.activeEdges = [...allEdges];
    } else {
      // Breadth-first search around rootId up to depth
      const visited = new Set([rootId]);
      let currentLevel = new Set([rootId]);

      for (let d = 0; d < depth; d++) {
        const nextLevel = new Set();
        allEdges.forEach(e => {
          if (currentLevel.has(e.source) && !visited.has(e.target)) {
            visited.add(e.target);
            nextLevel.add(e.target);
          }
          if (currentLevel.has(e.target) && !visited.has(e.source)) {
            visited.add(e.source);
            nextLevel.add(e.source);
          }
        });
        currentLevel = nextLevel;
      }

      this.activeNodes = allNodes.filter(n => visited.has(n.id));
      this.activeEdges = allEdges.filter(e => visited.has(e.source) && visited.has(e.target));
    }

    this.computeLayout(rootId);
    this.renderSVG(animate);

    if (rootId) {
      this.selectNode(rootId);
    } else if (this.selectedNodeId && this.activeNodes.some(n => n.id === this.selectedNodeId)) {
      this.selectNode(this.selectedNodeId);
    }
  }

  computeLayout(rootId = "") {
    const nodes = this.activeNodes;
    const edges = this.activeEdges;
    this.groupCards = []; // only the full no-rootId layout below populates this
    if (!nodes.length) return;

    // Group-based / Radial Layered Layout
    const KIND_LAYER_ORDER = {
      gateway: 0,
      ingress: 0,
      httproute: 1,
      service: 2,
      deployment: 3,
      statefulset: 3,
      daemonset: 3,
      workload: 3,
      job: 3,
      cronjob: 3,
      pod: 4,
      configmap: 5,
      secret: 5,
      pvc: 5,
      serviceaccount: 5,
      storageclass: 6,
      pv: 6
    };

    if (rootId) {
      // Radial / BFS ring layout around the root node
      const rootNode = nodes.find(n => n.id === rootId) || nodes[0];
      rootNode.x = 400;
      rootNode.y = 300;
      rootNode.depth = 0;

      // Assign depths from root
      const depthMap = { [rootNode.id]: 0 };
      const queue = [rootNode.id];
      while (queue.length) {
        const currId = queue.shift();
        const currD = depthMap[currId];
        edges.forEach(e => {
          let neighbor = null;
          if (e.source === currId && depthMap[e.target] === undefined) neighbor = e.target;
          if (e.target === currId && depthMap[e.source] === undefined) neighbor = e.source;
          if (neighbor) {
            depthMap[neighbor] = currD + 1;
            queue.push(neighbor);
          }
        });
      }

      // Group nodes by depth
      const depthGroups = {};
      nodes.forEach(n => {
        const d = depthMap[n.id] !== undefined ? depthMap[n.id] : 1;
        depthGroups[d] = depthGroups[d] || [];
        depthGroups[d].push(n);
      });

      // Place in concentric rings
      Object.keys(depthGroups).forEach(dStr => {
        const d = Number(dStr);
        if (d === 0) return;
        const group = depthGroups[d];
        const radius = d * 180;
        const step = (Math.PI * 2) / group.length;
        group.forEach((node, idx) => {
          const angle = idx * step - Math.PI / 2;
          node.x = rootNode.x + Math.cos(angle) * radius;
          node.y = rootNode.y + Math.sin(angle) * radius;
        });
      });
    } else {
      // App-grouped card layout: nodes are first split by app_group (see
      // get_namespace_map()'s _compute_app_groups on the backend -- every
      // node in this view carries one), each group gets its own local
      // Flow-Columns layout, and the groups themselves are packed into a
      // wrapping grid with a bordered "card" drawn behind each one in
      // renderSVG(). This is what turns "every resource in the namespace"
      // from one flat scatter into legible per-application clusters.
      this.groupCards = this._layoutAppGroups(nodes, KIND_LAYER_ORDER);
    }
  }

  // Lays out one group's nodes locally (relative to 0,0) using the same
  // column-by-kind logic the flat layout used to apply to everything at
  // once, and returns the bounding box it occupied.
  _layoutColumnsLocal(nodeList, kindLayerOrder, originX, originY) {
    const layers = {};
    nodeList.forEach(n => {
      const kindKey = (n.kind || n.group || "pod").toLowerCase();
      const layerIdx = kindLayerOrder[kindKey] !== undefined ? kindLayerOrder[kindKey] : 3;
      layers[layerIdx] = layers[layerIdx] || [];
      layers[layerIdx].push(n);
    });

    const sortedLayerIndices = Object.keys(layers).map(Number).sort((a, b) => a - b);
    const COL_WIDTH = 190;
    const ROW_HEIGHT = 110;
    // A layer used to be one column of unlimited height -- a namespace with
    // eight Deployments meant eight cards stacked straight down, each one
    // pushing the group card taller. Capping rows-per-layer and wrapping the
    // overflow into an adjacent sub-column keeps a busy layer roughly square
    // instead of turning into a single very tall strip.
    const MAX_ROWS_PER_LAYER = 4;
    let maxRows = 0;

    // First column's x must clear the card's own left border by a real
    // margin, not just avoid overlapping it exactly -- a node's *center* at
    // x = card half-width means its left edge sits flush on (or past, given
    // any rounding) the group card's own boundary, which is what made the
    // leftmost column of nodes look like it had no left padding at all.
    const LEFT_PAD = NODE_CARD_W / 2 + 20;
    let colCursor = 0; // next free sub-column slot, advances by however many sub-columns each layer consumes
    sortedLayerIndices.forEach((layerIdx) => {
      const columnNodes = layers[layerIdx];
      const subCols = Math.max(1, Math.ceil(columnNodes.length / MAX_ROWS_PER_LAYER));
      const rowsInLayer = Math.ceil(columnNodes.length / subCols);
      maxRows = Math.max(maxRows, rowsInLayer);
      columnNodes.forEach((node, i) => {
        const subCol = Math.floor(i / rowsInLayer);
        const row = i % rowsInLayer;
        node.x = originX + LEFT_PAD + (colCursor + subCol) * COL_WIDTH;
        node.y = originY + 60 + row * ROW_HEIGHT;
      });
      colCursor += subCols;
    });

    // Right edge of the last column's cards, plus a matching ~20px margin --
    // computed directly rather than approximated so left/right padding stay
    // visually equal regardless of column count (the previous formula added
    // LEFT_PAD on top of a flat N*COL_WIDTH, which double-counted it and
    // made single-column groups needlessly wide).
    const lastColX = LEFT_PAD + Math.max(0, colCursor - 1) * COL_WIDTH;
    const width = Math.max(220, lastColX + NODE_CARD_W / 2 + 20);
    const height = Math.max(140, maxRows * ROW_HEIGHT + 40);
    return { width, height };
  }

  // Groups nodes by app_group, lays each group out independently, and packs
  // the resulting cards into a wrapping grid (left-to-right, new row once a
  // card would overflow MAX_ROW_WIDTH). Returns the card rects for renderSVG.
  _layoutAppGroups(nodes, kindLayerOrder) {
    const MAX_ROW_WIDTH = 1400;
    const CARD_GAP = 36;
    const CARD_HEADER_H = 34;

    const groups = new Map();
    nodes.forEach(n => {
      const key = n.app_group || "Other";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(n);
    });

    // Larger groups first reads better in a wrapping grid than alphabetical.
    const ordered = [...groups.entries()].sort((a, b) => b[1].length - a[1].length);

    const cards = [];
    let cursorX = 0, cursorY = 0, rowHeight = 0;
    for (const [label, groupNodes] of ordered) {
      const { width, height } = this._layoutColumnsLocal(groupNodes, kindLayerOrder, 0, 0);
      const cardW = width, cardH = height + CARD_HEADER_H;

      if (cursorX > 0 && cursorX + cardW > MAX_ROW_WIDTH) {
        cursorX = 0;
        cursorY += rowHeight + CARD_GAP;
        rowHeight = 0;
      }

      groupNodes.forEach(n => { n.x += cursorX; n.y += cursorY + CARD_HEADER_H; });
      cards.push({ label, count: groupNodes.length, x: cursorX, y: cursorY, width: cardW, height: cardH });

      cursorX += cardW + CARD_GAP;
      rowHeight = Math.max(rowHeight, cardH);
    }
    return cards;
  }

  renderSVG(animate = true) {
    this.svg.innerHTML = "";

    // SVG Defs
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    defs.innerHTML = `
      <filter id="resourcemap-glow" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur in="SourceGraphic" stdDeviation="6" result="blur"/>
        <feMerge>
          <feMergeNode in="blur"/>
          <feMergeNode in="SourceGraphic"/>
        </feMerge>
      </filter>
      <filter id="resourcemap-shadow" x="-30%" y="-30%" width="160%" height="160%">
        <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="rgba(0,0,0,0.15)"/>
      </filter>
      <!-- refX must sit within the marker's own 0-10 viewBox to land the
           triangle's tip (drawn at x=10) on the path's actual endpoint --
           it previously pointed to x=28, well outside the shape entirely,
           which rendered the arrowhead offset and detached from the line
           it was supposed to cap. -->
      <marker id="resourcemap-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 1 L 10 5 L 0 9 z" fill="var(--border-strong, #94a3b8)"/>
      </marker>
      <marker id="resourcemap-arrow-highlight" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 1 L 10 5 L 0 9 z" fill="var(--accent, #1A73E8)"/>
      </marker>
    `;
    this.svg.appendChild(defs);

    const rootG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    rootG.id = "resourcemap-root-g";
    rootG.setAttribute("transform", `translate(${this.panX}, ${this.panY}) scale(${this.zoom})`);

    // App-group card backgrounds -- drawn first so edges/nodes sit on top.
    // A plain border alone reads too close in lightness to the canvas fill
    // to register at a glance (bg-elevated #fff vs bg-inset #eef2f7 vs
    // border #e2e8f2 are all near-white) -- a drop shadow plus a tinted
    // header strip (clipped to the card's own rounded corners) is what
    // actually makes each app group read as a distinct region.
    const cardsG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    cardsG.id = "resourcemap-cards-g";
    const CARD_HEADER_H = 34;
    (this.groupCards || []).forEach((card, i) => {
      const clipId = `resourcemap-card-clip-${i}`;
      const clipPath = document.createElementNS("http://www.w3.org/2000/svg", "clipPath");
      clipPath.setAttribute("id", clipId);
      const clipRect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      clipRect.setAttribute("x", card.x);
      clipRect.setAttribute("y", card.y);
      clipRect.setAttribute("width", card.width);
      clipRect.setAttribute("height", card.height);
      clipRect.setAttribute("rx", "14");
      clipPath.appendChild(clipRect);
      defs.appendChild(clipPath);

      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", card.x);
      rect.setAttribute("y", card.y);
      rect.setAttribute("width", card.width);
      rect.setAttribute("height", card.height);
      rect.setAttribute("rx", "14");
      rect.setAttribute("fill", "var(--bg-elevated, #ffffff)");
      rect.setAttribute("stroke", "var(--border-strong, #c8d3e4)");
      rect.setAttribute("stroke-width", "1.5");
      rect.setAttribute("filter", "url(#resourcemap-shadow)");
      cardsG.appendChild(rect);

      const headerG = document.createElementNS("http://www.w3.org/2000/svg", "g");
      headerG.setAttribute("clip-path", `url(#${clipId})`);
      const headerStrip = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      headerStrip.setAttribute("x", card.x);
      headerStrip.setAttribute("y", card.y);
      headerStrip.setAttribute("width", card.width);
      headerStrip.setAttribute("height", CARD_HEADER_H);
      headerStrip.setAttribute("fill", "var(--accent-soft, #e8f0fe)");
      headerG.appendChild(headerStrip);
      cardsG.appendChild(headerG);

      const headerLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      headerLabel.setAttribute("x", card.x + 16);
      headerLabel.setAttribute("y", card.y + 22);
      headerLabel.setAttribute("font-size", "12");
      headerLabel.setAttribute("font-weight", "800");
      headerLabel.setAttribute("letter-spacing", "0.02em");
      headerLabel.setAttribute("fill", "var(--accent, #1a73e8)");
      headerLabel.setAttribute("font-family", "var(--font-ui, sans-serif)");
      headerLabel.textContent = card.label.length > 28 ? card.label.slice(0, 27) + "…" : card.label;
      cardsG.appendChild(headerLabel);

      const countLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      countLabel.setAttribute("x", card.x + card.width - 12);
      countLabel.setAttribute("y", card.y + 22);
      countLabel.setAttribute("font-size", "10");
      countLabel.setAttribute("font-weight", "700");
      countLabel.setAttribute("text-anchor", "end");
      countLabel.setAttribute("fill", "var(--accent, #1a73e8)");
      countLabel.setAttribute("opacity", "0.7");
      countLabel.setAttribute("font-family", "var(--font-ui, sans-serif)");
      countLabel.textContent = `${card.count} resource${card.count === 1 ? "" : "s"}`;
      cardsG.appendChild(countLabel);
    });

    const edgesG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    edgesG.id = "resourcemap-edges-g";

    const nodesG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    nodesG.id = "resourcemap-nodes-g";

    const nodeMap = new Map(this.activeNodes.map(n => [n.id, n]));

    // A fixed per-edge delay reads as a nice cascade for a handful of
    // connections but turns into a multi-second crawl once a namespace pulls
    // in dozens of pods -- cap the *total* spread instead (same approach as
    // topology.js's shared graph renderer) so a large map still settles in
    // well under a second.
    const edgeDelayStep = Math.min(16, 300 / Math.max(1, this.activeEdges.length));

    // Render Edges -- anchored to the card's left/right edge (whichever
    // faces the other node) rather than center-to-center, so the curve
    // starts/ends at the card boundary instead of appearing to originate
    // from underneath the label text.
    this.activeEdges.forEach((e, edgeIdx) => {
      const src = nodeMap.get(e.source);
      const tgt = nodeMap.get(e.target);
      if (!src || !tgt) return;

      const srcX = src.x + (tgt.x >= src.x ? NODE_CARD_W / 2 : -NODE_CARD_W / 2);
      const tgtX = tgt.x + (tgt.x >= src.x ? -NODE_CARD_W / 2 : NODE_CARD_W / 2);

      const dx = tgtX - srcX;
      const cx1 = srcX + dx * 0.5;
      const cy1 = src.y;
      const cx2 = srcX + dx * 0.5;
      const cy2 = tgt.y;
      const d = `M ${srcX} ${src.y} C ${cx1} ${cy1}, ${cx2} ${cy2}, ${tgtX} ${tgt.y}`;

      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      path.setAttribute("class", animate ? "resourcemap-edge is-entering" : "resourcemap-edge");
      path.setAttribute("data-source", e.source);
      path.setAttribute("data-target", e.target);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "var(--border-strong, #cbd5e1)");
      path.setAttribute("stroke-width", "2");
      path.setAttribute("stroke-linecap", "round");
      path.setAttribute("marker-end", "url(#resourcemap-arrow)");
      if (animate) path.style.animationDelay = `${120 + edgeIdx * edgeDelayStep}ms`;
      edgesG.appendChild(path);

      // A second dashed path overlaid on the same curve, whose dash offset
      // animates continuously -- a subtle "traffic moving downstream"
      // indicator so a static diagram reads as a live system. Each edge
      // starts at a different phase (negative delay offset by index) so
      // parallel edges don't all pulse in visual lockstep.
      const flow = document.createElementNS("http://www.w3.org/2000/svg", "path");
      flow.setAttribute("d", d);
      flow.setAttribute("class", "resourcemap-edge-flow");
      flow.setAttribute("data-source", e.source);
      flow.setAttribute("data-target", e.target);
      flow.setAttribute("fill", "none");
      flow.style.animationDelay = `${-(edgeIdx * 260) % 1600}ms`;
      edgesG.appendChild(flow);
    });

    // Render Nodes -- rectangular cards, not circles. A circle only had
    // room for an icon, so the name/type had to live in a hover tooltip; a
    // card is wide enough to show both directly on the face. Hovering still
    // shows a tooltip (shared with Network/Storage topology, see
    // topology.js) for the fields that don't fit on the card -- namespace,
    // ports, capacity, and the rest of node.meta.
    const halfW = NODE_CARD_W / 2, halfH = NODE_CARD_H / 2;
    // Same total-spread cap as edges -- see edgeDelayStep above.
    const nodeDelayStep = Math.min(22, 480 / Math.max(1, this.activeNodes.length));
    this.activeNodes.forEach((node, nodeIdx) => {
      const kindKey = (node.kind || node.group || "pod").toLowerCase();
      const theme = RESOURCE_TYPE_THEME[kindKey] || { color: "var(--accent)", bg: "var(--accent-soft)", label: (node.kind || "RESOURCE").toUpperCase(), icon: "" };

      const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      g.setAttribute("class", "resourcemap-node");
      g.setAttribute("data-id", node.id);
      g.setAttribute("data-kind", kindKey);
      g.setAttribute("transform", `translate(${node.x}, ${node.y})`);
      g.style.cursor = "pointer";
      g.style.setProperty("--node-color", theme.color);

      // Selection Halo (hidden unless selected) -- lives directly on `g`,
      // outside the entrance-animated inner group below, so its own
      // continuous pulse animation never has to compose with a one-shot
      // entrance transform on the same element.
      const halo = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      halo.setAttribute("class", "resourcemap-node-halo");
      halo.setAttribute("x", -halfW - 5);
      halo.setAttribute("y", -halfH - 5);
      halo.setAttribute("width", NODE_CARD_W + 10);
      halo.setAttribute("height", NODE_CARD_H + 10);
      halo.setAttribute("rx", 12);
      halo.setAttribute("fill", theme.color);
      halo.setAttribute("fill-opacity", "0.16");
      halo.setAttribute("stroke", theme.color);
      halo.setAttribute("stroke-width", "2.5");
      halo.setAttribute("stroke-dasharray", "4 2");
      halo.style.display = node.id === this.selectedNodeId ? "block" : "none";
      g.appendChild(halo);

      // Everything else lives on an inner group so the entrance animation
      // (fade + scale, see .resourcemap-node__inner.is-entering) can use a
      // CSS `transform` without clobbering this node's own SVG positioning
      // `transform` attribute on `g` -- the two would otherwise fight, since
      // a CSS transform replaces rather than composes with one set via
      // attribute (same technique topology.js's shared renderer uses).
      const inner = document.createElementNS("http://www.w3.org/2000/svg", "g");
      inner.setAttribute("class", animate ? "resourcemap-node__inner is-entering" : "resourcemap-node__inner");
      if (animate) inner.style.animationDelay = `${nodeIdx * nodeDelayStep}ms`;
      g.appendChild(inner);

      // Card body
      const card = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      card.setAttribute("class", "resourcemap-node-card");
      card.setAttribute("x", -halfW);
      card.setAttribute("y", -halfH);
      card.setAttribute("width", NODE_CARD_W);
      card.setAttribute("height", NODE_CARD_H);
      card.setAttribute("rx", 9);
      card.setAttribute("fill", "var(--bg-elevated, #ffffff)");
      card.setAttribute("stroke", theme.color);
      card.setAttribute("stroke-width", "1.5");
      card.setAttribute("filter", "url(#resourcemap-shadow)");
      inner.appendChild(card);

      // Icon chip on the left
      const iconChip = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      iconChip.setAttribute("x", -halfW + 6);
      iconChip.setAttribute("y", -11);
      iconChip.setAttribute("width", 22);
      iconChip.setAttribute("height", 22);
      iconChip.setAttribute("rx", 6);
      iconChip.setAttribute("fill", theme.color);
      iconChip.setAttribute("fill-opacity", "0.14");
      inner.appendChild(iconChip);

      const iconG = document.createElementNS("http://www.w3.org/2000/svg", "g");
      iconG.setAttribute("transform", `translate(${-halfW + 11}, -6)`);
      iconG.setAttribute("color", theme.color);
      iconG.innerHTML = theme.icon || `<circle cx="12" cy="12" r="6" fill="${theme.color}"/>`;
      inner.appendChild(iconG);

      // Name + type, directly on the card face
      const nameText = document.createElementNS("http://www.w3.org/2000/svg", "text");
      nameText.setAttribute("x", -halfW + 34);
      nameText.setAttribute("y", -4);
      nameText.setAttribute("fill", "var(--fg, #1e293b)");
      nameText.setAttribute("font-size", "10.5");
      nameText.setAttribute("font-weight", "700");
      nameText.setAttribute("font-family", "var(--font-ui, sans-serif)");
      const truncated = node.label.length > 16 ? node.label.slice(0, 15) + "…" : node.label;
      nameText.textContent = truncated;
      inner.appendChild(nameText);

      const typeText = document.createElementNS("http://www.w3.org/2000/svg", "text");
      typeText.setAttribute("x", -halfW + 34);
      typeText.setAttribute("y", 10);
      typeText.setAttribute("fill", "var(--fg-muted, #64748b)");
      typeText.setAttribute("font-size", "8");
      typeText.setAttribute("font-weight", "700");
      typeText.setAttribute("letter-spacing", "0.05em");
      typeText.setAttribute("font-family", "var(--font-ui, sans-serif)");
      typeText.textContent = theme.label;
      inner.appendChild(typeText);

      // Health dot, top-right corner of the card
      const isHealthy = node.status === "healthy" || node.status === "Running" || node.status === "Bound";
      const isUnhealthy = node.status === "unhealthy" || node.status === "CrashLoopBackOff" || node.status === "Error";
      const dotColor = isHealthy ? "var(--success, #10b981)" : isUnhealthy ? "var(--danger, #ef4444)" : "var(--warning, #f59e0b)";

      const statusDot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      statusDot.setAttribute("cx", halfW - 2);
      statusDot.setAttribute("cy", -halfH + 2);
      statusDot.setAttribute("r", 4.5);
      statusDot.setAttribute("fill", dotColor);
      statusDot.setAttribute("stroke", "var(--bg-elevated, #ffffff)");
      statusDot.setAttribute("stroke-width", "1.5");
      inner.appendChild(statusDot);

      // Hover: highlight this node's connections (dim everything else) and
      // show the shared topology tooltip with whatever node.meta fields
      // apply to this resource kind -- the card face only has room for
      // name + type, everything else (namespace, ports, capacity, ...)
      // lives here instead.
      g.addEventListener("mouseenter", (e) => {
        this.highlightConnections(node.id);
        showNodeTooltip(node, e.clientX, e.clientY);
      });
      g.addEventListener("mousemove", (e) => moveNodeTooltip(e.clientX, e.clientY));
      g.addEventListener("mouseleave", () => {
        this.clearHighlights();
        hideNodeTooltip();
      });

      // Click event
      g.addEventListener("click", (e) => {
        e.stopPropagation();
        this.selectNode(node.id);
      });

      nodesG.appendChild(g);
    });

    rootG.appendChild(cardsG);
    rootG.appendChild(edgesG);
    rootG.appendChild(nodesG);
    this.svg.appendChild(rootG);
  }

  // Hovering a node previews its direct connections: the edges (and their
  // animated flow overlays) touching it get an accent glow, its own card
  // gets a glow filter, and everything not part of this node's chain dims --
  // driven entirely by CSS classes (see the .is-highlighted/.is-dimmed rules
  // in styles.css) rather than toggling individual style properties, so the
  // opacity/stroke/glow changes all transition smoothly together.
  highlightConnections(nodeId) {
    this.hoveredNodeId = nodeId;
    const connectedNodeIds = new Set([nodeId]);

    document.querySelectorAll(".resourcemap-edge, .resourcemap-edge-flow").forEach(edge => {
      const src = edge.getAttribute("data-source");
      const tgt = edge.getAttribute("data-target");
      if (src === nodeId || tgt === nodeId) {
        connectedNodeIds.add(src);
        connectedNodeIds.add(tgt);
        edge.classList.add("is-highlighted");
        edge.classList.remove("is-dimmed");
        if (edge.classList.contains("resourcemap-edge")) edge.setAttribute("marker-end", "url(#resourcemap-arrow-highlight)");
      } else {
        edge.classList.add("is-dimmed");
        edge.classList.remove("is-highlighted");
      }
    });

    document.querySelectorAll(".resourcemap-node").forEach(n => {
      const id = n.getAttribute("data-id");
      if (connectedNodeIds.has(id)) {
        n.classList.add("is-highlighted");
        n.classList.remove("is-dimmed");
      } else {
        n.classList.add("is-dimmed");
        n.classList.remove("is-highlighted");
      }
    });
  }

  clearHighlights() {
    this.hoveredNodeId = null;
    document.querySelectorAll(".resourcemap-edge, .resourcemap-edge-flow").forEach(edge => {
      edge.classList.remove("is-highlighted", "is-dimmed");
      if (edge.classList.contains("resourcemap-edge")) edge.setAttribute("marker-end", "url(#resourcemap-arrow)");
    });

    document.querySelectorAll(".resourcemap-node").forEach(n => {
      n.classList.remove("is-highlighted", "is-dimmed");
    });
  }

  selectNode(nodeId) {
    this.selectedNodeId = nodeId;
    const node = this.activeNodes.find(n => n.id === nodeId);
    if (!node) return;

    // Update glowing halos
    document.querySelectorAll(".resourcemap-node").forEach(n => {
      const id = n.getAttribute("data-id");
      const halo = n.querySelector(".resourcemap-node-halo");
      if (halo) {
        halo.style.display = id === nodeId ? "block" : "none";
      }
    });

    // Populate Right Drawer Panel and slide it into view (it starts
    // collapsed so the canvas has the full width until something is
    // actually selected).
    this.drawer?.classList.add("is-open");
    this.renderDrawer(node);
  }

  deselectNode() {
    this.selectedNodeId = null;
    this.drawer?.classList.remove("is-open");
    document.querySelectorAll(".resourcemap-node-halo").forEach(h => (h.style.display = "none"));
    const body = document.getElementById("resourcemap-drawer-body");
    const nameEl = document.getElementById("resourcemap-drawer-name");
    const typeEl = document.getElementById("resourcemap-drawer-type");
    if (nameEl) nameEl.textContent = "Select a resource";
    if (typeEl) typeEl.textContent = "RESOURCE DETAILS";
    if (body) {
      body.innerHTML = `
        <div style="color:var(--fg-subtle);font-size:0.82rem;text-align:center;padding:2rem 0;">
          Click any resource node in the map to inspect its real-time metadata, inbound dependencies, and outbound relationships.
        </div>
      `;
    }
  }


  renderDrawer(node) {
    const nameEl = document.getElementById("resourcemap-drawer-name");
    const typeEl = document.getElementById("resourcemap-drawer-type");
    const iconEl = document.getElementById("resourcemap-drawer-icon");
    const body = document.getElementById("resourcemap-drawer-body");
    if (!body) return;

    const kindKey = (node.kind || node.group || "pod").toLowerCase();
    const theme = RESOURCE_TYPE_THEME[kindKey] || { color: "var(--accent)", bg: "var(--accent-soft)", label: (node.kind || "RESOURCE").toUpperCase(), icon: "" };

    if (nameEl) nameEl.textContent = node.label;
    if (typeEl) typeEl.textContent = theme.label;
    if (iconEl) {
      iconEl.style.background = theme.bg;
      iconEl.style.color = theme.color;
      iconEl.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${theme.icon}</svg>`;
    }

    // Find Incoming and Outgoing connections
    const incomingEdges = this.activeEdges.filter(e => e.target === node.id);
    const outgoingEdges = this.activeEdges.filter(e => e.source === node.id);

    const nodeMap = new Map(this.activeNodes.map(n => [n.id, n]));
    const incomingNodes = incomingEdges.map(e => nodeMap.get(e.source)).filter(Boolean);
    const outgoingNodes = outgoingEdges.map(e => nodeMap.get(e.target)).filter(Boolean);

    const meta = node.meta || {};

    const properties = [
      { label: "Namespace", value: meta.namespace || "default" },
      { label: "Kind", value: node.kind || node.group || "—" },
      meta.type ? { label: "Type", value: meta.type } : null,
      meta.cluster_ip ? { label: "Cluster IP", value: meta.cluster_ip } : null,
      meta.node ? { label: "Node", value: meta.node } : null,
      meta.replicas !== undefined ? { label: "Replicas", value: `${meta.ready_replicas || 0}/${meta.replicas}` } : null,
      meta.created_at ? { label: "Age", value: timeAgo(meta.created_at) } : null,
    ].filter(Boolean);

    const propRows = properties.map(p => h("div", {
      style: "display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:0.82rem;"
    }, [
      h("span", { style: "color:var(--fg-muted);" }, p.label),
      h("span", { style: "font-family:var(--font-mono);font-weight:600;color:var(--fg);" }, String(p.value))
    ]));

    // Incoming list
    const incomingSection = h("div", { style: "display:flex;flex-direction:column;gap:6px;" }, [
      h("div", { style: "font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.06em;" }, "INCOMING"),
      incomingNodes.length ? h("div", { style: "display:flex;flex-direction:column;gap:4px;" }, incomingNodes.map(inNode => {
        const inTheme = RESOURCE_TYPE_THEME[(inNode.kind || inNode.group || "pod").toLowerCase()] || theme;
        return h("button", {
          type: "button",
          className: "btn--unstyled",
          style: "display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;background:var(--bg-inset);border:1px solid var(--border);cursor:pointer;text-align:left;transition:all 150ms;",
          onclick: () => this.selectNode(inNode.id)
        }, [
          h("span", { style: `color:${inTheme.color};font-size:0.75rem;font-weight:700;` }, "⇄"),
          h("span", { style: "font-family:var(--font-mono);font-size:0.82rem;font-weight:600;color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }, inNode.label)
        ]);
      })) : h("div", { style: "font-size:0.78rem;color:var(--fg-subtle);font-style:italic;" }, "No upstream incoming connections")
    ]);

    // Outgoing list
    const outgoingSection = h("div", { style: "display:flex;flex-direction:column;gap:6px;" }, [
      h("div", { style: "font-size:0.72rem;font-weight:700;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.06em;" }, "OUTGOING"),
      outgoingNodes.length ? h("div", { style: "display:flex;flex-direction:column;gap:4px;" }, outgoingNodes.map(outNode => {
        const outTheme = RESOURCE_TYPE_THEME[(outNode.kind || outNode.group || "pod").toLowerCase()] || theme;
        return h("button", {
          type: "button",
          className: "btn--unstyled",
          style: "display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;background:var(--bg-inset);border:1px solid var(--border);cursor:pointer;text-align:left;transition:all 150ms;",
          onclick: () => this.selectNode(outNode.id)
        }, [
          h("span", { style: `color:${outTheme.color};font-size:0.75rem;font-weight:700;` }, "◆"),
          h("span", { style: "font-family:var(--font-mono);font-size:0.82rem;font-weight:600;color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }, outNode.label)
        ]);
      })) : h("div", { style: "font-size:0.78rem;color:var(--fg-subtle);font-style:italic;" }, "No downstream outgoing connections")
    ]);

    // Action buttons
    const actions = h("div", { style: "display:flex;gap:8px;margin-top:auto;padding-top:1rem;border-top:1px solid var(--border);" }, [
      h("button", {
        type: "button",
        className: "btn btn--secondary btn--sm",
        style: "flex:1;",
        onclick: () => {
          if (this.onNodeAction) this.onNodeAction("yaml", node);
        }
      }, "View YAML"),
      kindKey === "pod" ? h("button", {
        type: "button",
        className: "btn btn--primary btn--sm",
        style: "flex:1;",
        onclick: () => {
          if (this.onNodeAction) this.onNodeAction("logs", node);
        }
      }, "Logs") : null,
      kindKey === "pod" ? h("button", {
        type: "button",
        className: "btn btn--primary btn--sm",
        style: "flex:1;",
        onclick: () => {
          if (this.onNodeAction) this.onNodeAction("inspect", node);
        }
      }, "Inspect") : null
    ].filter(Boolean));

    renderInto(body, h("div", { style: "display:flex;flex-direction:column;gap:1.2rem;height:100%;" }, [
      h("div", { style: "display:flex;flex-direction:column;" }, propRows),
      incomingSection,
      outgoingSection,
      actions
    ]));
  }
}
