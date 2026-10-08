"""MCP server: Waystone as tools for Claude Code, Claude Desktop, Cursor, Codex and any other MCP client.

    waystone mcp            # stdio transport

Tools: record_start / record_status / record_stop, run, show, edit, export, and an agent mode
(page_open, page_mark, page_act, page_close) for pages with no recording.

Sync Playwright objects must be used from the thread that created them, so the browser lives on one
dedicated worker thread and every tool call is forwarded to it. A recording blocks its thread until
the person presses stop in the panel (or record_stop is called), so each recording gets a thread of
its own with its own browser.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path
from typing import Any

try:
    from mcp.server.mcpserver import Image, MCPServer as FastMCP  # mcp 2.x
except ImportError:  # pragma: no cover
    try:
        from mcp.server.fastmcp import FastMCP, Image  # mcp 1.x
    except ImportError as e:
        raise ImportError("The MCP server needs the `mcp` package: pip install 'waystone-browser[mcp]'") from e

from waystone import Waystone, Workflow
from waystone.edit import apply as edit_apply
from waystone.export import FORMATS, export as export_wf, ext as export_ext

mcp = FastMCP("waystone", instructions=(
    "Waystone records a person doing a browser task once and writes a folder with step-by-step "
    "instructions, ranked selectors, screenshots and a replayable workflow. Use record_start to open a "
    "browser for the user, record_status to watch steps arrive, record_stop when they are done, run to "
    "replay a folder, show/edit/export to work with one. page_open/page_mark/page_act drive a page that "
    "has no recording."
))


# ---------------------------------------------------------------- browser worker

class _Worker:
    """One thread that owns a Waystone instance. `run(fn)` executes fn(ws) on that thread."""

    def __init__(self, *, headless: bool = False):
        self.headless = headless
        self._q: queue.Queue[tuple[Any, Future] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.ws: Waystone | None = None
        self.pages: dict[str, Any] = {}

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, fut = item
            try:
                if self.ws is None:
                    self.ws = Waystone(headless=self.headless)
                fut.set_result(fn(self.ws))
            except Exception as e:
                fut.set_exception(e)
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass

    def call(self, fn) -> Any:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="waystone-browser", daemon=True)
            self._thread.start()
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result()

    async def run(self, fn) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, self.call, fn)


_browser = _Worker(headless=False)
_headless = _Worker(headless=True)  # one sync Playwright per thread: headless runs get their own


# ---------------------------------------------------------------- recordings

class _Recording:
    def __init__(self, url: str, name: str, goal: str | None, out_dir: str, video: bool):
        self.id = uuid.uuid4().hex[:8]
        self.url, self.name, self.goal, self.out_dir, self.video = url, name, goal, out_dir, video
        self.rec = None
        self.workflow: Workflow | None = None
        self.error: str | None = None
        self.state = "starting"
        self.folder = str(Path(out_dir) / name)
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._main, name=f"waystone-rec-{self.id}", daemon=True)
        self.thread.start()

    def _main(self) -> None:
        try:
            with Waystone(headless=False) as ws:
                self.rec = ws.recorder(self.name, out_dir=self.out_dir, video=self.video, goal=self.goal)
                self.rec.start(self.url)
                self.state = "recording"
                self.ready.set()
                self.workflow = self.rec.wait()
                self.folder = str(self.rec.folder)
                self.state = "done"
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.state = "error"
        finally:
            self.ready.set()

    def steps(self) -> list[str]:
        wf = self.workflow or (self.rec.workflow if self.rec is not None else None)
        if wf is None:
            return []
        return [f"{s.index}. {s.describe()}" for s in list(wf.steps)]

    def request_stop(self) -> None:
        if self.rec is not None:
            self.rec._stop_requested = True


_recordings: dict[str, _Recording] = {}


def _readme(folder: str) -> str:
    p = Path(folder) / "README.md"
    return p.read_text() if p.exists() else ""


# ---------------------------------------------------------------- recording tools

@mcp.tool()
async def record_start(url: str, name: str, goal: str | None = None, out_dir: str = ".", video: bool = True) -> dict:
    """Open a visible browser at `url` and start recording the user's actions into `<out_dir>/<name>/`.
    Returns immediately with a recording_id. Tell the user: do the task normally, then press
    "Stop and save" in the panel at the top right (or call record_stop). `goal` is one line describing
    what the automation achieves; it goes at the top of the generated README."""
    r = _Recording(url, name, goal, out_dir, video)
    await asyncio.get_running_loop().run_in_executor(None, r.ready.wait, 60)
    _recordings[r.id] = r
    if r.state == "error":
        return {"recording_id": r.id, "state": r.state, "error": r.error}
    return {"recording_id": r.id, "state": r.state, "folder": r.folder,
            "tell_the_user": "A browser window is open. Do the task the way you normally would, then press 'Stop and save' in the panel at the top right."}


@mcp.tool()
async def record_status(recording_id: str) -> dict:
    """State of a recording (starting | recording | done | error) and the steps captured so far."""
    r = _recordings.get(recording_id)
    if r is None:
        return {"error": f"no recording {recording_id}"}
    return {"recording_id": r.id, "state": r.state, "steps": r.steps(), "folder": r.folder, "error": r.error}


@mcp.tool()
async def record_stop(recording_id: str, timeout_s: float = 90) -> dict:
    """Stop a recording (if the user has not already pressed stop), write the folder, and return its
    README text. Waits for the folder to be written."""
    r = _recordings.get(recording_id)
    if r is None:
        return {"error": f"no recording {recording_id}"}
    r.request_stop()
    await asyncio.get_running_loop().run_in_executor(None, r.thread.join, timeout_s)
    if r.thread.is_alive():
        return {"recording_id": r.id, "state": r.state, "error": "recording did not finish in time"}
    out = {"recording_id": r.id, "state": r.state, "folder": r.folder, "steps": r.steps(), "error": r.error}
    if r.state == "done":
        out["readme"] = _readme(r.folder)
    return out


# ---------------------------------------------------------------- folder tools

@mcp.tool()
async def run(folder: str, params: dict[str, str] | None = None, until: int | None = None, headless: bool = False, timeout_s: int = 10, canvas_hooks: bool = False) -> dict:
    """Replay a recorded folder deterministically. `params` fills {{placeholders}} (passwords, typed
    values made parameters with edit). `until` stops after that step index. On a miss the result has
    the failing step, the reason and the path of a screenshot taken at that moment."""
    def _replay(ws: Waystone):
        res = ws.replay(Workflow.load(folder), params or {}, timeout_ms=timeout_s * 1000, until=until, canvas_hooks=canvas_hooks)
        out: dict[str, Any] = {"ok": res.ok, "steps_run": res.steps_run, "final_url": res.final_url, "duration_s": res.duration_s}
        if res.error is not None:
            out["failed_step"] = res.error.step.index
            out["failed_step_description"] = res.error.step.describe()
            out["reason"] = res.error.reason
            if res.error.screenshot:
                p = Path(folder) / "waystone-failure.png"
                p.write_bytes(res.error.screenshot)
                out["failure_screenshot"] = str(p)
        return out

    return await (_headless if headless else _browser).run(_replay)


@mcp.tool()
async def show(folder: str) -> dict:
    """The generated README (step-by-step instructions with selectors and settle times) and the step list of a folder."""
    wf = Workflow.load(folder)
    return {"folder": folder, "name": wf.name, "start_url": wf.start_url, "steps": [f"{s.index}. {s.describe()}" for s in wf.steps], "readme": _readme(folder)}


@mcp.tool()
async def edit(folder: str, remove: list[int] | None = None, waits: list[list[int]] | None = None, moves: list[list[int]] | None = None, values: list[list[str]] | None = None, goal: str | None = None) -> dict:
    """Edit a recorded folder and regenerate README, screenshots numbering and the CDP run.
    remove: step indices to drop. waits: [[after_step, ms], ...]. moves: [[step, before_step], ...].
    values: [[step, new_value], ...] where new_value can be "{{name}}" to make it a parameter. goal: replace the goal line."""
    wf = edit_apply(folder, remove_ids=remove, waits=[(int(a), int(b)) for a, b in (waits or [])], moves=[(int(a), int(b)) for a, b in (moves or [])],
                    values=[(int(a), str(b)) for a, b in (values or [])], goal=goal)
    return {"folder": folder, "steps": [f"{s.index}. {s.describe()}" for s in wf.steps]}


@mcp.tool()
async def export(folder: str, format: str = "playwright-python", out: str | None = None) -> dict:
    """Write the workflow in another format: agent (plain-text brief), playwright-python, playwright-js,
    puppeteer, cdp, devtools (Chrome DevTools Recorder flow). Returns the path and, for text formats, the content."""
    if format not in FORMATS:
        return {"error": f"format must be one of {FORMATS}"}
    wf = Workflow.load(folder)
    text = export_wf(wf, format)
    p = Path(out) if out else Path(folder) / f"{wf.name}{export_ext(format)}"
    p.write_text(text)
    return {"path": str(p), "content": text if len(text) < 60000 else text[:60000] + "\n... (truncated)"}


# ---------------------------------------------------------------- agent mode

@mcp.tool()
async def page_open(url: str) -> dict:
    """Open a page with no recording, for driving it step by step. Returns a page_id for page_mark / page_act / page_close."""
    def go(ws: Waystone):
        pid = uuid.uuid4().hex[:8]
        _browser.pages[pid] = ws.page(url)
        return {"page_id": pid, "url": _browser.pages[pid].url}
    return await _browser.run(go)


@mcp.tool()
async def page_mark(page_id: str, viewport_only: bool = True) -> list:
    """Number every actionable element on the page. Returns the list (index, role, name) as text and the
    screenshot with the numbers drawn on it. Act on an element by its index with page_act."""
    def go(ws: Waystone):
        page = _browser.pages[page_id]
        m = page.mark(viewport_only=viewport_only)
        return m.to_prompt(), m.png
    text, png = await _browser.run(go)
    return [text, Image(data=png, format="png")]


@mcp.tool()
async def page_act(page_id: str, action: str, ref: int | None = None, text: str | None = None, key: str | None = None, dy: int = 600) -> dict:
    """Act on the page. action: click | dblclick | hover | type (ref + text; clears the field first) |
    press (key, e.g. Enter, Tab, Escape) | scroll (dy, negative for up) | back | goto (text = url).
    `ref` is an index from the last page_mark."""
    def go(ws: Waystone):
        page = _browser.pages[page_id]
        if action == "click":
            page.click(ref)
        elif action == "dblclick":
            page.dblclick(ref)
        elif action == "hover":
            page.hover(ref)
        elif action == "type":
            page.type(ref, text or "")
        elif action == "press":
            page.press(key or "Enter")
        elif action == "scroll":
            page.scroll(dy)
        elif action == "back":
            cdp = page.world.cdp
            h = cdp.send("Page.getNavigationHistory")
            if h["currentIndex"] > 0:
                cdp.send("Page.navigateToHistoryEntry", {"entryId": h["entries"][h["currentIndex"] - 1]["id"]})
                page._settle()
        elif action == "goto":
            page.goto(text or "")
        else:
            return {"error": f"unknown action {action}"}
        return {"url": page.url, "waited": page.last_wait}
    return await _browser.run(go)


@mcp.tool()
async def page_close(page_id: str) -> dict:
    """Close a page opened with page_open."""
    def go(ws: Waystone):
        p = _browser.pages.pop(page_id, None)
        if p is not None:
            p.close()
        return {"closed": p is not None}
    return await _browser.run(go)


def serve() -> None:
    mcp.run()


if __name__ == "__main__":
    serve()
