// Reusable render functions. Everything returns a DOM Node (built via the
// `h()` helper, never innerHTML with interpolated strings) so resource
// names/labels/messages coming back from the cluster can never be
// interpreted as markup.

const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_TAGS = new Set([
  "svg", "g", "rect", "circle", "path", "text", "title", "tspan", "line", "polygon", "polyline", "defs",
  "linearGradient", "stop",
]);

export function h(tag, attrs = {}, children = []) {
  // SVG elements MUST be created in the SVG namespace -- document.createElement('svg'/'rect'/...)
  // silently produces inert HTMLUnknownElements that never render as vector graphics.
  const node = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    // Always use setAttribute for class: SVGElement.className is an SVGAnimatedString,
    // not a plain settable string, so node.className = v silently no-ops on SVG nodes.
    // Accept "className" as an alias for "class" -- a huge number of call
    // sites across app.js use React-style `className:` (muscle memory from
    // JSX), and without this, setAttribute("className", v) below creates a
    // dead, non-standard attribute that no CSS selector ever matches: the
    // element silently gets zero classes applied. That's invisible in light
    // mode (the browser's plain UA button/element styling happens to look
    // plausible against this app's light background) but breaks outright in
    // dark mode (UA styling doesn't reliably follow it), which is how this
    // was actually noticed.
    if (k === "class" || k === "className") node.setAttribute("class", v);
    else if (k === "html") node.innerHTML = v; // only ever used with static, hardcoded SVG markup below
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }
  return node;
}

const ICONS = {
  warning: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4M12 17h.01"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  alertCircle: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
};

function icon(name, extraClass = "") {
  return h("svg", { class: `icon ${extraClass}`.trim(), viewBox: "0 0 24 24", html: ICONS[name] || "" });
}

function iconWrap(name) {
  return h("div", { class: "icon-wrap" }, icon(name));
}

// Small, original geometric glyphs (not the providers' trademarked logos) so
// each cloud badge is visually distinct at a glance while its background
// gradient still carries that provider's real brand colors.
const PROVIDER_GLYPHS = {
  gke: '<path d="M12 2 22 12 12 22 2 12z"/>',
  eks: '<path d="M12 2 20.5 7v10L12 22 3.5 17V7z"/>',
  aks: '<path d="M12 3 21 20H3z"/>',
};

function providerIcon(provider) {
  const p = (provider || "").toLowerCase();
  return h("span", { class: `badge__icon-wrap badge__icon-wrap--${p}` }, h("svg", { class: "badge__icon", viewBox: "0 0 24 24", fill: "white", html: PROVIDER_GLYPHS[p] || "" }));
}

export function clear(container) {
  if (!container) return;
  container.replaceChildren();
}

export function renderInto(container, node) {
  if (!container) return;
  container.replaceChildren(node);
}

// -- generic states ---------------------------------------------------------

export function emptyState(title, message) {
  return h("div", { class: "state-block" }, [iconWrap("inbox"), h("div", { class: "state-block__title" }, title), h("div", { class: "italic" }, message || "")]);
}

export function errorState(err, onRetry) {
  const message = err && err.message ? err.message : "Something went wrong.";
  const children = [iconWrap("alertCircle"), h("div", { class: "state-block__title" }, "Couldn't load this view"), h("div", { class: "italic" }, message)];
  if (onRetry) {
    children.push(h("button", { class: "btn btn--primary", style: "margin-top:8px;", onClick: onRetry }, "Retry"));
  }
  return h("div", { class: "state-block state-block--error" }, children);
}

export function loadingSkeleton(rows = 5) {
  const children = [];
  for (let i = 0; i < rows; i++) {
    children.push(h("div", { class: "skeleton skeleton-row", style: `width:${85 - (i % 3) * 12}%;` }));
  }
  return h("div", { style: "padding: var(--space-4);" }, children);
}

// -- status pills -------------------------------------------------------

export function statusPillKind(rawStatus, healthy) {
  if (typeof healthy === "boolean") return healthy ? "healthy" : "critical";
  const s = (rawStatus || "").toLowerCase();
  if (["ready", "running", "succeeded", "bound", "active"].includes(s)) return "healthy";
  if (["pending", "containercreating", "podinitializing"].includes(s)) return "warning";
  if (["notready", "failed", "unhealthy", "crashloopbackoff", "error", "evicted"].includes(s)) return "critical";
  return "unknown";
}

export function pill(label, kind) {
  return h("span", { class: `pill pill--${kind}` }, label);
}

export function statusPill(rawStatus, healthy) {
  const kind = statusPillKind(rawStatus, healthy);
  return pill(rawStatus ?? (healthy ? "Healthy" : "Unhealthy"), kind);
}

export function providerBadge(provider, label) {
  const p = (provider || "").toLowerCase();
  return h("span", { class: `badge badge--${p}` }, [providerIcon(p), label || (provider || "").toUpperCase()]);
}

export function envChip(env) {
  return h("span", { class: `env-chip env-chip--${env}` }, env);
}

// -- stat cards -----------------------------------------------------------

export function statCard(label, value, sub) {
  const children = [h("div", { class: "stat-card__label" }, label), h("div", { class: "stat-card__value" }, String(value))];
  if (sub) children.push(h("div", { class: "stat-card__sub" }, sub));
  return h("div", { class: "stat-card" }, children);
}

export function statGrid(cards) {
  return h("div", { class: "stat-grid" }, cards);
}

// -- panel ------------------------------------------------------------------

export function panel(title, bodyNode, headerExtras, padded = false) {
  // Flush (no body padding) is right for a table -- its cells carry their
  // own spacing, and the header row's background should span edge to edge.
  // Plain content (text, links) needs real padding, or it sits flush
  // against the panel's edges while the header above it stays indented --
  // pass padded=true for that case.
  const header = title === null ? null : h("div", { class: "panel__header" }, [h("span", { class: "panel__title" }, title), headerExtras || null]);
  const bodyClass = padded ? "panel__body" : "panel__body panel__body--flush";
  return h("div", { class: "panel" }, [header, h("div", { class: bodyClass }, bodyNode)]);
}

// -- tables -----------------------------------------------------------------

export function dataTable(columns, rows, opts = {}) {
  if (!rows.length) {
    return emptyState(opts.emptyTitle || "No resources", opts.emptyMessage || "Nothing found for this filter.");
  }
  const thead = h(
    "thead",
    {},
    h("tr", {}, columns.map((c) => h("th", {}, c.label)))
  );
  const tbody = h(
    "tbody",
    {},
    rows.map((row) =>
      h(
        "tr",
        {},
        columns.map((c) => {
          const value = c.render ? c.render(row) : String(row[c.key] ?? "—");
          const classes = [c.mono && "cell-mono", c.truncate && "cell-truncate"].filter(Boolean).join(" ") || undefined;
          // Truncation + a title tooltip only make sense for plain text --
          // a custom render() can return a pill/button DOM node, which sizes
          // itself and shouldn't be squeezed into an ellipsis.
          const titleAttr = c.truncate && typeof value === "string" ? value : undefined;
          return h("td", { class: classes, title: titleAttr }, value);
        })
      )
    )
  );
  return h("div", { class: "table-wrap" }, h("table", { class: "data-table" }, [thead, tbody]));
}

// -- select population --------------------------------------------------

export function fillSelect(selectEl, items, { valueKey, labelKey, placeholder, selected } = {}) {
  if (!selectEl) return;
  selectEl.replaceChildren();
  if (placeholder) selectEl.appendChild(h("option", { value: "" }, placeholder));
  for (const item of items) {
    const value = typeof item === "string" ? item : item[valueKey];
    const label = typeof item === "string" ? item : item[labelKey];
    const opt = h("option", { value }, label);
    if (selected !== undefined && value === selected) opt.setAttribute("selected", "");
    selectEl.appendChild(opt);
  }
}

// -- warning banner --------------------------------------------------------

export function warningBanner(text) {
  return h("div", { class: "warning-banner" }, [icon("warning"), h("div", {}, text)]);
}

export function revealButton(onClick) {
  return h("button", { class: "btn btn--danger", onClick }, [icon("eye"), " Reveal"]);
}
