// Helm Releases, Helm Version History, Helm Diff Viewer & Release Details Modal.

import { api } from "./api.js";
import { h, renderInto, emptyState, errorState, loadingSkeleton } from "./components.js";

const $ = (id) => document.getElementById(id);

const DEFAULT_MASK_NOTICE =
  "Secret data and credential-like values are masked server-side. The fp fingerprint changes when the underlying value changes.";

function releaseKey(ns, name) {
  return `${ns || "default"}/${name || ""}`;
}

function splitReleaseKey(key) {
  const raw = String(key || "");
  const slashIdx = raw.indexOf("/");
  if (slashIdx <= 0) return { namespace: "default", name: raw };
  return { namespace: raw.slice(0, slashIdx), name: raw.slice(slashIdx + 1) };
}

function statusPillTone(status) {
  const s = String(status || "").toLowerCase();
  if (s === "deployed" || s === "healthy") return "deployed";
  if (s === "failed" || s === "critical") return "failed";
  if (s.startsWith("pending") || s === "warning") return "pending";
  if (s === "superseded" || s.startsWith("uninstalled") || s === "uninstalling") return "superseded";
  return "neutral";
}

function renderHelmStatusPill(status) {
  const tone = statusPillTone(status);
  return h("span", { className: `helm-status-pill helm-status-pill--${tone}` }, [
    h("span", { className: `helm-status-dot helm-status-dot--${tone}` }),
    status || "unknown",
  ]);
}

function renderCurrentPill() {
  return h("span", { className: "helm-current-pill" }, [
    h("span", { className: "helm-status-dot helm-status-dot--deployed" }),
    "current",
  ]);
}

function formatAgeNode(iso, timeAgo) {
  const text = timeAgo ? timeAgo(iso) : iso || "—";
  return h("span", { title: iso || "", className: "helm-updated-cell" }, text || "—");
}

function renderDiffBlocksTable(blocks) {
  if (!blocks || !blocks.length) {
    return h("div", { className: "helm-diff-empty" }, "Both revisions have identical content for this comparison mode.");
  }

  const tbody = h("tbody", {});

  blocks.forEach((block) => {
    if (block.type === "collapsed") {
      const expandable = Array.isArray(block.lines) && block.lines.length > 0;
      const tr = h(
        "tr",
        {
          className: `helm-diff-row helm-diff-row--collapsed${expandable ? " is-expandable" : ""}`,
          title: expandable ? "Click to expand unchanged lines" : "",
          onClick: () => {
            if (!expandable) return;
            const expandedRows = block.lines.map((ln) => renderDiffCodeRow(ln));
            const frag = document.createDocumentFragment();
            expandedRows.forEach((row) => frag.appendChild(row));
            tr.replaceWith(frag);
          },
        },
        [
          h("td", { className: "helm-diff-ln helm-diff-ln--collapsed", colspan: "3" }, "⋯"),
          h(
            "td",
            { className: "helm-diff-code helm-diff-code--collapsed" },
            [
              `${block.count} unchanged ${block.count === 1 ? "line" : "lines"}`,
              expandable ? h("span", { className: "helm-diff-expand-hint" }, " (click to expand)") : null,
            ].filter(Boolean)
          ),
        ]
      );
      tbody.appendChild(tr);
      return;
    }

    if (block.type === "hunk" && Array.isArray(block.lines)) {
      block.lines.forEach((ln) => tbody.appendChild(renderDiffCodeRow(ln)));
    }
  });

  return h("div", { className: "helm-diff-table-wrap" }, h("table", { className: "helm-diff-table" }, tbody));
}

function renderDiffCodeRow(ln) {
  const kind = ln.kind || "context";
  const sign = kind === "add" ? "+" : kind === "remove" ? "-" : "";
  return h("tr", { className: `helm-diff-row helm-diff-row--${kind}` }, [
    h("td", { className: "helm-diff-ln" }, ln.old_line != null ? String(ln.old_line) : ""),
    h("td", { className: "helm-diff-ln" }, ln.new_line != null ? String(ln.new_line) : ""),
    h("td", { className: `helm-diff-sign helm-diff-sign--${kind}` }, sign),
    h("td", { className: "helm-diff-code" }, h("code", {}, ln.text ?? "")),
  ]);
}

export function createHelmViewsController(deps) {
  const {
    getClusterId,
    timeAgo,
    openManifestModal,
    openPodInspectorModal,
    openResourceMapForNamespace,
    switchView,
    nextLoadToken,
    isStaleToken,
  } = deps;

  const HELM_CACHE_TTL_MS = 30_000;

  const helmState = {
    releases: [],
    releasesFetchedAt: 0,
    namespaces: [],
    releasesAvailable: true,
    releasesError: null,
    filterNamespace: "",
    filterSearch: "",
    selectedReleaseKey: "",
    historyCache: new Map(), // `${clusterId}:${ns}/${name}` -> { data, ts }
    diffFromRev: null,
    diffToRev: null,
    diffMode: "values",
    diffData: null,
    detailsTab: "overview",
  };

  function resetHelmState() {
    helmState.releases = [];
    helmState.releasesFetchedAt = 0;
    helmState.namespaces = [];
    helmState.releasesAvailable = true;
    helmState.releasesError = null;
    helmState.filterNamespace = "";
    helmState.filterSearch = "";
    helmState.selectedReleaseKey = "";
    helmState.historyCache.clear();
    helmState.diffFromRev = null;
    helmState.diffToRev = null;
    helmState.diffMode = "values";
    helmState.diffData = null;
    helmState.detailsTab = "overview";
  }

  function findReleaseByKey(key) {
    const { namespace, name } = splitReleaseKey(key);
    return helmState.releases.find((r) => r.namespace === namespace && r.name === name) || null;
  }

  async function ensureReleaseInventory(force = false) {
    const clusterId = getClusterId();
    if (!clusterId) return [];
    const isFresh =
      !force &&
      helmState.releasesFetchedAt > 0 &&
      Date.now() - helmState.releasesFetchedAt < HELM_CACHE_TTL_MS;
    if (isFresh) return helmState.releases;
    const data = await api.getHelm(clusterId);
    helmState.releasesAvailable = Boolean(data.available);
    helmState.releasesError = data.error || null;
    helmState.releases = Array.isArray(data.items) ? data.items : [];
    helmState.namespaces = Array.isArray(data.namespaces)
      ? data.namespaces
      : Array.from(new Set(helmState.releases.map((r) => r.namespace).filter(Boolean))).sort();
    helmState.releasesFetchedAt = Date.now();
    return helmState.releases;
  }

  async function fetchReleaseHistory(namespace, name, force = false) {
    const clusterId = getClusterId();
    const key = `${clusterId}:${releaseKey(namespace, name)}`;
    const cached = !force ? helmState.historyCache.get(key) : null;
    if (cached && Date.now() - cached.ts < HELM_CACHE_TTL_MS) {
      return cached.data;
    }
    const data = await api.getHelmReleaseHistory(clusterId, namespace, name);
    helmState.historyCache.set(key, { data, ts: Date.now() });
    return data;
  }

  function resolveSelectedReleaseKey() {
    if (helmState.selectedReleaseKey && findReleaseByKey(helmState.selectedReleaseKey)) {
      return helmState.selectedReleaseKey;
    }
    if (helmState.releases.length > 0) {
      const first = helmState.releases[0];
      helmState.selectedReleaseKey = releaseKey(first.namespace, first.name);
      return helmState.selectedReleaseKey;
    }
    return "";
  }

  function isSecretKind(kind) {
    const base = String(kind || "").trim().toLowerCase().split("/").pop().split(".")[0];
    return base === "secret" || base === "secrets";
  }

  function openResourceFromHelm(res, rel) {
    if (!res || !res.name) return;
    if (isSecretKind(res.kind)) {
      if (rel) {
        openDetailsModal(rel.namespace, rel.name, Number(rel.revision) || null);
      }
      return;
    }
    if (String(res.kind || "").toLowerCase() === "pod" && openPodInspectorModal) {
      openPodInspectorModal(res.name, res.namespace || "default");
      return;
    }
    if (openManifestModal) {
      openManifestModal(res.kind || "Resource", res.name, res.namespace || "default");
    }
  }

  function navigateToHistory(namespace, name) {
    helmState.selectedReleaseKey = releaseKey(namespace, name);
    switchView("helm-history");
  }

  function navigateToDiff(namespace, name, fromRev, toRev, mode = "values") {
    helmState.selectedReleaseKey = releaseKey(namespace, name);
    helmState.diffFromRev = fromRev != null ? Number(fromRev) : null;
    helmState.diffToRev = toRev != null ? Number(toRev) : null;
    helmState.diffMode = mode || "values";
    switchView("helm-diff");
  }

  // -------------------------------------------------------------------------
  // 1. Helm Releases View
  // -------------------------------------------------------------------------

  function renderResourcesCell(rel) {
    const resources = Array.isArray(rel.resources) ? rel.resources : [];
    if (!resources.length) {
      return h("span", { className: "cell-muted" }, "—");
    }
    const maxVisible = 24;
    const visible = resources.slice(0, maxVisible);
    const squares = visible.map((res) => {
      const st = res.status || "unknown";
      const label = `${res.kind}/${res.name} (${st})`;
      return h("button", {
        type: "button",
        className: `helm-res-sq helm-res-sq--${st}`,
        title: label,
        "aria-label": label,
        onClick: (e) => {
          e.stopPropagation();
          openResourceFromHelm(res, rel);
        },
      });
    });
    if (resources.length > maxVisible) {
      squares.push(
        h(
          "button",
          {
            type: "button",
            className: "helm-res-more",
            title: `${resources.length} total resources — open Details`,
            onClick: (e) => {
              e.stopPropagation();
              openDetailsModal(rel.namespace, rel.name, Number(rel.revision) || null);
            },
          },
          `+${resources.length - maxVisible}`
        )
      );
    }
    return h("div", { className: "helm-res-squares" }, squares);
  }

  function ensureReleasesShell(container) {
    let toolbar = container.querySelector(".helm-toolbar");
    let panelHost = container.querySelector("#helm-releases-table-host");
    if (toolbar && panelHost) {
      return {
        nsSelect: toolbar.querySelector("#helm-ns-filter"),
        searchInput: toolbar.querySelector("#helm-search-input"),
        panelHost,
      };
    }

    const nsSelect = h("select", {
      id: "helm-ns-filter",
      className: "select helm-filter-select",
      "aria-label": "Filter Helm releases by namespace",
      onChange: (e) => {
        helmState.filterNamespace = e.target.value;
        renderFilteredReleasesTable();
      },
    });

    const searchInput = h("input", {
      id: "helm-search-input",
      type: "text",
      className: "input helm-search-input",
      placeholder: "Search release or chart...",
      value: helmState.filterSearch,
      "aria-label": "Search Helm releases",
      onInput: (e) => {
        helmState.filterSearch = e.target.value;
        renderFilteredReleasesTable();
      },
    });

    toolbar = h("div", { className: "helm-toolbar" }, [
      h("div", { className: "helm-field" }, [
        h("label", { className: "helm-field__label", for: "helm-ns-filter" }, "Namespace"),
        nsSelect,
      ]),
      h("div", { className: "helm-field" }, [
        h("label", { className: "helm-field__label", for: "helm-search-input" }, "Search"),
        searchInput,
      ]),
    ]);

    panelHost = h("div", { id: "helm-releases-table-host" });
    renderInto(container, h("div", { className: "helm-page-stack" }, [toolbar, panelHost]));
    return { nsSelect, searchInput, panelHost };
  }

  function populateReleasesNamespaceOptions(nsSelect) {
    if (!nsSelect) return;
    const nss = helmState.namespaces;
    const current = helmState.filterNamespace;
    nsSelect.replaceChildren(
      h("option", { value: "" }, `All namespaces (${nss.length})`),
      ...nss.map((ns) => h("option", { value: ns }, ns))
    );
    nsSelect.value = nss.includes(current) ? current : "";
    helmState.filterNamespace = nsSelect.value;
  }

  function renderFilteredReleasesTable() {
    const panelHost = $("helm-releases-table-host");
    if (!panelHost) return;

    if (!helmState.releasesAvailable) {
      renderInto(
        panelHost,
        emptyState("Helm not available", "The `helm` CLI is not installed on the server running KubeOrbit.")
      );
      return;
    }

    if (helmState.releasesError && !helmState.releases.length) {
      renderInto(panelHost, emptyState("Failed to list Helm releases", helmState.releasesError));
      return;
    }

    const q = (helmState.filterSearch || "").trim().toLowerCase();
    const nsFilter = helmState.filterNamespace;
    const filtered = helmState.releases.filter((r) => {
      if (nsFilter && r.namespace !== nsFilter) return false;
      if (!q) return true;
      return (
        String(r.name || "").toLowerCase().includes(q) ||
        String(r.chart || "").toLowerCase().includes(q) ||
        String(r.namespace || "").toLowerCase().includes(q)
      );
    });

    if (!filtered.length) {
      renderInto(
        panelHost,
        h(
          "div",
          { className: "panel" },
          emptyState(
            "No Helm releases found",
            helmState.releases.length
              ? "No releases match the active namespace or search filter."
              : "No Helm releases were detected in this cluster."
          )
        )
      );
      return;
    }

    const columns = ["RELEASE", "NAMESPACE", "CHART", "APP VERSION", "REVISION", "STATUS", "RESOURCES", "UPDATED", "ACTIONS"];
    const thead = h("thead", {}, h("tr", {}, columns.map((c) => h("th", {}, c))));

    const tbody = h(
      "tbody",
      {},
      filtered.map((rel) => {
        const revNum = Number(rel.revision) || 0;
        const canDiffPrev = revNum > 1;
        return h("tr", {}, [
          h(
            "td",
            {},
            h(
              "button",
              {
                type: "button",
                className: "helm-release-link",
                onClick: () => openDetailsModal(rel.namespace, rel.name, revNum || null),
              },
              rel.name || "—"
            )
          ),
          h("td", {}, rel.namespace || "default"),
          h("td", { className: "cell-mono" }, rel.chart || "—"),
          h("td", {}, rel.app_version || "—"),
          h("td", {}, String(rel.revision ?? "—")),
          h("td", {}, renderHelmStatusPill(rel.status)),
          h("td", {}, renderResourcesCell(rel)),
          h("td", {}, formatAgeNode(rel.updated, timeAgo)),
          h(
            "td",
            {},
            h("div", { className: "helm-actions-row" }, [
              h(
                "button",
                {
                  type: "button",
                  className: "helm-action-btn",
                  onClick: () => openDetailsModal(rel.namespace, rel.name, revNum || null),
                },
                "Details"
              ),
              h(
                "button",
                {
                  type: "button",
                  className: "helm-action-btn",
                  onClick: () => navigateToHistory(rel.namespace, rel.name),
                },
                "History"
              ),
              h(
                "button",
                {
                  type: "button",
                  className: "helm-action-btn",
                  disabled: !canDiffPrev,
                  title: canDiffPrev
                    ? `Compare revision ${revNum - 1} -> ${revNum}`
                    : "Only 1 revision exists for this release",
                  onClick: () => {
                    if (canDiffPrev) navigateToDiff(rel.namespace, rel.name, revNum - 1, revNum, "values");
                  },
                },
                "Diff vs previous"
              ),
              h(
                "button",
                {
                  type: "button",
                  className: "helm-action-btn",
                  onClick: () => openResourceMapForNamespace(rel.namespace || "default"),
                },
                "View in Resource Map"
              ),
            ])
          ),
        ]);
      })
    );

    renderInto(
      panelHost,
      h("div", { className: "panel" }, h("div", { className: "table-wrap" }, h("table", { className: "data-table" }, [thead, tbody])))
    );
  }

  async function loadHelmView(force = false) {
    const container = $("helm-content");
    if (!container) return;
    const token = nextLoadToken("helm");
    const hasShell = Boolean(container.querySelector(".helm-toolbar"));
    const hasFreshCache =
      !force && helmState.releasesFetchedAt > 0 && Date.now() - helmState.releasesFetchedAt < HELM_CACHE_TTL_MS;
    if (!hasShell && !hasFreshCache) renderInto(container, loadingSkeleton());

    try {
      await ensureReleaseInventory(force);
      if (isStaleToken("helm", token)) return;
      const { nsSelect, searchInput } = ensureReleasesShell(container);
      populateReleasesNamespaceOptions(nsSelect);
      if (searchInput && searchInput.value !== helmState.filterSearch) {
        searchInput.value = helmState.filterSearch;
      }
      renderFilteredReleasesTable();
    } catch (err) {
      if (isStaleToken("helm", token)) return;
      renderInto(container, errorState(err, () => loadHelmView(true)));
    }
  }

  // -------------------------------------------------------------------------
  // 2. Helm Version History View
  // -------------------------------------------------------------------------

  async function renderHistoryTableForSelected(tableHost, force = false) {
    if (!tableHost) return;
    const selKey = resolveSelectedReleaseKey();
    if (!selKey) {
      renderInto(
        tableHost,
        h("div", { className: "panel" }, emptyState("No Helm releases found", "Install a Helm release to view revision history."))
      );
      return;
    }

    const token = nextLoadToken("helm-history-table");
    const { namespace, name } = splitReleaseKey(selKey);
    const cacheKey = `${getClusterId()}:${releaseKey(namespace, name)}`;
    const cached = !force ? helmState.historyCache.get(cacheKey) : null;
    if (!(cached && Date.now() - cached.ts < HELM_CACHE_TTL_MS)) {
      renderInto(tableHost, loadingSkeleton());
    }

    try {
      const data = await fetchReleaseHistory(namespace, name, force);
      if (isStaleToken("helm-history-table", token)) return;
      const items = Array.isArray(data.items) ? data.items : [];
      if (!items.length) {
        renderInto(
          tableHost,
          h("div", { className: "panel" }, emptyState("No revisions found", `Release ${namespace}/${name} has no recorded revisions.`))
        );
        return;
      }

      const currentRev = data.current_revision ?? items[0].revision;
      const columns = ["REVISION", "STATUS", "CHART", "APP VERSION", "UPDATED", "DESCRIPTION", "COMPARE"];
      const thead = h("thead", {}, h("tr", {}, columns.map((c) => h("th", {}, c))));

      const tbody = h(
        "tbody",
        {},
        items.map((rev, idx) => {
          const prevEntry = idx < items.length - 1 ? items[idx + 1] : null;
          const isCurrent = Boolean(rev.is_current || rev.revision === currentRev);
          const compareBtns = [];
          if (prevEntry) {
            compareBtns.push(
              h(
                "button",
                {
                  type: "button",
                  className: "helm-action-btn",
                  title: `Compare revision ${prevEntry.revision} -> ${rev.revision}`,
                  onClick: () => navigateToDiff(namespace, name, prevEntry.revision, rev.revision, "values"),
                },
                "vs previous"
              )
            );
          }
          if (!isCurrent && currentRev != null) {
            compareBtns.push(
              h(
                "button",
                {
                  type: "button",
                  className: "helm-action-btn",
                  title: `Compare revision ${rev.revision} -> current revision ${currentRev}`,
                  onClick: () => navigateToDiff(namespace, name, rev.revision, currentRev, "values"),
                },
                "vs current"
              )
            );
          }

          return h("tr", {}, [
            h(
              "td",
              {},
              h("div", { className: "helm-rev-cell" }, [
                h(
                  "button",
                  {
                    type: "button",
                    className: "helm-rev-link",
                    title: `Inspect revision ${rev.revision}`,
                    onClick: () => openDetailsModal(namespace, name, rev.revision),
                  },
                  String(rev.revision)
                ),
                isCurrent ? renderCurrentPill() : null,
              ].filter(Boolean))
            ),
            h("td", {}, renderHelmStatusPill(rev.status)),
            h("td", { className: "cell-mono" }, rev.chart || "—"),
            h("td", {}, rev.app_version || "—"),
            h("td", {}, formatAgeNode(rev.updated, timeAgo)),
            h("td", {}, rev.description || "—"),
            h("td", {}, compareBtns.length ? h("div", { className: "helm-actions-row" }, compareBtns) : "—"),
          ]);
        })
      );

      renderInto(
        tableHost,
        h("div", { className: "panel" }, h("div", { className: "table-wrap" }, h("table", { className: "data-table" }, [thead, tbody])))
      );
    } catch (err) {
      if (isStaleToken("helm-history-table", token)) return;
      renderInto(tableHost, errorState(err, () => renderHistoryTableForSelected(tableHost)));
    }
  }

  async function loadHelmHistoryView(force = false) {
    const container = $("helm-history-content");
    if (!container) return;
    const token = nextLoadToken("helm-history");
    const hasFreshCache =
      !force && helmState.releasesFetchedAt > 0 && Date.now() - helmState.releasesFetchedAt < HELM_CACHE_TTL_MS;
    if (!hasFreshCache) renderInto(container, loadingSkeleton());

    try {
      await ensureReleaseInventory(force);
      if (isStaleToken("helm-history", token)) return;

      if (!helmState.releasesAvailable) {
        renderInto(container, emptyState("Helm not available", "The `helm` CLI is not installed on the server running KubeOrbit."));
        return;
      }

      const selectedKey = resolveSelectedReleaseKey();
      const releaseSelect = h(
        "select",
        {
          id: "helm-history-release-select",
          className: "select helm-release-select",
          "aria-label": "Select Helm release",
          onChange: (e) => {
            helmState.selectedReleaseKey = e.target.value;
            renderHistoryTableForSelected(tableHost, false);
          },
        },
        helmState.releases.length
          ? helmState.releases.map((r) => {
              const key = releaseKey(r.namespace, r.name);
              return h("option", { value: key, selected: key === selectedKey }, `${r.namespace} / ${r.name}`);
            })
          : [h("option", { value: "" }, "No releases")]
      );
      if (selectedKey) releaseSelect.value = selectedKey;

      const toolbar = h("div", { className: "helm-toolbar" }, [
        h("div", { className: "helm-field" }, [
          h("label", { className: "helm-field__label", for: "helm-history-release-select" }, "Release"),
          releaseSelect,
        ]),
      ]);

      const tableHost = h("div", { id: "helm-history-table-host" });
      renderInto(container, h("div", { className: "helm-page-stack" }, [toolbar, tableHost]));
      await renderHistoryTableForSelected(tableHost, force);
    } catch (err) {
      if (isStaleToken("helm-history", token)) return;
      renderInto(container, errorState(err, () => loadHelmHistoryView(true)));
    }
  }

  // -------------------------------------------------------------------------
  // 3. Helm Diff Viewer
  // -------------------------------------------------------------------------

  function formatRevisionOptionLabel(rev) {
    return `Rev ${rev.revision} · ${rev.chart || "chart"} · ${rev.status || "unknown"}`;
  }

  async function runDiffComparison(outputHost) {
    const selKey = resolveSelectedReleaseKey();
    if (!selKey || !helmState.diffFromRev || !helmState.diffToRev) {
      renderInto(
        outputHost,
        h("div", { className: "helm-diff-card" }, emptyState("Select revisions to compare", "Choose a release and two revisions above."))
      );
      return;
    }

    const token = nextLoadToken("helm-diff-run");
    renderInto(outputHost, loadingSkeleton(6));

    try {
      const { namespace, name } = splitReleaseKey(selKey);
      const data = await api.diffHelmRevisions(
        getClusterId(),
        namespace,
        name,
        helmState.diffFromRev,
        helmState.diffToRev,
        helmState.diffMode || "values"
      );
      if (isStaleToken("helm-diff-run", token)) return;
      helmState.diffData = data;

      const summaryPills = [];
      if (data.has_changes) {
        if (data.added_lines > 0) {
          summaryPills.push(h("span", { className: "helm-diff-stat helm-diff-stat--add" }, `+${data.added_lines}`));
        }
        if (data.removed_lines > 0) {
          summaryPills.push(h("span", { className: "helm-diff-stat helm-diff-stat--remove" }, `-${data.removed_lines}`));
        }
      } else {
        summaryPills.push(h("span", { className: "helm-diff-stat helm-diff-stat--same" }, "No changes"));
      }

      const card = h("div", { className: "helm-diff-card" }, [
        h("div", { className: "helm-diff-card__header" }, [
          h("span", { className: "helm-diff-card__title" }, `Revision ${data.from_revision} → Revision ${data.to_revision}`),
          h("div", { className: "helm-diff-card__stats" }, summaryPills),
        ]),
        renderDiffBlocksTable(data.blocks),
      ]);

      renderInto(outputHost, card);
    } catch (err) {
      if (isStaleToken("helm-diff-run", token)) return;
      renderInto(outputHost, errorState(err, () => runDiffComparison(outputHost)));
    }
  }

  function populateDiffRevisionSelects(fromSelect, toSelect, revs) {
    fromSelect.replaceChildren(...revs.map((r) => h("option", { value: String(r.revision) }, formatRevisionOptionLabel(r))));
    toSelect.replaceChildren(...revs.map((r) => h("option", { value: String(r.revision) }, formatRevisionOptionLabel(r))));

    const validNums = new Set(revs.map((r) => Number(r.revision)));
    if (!validNums.has(Number(helmState.diffToRev))) {
      helmState.diffToRev = revs[0]?.revision ?? null;
    }
    if (!validNums.has(Number(helmState.diffFromRev))) {
      const toIdx = revs.findIndex((r) => Number(r.revision) === Number(helmState.diffToRev));
      if (toIdx >= 0 && toIdx + 1 < revs.length) {
        helmState.diffFromRev = revs[toIdx + 1].revision;
      } else {
        helmState.diffFromRev = revs[revs.length - 1]?.revision ?? helmState.diffToRev;
      }
    }

    if (helmState.diffFromRev != null) fromSelect.value = String(helmState.diffFromRev);
    if (helmState.diffToRev != null) toSelect.value = String(helmState.diffToRev);
  }

  async function loadHelmDiffView(force = false) {
    const container = $("helm-diff-content");
    if (!container) return;
    const token = nextLoadToken("helm-diff");
    const hasFreshCache =
      !force && helmState.releasesFetchedAt > 0 && Date.now() - helmState.releasesFetchedAt < HELM_CACHE_TTL_MS;
    if (!hasFreshCache) renderInto(container, loadingSkeleton());

    try {
      await ensureReleaseInventory(force);
      if (isStaleToken("helm-diff", token)) return;

      if (!helmState.releasesAvailable) {
        renderInto(container, emptyState("Helm not available", "The `helm` CLI is not installed on the server running KubeOrbit."));
        return;
      }
      if (!helmState.releases.length) {
        renderInto(container, emptyState("No Helm releases found", "Install a Helm release to diff revisions."));
        return;
      }

      const selectedKey = resolveSelectedReleaseKey();
      const { namespace, name } = splitReleaseKey(selectedKey);
      const historyData = await fetchReleaseHistory(namespace, name, force);
      if (isStaleToken("helm-diff", token)) return;

      let revs = Array.isArray(historyData.items) ? historyData.items : [];

      const outputHost = h("div", { id: "helm-diff-output-host" });
      const fromSelect = h("select", {
        id: "helm-diff-from-select",
        className: "select helm-diff-rev-select",
        "aria-label": "From revision",
        onChange: (e) => {
          helmState.diffFromRev = Number(e.target.value);
        },
      });
      const toSelect = h("select", {
        id: "helm-diff-to-select",
        className: "select helm-diff-rev-select",
        "aria-label": "To revision",
        onChange: (e) => {
          helmState.diffToRev = Number(e.target.value);
        },
      });
      const modeSelect = h(
        "select",
        {
          id: "helm-diff-mode-select",
          className: "select helm-diff-mode-select",
          "aria-label": "Compare mode",
          onChange: (e) => {
            helmState.diffMode = e.target.value;
            runDiffComparison(outputHost);
          },
        },
        [
          h("option", { value: "values" }, "Values (user-supplied)"),
          h("option", { value: "all_values" }, "Values (computed)"),
          h("option", { value: "manifest" }, "Manifest (rendered YAML)"),
        ]
      );
      modeSelect.value = helmState.diffMode || "values";

      const releaseSelect = h(
        "select",
        {
          id: "helm-diff-release-select",
          className: "select helm-release-select",
          "aria-label": "Select Helm release",
          onChange: async (e) => {
            helmState.selectedReleaseKey = e.target.value;
            helmState.diffFromRev = null;
            helmState.diffToRev = null;
            const nextParts = splitReleaseKey(e.target.value);
            try {
              renderInto(outputHost, loadingSkeleton(5));
              const nextHist = await fetchReleaseHistory(nextParts.namespace, nextParts.name, false);
              revs = Array.isArray(nextHist.items) ? nextHist.items : [];
              populateDiffRevisionSelects(fromSelect, toSelect, revs);
              await runDiffComparison(outputHost);
            } catch (err) {
              renderInto(outputHost, errorState(err, () => loadHelmDiffView(true)));
            }
          },
        },
        helmState.releases.map((r) => {
          const key = releaseKey(r.namespace, r.name);
          return h("option", { value: key }, `${r.namespace} / ${r.name}`);
        })
      );
      releaseSelect.value = selectedKey;

      populateDiffRevisionSelects(fromSelect, toSelect, revs);

      const compareBtn = h(
        "button",
        {
          id: "helm-diff-compare-btn",
          type: "button",
          className: "btn btn--primary helm-compare-btn",
          onClick: () => runDiffComparison(outputHost),
        },
        "Compare"
      );

      const controlsBar = h("div", { className: "helm-diff-controls" }, [
        h("div", { className: "helm-field" }, [
          h("label", { className: "helm-field__label", for: "helm-diff-release-select" }, "Release"),
          releaseSelect,
        ]),
        h("div", { className: "helm-field" }, [
          h("label", { className: "helm-field__label", for: "helm-diff-from-select" }, "From revision"),
          fromSelect,
        ]),
        h("div", { className: "helm-field" }, [
          h("label", { className: "helm-field__label", for: "helm-diff-to-select" }, "To revision"),
          toSelect,
        ]),
        h("div", { className: "helm-field" }, [
          h("label", { className: "helm-field__label", for: "helm-diff-mode-select" }, "Compare"),
          modeSelect,
        ]),
        h("div", { className: "helm-field helm-field--btn" }, [compareBtn]),
      ]);

      const notice = h("p", { className: "helm-mask-notice" }, DEFAULT_MASK_NOTICE);
      renderInto(container, h("div", { className: "helm-page-stack" }, [controlsBar, notice, outputHost]));
      await runDiffComparison(outputHost);
    } catch (err) {
      if (isStaleToken("helm-diff", token)) return;
      renderInto(container, errorState(err, loadHelmDiffView));
    }
  }

  // -------------------------------------------------------------------------
  // 4. Helm Release Details Modal
  // -------------------------------------------------------------------------

  function closeDetailsModal() {
    const modal = $("helm-details-modal");
    if (modal) modal.hidden = true;
  }

  function renderDetailsBodyContent(data) {
    const tabs = [
      { id: "overview", label: "Overview & Resources" },
      { id: "user_values", label: "Values (User-Supplied)" },
      { id: "computed_values", label: "Values (Computed)" },
      { id: "manifest", label: "Manifest (YAML)" },
    ];

    const tabContentHost = h("div", { className: "helm-details-tabpanel" });

    const renderTabContent = (tabId) => {
      helmState.detailsTab = tabId;
      if (tabId === "overview") {
        const metaRows = [
          ["Release", data.name],
          ["Namespace", data.namespace],
          ["Revision", data.revision],
          ["Chart", data.chart || "—"],
          ["App Version", data.app_version || "—"],
          ["Status", renderHelmStatusPill(data.status)],
          ["Updated", data.updated ? `${timeAgo(data.updated)} (${data.updated})` : "—"],
          ["Description", data.description || "—"],
        ];
        const metaGrid = h(
          "div",
          { className: "helm-meta-grid" },
          metaRows.map(([k, v]) =>
            h("div", { className: "helm-meta-item" }, [
              h("span", { className: "helm-meta-item__key" }, k),
              h("span", { className: "helm-meta-item__val" }, typeof v === "string" || typeof v === "number" ? String(v) : v),
            ])
          )
        );

        const resources = Array.isArray(data.resources) ? data.resources : [];
        const resList = h("div", { className: "helm-details-section" }, [
          h("h4", { className: "helm-details-heading" }, `Rendered Resources (${resources.length})`),
          resources.length
            ? h(
                "div",
                { className: "helm-details-res-list" },
                resources.map((res) =>
                  h("div", { className: "helm-details-res-row" }, [
                    h("div", { className: "helm-details-res-row__left" }, [
                      h("span", { className: `helm-res-sq helm-res-sq--${res.status || "unknown"}` }),
                      h("span", { className: "helm-kind-badge" }, res.kind),
                      h("span", { className: "cell-mono" }, `${res.namespace || data.namespace}/${res.name}`),
                    ]),
                    h("div", { className: "helm-actions-row" }, [
                      String(res.kind || "").toLowerCase() === "pod"
                        ? h(
                            "button",
                            {
                              type: "button",
                              className: "helm-action-btn",
                              onClick: () => openPodInspectorModal(res.name, res.namespace || data.namespace),
                            },
                            "Inspect"
                          )
                        : null,
                      isSecretKind(res.kind)
                        ? h(
                            "span",
                            {
                              className: "helm-status-pill helm-status-pill--superseded",
                              title: "Kubernetes Secret values and YAML are disabled for security reasons",
                            },
                            "Masked"
                          )
                        : h(
                            "button",
                            {
                              type: "button",
                              className: "helm-action-btn",
                              onClick: () => openManifestModal(res.kind, res.name, res.namespace || data.namespace),
                            },
                            "YAML"
                          ),
                    ].filter(Boolean)),
                  ])
                )
              )
            : h("div", { className: "cell-muted" }, "No Kubernetes manifests rendered in this revision."),
        ]);

        const notesSection = data.notes?.trim()
          ? h("div", { className: "helm-details-section" }, [
              h("h4", { className: "helm-details-heading" }, "Release Notes"),
              h("pre", { className: "helm-code-block" }, data.notes),
            ])
          : null;

        renderInto(tabContentHost, h("div", { className: "helm-details-stack" }, [metaGrid, resList, notesSection].filter(Boolean)));
        return;
      }

      const yamlByTab = {
        user_values: data.user_values_yaml || "",
        computed_values: data.computed_values_yaml || "",
        manifest: data.manifest_yaml || "",
      };
      const emptyByTab = {
        user_values: "# No user-supplied values for this revision.",
        computed_values: "# No computed values available.",
        manifest: "# Empty manifest.",
      };
      const text = yamlByTab[tabId]?.trim() ? yamlByTab[tabId] : emptyByTab[tabId];
      const copyBtn = h(
        "button",
        {
          type: "button",
          className: "helm-action-btn",
          onClick: async () => {
            try {
              await navigator.clipboard?.writeText(text);
              copyBtn.textContent = "Copied";
              setTimeout(() => {
                copyBtn.textContent = "Copy YAML";
              }, 1400);
            } catch (_) {
              copyBtn.textContent = "Copy failed";
            }
          },
        },
        "Copy YAML"
      );

      renderInto(
        tabContentHost,
        h("div", { className: "helm-details-stack" }, [
          h("div", { className: "helm-code-toolbar" }, [
            h("span", { className: "helm-mask-notice" }, data.mask_notice || DEFAULT_MASK_NOTICE),
            copyBtn,
          ]),
          h("pre", { className: "helm-code-block helm-code-block--yaml" }, text),
        ])
      );
    };

    const tabBar = h(
      "div",
      { className: "helm-details-tabs", role: "tablist" },
      tabs.map((t) => {
        const btn = h(
          "button",
          {
            type: "button",
            role: "tab",
            className: `helm-details-tab${helmState.detailsTab === t.id ? " is-active" : ""}`,
            "aria-selected": String(helmState.detailsTab === t.id),
            onClick: () => {
              tabBar.querySelectorAll(".helm-details-tab").forEach((el) => {
                el.classList.remove("is-active");
                el.setAttribute("aria-selected", "false");
              });
              btn.classList.add("is-active");
              btn.setAttribute("aria-selected", "true");
              renderTabContent(t.id);
            },
          },
          t.label
        );
        return btn;
      })
    );

    renderTabContent(tabs.some((t) => t.id === helmState.detailsTab) ? helmState.detailsTab : "overview");
    return h("div", { className: "helm-details-shell" }, [tabBar, tabContentHost]);
  }

  async function openDetailsModal(namespace, releaseName, revision = null) {
    const modal = $("helm-details-modal");
    const titleEl = $("helm-details-modal-title");
    const subtitleEl = $("helm-details-modal-subtitle");
    const headerActions = $("helm-details-header-actions");
    const bodyEl = $("helm-details-modal-body");
    if (!modal || !bodyEl) return;

    helmState.detailsTab = "overview";
    modal.hidden = false;
    if (titleEl) titleEl.textContent = `${releaseName} (${namespace})`;
    if (subtitleEl) subtitleEl.textContent = revision ? `Revision ${revision} · Loading details...` : "Loading release details...";
    if (headerActions) headerActions.replaceChildren();
    renderInto(bodyEl, loadingSkeleton(6));

    try {
      const data = await api.getHelmReleaseDetails(getClusterId(), namespace, releaseName, revision);
      if (subtitleEl) {
        subtitleEl.textContent = [
          data.chart || null,
          data.revision != null ? `Revision ${data.revision}` : null,
          data.status || null,
        ]
          .filter(Boolean)
          .join(" · ");
      }
      if (headerActions) {
        const revNum = Number(data.revision) || 0;
        const btns = [
          h(
            "button",
            {
              type: "button",
              className: "helm-action-btn",
              onClick: () => {
                closeDetailsModal();
                navigateToHistory(namespace, releaseName);
              },
            },
            "History"
          ),
          h(
            "button",
            {
              type: "button",
              className: "helm-action-btn",
              disabled: revNum <= 1,
              onClick: () => {
                closeDetailsModal();
                navigateToDiff(namespace, releaseName, revNum - 1, revNum, "values");
              },
            },
            "Diff vs previous"
          ),
          h(
            "button",
            {
              type: "button",
              className: "helm-action-btn",
              onClick: () => {
                closeDetailsModal();
                openResourceMapForNamespace(namespace || "default");
              },
            },
            "View in Resource Map"
          ),
        ];
        headerActions.replaceChildren(...btns);
      }
      renderInto(bodyEl, renderDetailsBodyContent(data));
    } catch (err) {
      if (subtitleEl) subtitleEl.textContent = "Failed to load Helm release details";
      renderInto(bodyEl, errorState(err, () => openDetailsModal(namespace, releaseName, revision)));
    }
  }

  function wireHelmStaticControls() {
    $("helm-details-modal-close")?.addEventListener("click", closeDetailsModal);
    $("helm-details-modal")?.addEventListener("click", (e) => {
      if (e.target.id === "helm-details-modal") closeDetailsModal();
    });
  }

  return {
    loadHelmView,
    loadHelmHistoryView,
    loadHelmDiffView,
    openDetailsModal,
    closeDetailsModal,
    navigateToHistory,
    navigateToDiff,
    resetHelmState,
    wireHelmStaticControls,
  };
}
