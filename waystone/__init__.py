"""Waystone: record a workflow once, mark the page for agents, replay anywhere."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .engine import Engine
from .page import Mark, Marks, NotFound, Page
from .recorder import Recorder
from .replay import Replayer, ReplayResult, StepError
from .workflow import Step, Target, Workflow

__version__ = "0.2.1"
HOME_URL = os.environ.get("WAYSTONE_HOME", "https://www.google.com")
__all__ = ["Waystone", "Engine", "Page", "Mark", "Marks", "NotFound", "Recorder", "Replayer", "ReplayResult", "StepError", "Workflow", "Step", "Target"]


class Waystone:
    """
    with Waystone() as ws:
        rec = ws.record("book-table", "https://opentable.com")   # human drives, close tab to stop
        page = ws.page("https://opentable.com")                   # agent mode
        marks = page.mark(); page.click(marks.find("Sign in"))
        ws.replay(rec.workflow, params={"email": "a@b.co"})
    """

    def __init__(self, cdp_url: str | None = None, *, engine: str = "fortress", headless: bool = False, launch: bool = True, profile: str | Path | None = None, **fortress_kwargs: Any):
        """`Waystone()` launches Fortress. `Waystone(engine="chrome")` launches a stock Chromium.
        `Waystone("http://127.0.0.1:9222")` attaches to whatever is there."""
        self.engine = Engine.connect(cdp_url, engine=engine, headless=headless, launch=launch, profile=profile, fortress_kwargs=fortress_kwargs or None)

    # -- modes -------------------------------------------------------------

    def page(self, url: str | None = None, *, show: bool = False, timeout_ms: int = 10000) -> Page:
        p = Page(self.engine.context.new_page(), show=show, timeout_ms=timeout_ms)
        if url:
            p.goto(url)
        return p

    def attach(self, match: str | None = None, *, show: bool = False) -> Page:
        """Wrap an already-open tab (the last one, or the first whose url contains `match`)."""
        pages = self.engine.context.pages
        if not pages:
            raise RuntimeError("no open tabs")
        pw = next((p for p in pages if match and match in p.url), pages[-1])
        return Page(pw, show=show)

    def record(self, name: str, url: str | None = None, *, out_dir: str | Path = ".", video: bool = True, show: bool = True, attach: str | None = None, goal: str | None = None, on_step: Any = None) -> Recorder:
        """Record a human session. Returns the finished Recorder; `.workflow` and `.artifacts` hold the outputs.

        `attach="opentable"` records an already-open tab whose url contains that string (so you can log in first).
        """
        rec = Recorder(self.engine, name, out_dir=out_dir, video=video, show=show, goal=goal, on_step=on_step)
        if attach is None and url is None:
            url = HOME_URL
        if attach is not None:
            pages = self.engine.context.pages
            pw = next((p for p in pages if attach in p.url), pages[-1] if pages else None)
            if pw is None:
                raise RuntimeError("no open tab to attach to")
            rec.start(url, page=pw)
        else:
            rec.start(url)
        rec.wait()
        return rec

    def recorder(self, name: str, *, out_dir: str | Path = ".", video: bool = True, show: bool = True, goal: str | None = None, on_step: Any = None) -> Recorder:
        return Recorder(self.engine, name, out_dir=out_dir, video=video, show=show, goal=goal, on_step=on_step)

    def replay(self, wf: Workflow | str | Path, params: dict[str, str] | None = None, *, on_step: Any = None, shots_dir: str | Path | None = None, keep_open: bool = False, timeout_ms: int = 10000, show: bool = False, until: int | None = None, canvas_hooks: bool = False) -> ReplayResult:
        if not isinstance(wf, Workflow):
            wf = Workflow.load(wf)
        return Replayer(self.engine, on_step=on_step, shots_dir=shots_dir, timeout_ms=timeout_ms, show=show, canvas_hooks=canvas_hooks).run(wf, params, keep_open=keep_open, until=until)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.engine.close()

    def __enter__(self) -> "Waystone":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
