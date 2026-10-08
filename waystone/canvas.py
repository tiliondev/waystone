"""Canvas targets from the display list.

At record time the main-world hook (js/canvas_hook.js) tells us what the page drew. This module
turns "the primitives under and around the click" into a target description that survives scroll,
zoom, theme and data changes:

    label       text drawn inside the clicked region (the cell's value, the button's caption)
    row_anchor  nearest text to the LEFT on the same row (a row header, a list label)
    col_anchor  nearest text ABOVE in the same column (a column header)
    cell        the smallest rect/image primitive containing the click (the cell, the button)
    image       the sprite/image source if the click was on a drawn image
    offset      where inside `cell` the click was, as fractions

On replay the hook is not installed (stealth), so relocation reads the display list only when
`--canvas-hooks` is given; otherwise the description drives the pixel cascade (OCR anchors match
`row_anchor`/`col_anchor`/`label`, template match uses the crops). Either way the README can say
"the cell in row 5, column C" instead of a coordinate.
"""
from __future__ import annotations

from typing import Any


def _center(b: list[float]) -> tuple[float, float]:
    return b[0] + b[2] / 2, b[1] + b[3] / 2


def _text_prims(prims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in prims if p.get("k") == "text" and str(p.get("s", "")).strip()]


def describe(read: dict[str, Any], x: float, y: float) -> dict[str, Any] | None:
    """Build a semantic target from a canvas_primitives() read at page point (x, y)."""
    if not read:
        return None
    hit, near = read.get("hit", []), read.get("near", [])
    seen: set[int] = set()
    allp = []
    for p in hit + near:  # the reader may list a primitive in more than one bucket
        if p.get("i") in seen:
            continue
        seen.add(p.get("i")); allp.append(p)
    canvas_box = read.get("canvas") or [0, 0, 0, 0]
    out: dict[str, Any] = {"total": read.get("total", 0)}

    # the smallest non-background shape containing the click
    shapes = [p for p in hit if p.get("k") in ("rect", "image", "path") and p["bbox"][2] * p["bbox"][3] < canvas_box[2] * canvas_box[3] * 0.5]
    if shapes:
        cell = min(shapes, key=lambda p: p["bbox"][2] * p["bbox"][3])
        out["cell"] = {"k": cell["k"], "bbox": [round(v, 1) for v in cell["bbox"]], "fill": cell.get("fill"), "src": cell.get("src"), "sprite": cell.get("sprite")}
        bx, by, bw, bh = cell["bbox"]
        out["offset"] = {"ox": round((x - bx) / bw, 3) if bw else 0.5, "oy": round((y - by) / bh, 3) if bh else 0.5}
        if cell["k"] == "image":
            out["image"] = cell.get("src")

    texts = _text_prims(allp)
    # label: text inside the click's cell (or under the click if no cell)
    region = out["cell"]["bbox"] if "cell" in out else [x - 20, y - 12, 40, 24]
    rx, ry, rw, rh = region
    inside = [t for t in texts if _center(t["bbox"])[0] >= rx and _center(t["bbox"])[0] <= rx + rw and _center(t["bbox"])[1] >= ry and _center(t["bbox"])[1] <= ry + rh]
    if inside:
        inside.sort(key=lambda t: abs(_center(t["bbox"])[0] - x) + abs(_center(t["bbox"])[1] - y))
        out["label"] = inside[0]["s"]
        out["label_bbox"] = [round(v, 1) for v in inside[0]["bbox"]]

    # anchors: same row to the left, same column above. A header is the OUTERMOST text in that
    # direction that sits in a line shared by many other texts (a header row/column), or the one
    # whose fill colour differs from the body texts; failing both, the nearest.
    line_h = (out["cell"]["bbox"][3] if "cell" in out else 24) or 24
    col_w = (out["cell"]["bbox"][2] if "cell" in out else 60) or 60
    fills = {}
    for t in texts:
        fills[t.get("fill")] = fills.get(t.get("fill"), 0) + 1
    body_fill = max(fills, key=fills.get) if fills else None

    def pick(cands: list[dict[str, Any]], axis: int) -> dict[str, Any] | None:
        """cands are texts in the same row (axis=0, vary in x) or column (axis=1, vary in y)."""
        if not cands:
            return None
        other = 1 - axis
        def shared(t: dict[str, Any]) -> int:  # how many texts in the whole read share t's cross-axis line
            c = _center(t["bbox"])[axis]
            tol = (col_w if axis == 0 else line_h) * 0.45
            return sum(1 for u in texts if abs(_center(u["bbox"])[axis] - c) <= tol)
        pos = x if axis == 0 else y
        # 1. styled differently from the body (header fill): take the farthest such text
        styled = [t for t in cands if body_fill is not None and t.get("fill") not in (None, body_fill)]
        if styled:
            return max(styled, key=lambda t: abs(_center(t["bbox"])[axis] - pos))
        # 2. the farthest text whose line is shared by many others (a header row/column)
        far_first = sorted(cands, key=lambda t: -abs(_center(t["bbox"])[axis] - pos))
        for t in far_first:
            if shared(t) >= 3:
                return t
        # 3. nearest
        return min(cands, key=lambda t: abs(_center(t["bbox"])[axis] - pos))

    left = [t for t in texts if t["bbox"][0] + t["bbox"][2] <= x and abs(_center(t["bbox"])[1] - y) <= line_h * 0.6 and t.get("s") != out.get("label")]
    ra = pick(left, 0)
    if ra:
        out["row_anchor"] = {"s": ra["s"], "bbox": [round(v, 1) for v in ra["bbox"]], "dx": round(x - _center(ra["bbox"])[0], 1)}
    above = [t for t in texts if t["bbox"][1] + t["bbox"][3] <= y and abs(_center(t["bbox"])[0] - x) <= col_w * 0.6 and t.get("s") != out.get("label")]
    ca = pick(above, 1)
    if ca:
        out["col_anchor"] = {"s": ca["s"], "bbox": [round(v, 1) for v in ca["bbox"]], "dy": round(y - _center(ca["bbox"])[1], 1)}

    # a few more nearby texts with offsets, for triangulation when headers are not present
    others = sorted([t for t in texts if t.get("s") not in (out.get("label"), (out.get("row_anchor") or {}).get("s"), (out.get("col_anchor") or {}).get("s"))], key=lambda t: abs(_center(t["bbox"])[0] - x) + abs(_center(t["bbox"])[1] - y))[:6]
    out["anchors"] = [{"s": t["s"], "dx": round(x - _center(t["bbox"])[0], 1), "dy": round(y - _center(t["bbox"])[1], 1)} for t in others]
    return out


def phrase(desc: dict[str, Any]) -> str:
    """How a person would say it."""
    if not desc:
        return ""
    parts = []
    if desc.get("image"):
        parts.append(f"the image `{str(desc['image']).rsplit('/', 1)[-1][:40]}`")
    elif desc.get("label"):
        parts.append(f"the item reading “{desc['label']}”")
    elif desc.get("cell"):
        parts.append("the shape")
    where = []
    if desc.get("row_anchor"):
        where.append(f"row “{desc['row_anchor']['s']}”")
    if desc.get("col_anchor"):
        where.append(f"column “{desc['col_anchor']['s']}”")
    if where:
        parts.append("in " + ", ".join(where))
    elif desc.get("anchors"):
        a = desc["anchors"][0]
        parts.append(f"near “{a['s']}”")
    return " ".join(parts) if parts else "the drawn element"


def relocate(read: dict[str, Any], desc: dict[str, Any]) -> dict[str, Any] | None:
    """Given a fresh display-list read (whole canvas, page-space primitives) and a recorded
    description, find the click point now, or say why not.

    Rules are strict on purpose. If the recording has a row anchor and a column anchor, both must
    be present and they must intersect at a cell that reads the label (when the recording had one).
    A missing anchor means the target is not on screen; the result says which way it probably went
    so the runner can scroll and retry. Guessing from one anchor is what pixel matching did wrong."""
    if not read or not desc:
        return None
    prims = read.get("hit", []) + read.get("near", [])
    texts = _text_prims(prims)
    by_text: dict[str, list[dict[str, Any]]] = {}
    for t in texts:
        by_text.setdefault(t["s"], []).append(t)
    canvas_box = read.get("canvas") or [0, 0, 0, 0]

    def missing(kind: str, a: dict[str, Any]) -> dict[str, Any]:
        # which way did it go? headers are ordered: compare to the visible ones of the same kind
        hint = None
        if kind == "row_anchor":
            vis = sorted({t["s"] for t in texts if abs(_center(t["bbox"])[0] - _center(a["bbox"])[0]) < 30 and t["s"].isdigit()}, key=int)
            if vis and a["s"].isdigit():
                hint = "up" if int(a["s"]) < int(vis[0]) else "down"
        elif kind == "col_anchor":
            vis = sorted({t["s"] for t in texts if abs(_center(t["bbox"])[1] - _center(a["bbox"])[1]) < 30 and len(t["s"]) <= 3})
            if vis and a["s"] not in vis:
                hint = "left" if a["s"] < vis[0] else "right"
        return {"off_screen": True, "missing": kind, "text": a["s"], "scroll": hint}

    ra, ca = desc.get("row_anchor"), desc.get("col_anchor")
    if ra and ra["s"] not in by_text:
        return missing("row_anchor", ra)
    if ca and ca["s"] not in by_text:
        return missing("col_anchor", ca)

    # 1. row + column anchors: the intersection, confirmed by the label if there was one
    if ra and ca:
        best = None
        for r in by_text[ra["s"]]:
            for c in by_text[ca["s"]]:
                x = _center(c["bbox"])[0] + 0.0; y = _center(r["bbox"])[1]
                # the click sat at an offset from the column header's centre line; keep it
                x += (desc.get("col_anchor", {}).get("dx_from_col", 0) or 0)
                conf = 0.0
                if desc.get("label"):
                    if desc["label"] not in by_text:
                        continue
                    d = min(abs(_center(t["bbox"])[0] - x) + abs(_center(t["bbox"])[1] - y) for t in by_text[desc["label"]])
                    if d > max(canvas_box[2], canvas_box[3]) * 0.15:
                        continue
                    conf = -d
                if best is None or conf > best[0]:
                    best = (conf, x, y)
        if best:
            return {"x": best[1], "y": best[2], "via": "row+col"}
        return {"off_screen": True, "missing": "label", "text": desc.get("label"), "scroll": None}
    # 2. label alone, unique
    if desc.get("label") and len(by_text.get(desc["label"], [])) == 1:
        x, y = _center(by_text[desc["label"]][0]["bbox"])
        return {"x": x, "y": y, "via": "label"}
    # 3. one anchor plus its recorded offset, only if that anchor is unique
    for key in ("row_anchor", "col_anchor"):
        a = desc.get(key)
        if a and len(by_text.get(a["s"], [])) == 1:
            cx, cy = _center(by_text[a["s"]][0]["bbox"])
            return {"x": cx + a.get("dx", 0) if key == "row_anchor" else cx, "y": cy if key == "row_anchor" else cy + a.get("dy", 0), "via": key}
    for a in desc.get("anchors", []):
        if len(by_text.get(a["s"], [])) == 1:
            cx, cy = _center(by_text[a["s"]][0]["bbox"])
            return {"x": cx + a["dx"], "y": cy + a["dy"], "via": "anchor"}
    # 4. image identity
    if desc.get("image"):
        imgs = [p for p in prims if p.get("k") == "image" and p.get("src") == desc["image"] and p.get("sprite") == (desc.get("cell") or {}).get("sprite")]
        if len(imgs) == 1:
            x, y = _center(imgs[0]["bbox"])
            return {"x": x, "y": y, "via": "image"}
    return None
