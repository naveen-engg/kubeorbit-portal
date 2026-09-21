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

// Providers' actual brand marks (Microsoft's official Azure glyph, Google's
// official Cloud glyph -- both trace exactly, full precision) rather than
// abstract approximations. AWS has no standalone pictorial mark in its brand
// kit (its logo is the "aws" wordmark plus the smile underline as one
// unit) -- full precision there too, just cropped to the smile/arrow
// portion via viewBox instead of scaling the whole wordmark, since the
// wordmark's letterforms turn to mud below ~40px and every call site here
// renders this at 11-20px. Local gets a terminal glyph (a local kubeconfig
// context, not a cloud, so no brand mark applies). Colors come from the
// badge's own gradient; each entry carries its own viewBox since the AWS
// crop isn't the standard 0 0 24 24.
const PROVIDER_GLYPHS = {
  gke: {
    viewBox: "0 0 24 24",
    html: '<path d="M12.19 2.38a9.344 9.344 0 0 0-9.234 6.893c.053-.02-.055.013 0 0-3.875 2.551-3.922 8.11-.247 10.941l.006-.007-.007.03a6.717 6.717 0 0 0 4.077 1.356h5.173l.03.03h5.192c6.687.053 9.376-8.605 3.835-12.35a9.365 9.365 0 0 0-2.821-4.552l-.043.043.006-.05A9.344 9.344 0 0 0 12.19 2.38zm-.358 4.146c1.244-.04 2.518.368 3.486 1.15a5.186 5.186 0 0 1 1.862 4.078v.518c3.53-.07 3.53 5.262 0 5.193h-5.193l-.008.009v-.04H6.785a2.59 2.59 0 0 1-1.067-.23h.001a2.597 2.597 0 1 1 3.437-3.437l3.013-3.012A6.747 6.747 0 0 0 8.11 8.24c.018-.01.04-.026.054-.023a5.186 5.186 0 0 1 3.67-1.69z"/>',
  },
  eks: {
    viewBox: "-0.5 13 25.3 6.8",
    html: '<path d="M21.698 16.207c-2.626 1.94-6.442 2.969-9.722 2.969-4.598 0-8.74-1.7-11.87-4.526-.247-.223-.024-.527.272-.351 3.384 1.963 7.559 3.153 11.877 3.153 2.914 0 6.114-.607 9.06-1.852.439-.2.814.287.383.607zM22.792 14.961c-.336-.43-2.22-.207-3.074-.103-.255.032-.295-.192-.063-.36 1.5-1.053 3.967-.75 4.254-.399.287.36-.08 2.826-1.485 4.007-.215.184-.423.088-.327-.151.32-.79 1.03-2.57.695-2.994z"/>',
  },
  aks: {
    viewBox: "0 0 24 24",
    html: '<path d="M22.379 23.343a1.62 1.62 0 0 0 1.536-2.14v.002L17.35 1.76A1.62 1.62 0 0 0 15.816.657H8.184A1.62 1.62 0 0 0 6.65 1.76L.086 21.204a1.62 1.62 0 0 0 1.536 2.139h4.741a1.62 1.62 0 0 0 1.535-1.103l.977-2.892 4.947 3.675c.28.208.618.32.966.32m-3.084-12.531 3.624 10.739a.54.54 0 0 1-.51.713v-.001h-.03a.54.54 0 0 1-.322-.106l-9.287-6.9h4.853m6.313 7.006c.116-.326.13-.694.007-1.058L9.79 1.76a1.722 1.722 0 0 0-.007-.02h6.034a.54.54 0 0 1 .512.366l6.562 19.445a.54.54 0 0 1-.338.684"/>',
  },
  local: {
    viewBox: "0 0 24 24",
    html: '<rect x="4" y="5" width="16" height="12" rx="1.5" fill="none" stroke="white" stroke-width="2"/><path d="m8 9.5 2.5 2.5L8 14.5M13 14.5h3" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  },
};

function providerIcon(provider) {
  const p = (provider || "").toLowerCase();
  const glyph = PROVIDER_GLYPHS[p] || { viewBox: "0 0 24 24", html: "" };
  return h("span", { class: `badge__icon-wrap badge__icon-wrap--${p}` }, h("svg", { class: "badge__icon", viewBox: glyph.viewBox, fill: "white", html: glyph.html }));
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

// Cards used to be forced into equal grid columns with one fixed value
// font-size, so a short value like "2" got the same box as "Not configured"
// -- the long ones wrapped into a cramped multi-line mess. Sizing the value
// text down as it gets longer (and letting .stat-grid's flexbox give each
// card its own width -- see styles.css) keeps every value on one line
// without every card being forced to match the widest one.
function statValueSizeClass(text) {
  const len = text.length;
  if (len > 13) return "stat-card__value--xs";
  if (len > 8) return "stat-card__value--sm";
  if (len > 4) return "stat-card__value--md";
  return "";
}

export function statCard(label, value, sub, icon) {
  const text = String(value);
  const labelEl = h("span", { class: "stat-card__label" }, label);
  const header = icon
    ? h("div", { class: "stat-card__header" }, [h("span", { class: "stat-card__icon", html: icon }), labelEl])
    : labelEl;
  const children = [header, h("div", { class: `stat-card__value ${statValueSizeClass(text)}`.trim() }, text)];
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
