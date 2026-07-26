/*
AI GRAPH HIGHLIGHTER — compiled bundle.
Source of truth is main.ts in this same folder; rebuild with "npm run build".
*/

var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// main.ts
var main_exports = {};
__export(main_exports, {
  default: () => AIGraphHighlighterPlugin
});
module.exports = __toCommonJS(main_exports);
var import_obsidian = require("obsidian");
var MAX_NODES = 400;
var MAX_EDGES = 800;
function stripBom(s) {
  return s.charCodeAt(0) === 65279 ? s.slice(1) : s;
}
function validatePendingHighlight(raw) {
  const errors = [];
  const warnings = [];
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return { spec: null, errors: ["Top level must be a JSON object."], warnings };
  }
  const obj = raw;
  let nodes = [];
  if (Array.isArray(obj.nodes)) {
    for (const n of obj.nodes) {
      if (typeof n === "string" && n.trim().length > 0) nodes.push(n.trim());
      else warnings.push(`Ignored non-string entry in "nodes": ${JSON.stringify(n)}`);
    }
  } else if (obj.nodes !== void 0) {
    warnings.push(`"nodes" should be an array of strings.`);
  }
  const edges = [];
  if (Array.isArray(obj.edges)) {
    for (const e of obj.edges) {
      if (e === null || typeof e !== "object") {
        warnings.push(`Ignored non-object entry in "edges".`);
        continue;
      }
      const ed = e;
      const from = typeof ed.from === "string" ? ed.from.trim() : "";
      const to = typeof ed.to === "string" ? ed.to.trim() : "";
      if (!from || !to) {
        warnings.push(`Ignored edge with missing "from"/"to": ${JSON.stringify(e).slice(0, 120)}`);
        continue;
      }
      let strength;
      if (typeof ed.strength === "number" && isFinite(ed.strength)) {
        strength = Math.max(0, Math.min(1, ed.strength));
        if (strength !== ed.strength)
          warnings.push(`Edge "${from}" \u2192 "${to}": strength ${ed.strength} clamped to ${strength}.`);
      } else {
        strength = 0.5;
        warnings.push(`Edge "${from}" \u2192 "${to}": missing/invalid "strength", defaulting to 0.5.`);
      }
      let label;
      if (typeof ed.label === "string" && ed.label.trim().length > 0) {
        label = ed.label.trim();
        if (label.length > 300) label = label.slice(0, 297) + "\u2026";
      }
      edges.push({ from, to, strength, label });
    }
  } else if (obj.edges !== void 0) {
    warnings.push(`"edges" should be an array of objects.`);
  }
  const seen = new Set(nodes);
  for (const e of edges) {
    for (const name of [e.from, e.to]) {
      if (!seen.has(name)) {
        seen.add(name);
        nodes.push(name);
      }
    }
  }
  if (nodes.length === 0) {
    errors.push(`Nothing to highlight \u2014 provide "nodes" (string array) and/or "edges".`);
    return { spec: null, errors, warnings };
  }
  if (nodes.length > MAX_NODES) {
    warnings.push(`${nodes.length} nodes requested; using the first ${MAX_NODES}.`);
    nodes = nodes.slice(0, MAX_NODES);
  }
  if (edges.length > MAX_EDGES) {
    warnings.push(`${edges.length} edges requested; using the first ${MAX_EDGES}.`);
    edges.length = MAX_EDGES;
  }
  const center = typeof obj.center === "string" && obj.center.trim() ? obj.center.trim() : void 0;
  const title = typeof obj.title === "string" && obj.title.trim() ? obj.title.trim().slice(0, 200) : void 0;
  return { spec: { nodes, edges, center, title }, errors, warnings };
}
function parseCssColor(input) {
  const s = input.trim();
  let m = /^#([0-9a-f]{3})$/i.exec(s);
  if (m) {
    const h = m[1];
    return {
      r: parseInt(h[0] + h[0], 16),
      g: parseInt(h[1] + h[1], 16),
      b: parseInt(h[2] + h[2], 16)
    };
  }
  m = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(s);
  if (m) {
    const h = m[1];
    return {
      r: parseInt(h.slice(0, 2), 16),
      g: parseInt(h.slice(2, 4), 16),
      b: parseInt(h.slice(4, 6), 16)
    };
  }
  m = /^rgba?\(\s*([\d.]+)\s*[, ]\s*([\d.]+)\s*[, ]\s*([\d.]+)/i.exec(s);
  if (m) {
    return { r: +m[1], g: +m[2], b: +m[3] };
  }
  return null;
}
function rgbToInt(c) {
  return Math.round(c.r) << 16 | Math.round(c.g) << 8 | Math.round(c.b);
}
function rgbToHsl(c) {
  const r = c.r / 255, g = c.g / 255, b = c.b / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return { h: 0, s: 0, l };
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h;
  if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
  else if (max === g) h = ((b - r) / d + 2) / 6;
  else h = ((r - g) / d + 4) / 6;
  return { h, s, l };
}
function hslToRgb(h, s, l) {
  const hue2rgb = (p2, q2, t) => {
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p2 + (q2 - p2) * 6 * t;
    if (t < 1 / 2) return q2;
    if (t < 2 / 3) return p2 + (q2 - p2) * (2 / 3 - t) * 6;
    return p2;
  };
  if (s === 0) {
    const v = Math.round(l * 255);
    return { r: v, g: v, b: v };
  }
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  return {
    r: Math.round(hue2rgb(p, q, h + 1 / 3) * 255),
    g: Math.round(hue2rgb(p, q, h) * 255),
    b: Math.round(hue2rgb(p, q, h - 1 / 3) * 255)
  };
}
function mixRgb(a, b, t) {
  return {
    r: a.r + (b.r - a.r) * t,
    g: a.g + (b.g - a.g) * t,
    b: a.b + (b.b - a.b) * t
  };
}
function lighten(c, amt) {
  const { h, s, l } = rgbToHsl(c);
  return hslToRgb(h, s, l + (1 - l) * amt);
}
function weakVariant(accent) {
  const { h, s, l } = rgbToHsl(accent);
  return hslToRgb(h, s * 0.4, l + (1 - l) * 0.55);
}
function edgeStyleForStrength(strength, accent, weak) {
  const t = Math.pow(Math.max(0, Math.min(1, strength)), 0.8);
  return {
    color: rgbToInt(mixRgb(weak, accent, t)),
    alpha: 0.25 + 0.75 * t,
    widthCss: 1.6 + 3.4 * t
  };
}
function resolveBackgroundRgb(el) {
  let cur = el;
  for (let i = 0; cur && i < 12; i++) {
    const bg = getComputedStyle(cur).backgroundColor;
    if (bg && bg !== "transparent" && !/rgba?\([^)]*[,/]\s*0\s*\)$/.test(bg)) {
      const rgb = parseCssColor(bg);
      if (rgb) return rgb;
    }
    cur = cur.parentElement;
  }
  return el.ownerDocument.body.classList.contains("theme-light") ? { r: 255, g: 255, b: 255 } : { r: 30, g: 30, b: 30 };
}
function resolveTextRgb(el) {
  const c = parseCssColor(getComputedStyle(el).color);
  if (c) return c;
  return el.ownerDocument.body.classList.contains("theme-light") ? { r: 34, g: 34, b: 34 } : { r: 220, g: 220, b: 220 };
}
function gfxFillCircle(g, x, y, r, color, alpha) {
  if (typeof g.beginFill === "function") {
    g.lineStyle(0);
    g.beginFill(color, alpha);
    g.drawCircle(x, y, r);
    g.endFill();
  } else {
    g.circle(x, y, r);
    g.fill({ color, alpha });
  }
}
function gfxStrokeCircle(g, x, y, r, width, color, alpha) {
  if (typeof g.beginFill === "function") {
    g.lineStyle({ width, color, alpha });
    g.drawCircle(x, y, r);
    g.lineStyle(0);
  } else {
    g.circle(x, y, r);
    g.stroke({ width, color, alpha });
  }
}
function gfxSeg(g, x1, y1, x2, y2, width, color, alpha) {
  if (typeof g.beginFill === "function") {
    g.lineStyle({ width, color, alpha, cap: "round" });
    g.moveTo(x1, y1);
    g.lineTo(x2, y2);
    g.lineStyle(0);
  } else {
    g.moveTo(x1, y1);
    g.lineTo(x2, y2);
    g.stroke({ width, color, alpha, cap: "round" });
  }
}
function gfxDashedSeg(g, x1, y1, x2, y2, width, color, alpha, dash, gap) {
  const dx = x2 - x1, dy = y2 - y1;
  const len = Math.hypot(dx, dy);
  const step = dash + gap;
  if (!isFinite(len) || len <= 0 || step <= 0 || len / step > 400) {
    gfxSeg(g, x1, y1, x2, y2, width, color, alpha);
    return;
  }
  const ux = dx / len, uy = dy / len;
  for (let t0 = 0; t0 < len; t0 += step) {
    const t1 = Math.min(t0 + dash, len);
    gfxSeg(g, x1 + ux * t0, y1 + uy * t0, x1 + ux * t1, y1 + uy * t1, width, color, alpha);
  }
}
function gfxFillRect(g, x, y, w, h, color, alpha) {
  if (typeof g.beginFill === "function") {
    g.lineStyle(0);
    g.beginFill(color, alpha);
    g.drawRect(x, y, w, h);
    g.endFill();
  } else {
    g.rect(x, y, w, h);
    g.fill({ color, alpha });
  }
}
function safeDestroy(obj) {
  try {
    if (obj && !obj.destroyed) obj.destroy({ children: true });
  } catch (e) {
  }
}
function nodeDisplayName(n) {
  var _a, _b;
  try {
    const t = (_a = n.getDisplayText) == null ? void 0 : _a.call(n);
    if (t) return t;
  } catch (e) {
  }
  const base = (_b = n.id.split("/").pop()) != null ? _b : n.id;
  return base.replace(/\.md$/i, "");
}
function nodeRadius(n, r) {
  var _a, _b;
  let size;
  try {
    size = (_a = n.getSize) == null ? void 0 : _a.call(n);
  } catch (e) {
  }
  if (typeof size !== "number" || !isFinite(size)) {
    const w = typeof n.weight === "number" ? n.weight : 1;
    size = ((_b = r.fNodeSizeMult) != null ? _b : 1) * Math.max(8, Math.min(3 * Math.sqrt(w + 1), 30));
  }
  return size * (typeof r.nodeScale === "number" ? r.nodeScale : 1);
}
function distToSegment(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const l2 = dx * dx + dy * dy;
  if (l2 === 0) return Math.hypot(px - x1, py - y1);
  let t = ((px - x1) * dx + (py - y1) * dy) / l2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}
function waitFor(cond, timeoutMs, stepMs = 100) {
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      let ok = false;
      try {
        ok = cond();
      } catch (e) {
        ok = false;
      }
      if (ok) return resolve(true);
      if (Date.now() - start >= timeoutMs) return resolve(false);
      window.setTimeout(tick, stepMs);
    };
    tick();
  });
}
var DEFAULT_SETTINGS = {
  accentColor: "#00e5ff",
  dimStrength: 0.65,
  timeoutMinutes: 10,
  autoOpenGraph: true,
  centerOnNode: true,
  pulse: true,
  showNodeLabels: true,
  showEdgeLabels: true,
  notifyOnNewData: true,
  autoVisualizeOnChange: false,
  applySearchFilter: false
};
var HighlightController = class {
  constructor(plugin, view, spec) {
    this.plugin = plugin;
    this.view = view;
    this.spec = spec;
    this.stopped = false;
    this.rafId = null;
    this.frameCount = 0;
    this.startTime = performance.now();
    // Scavenged PIXI classes (never bundled — see file header).
    this.pixi = null;
    // Overlay display objects.
    this.dimLayer = null;
    this.fxLayer = null;
    this.dimGfx = null;
    this.edgeGfx = null;
    this.nodeGfx = null;
    this.labels = /* @__PURE__ */ new Map();
    // Spec resolution against live renderer nodes.
    this.resolvedByName = /* @__PURE__ */ new Map();
    this.uniqueNodes = [];
    this.edgeDraws = [];
    this.missing = [];
    this.lastNodesRef = null;
    this.lastNodesLen = -1;
    this.missingNoticeTimer = null;
    this.missingNoticeShown = false;
    // Theme-derived colors, refreshed periodically so theme switches are picked up.
    this.bgInt = 1973790;
    this.textInt = 14474460;
    // Interaction state.
    this.pointer = null;
    this.boundCanvas = null;
    this.tooltipEl = null;
    this.ttLabel = null;
    this.ttRoute = null;
    this.ttStrength = null;
    this.ttBarFill = null;
    this.prevFilter = null;
    this.filterEl = null;
    this.frame = () => {
      var _a, _b;
      if (this.stopped) return;
      if (!this.view.containerEl.isConnected) {
        this.plugin.clearHighlight(true);
        return;
      }
      const r = this.view.renderer;
      if (!r || !r.hanger || r.hanger.destroyed) {
        this.scheduleNext();
        return;
      }
      try {
        if (!this.ensurePixi(r)) {
          this.scheduleNext();
          return;
        }
        this.ensureLayers(r);
        const nodes = (_a = r.nodes) != null ? _a : [];
        if (nodes !== this.lastNodesRef || nodes.length !== this.lastNodesLen) {
          this.resolveSpec(r);
        }
        if (!this.centered) this.maybeCenter(r);
        if (!this.filterApplied) this.maybeApplyFilter();
        this.frameCount++;
        if (this.frameCount % 120 === 1) this.refreshThemeColors();
        this.bindCanvas(r);
        this.drawDim(r);
        this.drawEdges(r);
        this.drawNodes(r);
        this.updateLabels(r);
        this.updateTooltip(r);
        (_b = r.changed) == null ? void 0 : _b.call(r);
      } catch (e) {
        if (this.frameCount % 300 === 0) console.error("[AI Graph Highlighter] frame error", e);
      }
      this.scheduleNext();
    };
    this.onPointerMove = (ev) => {
      this.pointer = { x: ev.offsetX, y: ev.offsetY };
    };
    this.onPointerLeave = () => {
      this.pointer = null;
    };
    this.centered = !(plugin.settings.centerOnNode && spec.center);
    this.filterApplied = !plugin.settings.applySearchFilter;
  }
  get win() {
    var _a;
    return (_a = this.view.containerEl.win) != null ? _a : window;
  }
  attach() {
    this.createTooltip();
    this.refreshThemeColors();
    this.scheduleNext();
  }
  detach() {
    var _a, _b, _c;
    if (this.stopped) return;
    this.stopped = true;
    if (this.rafId !== null) {
      try {
        this.win.cancelAnimationFrame(this.rafId);
      } catch (e) {
      }
      this.rafId = null;
    }
    if (this.missingNoticeTimer !== null) {
      window.clearTimeout(this.missingNoticeTimer);
      this.missingNoticeTimer = null;
    }
    this.unbindCanvas();
    (_a = this.tooltipEl) == null ? void 0 : _a.remove();
    this.tooltipEl = null;
    this.restoreFilter();
    safeDestroy(this.dimLayer);
    safeDestroy(this.fxLayer);
    this.dimLayer = this.fxLayer = this.dimGfx = this.edgeGfx = this.nodeGfx = null;
    this.labels.clear();
    try {
      (_c = (_b = this.view.renderer) == null ? void 0 : _b.changed) == null ? void 0 : _c.call(_b);
    } catch (e) {
    }
  }
  // ------------------------------------------------------------------ frame
  scheduleNext() {
    if (this.stopped) return;
    try {
      this.rafId = this.win.requestAnimationFrame(this.frame);
    } catch (e) {
      this.rafId = window.requestAnimationFrame(this.frame);
    }
  }
  // ------------------------------------------------------- pixi + overlay
  /** Grab PIXI classes from live objects. Retries until at least one node circle exists. */
  ensurePixi(r) {
    var _a, _b;
    if (this.pixi) return true;
    const Container = (_a = r.hanger) == null ? void 0 : _a.constructor;
    let Graphics = null;
    let Text = null;
    for (const n of (_b = r.nodes) != null ? _b : []) {
      if (!Graphics && n.circle) Graphics = n.circle.constructor;
      if (!Text && n.text) Text = n.text.constructor;
      if (Graphics && Text) break;
    }
    if (!Graphics && r.highlight) Graphics = r.highlight.constructor;
    if (!Container || !Graphics) return false;
    this.pixi = { Container, Graphics, Text };
    return true;
  }
  /** (Re)create overlay containers and keep them parented to the current hanger. */
  ensureLayers(r) {
    if (!this.pixi) return;
    const { Container, Graphics } = this.pixi;
    if (!this.dimLayer || this.dimLayer.destroyed || !this.fxLayer || this.fxLayer.destroyed) {
      safeDestroy(this.dimLayer);
      safeDestroy(this.fxLayer);
      this.labels.clear();
      this.dimLayer = new Container();
      this.dimLayer.zIndex = 3;
      this.dimLayer.eventMode = "none";
      this.dimGfx = new Graphics();
      this.dimGfx.eventMode = "none";
      this.dimLayer.addChild(this.dimGfx);
      this.fxLayer = new Container();
      this.fxLayer.zIndex = 4;
      this.fxLayer.eventMode = "none";
      this.edgeGfx = new Graphics();
      this.edgeGfx.eventMode = "none";
      this.nodeGfx = new Graphics();
      this.nodeGfx.eventMode = "none";
      this.fxLayer.addChild(this.edgeGfx);
      this.fxLayer.addChild(this.nodeGfx);
    }
    if (this.dimLayer.parent !== r.hanger) r.hanger.addChild(this.dimLayer);
    if (this.fxLayer.parent !== r.hanger) r.hanger.addChild(this.fxLayer);
  }
  refreshThemeColors() {
    try {
      this.bgInt = rgbToInt(resolveBackgroundRgb(this.view.containerEl));
      this.textInt = rgbToInt(resolveTextRgb(this.view.containerEl));
    } catch (e) {
    }
  }
  // ------------------------------------------------------------- resolution
  /** Map spec names to live graph nodes. Re-run whenever the node set changes. */
  resolveSpec(r) {
    var _a, _b, _c, _d, _e;
    const nodes = (_a = r.nodes) != null ? _a : [];
    this.lastNodesRef = nodes;
    this.lastNodesLen = nodes.length;
    const byExact = /* @__PURE__ */ new Map();
    const byLower = /* @__PURE__ */ new Map();
    const byBase = /* @__PURE__ */ new Map();
    for (const n of nodes) {
      if (typeof n.id !== "string") continue;
      if (!byExact.has(n.id)) byExact.set(n.id, n);
      const lower = n.id.toLowerCase();
      if (!byLower.has(lower)) byLower.set(lower, n);
      const base = ((_b = n.id.split("/").pop()) != null ? _b : n.id).replace(/\.md$/i, "").toLowerCase();
      if (!byBase.has(base)) byBase.set(base, n);
    }
    const resolveName = (name) => {
      var _a2, _b2, _c2, _d2, _e2, _f;
      const exact = byExact.get(name);
      if (exact) return exact;
      const dest = this.plugin.app.metadataCache.getFirstLinkpathDest(
        name.replace(/\.md$/i, ""),
        ""
      );
      if (dest) {
        const n = (_a2 = byExact.get(dest.path)) != null ? _a2 : byLower.get(dest.path.toLowerCase());
        if (n) return n;
      }
      return (_f = (_e2 = (_c2 = (_b2 = byExact.get(name + ".md")) != null ? _b2 : byLower.get(name.toLowerCase())) != null ? _c2 : byLower.get(name.toLowerCase() + ".md")) != null ? _e2 : byBase.get(((_d2 = name.split("/").pop()) != null ? _d2 : name).replace(/\.md$/i, "").toLowerCase())) != null ? _f : null;
    };
    this.resolvedByName.clear();
    for (const name of this.spec.nodes) this.resolvedByName.set(name, resolveName(name));
    const uniq = /* @__PURE__ */ new Map();
    this.missing = [];
    for (const [name, n] of this.resolvedByName) {
      if (n) uniq.set(n.id, n);
      else this.missing.push(name);
    }
    this.uniqueNodes = [...uniq.values()];
    const nativeKeys = /* @__PURE__ */ new Set();
    for (const l of (_c = r.links) != null ? _c : []) {
      const s = (_d = l.source) == null ? void 0 : _d.id, t = (_e = l.target) == null ? void 0 : _e.id;
      if (s && t) {
        nativeKeys.add(s + "|" + t);
        nativeKeys.add(t + "|" + s);
      }
    }
    this.edgeDraws = this.spec.edges.map((e) => {
      var _a2, _b2;
      const a = (_a2 = this.resolvedByName.get(e.from)) != null ? _a2 : null;
      const b = (_b2 = this.resolvedByName.get(e.to)) != null ? _b2 : null;
      return {
        ...e,
        a,
        b,
        native: !!(a && b && nativeKeys.has(a.id + "|" + b.id))
      };
    });
    if (this.missingNoticeTimer === null && !this.missingNoticeShown) {
      this.missingNoticeTimer = window.setTimeout(() => {
        this.missingNoticeTimer = null;
        this.missingNoticeShown = true;
        if (this.stopped || this.missing.length === 0) return;
        const shown = this.missing.slice(0, 6).map((s) => `"${s}"`).join(", ");
        const extra = this.missing.length > 6 ? ` (+${this.missing.length - 6} more)` : "";
        new import_obsidian.Notice(
          `AI Graph Highlighter: ${this.missing.length} note(s) not found in the graph: ${shown}${extra}. Check spelling and graph filters.`,
          8e3
        );
      }, 2500);
    }
  }
  // -------------------------------------------------------------- centering
  maybeCenter(r) {
    var _a, _b, _c, _d;
    const name = this.spec.center;
    if (!name) {
      this.centered = true;
      return;
    }
    const elapsed = performance.now() - this.startTime;
    const n = (_a = this.resolvedByName.get(name)) != null ? _a : null;
    if (!n) {
      if (elapsed > 6e3) this.centered = true;
      return;
    }
    const el = r.interactiveEl;
    if (!el) return;
    const dpr = this.win.devicePixelRatio || 1;
    const w = el.clientWidth * dpr;
    const h = el.clientHeight * dpr;
    try {
      let s = r.scale || 1;
      if (s < 0.5) {
        s = 1;
        r.targetScale = s;
        (_b = r.setScale) == null ? void 0 : _b.call(r, s);
      }
      (_c = r.setPan) == null ? void 0 : _c.call(r, w / 2 - n.x * s, h / 2 - n.y * s);
      (_d = r.changed) == null ? void 0 : _d.call(r);
    } catch (e) {
      this.centered = true;
      return;
    }
    if (elapsed > 1500) this.centered = true;
  }
  // ---------------------------------------------------------- search filter
  maybeApplyFilter() {
    if (this.uniqueNodes.length === 0) {
      if (performance.now() - this.startTime > 6e3) this.filterApplied = true;
      return;
    }
    this.filterApplied = true;
    try {
      const input = this.view.containerEl.querySelector(
        [
          ".graph-controls .search-input-container input",
          ".graph-control-section.mod-filter input",
          ".graph-controls input[type='search']",
          ".graph-controls input[type='text']"
        ].join(", ")
      );
      if (!input) {
        console.warn("[AI Graph Highlighter] Graph filter input not found; skipping isolation.");
        return;
      }
      const parts = this.uniqueNodes.filter((n) => n.type !== "unresolved" && n.type !== "tag").map((n) => `path:"${n.id}"`);
      if (parts.length === 0) return;
      const query = parts.join(" OR ");
      if (query.length > 4e3) {
        console.warn("[AI Graph Highlighter] Filter query too long; skipping isolation.");
        return;
      }
      this.prevFilter = input.value;
      this.filterEl = input;
      input.value = query;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    } catch (e) {
      console.warn("[AI Graph Highlighter] Could not apply search filter", e);
    }
  }
  restoreFilter() {
    var _a;
    if (!this.filterEl) return;
    try {
      if (this.filterEl.isConnected) {
        this.filterEl.value = (_a = this.prevFilter) != null ? _a : "";
        this.filterEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
    } catch (e) {
    }
    this.filterEl = null;
    this.prevFilter = null;
  }
  // ----------------------------------------------------------------- drawing
  accentRgb() {
    var _a;
    return (_a = parseCssColor(this.plugin.settings.accentColor)) != null ? _a : parseCssColor(DEFAULT_SETTINGS.accentColor);
  }
  /** Dim everything: a translucent background-colored sheet over the whole viewport. */
  drawDim(r) {
    if (!this.dimGfx || this.dimGfx.destroyed) return;
    this.dimGfx.clear();
    const dim = this.plugin.settings.dimStrength;
    if (dim <= 0) return;
    const el = r.interactiveEl;
    if (!el) return;
    const dpr = this.win.devicePixelRatio || 1;
    const s = r.scale || 1;
    const left = -r.panX / s;
    const top = -r.panY / s;
    const w = el.clientWidth * dpr / s;
    const h = el.clientHeight * dpr / s;
    const mx = w * 0.25, my = h * 0.25;
    gfxFillRect(this.dimGfx, left - mx, top - my, w + 2 * mx, h + 2 * my, this.bgInt, dim);
  }
  drawEdges(r) {
    if (!this.edgeGfx || this.edgeGfx.destroyed) return;
    this.edgeGfx.clear();
    const accent = this.accentRgb();
    const weak = weakVariant(accent);
    const dpr = this.win.devicePixelRatio || 1;
    const s = r.scale || 1;
    const px = dpr / s;
    for (const e of this.edgeDraws) {
      const { a, b } = e;
      if (!a || !b || a === b || a.rendered === false || b.rendered === false) continue;
      const st = edgeStyleForStrength(e.strength, accent, weak);
      const w = st.widthCss * px;
      gfxSeg(this.edgeGfx, a.x, a.y, b.x, b.y, w * 3.4, st.color, st.alpha * 0.16);
      gfxSeg(this.edgeGfx, a.x, a.y, b.x, b.y, w * 1.8, st.color, st.alpha * 0.4);
      if (e.native) {
        gfxSeg(this.edgeGfx, a.x, a.y, b.x, b.y, w, st.color, st.alpha);
      } else {
        gfxDashedSeg(this.edgeGfx, a.x, a.y, b.x, b.y, w, st.color, st.alpha, 9 * px, 6 * px);
      }
    }
  }
  drawNodes(r) {
    if (!this.nodeGfx || this.nodeGfx.destroyed) return;
    this.nodeGfx.clear();
    const accent = this.accentRgb();
    const accentInt = rgbToInt(accent);
    const coreInt = rgbToInt(lighten(accent, 0.35));
    const dpr = this.win.devicePixelRatio || 1;
    const s = r.scale || 1;
    const px = dpr / s;
    const pulse = this.plugin.settings.pulse ? 0.5 + 0.5 * Math.sin(performance.now() / 260) : 0.75;
    for (const n of this.uniqueNodes) {
      if (n.rendered === false) continue;
      const base = nodeRadius(n, r);
      gfxStrokeCircle(
        this.nodeGfx,
        n.x,
        n.y,
        base * (1.35 + 0.12 * pulse),
        5 * px,
        accentInt,
        0.1 + 0.1 * pulse
      );
      gfxStrokeCircle(
        this.nodeGfx,
        n.x,
        n.y,
        base * (1.2 + 0.06 * pulse),
        3 * px,
        accentInt,
        0.28 + 0.14 * pulse
      );
      gfxFillCircle(this.nodeGfx, n.x, n.y, base * 1.1, accentInt, 0.92);
      gfxStrokeCircle(this.nodeGfx, n.x, n.y, base * 1.1, 1.75 * px, coreInt, 1);
    }
  }
  updateLabels(r) {
    var _a, _b, _c, _d, _e;
    if (!this.fxLayer || this.fxLayer.destroyed) return;
    const enabled = this.plugin.settings.showNodeLabels && !!((_a = this.pixi) == null ? void 0 : _a.Text) && this.uniqueNodes.length <= 80;
    if (!enabled) {
      for (const t of this.labels.values()) t.visible = false;
      return;
    }
    const dpr = this.win.devicePixelRatio || 1;
    const s = r.scale || 1;
    const px = dpr / s;
    const current = /* @__PURE__ */ new Set();
    let fontFamily = "sans-serif";
    try {
      fontFamily = getComputedStyle(this.view.containerEl).fontFamily || fontFamily;
    } catch (e) {
    }
    for (const n of this.uniqueNodes) {
      current.add(n.id);
      let t = this.labels.get(n.id);
      if (!t || t.destroyed) {
        try {
          t = new this.pixi.Text(nodeDisplayName(n), {
            fontFamily,
            fontSize: 13,
            fontWeight: "600",
            fill: this.textInt,
            stroke: this.bgInt,
            strokeThickness: 3,
            align: "center"
          });
          (_c = (_b = t.anchor) == null ? void 0 : _b.set) == null ? void 0 : _c.call(_b, 0.5, 0);
          t.resolution = 2;
          t.eventMode = "none";
          this.fxLayer.addChild(t);
          this.labels.set(n.id, t);
        } catch (e) {
          console.warn("[AI Graph Highlighter] Labels unavailable", e);
          this.pixi.Text = null;
          return;
        }
      }
      t.visible = n.rendered !== false;
      if (!t.visible) continue;
      const nt = n.text;
      if (nt && !nt.destroyed && isFinite(nt.x) && isFinite(nt.y)) {
        t.x = nt.x;
        t.y = nt.y;
      } else {
        t.x = n.x;
        t.y = n.y + nodeRadius(n, r) * 1.2 + 5 * px;
      }
      (_e = (_d = t.scale) == null ? void 0 : _d.set) == null ? void 0 : _e.call(_d, px);
      t.alpha = 1;
    }
    for (const [id, t] of this.labels) {
      if (!current.has(id)) {
        safeDestroy(t);
        this.labels.delete(id);
      }
    }
  }
  // ----------------------------------------------------------- edge tooltip
  createTooltip() {
    const host = this.view.containerEl;
    const tt = host.createDiv({ cls: "aigh-edge-tooltip" });
    tt.style.setProperty("--aigh-accent", this.plugin.settings.accentColor);
    this.ttLabel = tt.createDiv({ cls: "aigh-tt-label" });
    this.ttRoute = tt.createDiv({ cls: "aigh-tt-route" });
    this.ttStrength = tt.createDiv({ cls: "aigh-tt-strength" });
    const bar = tt.createDiv({ cls: "aigh-tt-bar" });
    this.ttBarFill = bar.createDiv({ cls: "aigh-tt-bar-fill" });
    this.tooltipEl = tt;
  }
  bindCanvas(r) {
    var _a;
    const el = (_a = r.interactiveEl) != null ? _a : null;
    if (el === this.boundCanvas) return;
    this.unbindCanvas();
    if (!el) return;
    el.addEventListener("pointermove", this.onPointerMove);
    el.addEventListener("pointerdown", this.onPointerMove);
    el.addEventListener("pointerleave", this.onPointerLeave);
    this.boundCanvas = el;
  }
  unbindCanvas() {
    const el = this.boundCanvas;
    if (!el) return;
    el.removeEventListener("pointermove", this.onPointerMove);
    el.removeEventListener("pointerdown", this.onPointerMove);
    el.removeEventListener("pointerleave", this.onPointerLeave);
    this.boundCanvas = null;
  }
  updateTooltip(r) {
    var _a;
    const tt = this.tooltipEl;
    if (!tt) return;
    const p = this.pointer;
    if (!p || !this.plugin.settings.showEdgeLabels) {
      tt.removeClass("aigh-visible");
      return;
    }
    const dpr = this.win.devicePixelRatio || 1;
    const s = r.scale || 1;
    const gx = (p.x * dpr - r.panX) / s;
    const gy = (p.y * dpr - r.panY) / s;
    let best = null;
    let bestD = Infinity;
    for (const e of this.edgeDraws) {
      const { a, b } = e;
      if (!a || !b || a === b || a.rendered === false || b.rendered === false) continue;
      if (Math.hypot(gx - a.x, gy - a.y) < nodeRadius(a, r) * 1.3 || Math.hypot(gx - b.x, gy - b.y) < nodeRadius(b, r) * 1.3)
        continue;
      const d = distToSegment(gx, gy, a.x, a.y, b.x, b.y);
      const threshold = Math.max(10, edgeStyleForStrength(e.strength, { r: 0, g: 0, b: 0 }, { r: 0, g: 0, b: 0 }).widthCss * 1.5) * dpr / s;
      if (d < threshold && d < bestD) {
        best = e;
        bestD = d;
      }
    }
    if (!best || !best.a || !best.b) {
      tt.removeClass("aigh-visible");
      return;
    }
    const aName = nodeDisplayName(best.a);
    const bName = nodeDisplayName(best.b);
    this.ttLabel.setText((_a = best.label) != null ? _a : `${aName} \u2194 ${bName}`);
    this.ttRoute.setText(`${aName} \u27F7 ${bName}${best.native ? "" : " \xB7 no direct link in vault"}`);
    this.ttStrength.setText(`Strength ${Math.round(best.strength * 100)}%`);
    this.ttBarFill.style.width = `${Math.round(best.strength * 100)}%`;
    tt.style.setProperty("--aigh-accent", this.plugin.settings.accentColor);
    tt.addClass("aigh-visible");
    const host = this.view.containerEl;
    const maxX = host.clientWidth - tt.offsetWidth - 8;
    const maxY = host.clientHeight - tt.offsetHeight - 8;
    tt.style.left = `${Math.max(8, Math.min(p.x + 16, maxX))}px`;
    tt.style.top = `${Math.max(8, Math.min(p.y + 18, maxY))}px`;
  }
};
var AIGraphHighlighterPlugin = class extends import_obsidian.Plugin {
  constructor() {
    super(...arguments);
    this.settings = { ...DEFAULT_SETTINGS };
    this.controller = null;
    this.timeoutTimer = null;
    this.lastSeenMtime = 0;
    this.watcherBusy = false;
  }
  get pendingPath() {
    var _a;
    const dir = (_a = this.manifest.dir) != null ? _a : `${this.app.vault.configDir}/plugins/${this.manifest.id}`;
    return (0, import_obsidian.normalizePath)(`${dir}/pending-highlight.json`);
  }
  async onload() {
    await this.loadSettings();
    this.addSettingTab(new AIGHSettingTab(this.app, this));
    this.addRibbonIcon("sparkles", "AI Graph Highlighter: Visualize pending highlight", () => {
      void this.visualizePending();
    });
    this.addCommand({
      id: "visualize-pending-highlight",
      name: "Visualize pending highlight",
      callback: () => void this.visualizePending()
    });
    this.addCommand({
      id: "clear-highlight",
      name: "Clear highlight",
      callback: () => this.clearHighlight()
    });
    this.registerObsidianProtocolHandler("ai-graph-highlighter", (params) => {
      if ("clear" in params) this.clearHighlight();
      else void this.visualizePending();
    });
    void this.initWatcher();
    this.register(() => this.clearHighlight(true));
  }
  async initWatcher() {
    var _a;
    try {
      const st = await this.app.vault.adapter.stat(this.pendingPath);
      this.lastSeenMtime = (_a = st == null ? void 0 : st.mtime) != null ? _a : 0;
    } catch (e) {
      this.lastSeenMtime = 0;
    }
    this.registerInterval(window.setInterval(() => void this.pollPending(), 2e3));
  }
  async pollPending() {
    var _a;
    if (this.watcherBusy) return;
    if (!this.settings.notifyOnNewData && !this.settings.autoVisualizeOnChange) return;
    this.watcherBusy = true;
    try {
      const st = await this.app.vault.adapter.stat(this.pendingPath);
      const mtime = (_a = st == null ? void 0 : st.mtime) != null ? _a : 0;
      if (mtime && mtime !== this.lastSeenMtime) {
        this.lastSeenMtime = mtime;
        if (this.settings.autoVisualizeOnChange) {
          void this.visualizePending();
        } else if (this.settings.notifyOnNewData) {
          let suffix = "";
          try {
            const parsed = JSON.parse(stripBom(await this.app.vault.adapter.read(this.pendingPath)));
            if (parsed && typeof parsed.title === "string" && parsed.title.trim())
              suffix = ` \u2014 \u201C${parsed.title.trim().slice(0, 80)}\u201D`;
          } catch (e) {
          }
          new import_obsidian.Notice(
            `AI Graph Highlighter: new highlight data${suffix}. Click the \u2728 ribbon icon or run \u201CVisualize pending highlight\u201D.`,
            8e3
          );
        }
      }
    } catch (e) {
    } finally {
      this.watcherBusy = false;
    }
  }
  async visualizePending() {
    var _a;
    const adapter = this.app.vault.adapter;
    const path = this.pendingPath;
    let raw;
    try {
      if (!await adapter.exists(path)) {
        new import_obsidian.Notice(
          `AI Graph Highlighter: no highlight data found.
Expected file: ${path}`,
          8e3
        );
        return;
      }
      raw = await adapter.read(path);
    } catch (e) {
      new import_obsidian.Notice(`AI Graph Highlighter: could not read ${path}: ${e.message}`, 8e3);
      return;
    }
    let json;
    try {
      json = JSON.parse(stripBom(raw));
    } catch (e) {
      new import_obsidian.Notice(
        `AI Graph Highlighter: pending-highlight.json is not valid JSON.
${e.message}`,
        1e4
      );
      return;
    }
    const { spec, errors, warnings } = validatePendingHighlight(json);
    if (!spec) {
      new import_obsidian.Notice(`AI Graph Highlighter: invalid highlight data:
\u2022 ${errors.join("\n\u2022 ")}`, 1e4);
      return;
    }
    if (warnings.length > 0) {
      console.warn("[AI Graph Highlighter] data warnings:", warnings);
      new import_obsidian.Notice(
        `AI Graph Highlighter: ${warnings.length} data warning(s) \u2014 see developer console.`,
        5e3
      );
    }
    try {
      const st = await adapter.stat(path);
      if (st == null ? void 0 : st.mtime) this.lastSeenMtime = st.mtime;
    } catch (e) {
    }
    const view = await this.ensureGraphView();
    if (!view) return;
    this.clearHighlight(true);
    this.controller = new HighlightController(this, view, spec);
    this.controller.attach();
    if (this.settings.timeoutMinutes > 0) {
      this.timeoutTimer = window.setTimeout(() => {
        this.timeoutTimer = null;
        this.clearHighlight(true);
        new import_obsidian.Notice("AI Graph Highlighter: highlight auto-cleared (timeout).");
      }, this.settings.timeoutMinutes * 6e4);
    }
    const timeoutNote = this.settings.timeoutMinutes > 0 ? ` Auto-clears in ${this.settings.timeoutMinutes} min.` : "";
    new import_obsidian.Notice(
      `\u26A1 ${(_a = spec.title) != null ? _a : "AI highlight"}: ${spec.nodes.length} note(s), ${spec.edges.length} connection(s).${timeoutNote}`,
      6e3
    );
  }
  /** Find (or open) a graph view and wait for its renderer to be ready. */
  async ensureGraphView() {
    var _a, _b, _c;
    const ws = this.app.workspace;
    let leaf = null;
    const recent = ws.getMostRecentLeaf();
    const recentType = (_a = recent == null ? void 0 : recent.view) == null ? void 0 : _a.getViewType();
    if (recent && (recentType === "graph" || recentType === "localgraph")) {
      leaf = recent;
    } else {
      leaf = (_c = (_b = ws.getLeavesOfType("graph")[0]) != null ? _b : ws.getLeavesOfType("localgraph")[0]) != null ? _c : null;
    }
    if (!leaf) {
      if (!this.settings.autoOpenGraph) {
        new import_obsidian.Notice(
          "AI Graph Highlighter: no Graph view is open. Open one, or enable \u201CAuto-open Graph view\u201D in settings."
        );
        return null;
      }
      leaf = ws.getLeaf("tab");
      await leaf.setViewState({ type: "graph", active: true });
    }
    await ws.revealLeaf(leaf);
    const view = leaf.view;
    const ready = await waitFor(() => {
      var _a2;
      return !!((_a2 = view.renderer) == null ? void 0 : _a2.hanger);
    }, 5e3, 100);
    if (!ready) {
      new import_obsidian.Notice("AI Graph Highlighter: the Graph view did not finish loading. Try again.");
      return null;
    }
    return view;
  }
  clearHighlight(silent = false) {
    if (this.timeoutTimer !== null) {
      window.clearTimeout(this.timeoutTimer);
      this.timeoutTimer = null;
    }
    if (this.controller) {
      this.controller.detach();
      this.controller = null;
      if (!silent) new import_obsidian.Notice("AI Graph Highlighter: highlight cleared.");
    }
  }
  async loadSettings() {
    var _a;
    this.settings = { ...DEFAULT_SETTINGS, ...(_a = await this.loadData()) != null ? _a : {} };
  }
  async saveSettings() {
    await this.saveData(this.settings);
  }
};
var AIGHSettingTab = class extends import_obsidian.PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }
  display() {
    const { containerEl } = this;
    containerEl.empty();
    const save = () => void this.plugin.saveSettings();
    new import_obsidian.Setting(containerEl).setName("Appearance").setHeading();
    const colorSetting = new import_obsidian.Setting(containerEl).setName("Accent color").setDesc(
      "Highlight color for nodes and edges. Edges fade from a pale version (strength 0) to this exact color (strength 1)."
    );
    const preview = createSpan({ cls: "aigh-settings-preview" });
    preview.style.setProperty("--aigh-accent", this.plugin.settings.accentColor);
    colorSetting.addColorPicker(
      (cp) => cp.setValue(this.plugin.settings.accentColor).onChange((v) => {
        this.plugin.settings.accentColor = v;
        preview.style.setProperty("--aigh-accent", v);
        save();
      })
    );
    colorSetting.controlEl.appendChild(preview);
    new import_obsidian.Setting(containerEl).setName("Dim strength").setDesc("How strongly the rest of the graph is dimmed while a highlight is active.").addSlider(
      (sl) => sl.setLimits(0, 0.9, 0.05).setValue(this.plugin.settings.dimStrength).setDynamicTooltip().onChange((v) => {
        this.plugin.settings.dimStrength = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("Pulse animation").setDesc("Subtle pulsing glow on highlighted nodes.").addToggle(
      (t) => t.setValue(this.plugin.settings.pulse).onChange((v) => {
        this.plugin.settings.pulse = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("Bright labels on highlighted notes").setDesc("Redraw the names of highlighted notes at full brightness above the dim layer.").addToggle(
      (t) => t.setValue(this.plugin.settings.showNodeLabels).onChange((v) => {
        this.plugin.settings.showNodeLabels = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("Edge labels on hover").setDesc("Show the AI's explanation of a connection when hovering a highlighted edge.").addToggle(
      (t) => t.setValue(this.plugin.settings.showEdgeLabels).onChange((v) => {
        this.plugin.settings.showEdgeLabels = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("Behavior").setHeading();
    new import_obsidian.Setting(containerEl).setName("Auto-clear timeout (minutes)").setDesc("Highlights clear themselves after this long. 0 = never.").addSlider(
      (sl) => sl.setLimits(0, 60, 1).setValue(this.plugin.settings.timeoutMinutes).setDynamicTooltip().onChange((v) => {
        this.plugin.settings.timeoutMinutes = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("Auto-open Graph view").setDesc("Open the global Graph view automatically when visualizing, if none is open.").addToggle(
      (t) => t.setValue(this.plugin.settings.autoOpenGraph).onChange((v) => {
        this.plugin.settings.autoOpenGraph = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("Center on focus node").setDesc("Pan/zoom to the highlight's \u201Ccenter\u201D node when applying.").addToggle(
      (t) => t.setValue(this.plugin.settings.centerOnNode).onChange((v) => {
        this.plugin.settings.centerOnNode = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("Isolate with search filter (experimental)").setDesc(
      "Also rewrite the graph's search filter to show only highlighted notes. Your previous filter is restored when the highlight clears."
    ).addToggle(
      (t) => t.setValue(this.plugin.settings.applySearchFilter).onChange((v) => {
        this.plugin.settings.applySearchFilter = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("AI integration").setHeading();
    new import_obsidian.Setting(containerEl).setName("Notify when new highlight data arrives").setDesc("Show a notice when the AI writes a new pending-highlight.json.").addToggle(
      (t) => t.setValue(this.plugin.settings.notifyOnNewData).onChange((v) => {
        this.plugin.settings.notifyOnNewData = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("Auto-visualize when new data arrives").setDesc("Skip the notice and immediately visualize whenever the AI updates the file.").addToggle(
      (t) => t.setValue(this.plugin.settings.autoVisualizeOnChange).onChange((v) => {
        this.plugin.settings.autoVisualizeOnChange = v;
        save();
      })
    );
    new import_obsidian.Setting(containerEl).setName("Data file").setDesc(`The AI should write highlight JSON to: ${this.plugin.pendingPath}`);
  }
};
