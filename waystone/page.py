"""Agent-facing page: mark it, then act by number.

All in-page work goes through raw CDP on Waystone's own session (isolated world for reading,
Input domain for acting, Page.navigate for navigation). Playwright is only the transport.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from playwright.sync_api import Page as PwPage

from . import annotate
from .input import Input
from .world import World


class NotFound(LookupError):
    pass


@dataclass
class Mark:
    index: int
    role: str
    name: str
    tag: str
    type: str | None
    rect: dict[str, float]
    candidates: list[dict[str, Any]]
    fingerprint: dict[str, Any]
    frame: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        kind = self.role if self.role != "generic" else self.tag
        extra = f" type={self.type}" if self.type and self.tag == "input" else ""
        fr = " (iframe)" if self.frame else ""
        return f'[{self.index}] {kind}{extra} "{self.name}"{fr}' if self.name else f"[{self.index}] {kind}{extra}{fr}"


@dataclass
class Marks:
    """Result of `page.mark()`: numbered elements plus the annotated screenshot."""

    elements: list[Mark]
    png: bytes = field(repr=False)
    url: str
    title: str
    dpr: float
    viewport: dict[str, int]

    def __iter__(self) -> Iterator[Mark]:
        return iter(self.elements)

    def __len__(self) -> int:
        return len(self.elements)

    def __getitem__(self, i: int) -> Mark:
        return self.elements[i]

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.png)
        return p

    def to_prompt(self) -> str:
        """Compact listing for an LLM: one line per element, index first."""
        head = f"{self.title} — {self.url}\n{len(self.elements)} actionable elements:"
        return head + "\n" + "\n".join(str(m) for m in self.elements)

    def find(self, text: str, *, role: str | None = None) -> Mark | None:
        """Exact (case-insensitive) name match first, then prefix, then substring."""
        t = text.lower().strip()
        pool = [m for m in self.elements if not role or m.role == role]
        for pred in (lambda n: n == t, lambda n: n.startswith(t), lambda n: t in n):
            for m in pool:
                if pred((m.name or "").lower()):
                    return m
        return None


class Page:
    """Wraps a Playwright page with an isolated world, marks, and self-healing actions."""

    def __init__(self, pw_page: PwPage, *, settle_ms: int = 300, timeout_ms: int = 10000, show: bool = False, dialogs: str = "accept"):
        self.pw = pw_page
        self.world = World(pw_page, dialogs=dialogs)
        self.input = Input(self.world.cdp, sleep=self._sleep)
        self.settle_ms = settle_ms
        self.timeout_ms = timeout_ms
        self.show = show
        self._last: Marks | None = None
        self.base_dir: Path | None = None  # folder holding steps/crops for visual targets
        self.canvas_hooks = False  # replay opt-in: read the canvas display list to relocate targets
        self._hover_at: tuple[float, float, float] | None = None  # (x, y, time) of the last hover
        self.last_wait: dict[str, Any] = {}
        self.settle_timeout_ms = 8000
        self._action_mark: float | None = None  # page time just before the last dispatched action
        self.popups: list[PwPage] = []
        pw_page.on("popup", lambda p: self.popups.append(p))  # a lambda: Playwright cannot wrap builtin bound methods

    def _sleep(self, ms: float) -> None:
        self.pw.wait_for_timeout(ms)

    def _ghost(self, on: bool) -> None:
        """Make our own overlays (recording HUD, marks) transparent to input while we dispatch."""
        try:
            self.world.eval(f"__ws.ghost({'true' if on else 'false'})")
        except Exception:
            pass

    # -- navigation --------------------------------------------------------

    def goto(self, url: str) -> "Page":
        self.world.navigate(url)
        self._settle()
        return self

    @property
    def url(self) -> str:
        return self.world.url

    def _settle(self, ms: int | None = None, timeout_ms: int | None = None) -> dict[str, Any]:
        """Wait for the document to be interactive, then for network + DOM to go quiet.

        Returns what was waited on ({waited_ms, requests, pending}) so callers can show it."""
        timeout_ms = timeout_ms or self.settle_timeout_ms
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self.world.ready_state() in ("interactive", "complete"):
                break
            self._sleep(50)
        self._sleep(ms if ms is not None else min(self.settle_ms, 150))
        info = self.world.wait_idle(quiet_ms=300, timeout_ms=timeout_ms, sleep=self._sleep, since=self._action_mark)
        self._action_mark = None
        self.last_wait = info
        return info

    def wait_for_url_change(self, old: str, timeout_ms: int = 15000) -> bool:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self.world.url != old:
                self._settle()
                return True
            self._sleep(100)
        return False

    # -- marks -------------------------------------------------------------

    def mark(self, *, viewport_only: bool = True, overlay: bool = False) -> Marks:
        """Scan actionable elements and return them numbered, with a red-boxed screenshot.

        `overlay=True` also paints the boxes into the live page (headed demos). Off by default
        because it mutates the DOM, which is detectable.
        """
        self.world.attach_closed_roots()
        raw = self.world.call("scan", {"viewportOnly": viewport_only})
        meta = next(m for m in raw if m.get("_meta"))
        items = [m for m in raw if not m.get("_meta")]
        # Cross-origin iframes: scan each in its own session, lift rects to page coordinates.
        for fs in list(self.world.frames.values()):
            try:
                self.world._refresh_frame_offset(fs)
                sub = fs.eval(f"__ws.scan({json.dumps({'viewportOnly': viewport_only})})")
            except Exception:
                continue
            for m in sub:
                if m.get("_meta"):
                    continue
                m["rect"] = {"x": m["rect"]["x"] + fs.offset["x"], "y": m["rect"]["y"] + fs.offset["y"], "w": m["rect"]["w"], "h": m["rect"]["h"]}
                m["frame"] = fs.chain + (m.get("frame") or [])
                items.append(m)
        items.sort(key=lambda m: (round(m["rect"]["y"] / 12), m["rect"]["x"]))
        for i, m in enumerate(items):
            m["index"] = i
        els = [Mark(**{k: v for k, v in m.items() if k in Mark.__dataclass_fields__}) for m in items]
        if overlay or self.show:
            self.world.eval(f"__ws.overlay({json.dumps(items)})")
        png = self.world.screenshot()
        if overlay or self.show:
            self.world.eval("__ws.clearOverlay()")
        annotated = annotate.marks(png, [m.__dict__ for m in els], meta["viewport"])
        self._last = Marks(elements=els, png=annotated, url=meta["url"], title=meta["title"], dpr=meta["dpr"], viewport=meta["viewport"])
        return self._last

    def _mark_from(self, ref: int | Mark) -> Mark:
        if isinstance(ref, Mark):
            return ref
        if self._last is None:
            raise RuntimeError("call page.mark() before acting by index")
        return self._last[ref]

    def _spec(self, ref: Any) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        """(candidates, fingerprint, frame) from a Mark, index, css string, or workflow Target."""
        if isinstance(ref, str):
            return [{"kind": "css", "value": ref}], {}, []
        if hasattr(ref, "candidates") and hasattr(ref, "fingerprint"):
            return ref.candidates, ref.fingerprint, list(getattr(ref, "frame", []) or [])
        m = self._mark_from(ref)
        return m.candidates, m.fingerprint, m.frame

    def _opts(self, ref: Any, **extra: Any) -> dict[str, Any]:
        _, _, frame = self._spec(ref)
        o: dict[str, Any] = {"frame": frame}
        off = getattr(ref, "offset", None) if not isinstance(ref, (str, int)) else None
        if off:
            o["offset"] = off
        o.update(extra)
        return o

    # -- resolution --------------------------------------------------------

    def locate(self, ref: Any, *, timeout_ms: int | None = None, **extra: Any) -> dict[str, Any]:
        """Resolve to a live element, polling until it appears. Scrolls it into view."""
        cands, fp, frame = self._spec(ref)
        opts = self._opts(ref, **extra)
        deadline = time.time() + (self.timeout_ms if timeout_ms is None else timeout_ms) / 1000
        last: dict[str, Any] = {}
        while True:
            try:
                last = self._resolve(cands, fp, opts)
            except Exception as e:  # mid-navigation
                last = {"found": False, "best": str(e)[:80]}
            if last.get("found"):
                return last
            if time.time() >= deadline:
                raise NotFound(f"element not found after {int((self.timeout_ms if timeout_ms is None else timeout_ms) / 1000)}s (best: {last.get('best')})")
            # A stray real mouse move (headed window) or a re-render can drop a CSS :hover menu;
            # re-assert the last hover so hover-revealed targets come back.
            if self._hover_at and time.time() - self._hover_at[2] < 15:
                self.input.move(self._hover_at[0], self._hover_at[1])
            self._sleep(250)

    def _resolve(self, cands: list[dict[str, Any]], fp: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
        """resolve() in the right document: the top page, a same-origin frame (core.js descends the
        chain itself), or a cross-origin frame's own session with the result lifted to page coords."""
        chain = opts.get("frame") or []
        r = self.world.call("resolve", cands, fp, opts)
        if not r.get("found") and any(" >>> " in c.get("value", "") for c in cands):
            if self.world.attach_closed_roots():
                r = self.world.call("resolve", cands, fp, opts)
        if r.get("found") or not chain:
            return r
        fs = self.world.frame_for_chain(chain)
        if fs is None:
            return r
        sub = dict(opts); sub["frame"] = chain[1:] if fs.chain and chain[:1] == fs.chain[:1] else []
        r2 = fs.eval(f"__ws.resolve({json.dumps(cands)}, {json.dumps(fp)}, {json.dumps(sub)})")
        if r2.get("found"):
            self.world._refresh_frame_offset(fs)
            for key in ("rect", "point"):
                if r2.get(key):
                    r2[key] = {**r2[key], "x": r2[key]["x"] + fs.offset["x"], "y": r2[key]["y"] + fs.offset["y"]}
            r2["_frame"] = fs.session_id
        return r2

    def _point(self, r: dict[str, Any]) -> tuple[float, float]:
        if r.get("point"):
            return r["point"]["x"], r["point"]["y"]
        rect = r["rect"]
        return rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2

    def _flash(self, r: dict[str, Any]) -> None:
        if self.show:
            try:
                self.world.eval(f"__ws.flash({json.dumps(r['rect'])})")
            except Exception:
                pass

    def _call_in(self, r: dict[str, Any] | None, fn: str, *args: Any) -> Any:
        """Call a __ws function where the element lives (top page or the cross-origin frame that resolved it)."""
        fs = self.world.frames.get((r or {}).get("_frame", ""))
        if fs is not None:
            return fs.eval(f"__ws.{fn}({', '.join(json.dumps(a) for a in args)})")
        return self.world.call(fn, *args)

    def _uncover(self, ref: Any, r: dict[str, Any]) -> dict[str, Any]:
        """If something covers the element: wait for loading overlays / network to clear, re-check,
        then nudge the scroll (sticky headers, banners), and only then give up."""
        if r.get("hittable", True):
            return r
        cands, fp, frame = self._spec(ref)
        # A loading overlay is the common case; it goes away by itself.
        self.world.wait_idle(quiet_ms=300, timeout_ms=self.settle_timeout_ms, sleep=self._sleep)
        deadline = time.time() + self.settle_timeout_ms / 1000
        while time.time() < deadline:
            r2 = self.world.call("resolve", cands, fp, self._opts(ref, scroll=False))
            if r2.get("found") and r2.get("hittable"):
                return r2
            if not self.world.busy():
                break
            self._sleep(150)
        for dy in (-120, 240, -360):
            self.input.wheel(0, dy)
            self._sleep(120)
            r2 = self.world.call("resolve", cands, fp, self._opts(ref, scroll=False))
            if r2.get("found") and r2.get("hittable"):
                return r2
        r["covered"] = True
        return r

    # -- actions -------------------------------------------------------------

    def _canvas_point(self, ref: Any) -> dict[str, Any] | None:
        """With canvas hooks on: relocate by what the page drew (row/column anchors, label, image)."""
        desc = getattr(ref, "canvas", None)
        if not desc or not self.canvas_hooks:
            return None
        from . import canvas

        css = [c["value"] for c in getattr(ref, "candidates", []) if c["kind"] == "css"]
        self.world.install_canvas_hooks()
        # make sure a frame exists: a resize-less repaint is app-specific, so give it a moment and read
        for _ in range(10):
            frame = self.world.canvas_frame(css)
            if frame and frame.get("total"):
                break
            self._sleep(100)
        else:
            return None
        r = canvas.relocate(frame, desc)
        # Off screen: scroll the canvas the way the anchors suggest and look again, a few times.
        tries = 0
        while r and r.get("off_screen") and r.get("scroll") and tries < 6:
            tries += 1
            cb = frame.get("canvas") or [0, 0, 400, 300]
            cx, cy = cb[0] + cb[2] / 2, cb[1] + cb[3] / 2
            dx, dy = {"up": (0, -300), "down": (0, 300), "left": (-300, 0), "right": (300, 0)}[r["scroll"]]
            self.input.wheel(dx, dy, cx, cy)
            self._sleep(250)
            frame = self.world.canvas_frame(css) or frame
            r = canvas.relocate(frame, desc)
        if not r:
            return None
        if r.get("off_screen"):
            return {"found": True, "kind": "coordinates", "score": 0, "point": {"x": desc_point(ref)[0], "y": desc_point(ref)[1]}, "rect": {"x": 0, "y": 0, "w": 8, "h": 8}, "hittable": False, "fallback": "not-on-screen", "why": f"{r['missing'].replace('_', ' ')} “{r['text']}” is not drawn on the canvas right now" + (f"; tried scrolling {r['scroll']}" if r.get("scroll") else ""), "skip": True}
        return {"found": True, "kind": "canvas", "via": r["via"], "score": 1.0, "point": {"x": r["x"], "y": r["y"]}, "rect": {"x": r["x"] - 4, "y": r["y"] - 4, "w": 8, "h": 8}, "hittable": True}

    def _visual_point(self, ref: Any) -> dict[str, Any] | None:
        """For a target with stored crops: find the click point in the current screenshot."""
        crop = getattr(ref, "crop", None)
        base = getattr(ref, "_dir", None) or getattr(self, "base_dir", None)
        if not crop or not base:
            return None
        from . import visual

        vt = visual.load(crop, base)
        if not any(k in vt for k in ("context", "target")):
            return None
        vp = self.world._top_viewport()
        shot = self.world.screenshot()
        r = visual.locate(shot, vt, vp, near=(crop.get("x", 0), crop.get("y", 0)))
        if r.get("found"):
            return {"found": True, "kind": "visual", "score": r["score"], "via": r["via"], "zoom": r["zoom"], "point": {"x": r["x"], "y": r["y"]}, "rect": {"x": r["x"] - 4, "y": r["y"] - 4, "w": 8, "h": 8}, "hittable": True}
        why = "ambiguous: the recorded spot matches more than one place" if r.get("ambiguous") else f"no visual match (best {r.get('score', 0):.2f})"
        return {"found": True, "kind": "coordinates", "score": r.get("score", 0), "point": {"x": crop["x"], "y": crop["y"]}, "rect": {"x": crop["x"] - 4, "y": crop["y"] - 4, "w": 8, "h": 8}, "hittable": True, "fallback": "recorded-coordinates", "why": why, "candidate": r.get("candidate")}

    def click(self, ref: Any, *, count: int = 1, modifiers: list[str] | None = None) -> dict[str, Any]:
        """Click by mark index, Mark, CSS selector or workflow Target. Real mouse events at the centre.
        Canvas-like targets with a stored crop are located visually; if the element stays covered,
        falls back to a programmatic click and flags it."""
        vis = (self._canvas_point(ref) or self._visual_point(ref)) if getattr(ref, "crop", None) or getattr(ref, "canvas", None) else None
        if vis is not None and vis.get("skip"):
            vis["wait"] = {}
            return vis  # not on screen: do not click anything
        if vis is not None:
            x, y = self._point(vis)
            self._flash(vis)
            self._ghost(True)
            try:
                self.input.click(x, y, count=count, modifiers=modifiers)
            finally:
                self._ghost(False)
            vis["wait"] = self._settle()
            return vis
        r = self._uncover(ref, self.locate(ref))
        self._flash(r)
        self._action_mark = self.world.mark_now()
        if r.get("covered") and not modifiers:
            cands, fp, frame = self._spec(ref)
            self.world.call("act", cands, fp, {"frame": frame}, "click")
            r["fallback"] = "js-click"
        else:
            x, y = self._point(r)
            self._ghost(True)
            try:
                self.input.click(x, y, count=count, modifiers=modifiers)
            finally:
                self._ghost(False)
        r["wait"] = self._settle()
        return r

    def click_background(self, ref: Any) -> dict[str, Any]:
        """A click on empty page area (dismiss/focus). Only performed if the spot is still inert,
        so replay never accidentally activates something that moved there."""
        try:
            r = self.locate(ref, timeout_ms=1500)
        except NotFound:
            return {"skipped": "background target gone"}
        x, y = self._point(r)
        if not self.world.call("inertAt", x, y):
            return {"skipped": "background spot now interactive"}
        self._ghost(True)
        try:
            self.input.click(x, y)
        finally:
            self._ghost(False)
        r["wait"] = self._settle(150)
        return r

    def dblclick(self, ref: Any) -> dict[str, Any]:
        return self.click(ref, count=2)

    def hover(self, ref: Any) -> dict[str, Any]:
        r = self.locate(ref)
        self._flash(r)
        x, y = self._point(r)
        self._ghost(True)
        try:
            self.input.hover(x, y)
        finally:
            self._ghost(False)
        self._hover_at = (x, y, time.time())
        return r

    def type(self, ref: Any, text: str, *, clear: bool = True, delay_ms: int = 20) -> dict[str, Any]:
        r = self.click(ref)
        if clear:
            self.input.select_all()
            self.input.press("Backspace")
        self.input.type(text, delay_ms)
        # Masked/controlled inputs sometimes eat keystrokes; verify and fall back to a value set.
        cands, fp, frame = self._spec(ref)
        sub = {"frame": [] if r.get("_frame") else frame}
        try:
            got = self._call_in(r, "value", cands, fp, sub)
        except Exception:
            got = None
        if got is not None and got != text and fp.get("type") not in ("password",) and len(text) > 0 and not _looks_masked(got, text):
            self._call_in(r, "act", cands, fp, sub, "setValue", text)
            r["fallback"] = "set-value"
        return r

    def press(self, key: str) -> None:
        self._action_mark = self.world.mark_now()
        self.input.press(key)
        self._settle()

    def select(self, ref: Any, value: str) -> dict[str, Any]:
        r = self.locate(ref)
        self._flash(r)
        cands, fp, frame = self._spec(ref)
        res = self.world.call("act", cands, fp, {"frame": frame}, "select", value)
        if not res.get("ok"):
            # Maybe the recorded value is a label; try matching option text.
            label_js = (
                "(() => { const el = __ws.handle(%s, %s, %s); if (!el) return false;"
                " const o = [...el.options].find(o => o.textContent.trim() === %s); if (!o) return false;"
                " el.value = o.value; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); return true; })()"
            ) % (json.dumps(cands), json.dumps(fp), json.dumps({"frame": frame}), json.dumps(value))
            if not self.world.eval(label_js):
                raise NotFound(f"could not select {value!r}")
        self._settle()
        return r

    def check(self, ref: Any, value: bool = True) -> dict[str, Any]:
        """Set a checkbox/radio. The input itself is often invisible (custom styling): if the state
        already matches this is a no-op; otherwise click it, or its label, or toggle programmatically."""
        r = self.locate(ref, allowHidden=True)
        if r.get("checked") is not None and bool(r["checked"]) == bool(value):
            r["noop"] = True
            return r
        cands, fp, frame = self._spec(ref)
        if r.get("hittable"):
            x, y = self._point(r)
            self._flash(r)
            self.input.click(x, y)
            self._settle(150)
            now = self.world.call("resolve", cands, fp, self._opts(ref, allowHidden=True, scroll=False))
            if now.get("checked") is not None and bool(now["checked"]) == bool(value):
                return r
        res = self.world.call("act", cands, fp, {"frame": frame}, "check", bool(value))
        if not res.get("ok"):
            raise NotFound("could not set checkbox state")
        r["fallback"] = "label-or-js"
        self._settle(150)
        return r

    def set_value(self, ref: Any, value: str) -> dict[str, Any]:
        """For range/date/color inputs where typing does not apply."""
        r = self.locate(ref)
        cands, fp, frame = self._spec(ref)
        self.world.call("act", cands, fp, {"frame": frame}, "setValue", value)
        return r

    def drag(self, ref: Any, to: tuple[float, float] | dict[str, float], *, from_: tuple[float, float] | None = None) -> dict[str, Any]:
        r = self.locate(ref)
        self._flash(r)
        x0, y0 = from_ if from_ else self._point(r)
        x1, y1 = (to["x"], to["y"]) if isinstance(to, dict) else to
        self._ghost(True)
        try:
            self.input.drag(x0, y0, x1, y1)
        finally:
            self._ghost(False)
        self._settle(150)
        return r

    def upload(self, ref: Any, files: list[str]) -> dict[str, Any]:
        r = self.locate(ref)
        cands, fp, frame = self._spec(ref)
        paths = [str(Path(f).expanduser().resolve()) for f in files]
        for p in paths:
            if not Path(p).exists():
                raise FileNotFoundError(p)
        oid = self.world.object_id("handle", cands, fp, {"frame": frame})
        if not oid:
            raise NotFound("file input not found")
        self.world.set_files(oid, paths)
        self._settle(150)
        return r

    def scroll(self, dy: int = 600, *, ref: Any = None, to: tuple[float, float] | None = None) -> None:
        if ref is not None and to is not None:
            cands, fp, frame = self._spec(ref)
            self.world.call("act", cands, fp, {"frame": frame}, "scroll", {"x": to[0], "y": to[1]})
        elif to is not None:
            self.world.eval(f"scrollTo({to[0]}, {to[1]})")
        else:
            self.input.wheel(0, dy)
        self._settle(150)

    # -- tabs ----------------------------------------------------------------

    def wait_for_popup(self, timeout_ms: int = 10000) -> PwPage | None:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self.popups:
                return self.popups.pop(0)
            self._sleep(100)
        return None

    # -- capture -------------------------------------------------------------

    def screenshot(self, path: str | Path | None = None, *, highlight: int | Mark | None = None) -> bytes:
        png = self.world.screenshot()
        if highlight is not None:
            m = self._mark_from(highlight)
            png = annotate.highlight(png, m.rect, self._last.viewport if self._last else None)
        if path:
            Path(path).write_bytes(png)
        return png

    def text(self, max_chars: int = 20000) -> str:
        """Visible text of the page, for a model that needs to read rather than click."""
        t = self.world.eval("document.body ? document.body.innerText : ''") or ""
        return t[:max_chars]

    def close(self) -> None:
        self.world.close()
        try:
            self.pw.close()
        except Exception:
            pass


def desc_point(ref: Any) -> tuple[float, float]:
    c = getattr(ref, "crop", None) or {}
    return float(c.get("x", 0)), float(c.get("y", 0))


def _looks_masked(got: str, want: str) -> bool:
    """Phone/card/date masks reformat input; accept if the digits survived."""
    d = lambda s: "".join(c for c in s if c.isalnum())
    return bool(d(want)) and d(got) == d(want)
