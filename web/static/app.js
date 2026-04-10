/* RayXI — Graph UI
 *
 * Cytoscape.js renders a unified node+edge graph for all five Semantic Stack
 * layers. The filter panel controls visibility per layer and per item.
 */

"use strict";

// ── Layer config (colours must match CSS vars) ─────────────────────────
const LAYERS = {
  "1": { label: "AST Site Map",       color: "#4ade80" },
  "2": { label: "Intent / Dual-Naming", color: "#60a5fa" },
  "3": { label: "FSM States",         color: "#fb923c" },
  "4": { label: "Race Map",           color: "#f87171" },
  "5": { label: "Simulation",         color: "#c084fc" },
};

// ── Cytoscape style sheet ──────────────────────────────────────────────
const CY_STYLE = [
  {
    selector: "node",
    style: {
      "label": "data(label)",
      "font-size": "11px",
      "font-family": '"JetBrains Mono", "Fira Code", monospace',
      "color": "#e2e8f0",
      "text-valign": "center",
      "text-halign": "center",
      "text-wrap": "wrap",
      "text-max-width": "110px",
      "width": 130,
      "height": 44,
      "border-width": 2,
      "border-color": "#2d3148",
      "background-color": "#1a1d27",
    },
  },
  // ── Layer 1: function nodes
  {
    selector: ".layer-1",
    style: {
      "shape": "round-rectangle",
      "background-color": "#0d2619",
      "border-color": "#4ade80",
    },
  },
  // ── Layer 2: intent overlay (adds sublabel + conflict border)
  {
    selector: ".layer-2",
    style: {
      "label": "data(label)\ndata(sublabel)",
      "font-size": "10px",
    },
  },
  {
    selector: ".conflict",
    style: {
      "border-color": "#ef4444",
      "border-width": 4,
      "background-color": "#2a0a0a",
    },
  },
  // ── Layer 3: state nodes
  {
    selector: ".layer-3",
    style: {
      "shape": "ellipse",
      "background-color": "#1e1008",
      "border-color": "#fb923c",
      "width": 150,
      "height": 58,
    },
  },
  {
    selector: ".terminal",
    style: {
      "background-color": "#0a1628",
      "border-color": "#60a5fa",
      "border-style": "double",
      "border-width": 4,
    },
  },
  // ── Layer 5: stuck state
  {
    selector: ".stuck",
    style: {
      "background-color": "#1e0b2e",
      "border-color": "#c084fc",
      "border-width": 5,
      "border-style": "dashed",
    },
  },
  // ── Edges base
  {
    selector: "edge",
    style: {
      "curve-style": "bezier",
      "target-arrow-shape": "triangle",
      "arrow-scale": 1.1,
      "font-size": "9px",
      "color": "#8892a4",
      "text-rotation": "autorotate",
      "text-margin-y": -8,
    },
  },
  // ── Transition edges (Layer 3)
  {
    selector: ".transition",
    style: {
      "line-color": "#7a4010",
      "target-arrow-color": "#7a4010",
      "width": 2,
    },
  },
  // ── Race edges (Layer 4)
  {
    selector: ".layer-4",
    style: {
      "line-color": "#7a2020",
      "target-arrow-color": "#7a2020",
      "line-style": "dashed",
      "label": "data(label)",
      "width": 2,
    },
  },
  {
    selector: ".risk-high",
    style: { "line-color": "#f87171", "target-arrow-color": "#f87171", "width": 3 },
  },
  // ── Simulation drop edges (Layer 5)
  {
    selector: ".sim-drop",
    style: {
      "line-color": "#6b3fa0",
      "target-arrow-color": "#6b3fa0",
      "line-style": "dotted",
      "curve-style": "loop",
      "loop-direction": "45deg",
      "loop-sweep": "-45deg",
      "label": "dropped",
      "width": 2,
    },
  },
  // ── Hidden
  {
    selector: ".hidden",
    style: { "display": "none" },
  },
  // ── Selected
  {
    selector: ":selected",
    style: {
      "overlay-color": "#ffffff",
      "overlay-opacity": 0.12,
      "overlay-padding": 4,
    },
  },
];

// ── State ──────────────────────────────────────────────────────────────
let cy = null;
let graphData = null;

// layerEnabled[n]  → bool (master toggle)
// itemVisible[id]  → bool (per-node/edge)
const layerEnabled = { "1": true, "2": true, "3": true, "4": true, "5": true };
const itemVisible = {};

// ── Boot ───────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  await loadSchemas();
  buildFilterSkeleton();
  attachTopbarListeners();

  // Auto-run with defaults on first load if source box is empty
  // (gives user the FSM graph immediately)
  await runGraph();
});

// ── Schema list ────────────────────────────────────────────────────────
async function loadSchemas() {
  try {
    const res = await fetch("/api/schemas");
    const data = await res.json();
    const sel = document.getElementById("schema-select");
    sel.innerHTML = data.schemas.map(s =>
      `<option value="${s}">${s}</option>`
    ).join("");
  } catch (_) {}
}

// ── Filter panel skeleton ──────────────────────────────────────────────
function buildFilterSkeleton() {
  const container = document.getElementById("filter-layers");
  container.innerHTML = Object.entries(LAYERS).map(([num, cfg]) => `
    <div class="layer-section expanded" data-layer="${num}" style="--dot-color:${cfg.color}">
      <div class="layer-header" onclick="toggleExpand(this.parentElement)">
        <input type="checkbox" class="layer-toggle-master"
               checked
               onchange="onLayerMasterToggle('${num}', this.checked)"
               onclick="event.stopPropagation()"
               title="Toggle entire layer">
        <span class="layer-dot" style="background:${cfg.color}"></span>
        <span class="layer-title">${cfg.label}</span>
        <span class="expand-icon">▾</span>
      </div>
      <div class="layer-items" id="layer-items-${num}"></div>
    </div>
  `).join("");
}

function toggleExpand(section) {
  section.classList.toggle("expanded");
}

// ── Run graph ──────────────────────────────────────────────────────────
async function runGraph() {
  const schema_id       = document.getElementById("schema-select").value;
  const source          = document.getElementById("source-input").value;
  const drop_probability = parseFloat(document.getElementById("drop-prob").value);
  const seed_raw        = document.getElementById("seed-input").value.trim();
  const initial_state_raw = document.getElementById("initial-state").value.trim();

  const body = {
    source,
    schema_id,
    interpret: true,
    model: "glm",
    drop_probability: isNaN(drop_probability) ? 0.3 : drop_probability,
    seed: seed_raw === "" ? null : parseInt(seed_raw, 10),
    initial_state: initial_state_raw === "" ? null : initial_state_raw,
    max_steps: 100,
  };

  showLoading(true);
  setRunBtn(false);

  try {
    const res = await fetch("/api/graph", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || res.statusText);
    }
    graphData = await res.json();
    renderGraph(graphData);
    populateFilterItems(graphData.filter_options);
    updateStatusBar(graphData.meta);
  } catch (err) {
    showError(err.message);
  } finally {
    showLoading(false);
    setRunBtn(true);
  }
}

// ── Graph rendering ────────────────────────────────────────────────────
function renderGraph(data) {
  const elements = [
    ...data.nodes.map(n => ({
      data:     { id: n.id, label: n.label, sublabel: n.sublabel, ...n.data },
      position: n.position,
      classes:  n.classes.join(" "),
    })),
    ...data.edges.map(e => ({
      data:    { id: e.id, source: e.source, target: e.target, label: e.label, ...e.data },
      classes: e.classes.join(" "),
    })),
  ];

  if (cy) cy.destroy();

  cy = cytoscape({
    container: document.getElementById("cy"),
    elements,
    style: CY_STYLE,
    layout: { name: "preset", animate: false },
    minZoom: 0.15,
    maxZoom: 3.5,
  });

  // Fit with padding after render
  cy.ready(() => cy.fit(undefined, 60));

  // Node click → detail panel
  cy.on("tap", "node", e => showDetail(e.target));
  cy.on("tap", "edge", e => showDetail(e.target));
  cy.on("tap", e => { if (e.target === cy) clearDetail(); });

  // Sync item checkboxes to current state
  applyAllVisibility();
}

// ── Filter panel population ────────────────────────────────────────────
function populateFilterItems(filterOptions) {
  Object.entries(filterOptions).forEach(([num, cfg]) => {
    const container = document.getElementById(`layer-items-${num}`);
    if (!container) return;

    container.innerHTML = cfg.items.map(item => {
      // Default all items visible
      if (!(item.id in itemVisible)) itemVisible[item.id] = true;

      return `
        <div class="item-row">
          <input type="checkbox"
                 ${itemVisible[item.id] ? "checked" : ""}
                 onchange="onItemToggle('${item.id}', this.checked)"
                 title="${item.id}">
          <span class="item-label">${item.label}</span>
          ${item.sublabel ? `<span class="item-sublabel">→ ${item.sublabel}</span>` : ""}
        </div>
      `;
    }).join("") || '<span style="color:var(--muted);font-size:11px">—</span>';
  });
}

// ── Visibility logic ───────────────────────────────────────────────────
function onLayerMasterToggle(layerNum, enabled) {
  layerEnabled[layerNum] = enabled;
  applyAllVisibility();
}

function onItemToggle(itemId, visible) {
  itemVisible[itemId] = visible;
  applyAllVisibility();
}

function applyAllVisibility() {
  if (!cy || !graphData) return;

  // Build sets of which element IDs are visible through each layer
  // An element is visible if ALL its owning layers are enabled AND
  // its own item checkbox is checked.
  graphData.nodes.forEach(n => {
    const layersOn = n.layers.some(l => layerEnabled[String(l)]);
    const itemOn   = itemVisible[n.id] !== false;
    const show     = layersOn && itemOn;
    const el       = cy.$(`#${CSS.escape(n.id)}`);
    if (show) el.removeClass("hidden");
    else      el.addClass("hidden");
  });

  graphData.edges.forEach(e => {
    const layerOn = layerEnabled[String(e.layer)];
    // Also hide edge if either endpoint is hidden
    const srcHidden = cy.$(`#${CSS.escape(e.source)}`).hasClass("hidden");
    const tgtHidden = cy.$(`#${CSS.escape(e.target)}`).hasClass("hidden");
    // Self-loops (sim-drop) only need the source check
    const show = layerOn && !srcHidden && (e.source === e.target || !tgtHidden);
    const el   = cy.$(`#${CSS.escape(e.id)}`);
    if (show) el.removeClass("hidden");
    else      el.addClass("hidden");
  });
}

// ── Detail panel ───────────────────────────────────────────────────────
function showDetail(el) {
  const panel = document.getElementById("detail");
  document.getElementById("detail-empty").style.display = "none";

  const d = el.data();
  const isNode = el.isNode();

  document.getElementById("detail-title").textContent =
    isNode ? d.label : `${d.source} → ${d.target}`;

  // Build rows from data fields, skip internal ones
  const skip = new Set(["id", "source", "target"]);
  const rows = Object.entries(d)
    .filter(([k]) => !skip.has(k) && d[k] !== "" && d[k] !== null && d[k] !== undefined)
    .map(([k, v]) => `
      <div class="detail-row">
        <span class="detail-key">${k}</span>
        <span class="detail-val">${formatVal(v)}</span>
      </div>
    `).join("");

  // Layer badges
  const layers = isNode
    ? (graphData?.nodes.find(n => n.id === el.id())?.layers || [])
    : [graphData?.edges.find(e => e.id === el.id())?.layer].filter(Boolean);

  const badgeClass = { "1":"green","2":"blue","3":"orange","4":"red","5":"purple" };
  const badges = layers.map(l =>
    `<span class="badge badge-${badgeClass[l] || 'green'}">${LAYERS[l]?.label || "L"+l}</span>`
  ).join(" ");

  document.getElementById("detail-body").innerHTML = `
    <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px">${badges}</div>
    ${rows}
  `;
}

function formatVal(v) {
  if (typeof v === "boolean") return v
    ? '<span style="color:var(--l4)">true</span>'
    : '<span style="color:var(--l1)">false</span>';
  if (Array.isArray(v)) return v.join(", ") || "—";
  return String(v);
}

function clearDetail() {
  document.getElementById("detail-title").textContent = "Select a node";
  document.getElementById("detail-body").innerHTML = "";
  document.getElementById("detail-empty").style.display = "";
}

// ── Status bar ─────────────────────────────────────────────────────────
function updateStatusBar(meta) {
  setText("stat-functions", meta.function_count);
  setText("stat-states",    meta.state_count);
  setText("stat-races",     meta.race_count);

  const dlEl = document.getElementById("stat-deadlock");
  const dlVal = dlEl.querySelector(".stat-val");
  if (meta.deadlock_detected) {
    dlVal.textContent = "DEADLOCK";
    dlEl.classList.remove("ok");
  } else {
    dlVal.textContent = "OK";
    dlEl.classList.add("ok");
  }

  setText("stat-schema", meta.schema_id);
  setText("stat-drop",   (meta.drop_probability * 100).toFixed(0) + "%");
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ── Canvas controls ────────────────────────────────────────────────────
function fitGraph()   { cy?.fit(undefined, 60); }
function resetLayout() {
  if (!cy) return;
  cy.layout({ name: "preset", animate: true, animationDuration: 350 }).run();
  cy.fit(undefined, 60);
}

// ── Topbar listeners ───────────────────────────────────────────────────
function attachTopbarListeners() {
  document.getElementById("btn-run").addEventListener("click", runGraph);
  document.getElementById("schema-select").addEventListener("change", runGraph);
}

// ── UI helpers ─────────────────────────────────────────────────────────
function showLoading(on) {
  document.getElementById("loading-overlay").classList.toggle("hidden", !on);
}

function setRunBtn(enabled) {
  document.getElementById("btn-run").disabled = !enabled;
}

function showError(msg) {
  const overlay = document.getElementById("loading-overlay");
  overlay.classList.remove("hidden");
  overlay.innerHTML = `
    <span style="color:var(--l4);font-size:13px">Error</span>
    <span style="max-width:320px;text-align:center;color:var(--muted)">${msg}</span>
    <button class="canvas-btn" onclick="document.getElementById('loading-overlay').classList.add('hidden')">Dismiss</button>
  `;
}
