# Diagrams

Editable Excalidraw sources for the system. Plain JSON files — no binary blobs.

| File | What it shows |
|---|---|
| [system.excalidraw](system.excalidraw) | Full system, editable source. |
| [system.png](system.png) | Pre-rendered PNG snapshot of the same diagram (regenerate with the script below after editing). |

![System diagram](system.png)

## How to view / edit

Three options, all work:

1. **excalidraw.com** — drag-and-drop the `.excalidraw` file onto the page. No install. Edits export back to a `.excalidraw` file.
2. **VS Code extension** — install [`pomdtr.excalidraw-editor`](https://marketplace.visualstudio.com/items?itemName=pomdtr.excalidraw-editor) and open the file. Edits save in place.
3. **Excalidraw desktop app** — File → Open.

Commit the updated `.excalidraw` file alongside the code change that prompted the diagram update — these are first-class docs, not generated artifacts.

## Conventions

- Stick to the [system.excalidraw](system.excalidraw) palette so future diagrams stay visually consistent: blue = client/auth, purple = LiveKit/state, yellow = agent worker, pink = Python adapters, green = external AI, red = observability.
- Solid arrows = data flow on the hot path. Dashed arrows = telemetry (Prometheus scrape, Langfuse spans).
- Show host-facing ports; omit internal-only ones unless they're the point of the diagram.

## Regenerating the PNG

The PNG is committed alongside the source so it can be browsed inline on GitHub / VSCode without an Excalidraw runtime. Regenerate after editing the source:

```
pip install Pillow            # one-time
python3 scripts/render_excalidraw.py docs/diagrams/system.excalidraw docs/diagrams/system.png
```

The renderer is a small Pillow-based script that handles the subset of Excalidraw used here (rectangles with rounded corners, multi-segment arrows with arrowheads and dashed style, multi-line text with bound containers and background fills). It deliberately does not try to reproduce the rough.js hand-drawn look — clean output is fine for a system diagram.
