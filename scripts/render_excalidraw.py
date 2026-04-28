#!/usr/bin/env python3
"""Render an Excalidraw .excalidraw JSON to PNG using Pillow.

Supports the subset used by docs/diagrams/system.excalidraw:
- rectangles (filled, optional rounded corners, optional dashed stroke)
- text (single/multi-line, font family 1/2/3, alignment, container bind)
- arrows with start/end arrowheads, dashed style, multi-segment points

Renders cleanly (no rough.js hand-drawn effect) at high DPI for crisp PNG.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

SCALE = 2  # 2x for retina-quality PNG

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def font_for(family: int, size: int) -> ImageFont.FreeTypeFont:
    path = {1: FONT_REG, 2: FONT_REG, 3: FONT_MONO}.get(family, FONT_REG)
    return ImageFont.truetype(path, int(size * SCALE))


def s(v: float) -> int:
    return int(round(v * SCALE))


def parse_color(c: str) -> str | None:
    if not c or c == "transparent":
        return None
    return c


def draw_dashed_line(draw: ImageDraw.ImageDraw, p1, p2, color: str, width: int, dash: int = 8, gap: int = 6) -> None:
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        return
    ux, uy = dx / length, dy / length
    pos = 0.0
    while pos < length:
        seg_end = min(pos + dash, length)
        sx, sy = x1 + ux * pos, y1 + uy * pos
        ex, ey = x1 + ux * seg_end, y1 + uy * seg_end
        draw.line([(sx, sy), (ex, ey)], fill=color, width=width)
        pos += dash + gap


def rounded_rect(draw: ImageDraw.ImageDraw, xy, radius, fill, outline, width, dashed: bool = False) -> None:
    x1, y1, x2, y2 = xy
    if fill:
        draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=None)
    if outline:
        if dashed:
            # outline as four dashed line segments (ignores corner radius for simplicity)
            draw_dashed_line(draw, (x1, y1), (x2, y1), outline, width)
            draw_dashed_line(draw, (x2, y1), (x2, y2), outline, width)
            draw_dashed_line(draw, (x2, y2), (x1, y2), outline, width)
            draw_dashed_line(draw, (x1, y2), (x1, y1), outline, width)
        else:
            draw.rounded_rectangle(xy, radius=radius, fill=None, outline=outline, width=width)


def draw_arrowhead(draw: ImageDraw.ImageDraw, tip, direction, color: str, size: int = 14, kind: str = "arrow") -> None:
    """Draw an arrowhead at `tip` pointing in `direction` (dx, dy normalized)."""
    tx, ty = tip
    dx, dy = direction
    # perpendicular
    px, py = -dy, dx
    base_x = tx - dx * size
    base_y = ty - dy * size
    half = size * 0.6
    p1 = (tx, ty)
    p2 = (base_x + px * half, base_y + py * half)
    p3 = (base_x - px * half, base_y - py * half)
    if kind == "triangle":
        draw.polygon([p1, p2, p3], fill=color, outline=color)
    else:  # "arrow"
        draw.line([p2, p1, p3], fill=color, width=max(2, int(SCALE)))


def render_text(img: Image.Image, draw: ImageDraw.ImageDraw, e: dict, elements_by_id: dict) -> None:
    text = e.get("text", "")
    if not text:
        return
    family = e.get("fontFamily", 1)
    size = e.get("fontSize", 16)
    color = parse_color(e.get("strokeColor")) or "#1e1e1e"
    bg = parse_color(e.get("backgroundColor"))
    align = e.get("textAlign", "left")
    valign = e.get("verticalAlign", "top")

    container_id = e.get("containerId")
    if container_id and container_id in elements_by_id:
        c = elements_by_id[container_id]
        bx1, by1 = s(c["x"]), s(c["y"])
        bx2 = s(c["x"] + c["width"])
        by2 = s(c["y"] + c["height"])
    else:
        bx1, by1 = s(e["x"]), s(e["y"])
        bx2 = s(e["x"] + e["width"])
        by2 = s(e["y"] + e["height"])

    font = font_for(family, size)
    # Pillow multiline_text supports its own anchoring; we do manual layout.
    lines = text.split("\n")
    # Measure each line
    line_heights = []
    line_widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])
    line_h = max(line_heights) if line_heights else 0
    line_gap = int(line_h * 0.25)
    total_h = sum(line_heights) + line_gap * (len(lines) - 1)

    if valign == "middle":
        y = (by1 + by2 - total_h) // 2
    elif valign == "bottom":
        y = by2 - total_h
    else:
        y = by1

    if bg:
        max_line_w = max(line_widths) if line_widths else 0
        if align == "center":
            bgx1 = (bx1 + bx2 - max_line_w) // 2
        elif align == "right":
            bgx1 = bx2 - max_line_w
        else:
            bgx1 = bx1
        pad = s(4)
        draw.rectangle(
            [bgx1 - pad, y - pad, bgx1 + max_line_w + pad, y + total_h + pad],
            fill=bg,
        )

    for i, line in enumerate(lines):
        w = line_widths[i]
        if align == "center":
            x = (bx1 + bx2 - w) // 2
        elif align == "right":
            x = bx2 - w
        else:
            x = bx1
        draw.text((x, y), line, font=font, fill=color)
        y += line_heights[i] + line_gap


def render_rect(draw: ImageDraw.ImageDraw, e: dict) -> None:
    x1 = s(e["x"]); y1 = s(e["y"])
    x2 = x1 + s(e["width"]); y2 = y1 + s(e["height"])
    fill = parse_color(e.get("backgroundColor"))
    stroke = parse_color(e.get("strokeColor"))
    width = s(e.get("strokeWidth", 2))
    radius = s(8) if e.get("roundness") else 0
    dashed = e.get("strokeStyle") == "dashed"
    rounded_rect(draw, (x1, y1, x2, y2), radius=radius, fill=fill, outline=stroke, width=width, dashed=dashed)


def render_arrow(draw: ImageDraw.ImageDraw, e: dict) -> None:
    color = parse_color(e.get("strokeColor")) or "#1e1e1e"
    width = s(e.get("strokeWidth", 2))
    dashed = e.get("strokeStyle") == "dashed"
    points = e.get("points", [])
    if len(points) < 2:
        return
    base_x, base_y = s(e["x"]), s(e["y"])
    abs_pts = [(base_x + s(px), base_y + s(py)) for px, py in points]

    for i in range(len(abs_pts) - 1):
        p1, p2 = abs_pts[i], abs_pts[i + 1]
        if dashed:
            draw_dashed_line(draw, p1, p2, color, width)
        else:
            draw.line([p1, p2], fill=color, width=width)

    # arrowheads
    end_kind = e.get("endArrowhead")
    start_kind = e.get("startArrowhead")
    if end_kind:
        a, b = abs_pts[-2], abs_pts[-1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        if L > 0:
            draw_arrowhead(draw, b, (dx / L, dy / L), color, size=s(8), kind=end_kind)
    if start_kind:
        a, b = abs_pts[1], abs_pts[0]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        if L > 0:
            draw_arrowhead(draw, b, (dx / L, dy / L), color, size=s(8), kind=start_kind)


def main(in_path: str, out_path: str) -> None:
    data = json.loads(Path(in_path).read_text())
    elements = [e for e in data["elements"] if not e.get("isDeleted", False)]
    elements_by_id = {e["id"]: e for e in elements}

    # Bounding box
    xs, ys = [], []
    for e in elements:
        x, y = e["x"], e["y"]
        if e["type"] == "arrow":
            for px, py in e.get("points", []):
                xs.append(x + px); ys.append(y + py)
        else:
            w, h = e.get("width", 0) or 0, e.get("height", 0) or 0
            xs += [x, x + w]; ys += [y, y + h]
    margin = 30
    min_x, min_y = min(xs) - margin, min(ys) - margin
    max_x, max_y = max(xs) + margin, max(ys) + margin
    canvas_w = s(max_x - min_x)
    canvas_h = s(max_y - min_y)

    # Translate so min_x/min_y → 0
    for e in elements:
        e["x"] -= min_x
        e["y"] -= min_y

    bg = data.get("appState", {}).get("viewBackgroundColor", "#ffffff") or "#ffffff"
    img = Image.new("RGB", (canvas_w, canvas_h), bg)
    draw = ImageDraw.Draw(img)

    # Render order: rectangles → arrows → text (so text is on top)
    for e in elements:
        if e["type"] == "rectangle":
            render_rect(draw, e)
    for e in elements:
        if e["type"] == "arrow":
            render_arrow(draw, e)
    for e in elements:
        if e["type"] == "text":
            render_text(img, draw, e, elements_by_id)

    img.save(out_path, "PNG", optimize=True)
    print(f"wrote {out_path} ({canvas_w}x{canvas_h})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
