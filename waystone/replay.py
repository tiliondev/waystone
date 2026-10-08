"""Replay a Workflow with self-healing selectors, across tabs, iframes and shadow DOM."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Page as PwPage

from .engine import Engine
from .page import NotFound, Page
from .workflow import Step, Workflow, fill_params


class StepError(RuntimeError):
    def __init__(self, step: Step, reason: str, screenshot: bytes | None = None):
        super().__init__(f"step {step.index} ({step.describe()}): {reason}")
        self.step = step
        self.reason = reason
        self.screenshot = screenshot


@dataclass
class ReplayResult:
    ok: bool
    steps_run: int
    healed: list[dict[str, Any]] = field(default_factory=list)
    error: StepError | None = None
    final_url: str | None = None
    duration_s: float = 0.0


class Replayer:
    def __init__(
        self,
        engine: Engine,
        *,
        on_step: Callable[[Step, dict[str, Any] | None], None] | None = None,
        shots_dir: str | Path | None = None,
        timeout_ms: int = 10000,
        show: bool = False,
        canvas_hooks: bool = False,
    ):
        self.canvas_hooks = canvas_hooks
        self.engine = engine
        self.on_step = on_step
        self.shots_dir = Path(shots_dir) if shots_dir else None
        self.timeout_ms = timeout_ms
        self.show = show
        self.tabs: dict[int, Page] = {}
        self._new_pages: list[PwPage] = []

    def run(self, wf: Workflow, params: dict[str, str] | None = None, *, page: Page | None = None, keep_open: bool = False, until: int | None = None) -> ReplayResult:
        """Run every step, or only steps 0..`until` inclusive."""
        params = params or {}
        missing = [p for p in wf.params if p not in params]
        if missing:
            raise KeyError(f"missing params: {', '.join(missing)}")

        ctx = self.engine.context
        ctx.on("page", lambda p: self._new_pages.append(p))
        own_page = page is None
        if page is None:
            page = Page(ctx.new_page(), timeout_ms=self.timeout_ms, show=self.show)
        page.base_dir = wf._dir
        page.canvas_hooks = self.canvas_hooks
        if self.canvas_hooks:
            page.world.install_canvas_hooks()  # before the first navigation, so the first paint is seen
        self.tabs = {0: page}
        res = ReplayResult(ok=False, steps_run=0)
        if self.shots_dir:
            self.shots_dir.mkdir(parents=True, exist_ok=True)

        t0 = time.time()
        prev: Step | None = None
        cur = page
        try:
            for step in wf.steps:
                if until is not None and step.index > until:
                    break
                cur = self._tab_for(step, cur)
                info = self._do(cur, step, prev, params)
                if info:
                    if info.get("fallback"):
                        res.healed.append({"step": step.index, "via": info["fallback"]})
                    elif info.get("kind") not in (None, "css") and info.get("score", 1) < 0.9:
                        res.healed.append({"step": step.index, "via": info.get("kind"), "score": info.get("score")})
                if self.on_step:
                    self.on_step(step, info)
                if self.shots_dir:
                    try:
                        cur.screenshot(self.shots_dir / f"{step.index:03d}.png")
                    except Exception:
                        pass
                res.steps_run += 1
                prev = step
            res.ok = True
        except StepError as e:
            res.error = e
        except Exception as e:
            shot = None
            try:
                shot = cur.screenshot()
            except Exception:
                pass
            idx = min(res.steps_run, len(wf.steps) - 1)
            res.error = StepError(wf.steps[idx], f"{type(e).__name__}: {e}", shot)
        finally:
            res.duration_s = round(time.time() - t0, 2)
            res.final_url = cur.url
            if own_page and not keep_open:
                for p in self.tabs.values():
                    p.close()
        return res

    # -- tabs ---------------------------------------------------------------

    def _tab_for(self, step: Step, cur: Page) -> Page:
        if step.tab in self.tabs:
            return self.tabs[step.tab]
        # A tab we have not seen yet: it should have been opened by the previous action.
        deadline = time.time() + self.timeout_ms / 1000
        while time.time() < deadline:
            fresh = [p for p in self._new_pages if all(p is not t.pw for t in self.tabs.values())]
            if fresh:
                new = Page(fresh[0], timeout_ms=self.timeout_ms, show=self.show)
                new.base_dir = cur.base_dir
                new.canvas_hooks = self.canvas_hooks
                self.tabs[step.tab] = new
                new._settle()
                return new
            cur._sleep(100)
        raise StepError(step, f"expected tab {step.tab} to open, it did not", cur.screenshot())

    # -- steps --------------------------------------------------------------

    def _do(self, page: Page, step: Step, prev: Step | None, params: dict[str, str]) -> dict[str, Any] | None:
        t = step.type
        try:
            # The recorder measured how long this step took to settle; give replay at least that budget.
            budget = max(self.timeout_ms, int((step.settle_ms or 0) * 2) + 2000)
            page.settle_timeout_ms = budget
            if t == "navigate":
                # A navigation right after an action is a consequence, not an instruction: wait for it.
                if step.note == "after-action" and prev and prev.type not in ("navigate", "new-tab", "switch-tab"):
                    before = getattr(prev, "_url_before", None)
                    if before is not None and before == page.url:
                        page.wait_for_url_change(before, self.timeout_ms)
                    else:
                        page._settle(500)
                    return None
                page.goto(step.url or "")
                return None
            if t in ("new-tab", "switch-tab"):
                page._settle()
                return None
            if t == "dialog":
                return None  # auto-handled by the World's dialog policy
            if t == "wait":
                if step.target:
                    page.locate(step.target)
                elif step.settle_ms:
                    page._sleep(step.settle_ms)
                else:
                    page._settle()
                return None
            if t == "print":
                # The human printed; the replay produces the PDF they would have got.
                if self.shots_dir:
                    out = self.shots_dir / f"{step.index:03d}.pdf"
                    out.write_bytes(page.world.pdf())
                    step.note = out.name
                    return {"pdf": str(out)}
                return None
            if t == "scroll":
                if step.target:
                    page.scroll(ref=step.target, to=(step.x or 0, step.y or 0))
                else:
                    page.scroll(to=(step.x or 0, step.y or 0))
                return None
            if t == "press":
                step._url_before = page.url  # type: ignore[attr-defined]
                page.press(step.key or "Enter")
                return None

            assert step.target is not None, f"step {step.index} has no target"
            step._url_before = page.url  # type: ignore[attr-defined]
            if t == "click":
                if step.note == "background":
                    return page.click_background(step.target)
                r = page.click(step.target, modifiers=step.modifiers)
                if r.get("skip"):
                    raise StepError(step, r.get("why", "target not on screen"), _safe_shot(page))
                return r
            if t == "dblclick":
                return page.dblclick(step.target)
            if t == "hover":
                return page.hover(step.target)
            if t == "type":
                return page.type(step.target, fill_params(str(step.value or ""), params))
            if t == "select":
                return page.select(step.target, str(step.value or ""))
            if t == "check":
                return page.check(step.target, bool(step.value))
            if t == "set":
                return page.set_value(step.target, str(step.value or ""))
            if t == "drag":
                return page.drag(step.target, step.to or {"x": step.x or 0, "y": step.y or 0}, from_=(step.x, step.y) if step.x is not None else None)
            if t == "upload":
                files = [fill_params(f, params) for f in (step.files or [])]
                return page.upload(step.target, files)
        except NotFound as e:
            raise StepError(step, str(e), _safe_shot(page)) from e
        except FileNotFoundError as e:
            raise StepError(step, f"file not found: {e}", None) from e
        raise StepError(step, f"unknown step type {t}")


def _safe_shot(page: Page) -> bytes | None:
    try:
        return page.screenshot()
    except Exception:
        return None
