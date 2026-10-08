"""Visual targets for canvas and other element-less clicks.

When the thing clicked has no DOM identity of its own (a <canvas>, a map tile, a drawing app's
toolbar, a spreadsheet grid), the recorder stores crops of the screenshot around the click point
and replay relocates them by normalised cross-correlation (template matching).

What makes this hold up on real pages:

  uniqueness at record time   The tight crop is grown until it matches exactly one place in the
                              recording screenshot. A grid cell on its own matches every cell; the
                              same cell plus its row number and column header does not.
  two crops                   `target` (tight, what was clicked) and `context` (wide, the stable
                              surroundings). Replay finds the context first and the target inside
                              it, so a cell whose number changed is still found by its neighbours.
  masked centre               A `ring` crop with the clicked thing blanked out, for data-dependent
                              content: match what surrounds the value, not the value.
  multi-scale                 1.0x first; 0.8x to 1.25x when that is weak, for zoomed canvases.
  ambiguity is reported       If no crop size gives a unique match at record time, the step is
                              marked `ambiguous` so the README and the runner can say so.

Pillow + numpy only. Matching runs on a half-resolution grayscale; a whole-viewport coarse-to-fine
search is 100-200ms.
"""
from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image

SCALE = 0.5            # matching resolution relative to css px
MIN_SCORE = 0.80       # below this a match is not trusted
UNIQUE_MARGIN = 0.08   # best match must beat the runner-up by this much to count as unique
TIGHT = 24             # css px half-size of the target crop
GROW = (48, 80, 120, 180, 260)  # half-sizes tried for the context crop until unique
SCALES = (1.0, 0.9, 1.1, 0.8, 1.25)


# --- image helpers ------------------------------------------------------------

def _gray(png: bytes, css_scale: float, factor: float = 1.0) -> np.ndarray:
    im = Image.open(io.BytesIO(png)).convert("L")
    w, h = im.size
    im = im.resize((max(1, int(w / css_scale * SCALE * factor)), max(1, int(h / css_scale * SCALE * factor))), Image.BILINEAR)
    return np.asarray(im, dtype=np.float32)


def _png(im: Image.Image) -> bytes:
    out = io.BytesIO(); im.save(out, format="PNG"); return out.getvalue()


def _cut(im: Image.Image, cx: float, cy: float, half: float) -> tuple[Image.Image, float, float]:
    """Square crop clamped to the image. Returns crop and the centre's offset inside it (image px)."""
    x0, y0 = int(max(0, cx - half)), int(max(0, cy - half))
    x1, y1 = int(min(im.width, cx + half)), int(min(im.height, cy + half))
    return im.crop((x0, y0, x1, y1)), cx - x0, cy - y0


def _ncc(img: np.ndarray, tpl: np.ndarray) -> np.ndarray:
    """Normalised cross-correlation of tpl at every position in img. Values in [-1, 1]."""
    H, W = img.shape; h, w = tpl.shape
    if h > H or w > W or h == 0 or w == 0:
        return np.full((1, 1), -1.0, dtype=np.float32)
    t = tpl - tpl.mean()
    tn = float(np.sqrt((t * t).sum())) + 1e-6
    win = np.lib.stride_tricks.sliding_window_view(img, (h, w))
    wc = win - win.mean(axis=(2, 3), keepdims=True)
    num = np.einsum("ijkl,kl->ij", wc, t)
    den = np.sqrt(np.einsum("ijkl,ijkl->ij", wc, wc)) * tn + 1e-6
    return (num / den).astype(np.float32)


def _ncc_masked(img: np.ndarray, tpl: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """NCC where only mask==1 pixels of the template count (the centre is blanked out)."""
    H, W = img.shape; h, w = tpl.shape
    if h > H or w > W:
        return np.full((1, 1), -1.0, dtype=np.float32)
    n = float(mask.sum()) + 1e-6
    tm = (tpl * mask).sum() / n
    t = (tpl - tm) * mask
    tn = float(np.sqrt((t * t).sum())) + 1e-6
    win = np.lib.stride_tricks.sliding_window_view(img, (h, w))
    wmean = np.einsum("ijkl,kl->ij", win, mask) / n
    wc = (win - wmean[:, :, None, None]) * mask
    num = np.einsum("ijkl,kl->ij", wc, t)
    den = np.sqrt(np.einsum("ijkl,ijkl->ij", wc, wc)) * tn + 1e-6
    return (num / den).astype(np.float32)


def _down(a: np.ndarray, f: int) -> np.ndarray:
    H, W = (a.shape[0] // f) * f, (a.shape[1] // f) * f
    return a[:H, :W].reshape(H // f, f, W // f, f).mean(axis=(1, 3))


def _top2(sc: np.ndarray, exclude_radius: int) -> tuple[float, int, int, float]:
    """Best score and position, plus the best score at least `exclude_radius` away (the runner-up)."""
    iy, ix = np.unravel_index(int(np.argmax(sc)), sc.shape)
    best = float(sc[iy, ix])
    m = sc.copy()
    y0, y1 = max(0, iy - exclude_radius), min(sc.shape[0], iy + exclude_radius + 1)
    x0, x1 = max(0, ix - exclude_radius), min(sc.shape[1], ix + exclude_radius + 1)
    m[y0:y1, x0:x1] = -2
    second = float(m.max()) if m.size else -1.0
    return best, int(ix), int(iy), second


# --- record time --------------------------------------------------------------

def capture(png: bytes, x: float, y: float, viewport: dict[str, Any] | None) -> dict[str, Any]:
    """Build the visual target for a click at css point (x, y) in `png`.

    Returns {target, context, ring, ambiguous, scale}. Each crop is {png, ox, oy} with the click
    point's offset inside the crop in css px. `context` is grown until it is unique in the
    screenshot; `ambiguous` is True if no size achieved that."""
    im = Image.open(io.BytesIO(png)).convert("RGB")
    s = im.width / float(viewport["w"]) if viewport and viewport.get("w") else 1.0
    cx, cy = x * s, y * s
    g_full = _gray(png, s)

    def crop(half_css: float) -> dict[str, Any]:
        c, ox, oy = _cut(im, cx, cy, half_css * s)
        return {"png": _png(c), "ox": ox / s, "oy": oy / s, "half": half_css}

    def unique(c: dict[str, Any]) -> bool:
        tpl = _gray(c["png"], s)
        if tpl.shape[0] >= g_full.shape[0] or tpl.shape[1] >= g_full.shape[1]:
            return True  # crop is most of the screen; trivially unique
        best, _, _, second = _top2(_ncc(g_full, tpl), exclude_radius=max(4, int(min(tpl.shape) * 0.5)))
        return best - second >= UNIQUE_MARGIN

    target = crop(TIGHT)
    context = None
    for half in GROW:
        c = crop(half)
        if unique(c):
            context = c
            break
    ambiguous = context is None
    if context is None:
        context = crop(GROW[-1])

    # ring: the context with the target area blanked, for data-dependent centres
    ring_im = Image.open(io.BytesIO(context["png"])).convert("RGB")
    mask = Image.new("L", ring_im.size, 255)
    from PIL import ImageDraw
    d = ImageDraw.Draw(mask)
    r = TIGHT * s
    d.rectangle((context["ox"] * s - r, context["oy"] * s - r, context["ox"] * s + r, context["oy"] * s + r), fill=0)
    ring = {"png": _png(ring_im), "mask": _png(mask), "ox": context["ox"], "oy": context["oy"], "half": context["half"]}
    return {"target": target, "context": context, "ring": ring, "ambiguous": ambiguous, "scale": s, "x": x, "y": y}


# --- replay time ----------------------------------------------------------------

def locate(png: bytes, vt: dict[str, Any], viewport: dict[str, Any] | None, *, near: tuple[float, float] | None = None) -> dict[str, Any]:
    """Find the recorded click point in a fresh screenshot.

    Order: context crop (unique by construction) at 1.0x, near the recorded point then globally;
    then multi-scale; then the ring (masked centre); then the tight target alone. The first result
    above MIN_SCORE wins. Returns css point, score, which crop and scale matched."""
    s = vt.get("scale", 1.0)
    img = _gray(png, s)
    k = SCALE
    attempts: list[tuple[str, float]] = [("context", 1.0)]
    attempts += [("context", f) for f in SCALES[1:]]
    attempts += [("ring", 1.0), ("target", 1.0)]
    attempts += [("target", f) for f in SCALES[1:]]

    def run(kind: str, factor: float) -> tuple[float, float, float, float] | None:
        c = vt.get(kind)
        if not c:
            return None
        tpl = _gray(c["png"], s, factor)
        if tpl.shape[0] >= img.shape[0] or tpl.shape[1] >= img.shape[1] or tpl.size < 16:
            return None
        mask = None
        if kind == "ring":
            mask = (_gray(c["mask"], s, factor) > 127).astype(np.float32)
            if mask.shape != tpl.shape:
                mask = None
        corr = (lambda a, b: _ncc_masked(a, b, mask)) if mask is not None else _ncc
        best: tuple[float, int, int] | None = None
        if near is not None and factor == 1.0:
            rad = max(tpl.shape) + 120 * k
            nx, ny = near[0] * k - c["ox"] * k * factor, near[1] * k - c["oy"] * k * factor
            x0, y0 = int(max(0, nx - rad)), int(max(0, ny - rad)); x1, y1 = int(min(img.shape[1], nx + rad + tpl.shape[1])), int(min(img.shape[0], ny + rad + tpl.shape[0]))
            if x1 - x0 > tpl.shape[1] and y1 - y0 > tpl.shape[0]:
                sc = corr(img[y0:y1, x0:x1], tpl)
                iy, ix = np.unravel_index(int(np.argmax(sc)), sc.shape)
                best = (float(sc[iy, ix]), int(ix) + x0, int(iy) + y0)
        if best is None or best[0] < MIN_SCORE:
            # coarse-to-fine global search
            f = 2
            ci, ct = _down(img, f), _down(tpl, f)
            cand: tuple[float, int, int] | None = None
            if ct.shape[0] >= 4 and ct.shape[1] >= 4 and ci.shape[0] > ct.shape[0] and ci.shape[1] > ct.shape[1]:
                sc0 = _ncc(ci, ct)
                iy, ix = np.unravel_index(int(np.argmax(sc0)), sc0.shape)
                gx, gy = ix * f, iy * f
                rad = max(tpl.shape)
                x0, y0 = int(max(0, gx - rad)), int(max(0, gy - rad)); x1, y1 = int(min(img.shape[1], gx + rad + tpl.shape[1])), int(min(img.shape[0], gy + rad + tpl.shape[0]))
                if x1 - x0 > tpl.shape[1] and y1 - y0 > tpl.shape[0]:
                    sc = corr(img[y0:y1, x0:x1], tpl)
                    iy, ix = np.unravel_index(int(np.argmax(sc)), sc.shape)
                    cand = (float(sc[iy, ix]), int(ix) + x0, int(iy) + y0)
            if cand is None:
                sc = corr(img, tpl)
                iy, ix = np.unravel_index(int(np.argmax(sc)), sc.shape)
                cand = (float(sc[iy, ix]), int(ix), int(iy))
            if best is None or cand[0] > best[0]:
                best = cand
        score, mx, my = best
        # how clearly this beats the next-best place on the whole image (grids produce near-ties)
        full = corr(img, tpl) if img.size <= 400_000 else None
        second = -1.0
        if full is not None:
            _, _, _, second = _top2(full, exclude_radius=max(4, int(min(tpl.shape) * 0.5)))
        return score, mx / k + c["ox"] * factor, my / k + c["oy"] * factor, second

    def verify_target(px: float, py: float) -> float:
        """Does the tight target crop also sit at this point? Returns its NCC there (or -1)."""
        c = vt.get("target")
        if not c:
            return 1.0
        tpl = _gray(c["png"], s)
        cx, cy = int(px * k - c["ox"] * k), int(py * k - c["oy"] * k)
        pad = 3
        x0, y0 = max(0, cx - pad), max(0, cy - pad); x1, y1 = min(img.shape[1], cx + tpl.shape[1] + pad), min(img.shape[0], cy + tpl.shape[0] + pad)
        if x1 - x0 < tpl.shape[1] or y1 - y0 < tpl.shape[0]:
            return -1.0
        return float(_ncc(img[y0:y1, x0:x1], tpl).max())

    tried = []
    weak: tuple[float, float, float, str, float] | None = None
    for kind, factor in attempts:
        r = run(kind, factor)
        if r is None:
            continue
        score, px, py, second = r
        tried.append((kind, factor, round(score, 3)))
        if score < MIN_SCORE:
            continue
        clear = score - second >= UNIQUE_MARGIN
        agrees = kind == "target" or verify_target(px, py) >= MIN_SCORE
        if clear and agrees:
            return {"found": True, "score": round(score, 3), "x": px, "y": py, "via": kind, "zoom": factor}
        # A match that is not clearly unique is kept only as a last resort, and reported as such.
        if agrees and (weak is None or score > weak[0]):
            weak = (score, px, py, kind, factor, second)
    if weak is not None:
        score, px, py, kind, factor, second = weak
        # If the ambiguous match is at the recorded position anyway, take it: nothing moved.
        if near is not None and abs(px - near[0]) < 3 and abs(py - near[1]) < 3:
            return {"found": True, "score": round(score, 3), "x": px, "y": py, "via": kind, "zoom": factor}
        return {"found": False, "score": round(score, 3), "via": kind, "zoom": factor, "x": vt.get("x"), "y": vt.get("y"), "ambiguous": True, "runner_up": round(second, 3), "candidate": {"x": px, "y": py}}
    best = max(tried, key=lambda t: t[2]) if tried else ("none", 1.0, 0.0)
    return {"found": False, "score": best[2], "via": best[0], "zoom": best[1], "x": vt.get("x"), "y": vt.get("y")}


# --- storage -------------------------------------------------------------------

def save(vt: dict[str, Any], folder, stem: str) -> dict[str, Any]:
    """Write the crops under <folder>/steps/crops/<stem>-*.png and return the JSON-safe record."""
    from pathlib import Path

    d = Path(folder) / "steps" / "crops"; d.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {"x": vt["x"], "y": vt["y"], "scale": vt["scale"], "ambiguous": vt["ambiguous"]}
    for kind in ("target", "context", "ring"):
        c = vt[kind]
        p = d / f"{stem}-{kind}.png"; p.write_bytes(c["png"])
        rec[kind] = {"path": f"steps/crops/{p.name}", "ox": c["ox"], "oy": c["oy"], "half": c["half"]}
        if "mask" in c:
            m = d / f"{stem}-ring-mask.png"; m.write_bytes(c["mask"]); rec[kind]["mask"] = f"steps/crops/{m.name}"
    rec["path"] = rec["context"]["path"]  # what the README points at
    return rec


def load(rec: dict[str, Any], folder) -> dict[str, Any]:
    from pathlib import Path

    f = Path(folder)
    vt: dict[str, Any] = {"x": rec.get("x"), "y": rec.get("y"), "scale": rec.get("scale", 1.0), "ambiguous": rec.get("ambiguous", False)}
    for kind in ("target", "context", "ring"):
        c = rec.get(kind)
        if not c:
            continue
        try:
            vt[kind] = {"png": (f / c["path"]).read_bytes(), "ox": c["ox"], "oy": c["oy"]}
            if c.get("mask"):
                vt[kind]["mask"] = (f / c["mask"]).read_bytes()
        except OSError:
            continue
    return vt
