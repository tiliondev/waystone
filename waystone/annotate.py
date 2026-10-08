"""Draw marks on screenshots. The DOM is never touched; boxes are painted on the PNG."""
from __future__ import annotations

import io
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

RED = "#e5322d"
_FONT_PATHS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _scale(img: Image.Image, viewport: dict | None) -> float:
    """Screenshots may come back in CSS or device pixels; measure instead of trusting DPR."""
    if viewport and viewport.get("w"):
        return img.width / float(viewport["w"])
    return 1.0


def _box(rect: dict, s: float) -> tuple[int, int, int, int]:
    x0 = int(rect["x"] * s)
    y0 = int(rect["y"] * s)
    return x0, y0, int(x0 + rect["w"] * s), int(y0 + rect["h"] * s)


def highlight(png: bytes, rect: dict, viewport: dict | None = None, *, color: str = RED, label: str | None = None) -> bytes:
    """Box one element. Used by the recorder: one screenshot per step, target in red."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    s = _scale(img, viewport)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = _box(rect, s)
    d.rectangle((x0, y0, x1, y1), outline=color, width=max(2, round(3 * s)))
    if label:
        _label(d, x0, y0, x1, y1, label, color, s)
    return _bytes(img)


def marks(png: bytes, elements: Iterable[dict], viewport: dict | None = None, *, color: str = RED) -> bytes:
    """Set-of-marks: number every actionable element so a model can say "click 7"."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    s = _scale(img, viewport)
    d = ImageDraw.Draw(img)
    w = max(1, round(1.5 * s))
    for m in elements:
        x0, y0, x1, y1 = _box(m["rect"], s)
        d.rectangle((x0, y0, x1, y1), outline=color, width=w)
    for m in elements:  # labels on top of every box
        x0, y0, x1, y1 = _box(m["rect"], s)
        _label(d, x0, y0, x1, y1, str(m["index"]), color, s)
    return _bytes(img)


def _label(d: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int, text: str, color: str, s: float) -> None:
    size = max(9, round(10 * s))
    f = _font(size)
    pad = max(1, round(2 * s))
    tw = int(d.textlength(text, font=f))
    bw, bh = tw + pad * 2, size + pad * 2
    # Inside the box's top-left corner if it fits, otherwise just above it.
    if (y1 - y0) >= bh + 2 and (x1 - x0) >= bw + 2:
        lx, ly = x0 + 1, y0 + 1
    else:
        lx, ly = x0, max(0, y0 - bh)
    d.rectangle((lx, ly, lx + bw, ly + bh), fill=color)
    d.text((lx + pad, ly + pad - 1), text, fill="white", font=f)


def _bytes(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def caption(png: bytes, title: str, detail: str | None = None, viewport: dict | None = None) -> bytes:
    """Write what happened onto the screenshot: a bar along the top with the step title and, under it,
    the selector. So an agent looking at the image knows the action without reading the JSON."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    s = _scale(img, viewport)
    size = max(12, round(13 * s)); small = max(10, round(11 * s))
    f, fs = _font(size), _font(small)
    pad = max(6, round(8 * s))
    d = ImageDraw.Draw(img, "RGBA")
    h = size + pad * 2 + (small + round(4 * s) if detail else 0)
    d.rectangle((0, 0, img.width, h), fill=(255, 255, 255, 235))
    d.line((0, h, img.width, h), fill=(0, 0, 0, 40), width=max(1, round(s)))
    d.text((pad, pad - 1), title, fill=(0, 0, 0), font=f)
    if detail:
        d.text((pad, pad + size + round(3 * s)), detail, fill=(110, 110, 110), font=fs)
    return _bytes(img)
