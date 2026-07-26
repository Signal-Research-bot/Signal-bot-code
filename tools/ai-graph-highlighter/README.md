# AI Graph Highlighter

> **Not part of the Signal research bot.** This is a standalone Obsidian plugin
> published from the same repository for convenience. It has no connection to
> the bot's pipeline: it makes no network calls, touches no Signal data, and is
> covered by none of the privacy claims in the root `README.md` or `PRIVACY.md`.
> The bot's test suite does not exercise it.
>
> If you came here to audit what the bot does with your messages, this directory
> is not it — read `signal_research_bot/egress.py` and `PRIVACY.md`.
>
> **Install:** copy `main.js`, `manifest.json` and `styles.css` into
> `<your vault>/.obsidian/plugins/ai-graph-highlighter/` and enable it in
> Obsidian's community-plugins settings. Or build from source with
> `npm install && npm run build`.

An Obsidian plugin that lets an AI assistant (or any external tool that can write a file)
hand you a set of notes and **weighted** connections, then light them up inside Obsidian's
Graph view with one click:

- **Highlighted nodes** are drawn in a bright accent color with a pulsing glow and a
  full-brightness label, while the rest of the graph is dimmed underneath.
- **Highlighted edges** use a gradient of the same accent color driven by `strength`
  (0.0–1.0): strong relationships at `0.95` render thick, fully opaque, and brightest;
  weak associations at `0.15` render thin, pale, and translucent. Opacity, brightness,
  and width all interpolate smoothly.
- Edges that don't correspond to an actual link between the two notes are drawn
  **dashed** — useful when the AI asserts a relationship your vault hasn't yet linked.
- **Hover a highlighted edge** to see the AI's explanation (`label`) and the strength.

Everything is a non-destructive overlay: your graph settings, colors, and filters are
untouched, and clearing the highlight (or the auto-clear timeout) restores the normal
appearance exactly.

---

## Installation

The plugin lives in `.obsidian/plugins/ai-graph-highlighter/`. Obsidian only needs
three of the files: `manifest.json`, `main.js`, `styles.css` (the rest is TypeScript
source and build tooling).

1. Copy this folder to `<your vault>/.obsidian/plugins/ai-graph-highlighter/`
   (already done if you are reading this inside your vault).
2. In Obsidian: **Settings → Community plugins** → make sure Restricted mode is off →
   enable **AI Graph Highlighter**. If Obsidian was already running, run
   "Reload app without saving" from the command palette first so it picks up the
   new folder.
3. You should see a ✨ (sparkles) icon in the left ribbon.

### Building from source

```bash
cd .obsidian/plugins/ai-graph-highlighter
npm install
npm run build     # type-checks with tsc, bundles main.ts → main.js with esbuild
```

`npm run dev` starts esbuild in watch mode for development.

---

## How to use it

1. The AI writes highlight data to
   `.obsidian/plugins/ai-graph-highlighter/pending-highlight.json` (schema below).
   The plugin polls the file every 2 seconds and shows a notice when new data arrives.
2. Click the ✨ ribbon icon, or run **"AI Graph Highlighter: Visualize pending
   highlight"** from the command palette. The Graph view opens (or is focused), pans
   to the `center` node, and the highlight appears.
3. Clear it with **"AI Graph Highlighter: Clear highlight"**, or just wait — it
   auto-clears after 10 minutes (configurable), and also clears itself if you close
   the graph.

An example `pending-highlight.json` showing software ecosystem relationships is included
in this folder. Try it immediately after enabling the plugin to see the highlighting in action.

### External triggering

`obsidian://ai-graph-highlighter` visualizes the pending file;
`obsidian://ai-graph-highlighter?clear` clears. From a shell:

```
start "" "obsidian://ai-graph-highlighter"     (Windows)
open "obsidian://ai-graph-highlighter"         (macOS)
```

There is also a setting to **auto-visualize whenever the file changes**, which makes
the flow fully hands-free: the AI writes the file, and ~2 s later the graph lights up.

---

## JSON schema (what the AI writes)

File: `.obsidian/plugins/ai-graph-highlighter/pending-highlight.json`

```jsonc
{
  "title": "Example relationship network",    // optional, shown in notices
  "center": "Node Name",                       // optional, node to pan/zoom to
  "nodes": [
    "Node Name",                               // note basename…
    "Folder/Note Name.md"                      // …or full vault path, both fine
  ],
  "edges": [
    {
      "from": "Source Node",
      "to": "Target Node",
      "strength": 0.95,                        // REQUIRED, 0.0–1.0
      "label": "Description of the relationship"
    },
    {
      "from": "Source Node",
      "to": "Another Node",
      "strength": 0.3,
      "label": "Weaker relationship"
    }
  ]
}
```

Rules and behavior:

- `strength` drives the gradient: `1.0` → fully opaque / brightest / thickest,
  `0.0` → very pale / translucent / thin. Values are clamped to [0, 1]; a missing
  strength falls back to `0.5` with a warning.
- Node names resolve like wikilinks: basename, basename + `.md`, or full path all
  work, case-insensitively. Edge endpoints are automatically added to the node set,
  so a pure-edges file is valid.
- Unknown notes never break the highlight — the rest renders, and a notice lists
  what couldn't be found (after a grace period, since a freshly opened graph streams
  its nodes in asynchronously).
- Malformed JSON, wrong types, oversized labels etc. produce clear notices/warnings
  instead of failures.

### Suggested strength rubric for the AI

| Strength  | Meaning                                                  |
| --------- | -------------------------------------------------------- |
| 0.9 – 1.0 | Primary relationship, critical dependency, direct control |
| 0.7 – 0.9 | Strong connection, core collaboration, major influence    |
| 0.4 – 0.7 | Significant relationship, meaningful collaboration        |
| 0.2 – 0.4 | Secondary relationship, minor connection, indirect link   |
| 0.0 – 0.2 | Weak association, passing mention, unconfirmed rumor      |

Adapt this to your domain: adjust labels and thresholds based on whether you're
mapping research topics, people networks, projects, or other structures.

### Prompt snippet you can give an AI assistant

> To visualize connections in the graph, write a JSON file to
> `.obsidian/plugins/ai-graph-highlighter/pending-highlight.json` with keys
> `title`, `center`, `nodes` (array of note names) and `edges` (array of
> `{from, to, strength (0–1, required), label}`). Assign `strength` based on the
> importance or closeness of each relationship (0.95 for a primary relationship,
> 0.15 for a weak association). Then tell the user to click the ✨ icon or run
> `obsidian://ai-graph-highlighter` in a shell to visualize.

---

## Settings

| Setting | Default | Notes |
| --- | --- | --- |
| Accent color | `#00e5ff` (electric cyan) | Color picker; strong contrast on light & dark themes |
| Dim strength | 0.65 | 0 disables dimming of the non-highlighted graph |
| Pulse animation | on | Subtle glow pulse on highlighted nodes |
| Bright labels | on | Redraws highlighted notes' names above the dim layer |
| Edge labels on hover | on | Tooltip with the AI's `label` + strength meter |
| Auto-clear timeout | 10 min | 0 = never |
| Auto-open Graph view | on | Opens the global graph if none is open |
| Center on focus node | on | Pans/zooms to `center` while the layout settles |
| Isolate with search filter | off | Experimental: temporarily rewrites the graph search filter to only the highlighted notes; restored on clear |
| Notify on new data | on | Notice when the AI updates the JSON |
| Auto-visualize on new data | off | Fully hands-free mode |

Works on the global Graph view and on Local graphs (highlight targets whichever
graph is active). Desktop and mobile.

---

## How it works / maintenance notes

Obsidian's graph has no public styling API, so the plugin draws overlays with the
same PIXI (v7) instance Obsidian itself uses — verified against Obsidian 1.12.7:

- The graph renderer exposes a `hanger` container whose transform is pan/zoom;
  node objects carry live `x`/`y` positions in "graph units".
- The plugin adds two of its own containers to the hanger (dim layer + fx layer,
  zIndex above native circles/labels) and redraws them every animation frame from
  live node positions. Native display objects are **never modified**.
- PIXI classes are scavenged from live objects (`hanger.constructor`,
  `node.circle.constructor`) rather than bundling `pixi.js`, so the plugin can't
  clash with Obsidian's renderer version. Drawing goes through small compat helpers
  that also support the PIXI v8 API in case a future Obsidian upgrades.
- Everything self-heals: if the renderer rebuilds its scene (filters, settings,
  theme change) the overlay notices destroyed/orphaned containers and rebuilds.
  All internal access is defensively guarded — if a future Obsidian renames these
  internals, the highlight degrades to a no-op instead of breaking the graph.

Uninstall by disabling the plugin and deleting the folder; nothing else in the vault
is touched (settings live in `data.json` inside this folder).
