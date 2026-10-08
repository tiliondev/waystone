"""Record a human session into a Workflow.

Outputs, for a recording named `book-table` in `out_dir`:
  book-table.json        the workflow (steps, selectors, fingerprints)
  book-table_steps/      one PNG per step, target boxed in red
  book-table.mp4         screencast of the whole session (gif if ffmpeg is missing)
  book-table.md          human-readable trace: every step with its screenshot

Follows new tabs/popups, same-origin iframes and shadow DOM. Password fields are never
stored: they become {{param}} placeholders.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Page as PwPage

from . import annotate
from .engine import Engine
from .video import Screencast
from .workflow import Step, Target, Workflow
from .world import World


def _is_background(tgt: dict[str, Any]) -> bool:
    """A click on a huge, non-interactive container: a dismiss/focus click, not an action."""
    fp, r, vp = tgt.get("fingerprint", {}), tgt.get("rect", {}), tgt.get("viewport", {})
    if fp.get("role") not in (None, "generic") or fp.get("pointer") or fp.get("tag") in ("canvas", "video", "svg"):
        return False
    area = r.get("w", 0) * r.get("h", 0)
    return area > 0.3 * vp.get("w", 1280) * vp.get("h", 800)


def _panel_text(s: Step) -> str:
    d = s.describe()
    for prefix in (s.type + " ", "navigate ", "new tab ", "switch to "):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d[:90]


def _short(url: str, n: int = 60) -> str:
    u = url.split("?")[0]
    return u if len(u) <= n else "…" + u[-(n - 1):]


@dataclass
class _Tab:
    index: int
    page: PwPage
    world: World
    screencast: Screencast | None = None
    pending_shot: tuple[dict[str, Any], bytes] | None = None
    closed: bool = False
    last_action_t: float = 0.0
    settling: Any = None  # Step currently being measured for settle time
    rec_ctx: int | None = None  # execution context of the recorder world in the top frame
    ctx_link: tuple[dict[str, Any], float] | None = None  # last right-clicked link, for "Open in new tab"


class Recorder:
    """
    rec = Recorder(engine, "book-table", out_dir="out")
    rec.start("https://opentable.com")
    wf = rec.wait()        # human drives; close the tab(s) or Ctrl+C to stop
    """

    def __init__(
        self,
        engine: Engine,
        name: str,
        *,
        out_dir: str | Path = ".",
        video: bool = True,
        show: bool = True,
        panel: bool = True,
        goal: str | None = None,
        on_step: Callable[[Step], None] | None = None,
    ):
        self.engine = engine
        self.name = name
        self.out_dir = Path(out_dir)
        self.folder = self.out_dir / name
        self.shots_dir = self.folder / "steps"
        self.video = video
        self.show = show
        self.panel = panel
        self.goal = goal
        self.on_step = on_step
        self.paused = False
        self._stop_requested = False
        self._last_push = 0.0
        self.workflow: Workflow | None = None
        self.tabs: list[_Tab] = []
        self.active: int = 0
        self.artifacts: dict[str, Path] = {}
        self._t0 = 0.0
        self._secret_n = 0
        self._pending_new: list[Step] = []
        self._stopped = False
        self._ctx = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def pw_page(self) -> PwPage | None:
        return self.tabs[0].page if self.tabs else None

    @property
    def world(self) -> World | None:
        return self.tabs[0].world if self.tabs else None

    def start(self, url: str | None = None, *, page: PwPage | None = None) -> "Recorder":
        """Open `url` in a new tab, or attach to an existing Playwright `page`."""
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        ctx = self.engine.context
        pw_page = page or ctx.new_page()
        self._t0 = time.time()
        tab = self._attach(pw_page)
        vp = tab.world.eval("({width: innerWidth, height: innerHeight})") or {"width": 1280, "height": 800}
        self.workflow = Workflow(
            name=self.name,
            start_url=url or pw_page.url,
            viewport=vp,
            engine=self.engine.version,
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )
        self._ctx = ctx
        ctx.on("page", self._on_new_page)
        if url:
            tab.world.navigate(url)
            self._wait_ready(tab)
        self._add_shot(tab, Step(index=0, type="navigate", url=self.workflow.start_url, t=0), None)
        return self

    def _attach(self, pw_page: PwPage) -> _Tab:
        world = World(pw_page)
        tab = _Tab(index=len(self.tabs), page=pw_page, world=world)
        self.tabs.append(tab)
        world.listen(lambda ev, t=tab: self._on_event(t, ev))
        world.on_navigated(lambda url, t=tab: self._on_nav(t, url))
        world.on_dialog(lambda ev, t=tab: self._on_dialog(t, ev))
        pw_page.on("close", lambda _, t=tab: self._on_close(t))
        world.cdp.on("Inspector.detached", lambda _, t=tab: self._on_close(t))
        world.start_recording(show=self.show, panel=self.panel)
        if self.video:
            vp = world.eval("({w: innerWidth, h: innerHeight, dpr: devicePixelRatio})") or {"w": 1280, "h": 800, "dpr": 1}
            dpr = min(2.0, float(vp["dpr"]))
            tab.screencast = Screencast(world.cdp, max_width=int(vp["w"] * dpr), max_height=int(vp["h"] * dpr), quality=85)
            tab.screencast.start()
        return tab

    def _on_new_page(self, pw_page: PwPage) -> None:
        if self._stopped or any(t.page is pw_page for t in self.tabs):
            return
        try:
            tab = self._attach(pw_page)
        except Exception:
            return
        new_step = Step(index=0, type="new-tab", tab=tab.index, url=tab.world.url or pw_page.url, t=self._rel(None))
        # Right-click → "Open link in new tab": the context menu is native, so the only trace is the
        # contextmenu event on the link. Record it as a modifier-click, which replay can perform.
        for t in self.tabs:
            if t.ctx_link and time.time() - t.ctx_link[1] < 10:
                tgt, _ = t.ctx_link
                t.ctx_link = None
                mods = ["Meta"] if sys.platform == "darwin" else ["Control"]
                self._append(Step(index=0, type="click", t=self._rel(None), tab=t.index, target=Target.from_js(tgt), modifiers=mods, note="open in new tab"))
                self._append(new_step)
                return
        # Non-blocking (runs inside Playwright's event dispatch). The popup event arrives before the
        # click that opened it, so the new-tab step is parked and emitted after that click.
        self._pending_new.append((new_step, time.time()))

    def _on_close(self, tab: _Tab) -> None:
        tab.closed = True

    @property
    def all_closed(self) -> bool:
        return bool(self.tabs) and all(t.closed for t in self.tabs)

    def wait(self, poll_ms: int = 100) -> Workflow:
        """Block until every recorded tab is closed or KeyboardInterrupt, then finalize."""
        try:
            while not self.all_closed and not self._stop_requested:
                live = next((t for t in self.tabs if not t.closed), None)
                if live is None:
                    break
                try:
                    live.page.wait_for_timeout(poll_ms)
                except Exception as e:  # tab closed under us
                    if "closed" not in str(e).lower() and os.environ.get("WAYSTONE_DEBUG"):
                        print(f"[waystone] wait loop: {type(e).__name__}: {e}", flush=True)
                    live.closed = True
                self._measure_settle()
                self._flush_new(older_than=1.5)  # a tab nobody clicked for (user-opened, ⌘T): record it as is
                if time.time() - self._last_push > 1.0:
                    self._last_push = time.time()
                    for t in self.tabs:
                        if not t.closed:
                            self._push_panel(t)
        except KeyboardInterrupt:
            pass
        return self.stop()

    def stop(self) -> Workflow:
        assert self.workflow is not None
        self._stopped = True
        if self._ctx is not None:
            try:
                self._ctx.remove_listener("page", self._on_new_page)
            except Exception:
                pass
        self._flush_new()
        frames: list[tuple[float, bytes]] = []
        for tab in self.tabs:
            if tab.screencast:
                tab.screencast.stop()
                frames.extend(tab.screencast.frames)
            try:
                tab.world.stop_recording()
            except Exception:
                pass
            if not tab.closed:
                try:
                    tab.page.close()
                except Exception:
                    pass
            tab.world.close()
        self.artifacts["steps"] = self.shots_dir
        video_path = None
        if self.video and len(frames) > 1:
            frames.sort(key=lambda f: f[0])
            sc = Screencast.__new__(Screencast)
            sc.frames = frames
            vw, vh = self.workflow.viewport["width"], self.workflow.viewport["height"]
            steps = [{"t_ms": s.t, "rect": s.target.rect if s.target else None, "viewport": {"w": vw, "h": vh}} for s in self.workflow.steps]
            video_path = sc.write(self.folder / "video.mp4", t0=self._t0, steps=steps)
        from .automation import write_folder
        self.artifacts.update(write_folder(self.workflow, self.folder, goal=self.goal, video=video_path))
        self.artifacts["folder"] = self.folder
        self.workflow._dir = self.folder
        for tab in self.tabs:  # flip the HUD to Saved on any tab still open (worlds are closed by now, so best effort)
            try:
                tab.world.eval_in(tab.rec_ctx, "globalThis.__ws_panel && __ws_panel.saved()")
            except Exception:
                pass
        return self.workflow

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *exc: object) -> None:
        if self.workflow and self.tabs:
            self.stop()

    # -- helpers ---------------------------------------------------------------

    def _wait_ready(self, tab: _Tab, timeout_ms: int = 8000) -> None:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline and tab.world.ready_state() not in ("interactive", "complete"):
            tab.page.wait_for_timeout(50)
        tab.page.wait_for_timeout(200)

    def _measure_settle(self) -> None:
        """After an action, note how long until the page went quiet and what it loaded.
        Replay uses this as its wait budget and the trace shows it."""
        for tab in self.tabs:
            step = tab.settling
            if step is None or tab.closed:
                continue
            try:
                idle = tab.world.is_idle(300, since=getattr(step, "_mark", None))
            except Exception:
                idle = True
            elapsed = int((time.time() - tab.last_action_t) * 1000)
            if idle or elapsed > 15000:
                step.settle_ms = max(0, elapsed - 300)
                step.loaded = [_short(u) for u in tab.world.requests_since(tab.last_action_t)][:8]
                tab.settling = None

    def _add(self, step: Step) -> None:
        assert self.workflow is not None
        # After a page change, the first interaction's element is what "ready" means: record it as a
        # wait step so replay (and agents) know what to wait for before continuing.
        if step.type in ("click", "dblclick", "type", "select", "check", "hover", "set", "upload") and step.target is not None and self.workflow.steps:
            prev = self.workflow.steps[-1]
            if prev.type in ("navigate", "new-tab", "switch-tab") or (prev.type == "scroll" and len(self.workflow.steps) > 1 and self.workflow.steps[-2].type in ("navigate", "new-tab", "switch-tab")):
                w = Step(index=0, type="wait", tab=step.tab, t=step.t, target=Target(candidates=step.target.candidates, fingerprint=step.target.fingerprint, rect=step.target.rect, frame=step.target.frame), note="page ready")
                w.index = len(self.workflow.steps)
                self.workflow.steps.append(w)  # no HUD push here: that yields, and a nested event could slip in between the wait and its step
        pending = self._pending_new
        if pending and step.type != "new-tab":
            if step.tab in {p.tab for p, _ in pending}:
                self._flush_new()  # first event from the new tab: the tab must exist before it
            else:
                self._append(step)  # the click that opened the tab comes first
                self._flush_new()
                return
        self._append(step)

    def _add_shot(self, tab: _Tab, step: Step, target: dict[str, Any] | None, png: bytes | None = None) -> Step:
        """Append first, capture second: the screenshot call yields to the event loop, and a later
        event must not get its step in before this one."""
        self._add(step)
        step.screenshot = self._shot(tab, target, png, index=step.index, step=step)
        return step

    def _push_panel(self, tab: _Tab) -> None:
        """Send the canonical step list to the HUD in that tab (best effort)."""
        if not self.panel or tab.rec_ctx is None or tab.closed or not self.workflow:
            return
        steps = [{"i": s.index, "kind": s.type, "text": _panel_text(s)} for s in self.workflow.steps[-40:]]
        payload = {"steps": steps, "t0": int(self._t0 * 1000), "paused": self.paused}
        try:
            tab.world.eval_in(tab.rec_ctx, f"globalThis.__ws_panel && __ws_panel.set({json.dumps(payload)})")
        except Exception:
            pass  # context mid-navigation; the next `ready` brings a fresh one and the wait loop re-pushes

    def _append(self, step: Step) -> None:
        assert self.workflow is not None
        step.index = len(self.workflow.steps)
        self.workflow.steps.append(step)
        if step.type not in ("scroll", "new-tab", "switch-tab", "dialog"):
            tab = self.tabs[step.tab] if step.tab < len(self.tabs) else None
            if tab is not None:
                if tab.settling is not None:
                    self._measure_settle()
                tab.last_action_t = time.time()
                step._mark = (tab.world.mark_now() or 0) - 150  # type: ignore[attr-defined]
                tab.settling = step
        if self.on_step:
            self.on_step(step)
        if step.tab < len(self.tabs):
            self._push_panel(self.tabs[step.tab])

    def _flush_new(self, older_than: float | None = None) -> None:
        keep: list[tuple[Step, float]] = []
        flush: list[Step] = []
        for st, at in self._pending_new:
            (flush if older_than is None or time.time() - at > older_than else keep).append(st)
        self._pending_new = keep
        for st in flush:
            self._append(st)

    def _rel(self, t_ms: int | None) -> int:
        now_ms = int(time.time() * 1000)
        return max(0, (t_ms or now_ms) - int(self._t0 * 1000))

    def _shot(self, tab: _Tab, target: dict[str, Any] | None, png: bytes | None = None, *, index: int | None = None, step: Step | None = None) -> str:
        assert self.workflow is not None
        try:
            png = png or tab.world.screenshot()
            if target:
                png = annotate.highlight(png, target["rect"], target.get("viewport"))
            if step is not None:
                from .automation import instruction
                from .export import best, describe_sel
                detail = describe_sel(best(step.target)) if step.target else None
                png = annotate.caption(png, f"{step.index} · {instruction(step)}", detail, (target or {}).get("viewport") or (self.workflow.viewport and {"w": self.workflow.viewport["width"]}))
        except Exception:
            return ""
        name = f"{(len(self.workflow.steps) if index is None else index):03d}.png"
        (self.shots_dir / name).write_bytes(png)
        return f"{self.shots_dir.name}/{name}"

    def _store_crop(self, tab: _Tab, target: Target, png: bytes | None) -> None:
        """Canvas-like target: keep crops around the click point so replay can find it visually."""
        from . import visual

        try:
            png = png or tab.world.screenshot()
            r = target.rect
            x = r["x"] + r["w"] * target.offset["ox"]
            y = r["y"] + r["h"] * target.offset["oy"]
            vp = {"w": self.workflow.viewport["width"], "h": self.workflow.viewport["height"]} if self.workflow and self.workflow.viewport else None
            vt = visual.capture(png, x, y, vp)
            target.crop = visual.save(vt, self.folder, f"{len(self.workflow.steps):03d}")
        except Exception:
            pass

    def _describe_canvas(self, tab: _Tab, target: Target) -> None:
        """Identify the clicked canvas primitive from the display list (label, row/column anchors, cell)."""
        from . import canvas

        try:
            r = target.rect
            x = r["x"] + r["w"] * target.offset["ox"]
            y = r["y"] + r["h"] * target.offset["oy"]
            css = [c["value"] for c in target.candidates if c["kind"] == "css"]
            read = tab.world.canvas_primitives(css, x, y, radius=600)
            desc = canvas.describe(read, x, y) if read and read.get("total") else None
            if desc:
                target.canvas = desc
                if target.crop is not None:
                    target.crop["phrase"] = canvas.phrase(desc)
        except Exception:
            pass

    def _secret_param(self, tgt: dict[str, Any]) -> str:
        fp = tgt.get("fingerprint", {})
        base = re.sub(r"[^a-z0-9]+", "_", (fp.get("name") or "password").lower()).strip("_")[:24] or "password"
        self._secret_n += 1
        return "{{" + (base if self._secret_n == 1 else f"{base}_{self._secret_n}") + "}}"

    # -- events ----------------------------------------------------------------

    def _on_event(self, tab: _Tab, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        if self._stopped:
            return
        if kind == "ready":
            if ev.get("top"):
                tab.rec_ctx = ev.get("_ctx")
                self._push_panel(tab)
            return
        if kind == "control":
            action = ev.get("action")
            if action == "pause":
                self.paused = True
            elif action == "resume":
                self.paused = False
            elif action == "stop":
                self._stop_requested = True
            for t in self.tabs:
                self._push_panel(t)
            return
        if self.paused:
            return
        if tab.index != self.active and kind != "pre-click":
            self.active = tab.index
            self._add(Step(index=0, type="switch-tab", tab=tab.index, t=self._rel(ev.get("t"))))
        if kind == "pre-click":
            # The latest screencast frame predates the mousedown, so it still shows menus/dropdowns
            # the page tears down on mousedown. A fresh screenshot here would often arrive too late.
            try:
                before = tab.screencast.latest() if tab.screencast else None
                tab.pending_shot = (ev["target"], before or tab.world.screenshot())
            except Exception:
                tab.pending_shot = None
            return
        tab.last_action_t = time.time()
        t = self._rel(ev.get("t"))
        tgt = ev.get("target")
        target = Target.from_js(tgt) if tgt else None
        wf = self.workflow
        assert wf is not None
        last = wf.steps[-1] if wf.steps else None

        if kind == "contextmenu":
            tab.ctx_link = (tgt, time.time()) if ev.get("href") else None
            return
        if kind == "click" or kind == "check":
            png = None
            if tab.pending_shot and tab.pending_shot[0]["rect"] == tgt["rect"]:
                png = tab.pending_shot[1]
            tab.pending_shot = None
            step = Step(index=0, type=kind, t=t, tab=tab.index, target=target, modifiers=ev.get("modifiers") or None)
            if target is not None and target.visual and target.offset:
                self._store_crop(tab, target, png)
                self._describe_canvas(tab, target)
            if kind == "check":
                step.value = bool(ev.get("value"))
            elif _is_background(tgt):
                step.note = "background"
            self._add_shot(tab, step, tgt, png)
        elif kind == "check-state":
            if last and last.type == "check" and last.target and last.target.candidates == tgt["candidates"]:
                last.value = bool(ev.get("value"))
        elif kind == "dblclick":
            # Collapse the two preceding clicks on the same target into one dblclick.
            shot = None
            while wf.steps and wf.steps[-1].type == "click" and wf.steps[-1].target and wf.steps[-1].target.candidates == tgt["candidates"]:
                shot = wf.steps.pop().screenshot or shot
            step = Step(index=0, type="dblclick", t=t, tab=tab.index, target=target, screenshot=shot)
            self._add(step)
            if not shot:
                step.screenshot = self._shot(tab, tgt, index=step.index, step=step)
        elif kind == "type":
            if last and last.type == "type" and last.target and last.target.candidates == tgt["candidates"]:
                last.value = self._secret_param(tgt) if last.secret else ev.get("value", "")
                return
            secret = bool(ev.get("secret"))
            value = self._secret_param(tgt) if secret else ev.get("value", "")
            self._add_shot(tab, Step(index=0, type="type", t=t, tab=tab.index, target=target, value=value, secret=secret), tgt)
        elif kind == "press":
            self._add_shot(tab, Step(index=0, type="press", t=t, tab=tab.index, key=ev["key"], target=target), tgt)
        elif kind == "select":
            self._add_shot(tab, Step(index=0, type="select", t=t, tab=tab.index, target=target, value=ev.get("value", ""), note=ev.get("label")), tgt)
        elif kind == "set":
            self._add_shot(tab, Step(index=0, type="set", t=t, tab=tab.index, target=target, value=ev.get("value", "")), tgt)
        elif kind == "hover":
            if last and last.type == "hover" and last.target and last.target.candidates == tgt["candidates"]:
                return
            self._add_shot(tab, Step(index=0, type="hover", t=t, tab=tab.index, target=target), tgt)
        elif kind == "drag":
            tab.pending_shot = None
            self._add_shot(tab, Step(index=0, type="drag", t=t, tab=tab.index, target=target, x=ev["from"]["x"], y=ev["from"]["y"], to=ev["to"]), tgt)
        elif kind == "upload":
            # The browser never exposes the local path, only the name: each file becomes a {{param}}.
            names = ev.get("files", [])
            params = ["{{" + (re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_") or "file") + "}}" for n in names]
            self._add_shot(tab, Step(index=0, type="upload", t=t, tab=tab.index, target=target, files=params, note=", ".join(names)), tgt)
        elif kind == "print":
            self._add_shot(tab, Step(index=0, type="print", t=t, tab=tab.index), None)
        elif kind == "scroll":
            if last and last.type == "scroll" and (last.target.candidates if last.target else None) == (tgt["candidates"] if tgt else None):
                last.x, last.y = ev.get("x", 0), ev.get("y", 0)
                return
            self._add(Step(index=0, type="scroll", t=t, tab=tab.index, target=target, x=ev.get("x", 0), y=ev.get("y", 0), frame=ev.get("frame") or None))

    def _on_nav(self, tab: _Tab, url: str) -> None:
        wf = self.workflow
        if not wf or self._stopped or url.startswith(("about:", "chrome")):
            return
        if len(wf.steps) == 0 or (len(wf.steps) == 1 and url == wf.start_url):
            return
        caused = (time.time() - tab.last_action_t) < 3.0
        self._add(Step(index=0, type="navigate", url=url, tab=tab.index, t=self._rel(None), note="after-action" if caused else None))

    def _on_dialog(self, tab: _Tab, ev: dict[str, Any]) -> None:
        if self._stopped:
            return
        self._add(Step(index=0, type="dialog", tab=tab.index, t=self._rel(None), note=ev.get("type"), value=ev.get("message", "")[:200]))
