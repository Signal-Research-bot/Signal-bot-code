/**
 * AI Graph Highlighter
 * --------------------
 * Lets an AI assistant (e.g. Claudian / Claude) drop a JSON file describing a set of
 * notes and weighted connections, then visualizes that set inside Obsidian's Graph
 * view: accent-colored, glowing nodes and gradient edges (opacity/brightness/width
 * scale with a 0–1 "strength") drawn over a dimmed version of the normal graph.
 *
 * How it draws (important for future maintenance):
 * Obsidian's graph is rendered with PIXI (v7 at the time of writing, verified against
 * Obsidian 1.12.7). The renderer keeps a `hanger` PIXI.Container whose transform is
 * pan/zoom (`hanger.x = panX`, `hanger.scale = scale`; coordinates inside the hanger
 * are "graph units", and panX/panY/scale map graph units to *physical* pixels, i.e.
 * CSS pixels × devicePixelRatio). Node circles are PIXI.Graphics children of the
 * hanger positioned at (node.x, node.y); labels are PIXI.Text children with zIndex 2.
 *
 * This plugin is strictly non-destructive: it never mutates native display objects.
 * It adds two containers of its own to the hanger (a dim layer and an fx layer,
 * zIndex 3/4 so they sort above native circles/labels) and redraws them every frame
 * from live node positions. PIXI classes are scavenged from live objects
 * (`hanger.constructor`, `node.circle.constructor`, …) instead of bundling pixi.js,
 * so there can never be a version clash with Obsidian's own PIXI.
 *
 * Everything self-heals per frame: if the renderer rebuilds its scene (filter change,
 * settings change, theme change), the overlay detects destroyed/orphaned containers
 * and rebuilds itself. All of this is undocumented API, so every access is
 * defensively guarded — worst case the highlight silently does nothing, it never
 * breaks the graph.
 */

import {
	App,
	Notice,
	Plugin,
	PluginSettingTab,
	Setting,
	View,
	WorkspaceLeaf,
	normalizePath,
} from "obsidian";

// ---------------------------------------------------------------------------
// Data model — what the AI writes into pending-highlight.json
// ---------------------------------------------------------------------------

interface HighlightEdge {
	from: string;
	to: string;
	/** 0.0 (weakest) … 1.0 (strongest). Drives edge color, opacity and width. */
	strength: number;
	/** Optional human explanation, shown in a tooltip when hovering the edge. */
	label?: string;
}

interface HighlightSpec {
	/** Note names or vault paths. Edge endpoints are unioned in automatically. */
	nodes: string[];
	edges: HighlightEdge[];
	/** Optional node to pan/zoom to when the highlight is applied. */
	center?: string;
	/** Optional title, used in notices. */
	title?: string;
}

interface ValidationResult {
	spec: HighlightSpec | null;
	errors: string[];
	warnings: string[];
}

const MAX_NODES = 400;
const MAX_EDGES = 800;

function stripBom(s: string): string {
	return s.charCodeAt(0) === 0xfeff ? s.slice(1) : s;
}

/** Validate untrusted JSON into a HighlightSpec, collecting human-readable problems. */
function validatePendingHighlight(raw: unknown): ValidationResult {
	const errors: string[] = [];
	const warnings: string[] = [];

	if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
		return { spec: null, errors: ["Top level must be a JSON object."], warnings };
	}
	const obj = raw as Record<string, unknown>;

	// nodes
	let nodes: string[] = [];
	if (Array.isArray(obj.nodes)) {
		for (const n of obj.nodes) {
			if (typeof n === "string" && n.trim().length > 0) nodes.push(n.trim());
			else warnings.push(`Ignored non-string entry in "nodes": ${JSON.stringify(n)}`);
		}
	} else if (obj.nodes !== undefined) {
		warnings.push(`"nodes" should be an array of strings.`);
	}

	// edges
	const edges: HighlightEdge[] = [];
	if (Array.isArray(obj.edges)) {
		for (const e of obj.edges) {
			if (e === null || typeof e !== "object") {
				warnings.push(`Ignored non-object entry in "edges".`);
				continue;
			}
			const ed = e as Record<string, unknown>;
			const from = typeof ed.from === "string" ? ed.from.trim() : "";
			const to = typeof ed.to === "string" ? ed.to.trim() : "";
			if (!from || !to) {
				warnings.push(`Ignored edge with missing "from"/"to": ${JSON.stringify(e).slice(0, 120)}`);
				continue;
			}
			let strength: number;
			if (typeof ed.strength === "number" && isFinite(ed.strength)) {
				strength = Math.max(0, Math.min(1, ed.strength));
				if (strength !== ed.strength)
					warnings.push(`Edge "${from}" → "${to}": strength ${ed.strength} clamped to ${strength}.`);
			} else {
				strength = 0.5;
				warnings.push(`Edge "${from}" → "${to}": missing/invalid "strength", defaulting to 0.5.`);
			}
			let label: string | undefined;
			if (typeof ed.label === "string" && ed.label.trim().length > 0) {
				label = ed.label.trim();
				if (label.length > 300) label = label.slice(0, 297) + "…";
			}
			edges.push({ from, to, strength, label });
		}
	} else if (obj.edges !== undefined) {
		warnings.push(`"edges" should be an array of objects.`);
	}

	// Union edge endpoints into the node set (dedupe, preserve order).
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
		errors.push(`Nothing to highlight — provide "nodes" (string array) and/or "edges".`);
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

	const center = typeof obj.center === "string" && obj.center.trim() ? obj.center.trim() : undefined;
	const title =
		typeof obj.title === "string" && obj.title.trim() ? obj.title.trim().slice(0, 200) : undefined;

	return { spec: { nodes, edges, center, title }, errors, warnings };
}

// ---------------------------------------------------------------------------
// Color utilities (dependency-free)
// ---------------------------------------------------------------------------

interface RGB {
	r: number;
	g: number;
	b: number;
}

function parseCssColor(input: string): RGB | null {
	const s = input.trim();
	let m = /^#([0-9a-f]{3})$/i.exec(s);
	if (m) {
		const h = m[1];
		return {
			r: parseInt(h[0] + h[0], 16),
			g: parseInt(h[1] + h[1], 16),
			b: parseInt(h[2] + h[2], 16),
		};
	}
	m = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(s);
	if (m) {
		const h = m[1];
		return {
			r: parseInt(h.slice(0, 2), 16),
			g: parseInt(h.slice(2, 4), 16),
			b: parseInt(h.slice(4, 6), 16),
		};
	}
	m = /^rgba?\(\s*([\d.]+)\s*[, ]\s*([\d.]+)\s*[, ]\s*([\d.]+)/i.exec(s);
	if (m) {
		return { r: +m[1], g: +m[2], b: +m[3] };
	}
	return null;
}

function rgbToInt(c: RGB): number {
	return (Math.round(c.r) << 16) | (Math.round(c.g) << 8) | Math.round(c.b);
}

function rgbToHsl(c: RGB): { h: number; s: number; l: number } {
	const r = c.r / 255, g = c.g / 255, b = c.b / 255;
	const max = Math.max(r, g, b), min = Math.min(r, g, b);
	const l = (max + min) / 2;
	if (max === min) return { h: 0, s: 0, l };
	const d = max - min;
	const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
	let h: number;
	if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
	else if (max === g) h = ((b - r) / d + 2) / 6;
	else h = ((r - g) / d + 4) / 6;
	return { h, s, l };
}

function hslToRgb(h: number, s: number, l: number): RGB {
	const hue2rgb = (p: number, q: number, t: number): number => {
		if (t < 0) t += 1;
		if (t > 1) t -= 1;
		if (t < 1 / 6) return p + (q - p) * 6 * t;
		if (t < 1 / 2) return q;
		if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
		return p;
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
		b: Math.round(hue2rgb(p, q, h - 1 / 3) * 255),
	};
}

function mixRgb(a: RGB, b: RGB, t: number): RGB {
	return {
		r: a.r + (b.r - a.r) * t,
		g: a.g + (b.g - a.g) * t,
		b: a.b + (b.b - a.b) * t,
	};
}

/** Lighten toward white by `amt` (0–1) via HSL. */
function lighten(c: RGB, amt: number): RGB {
	const { h, s, l } = rgbToHsl(c);
	return hslToRgb(h, s, l + (1 - l) * amt);
}

/** Pale, desaturated variant of the accent — the weak (strength 0) end of the gradient. */
function weakVariant(accent: RGB): RGB {
	const { h, s, l } = rgbToHsl(accent);
	return hslToRgb(h, s * 0.4, l + (1 - l) * 0.55);
}

interface EdgeStyle {
	color: number;
	alpha: number;
	widthCss: number;
}

/**
 * The gradient at the heart of the plugin: strength 1 → fully opaque, brightest
 * accent; strength 0 → pale, low-opacity, desaturated version of the same hue.
 * Width also scales mildly. Perceptual-ish curve so mid strengths spread nicely.
 */
function edgeStyleForStrength(strength: number, accent: RGB, weak: RGB): EdgeStyle {
	const t = Math.pow(Math.max(0, Math.min(1, strength)), 0.8);
	return {
		color: rgbToInt(mixRgb(weak, accent, t)),
		alpha: 0.25 + 0.75 * t,
		widthCss: 1.6 + 3.4 * t,
	};
}

/** Resolve the effective background color behind the graph (for the dim layer). */
function resolveBackgroundRgb(el: HTMLElement): RGB {
	let cur: HTMLElement | null = el;
	for (let i = 0; cur && i < 12; i++) {
		const bg = getComputedStyle(cur).backgroundColor;
		if (bg && bg !== "transparent" && !/rgba?\([^)]*[,/]\s*0\s*\)$/.test(bg)) {
			const rgb = parseCssColor(bg);
			if (rgb) return rgb;
		}
		cur = cur.parentElement;
	}
	return el.ownerDocument.body.classList.contains("theme-light")
		? { r: 255, g: 255, b: 255 }
		: { r: 30, g: 30, b: 30 };
}

function resolveTextRgb(el: HTMLElement): RGB {
	const c = parseCssColor(getComputedStyle(el).color);
	if (c) return c;
	return el.ownerDocument.body.classList.contains("theme-light")
		? { r: 34, g: 34, b: 34 }
		: { r: 220, g: 220, b: 220 };
}

// ---------------------------------------------------------------------------
// PIXI compat helpers — support the v7 API (current) with a v8 fallback, since
// we operate on scavenged Graphics instances of whatever PIXI Obsidian ships.
// ---------------------------------------------------------------------------

/* eslint-disable @typescript-eslint/no-explicit-any */

function gfxFillCircle(g: any, x: number, y: number, r: number, color: number, alpha: number): void {
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

function gfxStrokeCircle(
	g: any, x: number, y: number, r: number, width: number, color: number, alpha: number
): void {
	if (typeof g.beginFill === "function") {
		g.lineStyle({ width, color, alpha });
		g.drawCircle(x, y, r);
		g.lineStyle(0);
	} else {
		g.circle(x, y, r);
		g.stroke({ width, color, alpha });
	}
}

function gfxSeg(
	g: any, x1: number, y1: number, x2: number, y2: number,
	width: number, color: number, alpha: number
): void {
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

/** Dashed segment; falls back to solid when a dash count would get silly. */
function gfxDashedSeg(
	g: any, x1: number, y1: number, x2: number, y2: number,
	width: number, color: number, alpha: number, dash: number, gap: number
): void {
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

function gfxFillRect(
	g: any, x: number, y: number, w: number, h: number, color: number, alpha: number
): void {
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

function safeDestroy(obj: any): void {
	try {
		if (obj && !obj.destroyed) obj.destroy({ children: true });
	} catch {
		/* already torn down by the renderer — fine */
	}
}

// ---------------------------------------------------------------------------
// Loose typings for Obsidian's undocumented graph internals
// ---------------------------------------------------------------------------

interface GraphNodeLike {
	id: string;
	x: number;
	y: number;
	weight?: number;
	type?: string;
	rendered?: boolean;
	circle?: any;
	text?: any;
	getSize?: () => number;
	getDisplayText?: () => string;
}

interface GraphLinkLike {
	source?: GraphNodeLike;
	target?: GraphNodeLike;
}

interface GraphRendererLike {
	hanger?: any;
	nodes?: GraphNodeLike[];
	links?: GraphLinkLike[];
	panX: number;
	panY: number;
	scale: number;
	targetScale?: number;
	nodeScale?: number;
	fNodeSizeMult?: number;
	interactiveEl?: HTMLCanvasElement;
	setPan?: (x: number, y: number) => void;
	setScale?: (s: number) => void;
	changed?: () => void;
}

type GraphViewLike = View & { renderer?: GraphRendererLike };

function nodeDisplayName(n: GraphNodeLike): string {
	try {
		const t = n.getDisplayText?.();
		if (t) return t;
	} catch {
		/* fall through */
	}
	const base = n.id.split("/").pop() ?? n.id;
	return base.replace(/\.md$/i, "");
}

function nodeRadius(n: GraphNodeLike, r: GraphRendererLike): number {
	let size: number | undefined;
	try {
		size = n.getSize?.();
	} catch {
		/* fall through */
	}
	if (typeof size !== "number" || !isFinite(size)) {
		// Mirror of the renderer's own formula, as a fallback.
		const w = typeof n.weight === "number" ? n.weight : 1;
		size = (r.fNodeSizeMult ?? 1) * Math.max(8, Math.min(3 * Math.sqrt(w + 1), 30));
	}
	return size * (typeof r.nodeScale === "number" ? r.nodeScale : 1);
}

function distToSegment(
	px: number, py: number, x1: number, y1: number, x2: number, y2: number
): number {
	const dx = x2 - x1, dy = y2 - y1;
	const l2 = dx * dx + dy * dy;
	if (l2 === 0) return Math.hypot(px - x1, py - y1);
	let t = ((px - x1) * dx + (py - y1) * dy) / l2;
	t = Math.max(0, Math.min(1, t));
	return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

function waitFor(cond: () => boolean, timeoutMs: number, stepMs = 100): Promise<boolean> {
	return new Promise((resolve) => {
		const start = Date.now();
		const tick = (): void => {
			let ok = false;
			try {
				ok = cond();
			} catch {
				ok = false;
			}
			if (ok) return resolve(true);
			if (Date.now() - start >= timeoutMs) return resolve(false);
			window.setTimeout(tick, stepMs);
		};
		tick();
	});
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

interface AIGHSettings {
	accentColor: string;
	dimStrength: number;
	timeoutMinutes: number;
	autoOpenGraph: boolean;
	centerOnNode: boolean;
	pulse: boolean;
	showNodeLabels: boolean;
	showEdgeLabels: boolean;
	notifyOnNewData: boolean;
	autoVisualizeOnChange: boolean;
	applySearchFilter: boolean;
}

const DEFAULT_SETTINGS: AIGHSettings = {
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
	applySearchFilter: false,
};

// ---------------------------------------------------------------------------
// HighlightController — one active highlight session on one graph view
// ---------------------------------------------------------------------------

interface ResolvedEdge {
	from: string;
	to: string;
	strength: number;
	label?: string;
	a: GraphNodeLike | null;
	b: GraphNodeLike | null;
	/** Whether a real link exists in the graph — drawn solid; AI-asserted-only edges are dashed. */
	native: boolean;
}

class HighlightController {
	private stopped = false;
	private rafId: number | null = null;
	private frameCount = 0;
	private readonly startTime = performance.now();

	// Scavenged PIXI classes (never bundled — see file header).
	private pixi: { Container: any; Graphics: any; Text: any | null } | null = null;

	// Overlay display objects.
	private dimLayer: any = null;
	private fxLayer: any = null;
	private dimGfx: any = null;
	private edgeGfx: any = null;
	private nodeGfx: any = null;
	private labels = new Map<string, any>();

	// Spec resolution against live renderer nodes.
	private resolvedByName = new Map<string, GraphNodeLike | null>();
	private uniqueNodes: GraphNodeLike[] = [];
	private edgeDraws: ResolvedEdge[] = [];
	private missing: string[] = [];
	private lastNodesRef: GraphNodeLike[] | null = null;
	private lastNodesLen = -1;
	private missingNoticeTimer: number | null = null;
	private missingNoticeShown = false;

	// Theme-derived colors, refreshed periodically so theme switches are picked up.
	private bgInt = 0x1e1e1e;
	private textInt = 0xdcdcdc;

	// Interaction state.
	private pointer: { x: number; y: number } | null = null;
	private boundCanvas: HTMLCanvasElement | null = null;
	private tooltipEl: HTMLElement | null = null;
	private ttLabel: HTMLElement | null = null;
	private ttRoute: HTMLElement | null = null;
	private ttStrength: HTMLElement | null = null;
	private ttBarFill: HTMLElement | null = null;

	// Center-on-node / filter, applied once resolution succeeds.
	private centered: boolean;
	private filterApplied: boolean;
	private prevFilter: string | null = null;
	private filterEl: HTMLInputElement | null = null;

	constructor(
		private readonly plugin: AIGraphHighlighterPlugin,
		private readonly view: GraphViewLike,
		private readonly spec: HighlightSpec
	) {
		this.centered = !(plugin.settings.centerOnNode && spec.center);
		this.filterApplied = !plugin.settings.applySearchFilter;
	}

	private get win(): Window {
		return ((this.view.containerEl as any).win as Window) ?? window;
	}

	attach(): void {
		this.createTooltip();
		this.refreshThemeColors();
		this.scheduleNext();
	}

	detach(): void {
		if (this.stopped) return;
		this.stopped = true;
		if (this.rafId !== null) {
			try {
				this.win.cancelAnimationFrame(this.rafId);
			} catch {
				/* window may be gone (popout closed) */
			}
			this.rafId = null;
		}
		if (this.missingNoticeTimer !== null) {
			window.clearTimeout(this.missingNoticeTimer);
			this.missingNoticeTimer = null;
		}
		this.unbindCanvas();
		this.tooltipEl?.remove();
		this.tooltipEl = null;
		this.restoreFilter();
		safeDestroy(this.dimLayer);
		safeDestroy(this.fxLayer);
		this.dimLayer = this.fxLayer = this.dimGfx = this.edgeGfx = this.nodeGfx = null;
		this.labels.clear();
		try {
			this.view.renderer?.changed?.();
		} catch {
			/* renderer already gone */
		}
	}

	// ------------------------------------------------------------------ frame

	private scheduleNext(): void {
		if (this.stopped) return;
		try {
			this.rafId = this.win.requestAnimationFrame(this.frame);
		} catch {
			this.rafId = window.requestAnimationFrame(this.frame);
		}
	}

	private frame = (): void => {
		if (this.stopped) return;

		// The user navigated away / closed the graph — end the session.
		if (!this.view.containerEl.isConnected) {
			this.plugin.clearHighlight(true);
			return;
		}

		const r = this.view.renderer;
		if (!r || !r.hanger || r.hanger.destroyed) {
			// Renderer mid-rebuild; try again next frame.
			this.scheduleNext();
			return;
		}

		try {
			if (!this.ensurePixi(r)) {
				this.scheduleNext();
				return;
			}
			this.ensureLayers(r);

			const nodes = r.nodes ?? [];
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

			// Keep the renderer producing frames while we animate (it normally idles).
			r.changed?.();
		} catch (e) {
			// Never let an internals change break the graph — log once in a while.
			if (this.frameCount % 300 === 0) console.error("[AI Graph Highlighter] frame error", e);
		}
		this.scheduleNext();
	};

	// ------------------------------------------------------- pixi + overlay

	/** Grab PIXI classes from live objects. Retries until at least one node circle exists. */
	private ensurePixi(r: GraphRendererLike): boolean {
		if (this.pixi) return true;
		const Container = r.hanger?.constructor;
		let Graphics: any = null;
		let Text: any = null;
		for (const n of r.nodes ?? []) {
			if (!Graphics && n.circle) Graphics = n.circle.constructor;
			if (!Text && n.text) Text = n.text.constructor;
			if (Graphics && Text) break;
		}
		if (!Graphics && (r as any).highlight) Graphics = (r as any).highlight.constructor;
		if (!Container || !Graphics) return false;
		this.pixi = { Container, Graphics, Text };
		return true;
	}

	/** (Re)create overlay containers and keep them parented to the current hanger. */
	private ensureLayers(r: GraphRendererLike): void {
		if (!this.pixi) return;
		const { Container, Graphics } = this.pixi;

		if (!this.dimLayer || this.dimLayer.destroyed || !this.fxLayer || this.fxLayer.destroyed) {
			safeDestroy(this.dimLayer);
			safeDestroy(this.fxLayer);
			this.labels.clear(); // label Texts were children of fxLayer

			this.dimLayer = new Container();
			this.dimLayer.zIndex = 3; // above native circles (0), hover ring (1), labels (2)
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

	private refreshThemeColors(): void {
		try {
			this.bgInt = rgbToInt(resolveBackgroundRgb(this.view.containerEl));
			this.textInt = rgbToInt(resolveTextRgb(this.view.containerEl));
		} catch {
			/* keep previous */
		}
	}

	// ------------------------------------------------------------- resolution

	/** Map spec names to live graph nodes. Re-run whenever the node set changes. */
	private resolveSpec(r: GraphRendererLike): void {
		const nodes = r.nodes ?? [];
		this.lastNodesRef = nodes;
		this.lastNodesLen = nodes.length;

		const byExact = new Map<string, GraphNodeLike>();
		const byLower = new Map<string, GraphNodeLike>();
		const byBase = new Map<string, GraphNodeLike>();
		for (const n of nodes) {
			if (typeof n.id !== "string") continue;
			if (!byExact.has(n.id)) byExact.set(n.id, n);
			const lower = n.id.toLowerCase();
			if (!byLower.has(lower)) byLower.set(lower, n);
			const base = (n.id.split("/").pop() ?? n.id).replace(/\.md$/i, "").toLowerCase();
			if (!byBase.has(base)) byBase.set(base, n);
		}

		const resolveName = (name: string): GraphNodeLike | null => {
			const exact = byExact.get(name);
			if (exact) return exact;
			// Resolve like a wikilink would (handles bare names, subfolders, missing .md).
			const dest = this.plugin.app.metadataCache.getFirstLinkpathDest(
				name.replace(/\.md$/i, ""), ""
			);
			if (dest) {
				const n = byExact.get(dest.path) ?? byLower.get(dest.path.toLowerCase());
				if (n) return n;
			}
			return (
				byExact.get(name + ".md") ??
				byLower.get(name.toLowerCase()) ??
				byLower.get(name.toLowerCase() + ".md") ??
				byBase.get((name.split("/").pop() ?? name).replace(/\.md$/i, "").toLowerCase()) ??
				null
			);
		};

		this.resolvedByName.clear();
		for (const name of this.spec.nodes) this.resolvedByName.set(name, resolveName(name));

		const uniq = new Map<string, GraphNodeLike>();
		this.missing = [];
		for (const [name, n] of this.resolvedByName) {
			if (n) uniq.set(n.id, n);
			else this.missing.push(name);
		}
		this.uniqueNodes = [...uniq.values()];

		const nativeKeys = new Set<string>();
		for (const l of r.links ?? []) {
			const s = l.source?.id, t = l.target?.id;
			if (s && t) {
				nativeKeys.add(s + "|" + t);
				nativeKeys.add(t + "|" + s);
			}
		}
		this.edgeDraws = this.spec.edges.map((e) => {
			const a = this.resolvedByName.get(e.from) ?? null;
			const b = this.resolvedByName.get(e.to) ?? null;
			return {
				...e,
				a,
				b,
				native: !!(a && b && nativeKeys.has(a.id + "|" + b.id)),
			};
		});

		// Report names that never resolve, but give a freshly-opened graph a moment
		// to stream its nodes in first.
		if (this.missingNoticeTimer === null && !this.missingNoticeShown) {
			this.missingNoticeTimer = window.setTimeout(() => {
				this.missingNoticeTimer = null;
				this.missingNoticeShown = true;
				if (this.stopped || this.missing.length === 0) return;
				const shown = this.missing.slice(0, 6).map((s) => `"${s}"`).join(", ");
				const extra = this.missing.length > 6 ? ` (+${this.missing.length - 6} more)` : "";
				new Notice(
					`AI Graph Highlighter: ${this.missing.length} note(s) not found in the graph: ${shown}${extra}. Check spelling and graph filters.`,
					8000
				);
			}, 2500);
		}
	}

	// -------------------------------------------------------------- centering

	private maybeCenter(r: GraphRendererLike): void {
		const name = this.spec.center;
		if (!name) {
			this.centered = true;
			return;
		}
		const elapsed = performance.now() - this.startTime;
		const n = this.resolvedByName.get(name) ?? null;
		if (!n) {
			if (elapsed > 6000) this.centered = true; // give up quietly
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
				(r as any).targetScale = s;
				r.setScale?.(s);
			}
			r.setPan?.(w / 2 - n.x * s, h / 2 - n.y * s);
			r.changed?.();
		} catch {
			this.centered = true;
			return;
		}
		// Track the node briefly while force-layout settles, then release the camera.
		if (elapsed > 1500) this.centered = true;
	}

	// ---------------------------------------------------------- search filter

	private maybeApplyFilter(): void {
		if (this.uniqueNodes.length === 0) {
			if (performance.now() - this.startTime > 6000) this.filterApplied = true;
			return;
		}
		this.filterApplied = true;
		try {
			const input = this.view.containerEl.querySelector<HTMLInputElement>(
				[
					".graph-controls .search-input-container input",
					".graph-control-section.mod-filter input",
					".graph-controls input[type='search']",
					".graph-controls input[type='text']",
				].join(", ")
			);
			if (!input) {
				console.warn("[AI Graph Highlighter] Graph filter input not found; skipping isolation.");
				return;
			}
			const parts = this.uniqueNodes
				.filter((n) => n.type !== "unresolved" && n.type !== "tag")
				.map((n) => `path:"${n.id}"`);
			if (parts.length === 0) return;
			const query = parts.join(" OR ");
			if (query.length > 4000) {
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

	private restoreFilter(): void {
		if (!this.filterEl) return;
		try {
			if (this.filterEl.isConnected) {
				this.filterEl.value = this.prevFilter ?? "";
				this.filterEl.dispatchEvent(new Event("input", { bubbles: true }));
			}
		} catch {
			/* view already closed */
		}
		this.filterEl = null;
		this.prevFilter = null;
	}

	// ----------------------------------------------------------------- drawing

	private accentRgb(): RGB {
		return parseCssColor(this.plugin.settings.accentColor) ?? parseCssColor(DEFAULT_SETTINGS.accentColor)!;
	}

	/** Dim everything: a translucent background-colored sheet over the whole viewport. */
	private drawDim(r: GraphRendererLike): void {
		if (!this.dimGfx || this.dimGfx.destroyed) return;
		this.dimGfx.clear();
		const dim = this.plugin.settings.dimStrength;
		if (dim <= 0) return;
		const el = r.interactiveEl;
		if (!el) return;
		const dpr = this.win.devicePixelRatio || 1;
		const s = r.scale || 1;
		// Viewport rect in graph units (hanger space): graph = (physicalPx - pan) / scale.
		const left = -r.panX / s;
		const top = -r.panY / s;
		const w = (el.clientWidth * dpr) / s;
		const h = (el.clientHeight * dpr) / s;
		const mx = w * 0.25, my = h * 0.25;
		gfxFillRect(this.dimGfx, left - mx, top - my, w + 2 * mx, h + 2 * my, this.bgInt, dim);
	}

	private drawEdges(r: GraphRendererLike): void {
		if (!this.edgeGfx || this.edgeGfx.destroyed) return;
		this.edgeGfx.clear();
		const accent = this.accentRgb();
		const weak = weakVariant(accent);
		const dpr = this.win.devicePixelRatio || 1;
		const s = r.scale || 1;
		const px = dpr / s; // one CSS pixel, in graph units

		for (const e of this.edgeDraws) {
			const { a, b } = e;
			if (!a || !b || a === b || a.rendered === false || b.rendered === false) continue;
			const st = edgeStyleForStrength(e.strength, accent, weak);
			const w = st.widthCss * px;
			// Two soft glow passes under a crisp core read as a neon gradient.
			gfxSeg(this.edgeGfx, a.x, a.y, b.x, b.y, w * 3.4, st.color, st.alpha * 0.16);
			gfxSeg(this.edgeGfx, a.x, a.y, b.x, b.y, w * 1.8, st.color, st.alpha * 0.4);
			if (e.native) {
				gfxSeg(this.edgeGfx, a.x, a.y, b.x, b.y, w, st.color, st.alpha);
			} else {
				// AI-asserted relationship with no actual link in the vault → dashed.
				gfxDashedSeg(this.edgeGfx, a.x, a.y, b.x, b.y, w, st.color, st.alpha, 9 * px, 6 * px);
			}
		}
	}

	private drawNodes(r: GraphRendererLike): void {
		if (!this.nodeGfx || this.nodeGfx.destroyed) return;
		this.nodeGfx.clear();
		const accent = this.accentRgb();
		const accentInt = rgbToInt(accent);
		const coreInt = rgbToInt(lighten(accent, 0.35));
		const dpr = this.win.devicePixelRatio || 1;
		const s = r.scale || 1;
		const px = dpr / s;
		const pulse = this.plugin.settings.pulse
			? 0.5 + 0.5 * Math.sin(performance.now() / 260)
			: 0.75;

		for (const n of this.uniqueNodes) {
			if (n.rendered === false) continue;
			const base = nodeRadius(n, r);
			// Outer halo (pulsing) → inner ring → accent fill → crisp rim.
			gfxStrokeCircle(
				this.nodeGfx, n.x, n.y,
				base * (1.35 + 0.12 * pulse), 5 * px, accentInt, 0.1 + 0.1 * pulse
			);
			gfxStrokeCircle(
				this.nodeGfx, n.x, n.y,
				base * (1.2 + 0.06 * pulse), 3 * px, accentInt, 0.28 + 0.14 * pulse
			);
			gfxFillCircle(this.nodeGfx, n.x, n.y, base * 1.1, accentInt, 0.92);
			gfxStrokeCircle(this.nodeGfx, n.x, n.y, base * 1.1, 1.75 * px, coreInt, 1);
		}
	}

	private updateLabels(r: GraphRendererLike): void {
		if (!this.fxLayer || this.fxLayer.destroyed) return;
		const enabled =
			this.plugin.settings.showNodeLabels && !!this.pixi?.Text && this.uniqueNodes.length <= 80;
		if (!enabled) {
			for (const t of this.labels.values()) t.visible = false;
			return;
		}
		const dpr = this.win.devicePixelRatio || 1;
		const s = r.scale || 1;
		const px = dpr / s;
		const current = new Set<string>();
		let fontFamily = "sans-serif";
		try {
			fontFamily = getComputedStyle(this.view.containerEl).fontFamily || fontFamily;
		} catch {
			/* keep default */
		}

		for (const n of this.uniqueNodes) {
			current.add(n.id);
			let t = this.labels.get(n.id);
			if (!t || t.destroyed) {
				try {
					t = new this.pixi!.Text(nodeDisplayName(n), {
						fontFamily,
						fontSize: 13,
						fontWeight: "600",
						fill: this.textInt,
						stroke: this.bgInt,
						strokeThickness: 3,
						align: "center",
					});
					t.anchor?.set?.(0.5, 0);
					t.resolution = 2;
					t.eventMode = "none";
					this.fxLayer.addChild(t);
					this.labels.set(n.id, t);
				} catch (e) {
					// PIXI Text API mismatch — disable labels for this session.
					console.warn("[AI Graph Highlighter] Labels unavailable", e);
					this.pixi!.Text = null;
					return;
				}
			}
			t.visible = n.rendered !== false;
			if (!t.visible) continue;
			// Sit exactly where the native (now dimmed) label sits when it exists,
			// otherwise just below the halo.
			const nt = n.text;
			if (nt && !nt.destroyed && isFinite(nt.x) && isFinite(nt.y)) {
				t.x = nt.x;
				t.y = nt.y;
			} else {
				t.x = n.x;
				t.y = n.y + nodeRadius(n, r) * 1.2 + 5 * px;
			}
			t.scale?.set?.(px); // constant on-screen size regardless of zoom
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

	private createTooltip(): void {
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

	private onPointerMove = (ev: PointerEvent): void => {
		this.pointer = { x: ev.offsetX, y: ev.offsetY };
	};
	private onPointerLeave = (): void => {
		this.pointer = null;
	};

	private bindCanvas(r: GraphRendererLike): void {
		const el = r.interactiveEl ?? null;
		if (el === this.boundCanvas) return;
		this.unbindCanvas();
		if (!el) return;
		el.addEventListener("pointermove", this.onPointerMove);
		el.addEventListener("pointerdown", this.onPointerMove); // mobile tap
		el.addEventListener("pointerleave", this.onPointerLeave);
		this.boundCanvas = el;
	}

	private unbindCanvas(): void {
		const el = this.boundCanvas;
		if (!el) return;
		el.removeEventListener("pointermove", this.onPointerMove);
		el.removeEventListener("pointerdown", this.onPointerMove);
		el.removeEventListener("pointerleave", this.onPointerLeave);
		this.boundCanvas = null;
	}

	private updateTooltip(r: GraphRendererLike): void {
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

		let best: ResolvedEdge | null = null;
		let bestD = Infinity;
		for (const e of this.edgeDraws) {
			const { a, b } = e;
			if (!a || !b || a === b || a.rendered === false || b.rendered === false) continue;
			// Near an endpoint the native node tooltip should win.
			if (
				Math.hypot(gx - a.x, gy - a.y) < nodeRadius(a, r) * 1.3 ||
				Math.hypot(gx - b.x, gy - b.y) < nodeRadius(b, r) * 1.3
			)
				continue;
			const d = distToSegment(gx, gy, a.x, a.y, b.x, b.y);
			const threshold = (Math.max(10, edgeStyleForStrength(e.strength, { r: 0, g: 0, b: 0 }, { r: 0, g: 0, b: 0 }).widthCss * 1.5) * dpr) / s;
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
		this.ttLabel!.setText(best.label ?? `${aName} ↔ ${bName}`);
		this.ttRoute!.setText(`${aName} ⟷ ${bName}${best.native ? "" : " · no direct link in vault"}`);
		this.ttStrength!.setText(`Strength ${Math.round(best.strength * 100)}%`);
		this.ttBarFill!.style.width = `${Math.round(best.strength * 100)}%`;
		tt.style.setProperty("--aigh-accent", this.plugin.settings.accentColor);

		// Position near the cursor, clamped inside the view.
		tt.addClass("aigh-visible");
		const host = this.view.containerEl;
		const maxX = host.clientWidth - tt.offsetWidth - 8;
		const maxY = host.clientHeight - tt.offsetHeight - 8;
		tt.style.left = `${Math.max(8, Math.min(p.x + 16, maxX))}px`;
		tt.style.top = `${Math.max(8, Math.min(p.y + 18, maxY))}px`;
	}
}

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

export default class AIGraphHighlighterPlugin extends Plugin {
	settings: AIGHSettings = { ...DEFAULT_SETTINGS };

	private controller: HighlightController | null = null;
	private timeoutTimer: number | null = null;
	private lastSeenMtime = 0;
	private watcherBusy = false;

	get pendingPath(): string {
		const dir = this.manifest.dir ?? `${this.app.vault.configDir}/plugins/${this.manifest.id}`;
		return normalizePath(`${dir}/pending-highlight.json`);
	}

	async onload(): Promise<void> {
		await this.loadSettings();
		this.addSettingTab(new AIGHSettingTab(this.app, this));

		this.addRibbonIcon("sparkles", "AI Graph Highlighter: Visualize pending highlight", () => {
			void this.visualizePending();
		});

		this.addCommand({
			id: "visualize-pending-highlight",
			name: "Visualize pending highlight",
			callback: () => void this.visualizePending(),
		});
		this.addCommand({
			id: "clear-highlight",
			name: "Clear highlight",
			callback: () => this.clearHighlight(),
		});

		// obsidian://ai-graph-highlighter          → visualize
		// obsidian://ai-graph-highlighter?clear    → clear
		// Lets an external AI/CLI trigger the visualization deterministically.
		this.registerObsidianProtocolHandler("ai-graph-highlighter", (params) => {
			if ("clear" in params) this.clearHighlight();
			else void this.visualizePending();
		});

		// Watch pending-highlight.json. The vault API doesn't emit events for files
		// under the config dir, so poll mtime via the adapter (cheap: one stat / 2 s).
		void this.initWatcher();

		this.register(() => this.clearHighlight(true));
	}

	private async initWatcher(): Promise<void> {
		try {
			const st = await this.app.vault.adapter.stat(this.pendingPath);
			this.lastSeenMtime = st?.mtime ?? 0;
		} catch {
			this.lastSeenMtime = 0;
		}
		this.registerInterval(window.setInterval(() => void this.pollPending(), 2000));
	}

	private async pollPending(): Promise<void> {
		if (this.watcherBusy) return;
		if (!this.settings.notifyOnNewData && !this.settings.autoVisualizeOnChange) return;
		this.watcherBusy = true;
		try {
			const st = await this.app.vault.adapter.stat(this.pendingPath);
			const mtime = st?.mtime ?? 0;
			if (mtime && mtime !== this.lastSeenMtime) {
				this.lastSeenMtime = mtime;
				if (this.settings.autoVisualizeOnChange) {
					void this.visualizePending();
				} else if (this.settings.notifyOnNewData) {
					let suffix = "";
					try {
						const parsed = JSON.parse(stripBom(await this.app.vault.adapter.read(this.pendingPath)));
						if (parsed && typeof parsed.title === "string" && parsed.title.trim())
							suffix = ` — “${parsed.title.trim().slice(0, 80)}”`;
					} catch {
						/* title is best-effort */
					}
					new Notice(
						`AI Graph Highlighter: new highlight data${suffix}. Click the ✨ ribbon icon or run “Visualize pending highlight”.`,
						8000
					);
				}
			}
		} catch {
			/* transient fs error — retry next poll */
		} finally {
			this.watcherBusy = false;
		}
	}

	async visualizePending(): Promise<void> {
		const adapter = this.app.vault.adapter;
		const path = this.pendingPath;

		let raw: string;
		try {
			if (!(await adapter.exists(path))) {
				new Notice(
					`AI Graph Highlighter: no highlight data found.\nExpected file: ${path}`,
					8000
				);
				return;
			}
			raw = await adapter.read(path);
		} catch (e) {
			new Notice(`AI Graph Highlighter: could not read ${path}: ${(e as Error).message}`, 8000);
			return;
		}

		let json: unknown;
		try {
			json = JSON.parse(stripBom(raw));
		} catch (e) {
			new Notice(
				`AI Graph Highlighter: pending-highlight.json is not valid JSON.\n${(e as Error).message}`,
				10000
			);
			return;
		}

		const { spec, errors, warnings } = validatePendingHighlight(json);
		if (!spec) {
			new Notice(`AI Graph Highlighter: invalid highlight data:\n• ${errors.join("\n• ")}`, 10000);
			return;
		}
		if (warnings.length > 0) {
			console.warn("[AI Graph Highlighter] data warnings:", warnings);
			new Notice(
				`AI Graph Highlighter: ${warnings.length} data warning(s) — see developer console.`,
				5000
			);
		}

		// Keep track of what we've visualized so the file watcher doesn't re-announce it.
		try {
			const st = await adapter.stat(path);
			if (st?.mtime) this.lastSeenMtime = st.mtime;
		} catch {
			/* non-fatal */
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
				new Notice("AI Graph Highlighter: highlight auto-cleared (timeout).");
			}, this.settings.timeoutMinutes * 60_000);
		}

		const timeoutNote =
			this.settings.timeoutMinutes > 0 ? ` Auto-clears in ${this.settings.timeoutMinutes} min.` : "";
		new Notice(
			`⚡ ${spec.title ?? "AI highlight"}: ${spec.nodes.length} note(s), ${spec.edges.length} connection(s).${timeoutNote}`,
			6000
		);
	}

	/** Find (or open) a graph view and wait for its renderer to be ready. */
	private async ensureGraphView(): Promise<GraphViewLike | null> {
		const ws = this.app.workspace;
		let leaf: WorkspaceLeaf | null = null;

		const recent = ws.getMostRecentLeaf();
		const recentType = recent?.view?.getViewType();
		if (recent && (recentType === "graph" || recentType === "localgraph")) {
			leaf = recent;
		} else {
			leaf = ws.getLeavesOfType("graph")[0] ?? ws.getLeavesOfType("localgraph")[0] ?? null;
		}

		if (!leaf) {
			if (!this.settings.autoOpenGraph) {
				new Notice(
					"AI Graph Highlighter: no Graph view is open. Open one, or enable “Auto-open Graph view” in settings."
				);
				return null;
			}
			leaf = ws.getLeaf("tab");
			await leaf.setViewState({ type: "graph", active: true });
		}

		await ws.revealLeaf(leaf);
		const view = leaf.view as GraphViewLike;
		const ready = await waitFor(() => !!view.renderer?.hanger, 5000, 100);
		if (!ready) {
			new Notice("AI Graph Highlighter: the Graph view did not finish loading. Try again.");
			return null;
		}
		return view;
	}

	clearHighlight(silent = false): void {
		if (this.timeoutTimer !== null) {
			window.clearTimeout(this.timeoutTimer);
			this.timeoutTimer = null;
		}
		if (this.controller) {
			this.controller.detach();
			this.controller = null;
			if (!silent) new Notice("AI Graph Highlighter: highlight cleared.");
		}
	}

	async loadSettings(): Promise<void> {
		this.settings = { ...DEFAULT_SETTINGS, ...((await this.loadData()) ?? {}) };
	}

	async saveSettings(): Promise<void> {
		await this.saveData(this.settings);
	}
}

// ---------------------------------------------------------------------------
// Settings tab
// ---------------------------------------------------------------------------

class AIGHSettingTab extends PluginSettingTab {
	constructor(app: App, private readonly plugin: AIGraphHighlighterPlugin) {
		super(app, plugin);
	}

	display(): void {
		const { containerEl } = this;
		containerEl.empty();

		const save = () => void this.plugin.saveSettings();

		// -- Appearance ------------------------------------------------------
		new Setting(containerEl).setName("Appearance").setHeading();

		const colorSetting = new Setting(containerEl)
			.setName("Accent color")
			.setDesc(
				"Highlight color for nodes and edges. Edges fade from a pale version (strength 0) to this exact color (strength 1)."
			);
		const preview = createSpan({ cls: "aigh-settings-preview" });
		preview.style.setProperty("--aigh-accent", this.plugin.settings.accentColor);
		colorSetting.addColorPicker((cp) =>
			cp.setValue(this.plugin.settings.accentColor).onChange((v) => {
				this.plugin.settings.accentColor = v;
				preview.style.setProperty("--aigh-accent", v);
				save();
			})
		);
		colorSetting.controlEl.appendChild(preview);

		new Setting(containerEl)
			.setName("Dim strength")
			.setDesc("How strongly the rest of the graph is dimmed while a highlight is active.")
			.addSlider((sl) =>
				sl
					.setLimits(0, 0.9, 0.05)
					.setValue(this.plugin.settings.dimStrength)
					.setDynamicTooltip()
					.onChange((v) => {
						this.plugin.settings.dimStrength = v;
						save();
					})
			);

		new Setting(containerEl)
			.setName("Pulse animation")
			.setDesc("Subtle pulsing glow on highlighted nodes.")
			.addToggle((t) =>
				t.setValue(this.plugin.settings.pulse).onChange((v) => {
					this.plugin.settings.pulse = v;
					save();
				})
			);

		new Setting(containerEl)
			.setName("Bright labels on highlighted notes")
			.setDesc("Redraw the names of highlighted notes at full brightness above the dim layer.")
			.addToggle((t) =>
				t.setValue(this.plugin.settings.showNodeLabels).onChange((v) => {
					this.plugin.settings.showNodeLabels = v;
					save();
				})
			);

		new Setting(containerEl)
			.setName("Edge labels on hover")
			.setDesc("Show the AI's explanation of a connection when hovering a highlighted edge.")
			.addToggle((t) =>
				t.setValue(this.plugin.settings.showEdgeLabels).onChange((v) => {
					this.plugin.settings.showEdgeLabels = v;
					save();
				})
			);

		// -- Behavior --------------------------------------------------------
		new Setting(containerEl).setName("Behavior").setHeading();

		new Setting(containerEl)
			.setName("Auto-clear timeout (minutes)")
			.setDesc("Highlights clear themselves after this long. 0 = never.")
			.addSlider((sl) =>
				sl
					.setLimits(0, 60, 1)
					.setValue(this.plugin.settings.timeoutMinutes)
					.setDynamicTooltip()
					.onChange((v) => {
						this.plugin.settings.timeoutMinutes = v;
						save();
					})
			);

		new Setting(containerEl)
			.setName("Auto-open Graph view")
			.setDesc("Open the global Graph view automatically when visualizing, if none is open.")
			.addToggle((t) =>
				t.setValue(this.plugin.settings.autoOpenGraph).onChange((v) => {
					this.plugin.settings.autoOpenGraph = v;
					save();
				})
			);

		new Setting(containerEl)
			.setName("Center on focus node")
			.setDesc("Pan/zoom to the highlight's “center” node when applying.")
			.addToggle((t) =>
				t.setValue(this.plugin.settings.centerOnNode).onChange((v) => {
					this.plugin.settings.centerOnNode = v;
					save();
				})
			);

		new Setting(containerEl)
			.setName("Isolate with search filter (experimental)")
			.setDesc(
				"Also rewrite the graph's search filter to show only highlighted notes. Your previous filter is restored when the highlight clears."
			)
			.addToggle((t) =>
				t.setValue(this.plugin.settings.applySearchFilter).onChange((v) => {
					this.plugin.settings.applySearchFilter = v;
					save();
				})
			);

		// -- Integration -----------------------------------------------------
		new Setting(containerEl).setName("AI integration").setHeading();

		new Setting(containerEl)
			.setName("Notify when new highlight data arrives")
			.setDesc("Show a notice when the AI writes a new pending-highlight.json.")
			.addToggle((t) =>
				t.setValue(this.plugin.settings.notifyOnNewData).onChange((v) => {
					this.plugin.settings.notifyOnNewData = v;
					save();
				})
			);

		new Setting(containerEl)
			.setName("Auto-visualize when new data arrives")
			.setDesc("Skip the notice and immediately visualize whenever the AI updates the file.")
			.addToggle((t) =>
				t.setValue(this.plugin.settings.autoVisualizeOnChange).onChange((v) => {
					this.plugin.settings.autoVisualizeOnChange = v;
					save();
				})
			);

		new Setting(containerEl)
			.setName("Data file")
			.setDesc(`The AI should write highlight JSON to: ${this.plugin.pendingPath}`);
	}
}
