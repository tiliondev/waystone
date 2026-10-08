"""Isolated-world plumbing over raw CDP.

Everything Waystone injects lives in a dedicated isolated world named "waystone".
Page scripts share the DOM with it but cannot see its globals, so no `__ws`
function ever shows up on the page's `window`, and the recorder's binding is
registered for that world only. This keeps Fortress's stealth intact.
"""
from __future__ import annotations

import base64
import time
import json
from importlib import resources
from typing import Any, Callable

from playwright.sync_api import CDPSession, Page

WORLD = "waystone"
BINDING = "__waystone_emit"

# The one thing injected into the main world: window.print() opens a native modal dialog that no
# CDP command can close and that freezes the page. The stub turns it into a DOM event instead,
# which the recorder captures as a `print` step and replay fulfils with Page.printToPDF.
PRINT_STUB = (
    "(() => { if (window.print && window.print.__ws) return; const d = document;"
    " const p = function print() { try { d.dispatchEvent(new CustomEvent('waystone:print')); } catch (e) {} };"
    " p.__ws = 1; try { Object.defineProperty(window, 'print', { value: p, writable: true, configurable: true }); } catch (e) { window.print = p; } })();"
)


def _js(name: str) -> str:
    return resources.files("waystone").joinpath("js", name).read_text(encoding="utf-8")


CORE_JS = _js("core.js")
RECORDER_JS = _js("recorder.js")
CANVAS_HOOK_JS = _js("canvas_hook.js")
PANEL_JS = _js("panel.js").replace("/* ASSETS_PLACEHOLDER */", _js("panel_assets.js"))


class _FrameSession:
    """A cross-origin iframe. It is its own CDP target, so it gets its own session, its own isolated
    world and its own recorder injection. Coordinates it reports are local to the frame; `offset`
    (the frame element's position in the top page, maintained by World) turns them into page points."""

    def __init__(self, world: "World", session_id: str, target_id: str, info: dict[str, Any]):
        self.world = world
        self.session_id = session_id
        self.target_id = target_id
        self.url = info.get("url", "")
        self.frame_id: str | None = None
        self.ctx: int | None = None
        self.offset = {"x": 0.0, "y": 0.0}
        self.chain: list[str] = []  # css path of the iframe element(s) from the top document

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.world._send_to(self.session_id, method, params)

    def ensure_context(self) -> int:
        if self.ctx is not None:
            return self.ctx
        if self.frame_id is None:
            self.frame_id = self.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
        self.ctx = self.send("Page.createIsolatedWorld", {"frameId": self.frame_id, "worldName": WORLD})["executionContextId"]
        self._raw_eval(CORE_JS, self.ctx)
        return self.ctx

    def _raw_eval(self, expression: str, ctx: int, by_value: bool = True) -> Any:
        r = self.send("Runtime.evaluate", {"expression": expression, "contextId": ctx, "returnByValue": by_value, "awaitPromise": True})
        if "exceptionDetails" in r:
            ex = r["exceptionDetails"]
            raise RuntimeError(ex.get("exception", {}).get("description") or ex.get("text") or "evaluation failed")
        res = r.get("result", {})
        return res.get("value") if by_value else res

    def eval(self, expression: str, by_value: bool = True) -> Any:
        for attempt in range(2):
            ctx = self.ensure_context()
            try:
                return self._raw_eval(expression, ctx, by_value)
            except Exception as e:
                self.ctx = None
                if attempt == 1 or "context" not in str(e).lower():
                    raise
        raise RuntimeError("unreachable")


class World:
    """One isolated world per Playwright page, created lazily and recreated after navigation.
    Cross-origin iframes are attached as child targets and get their own `_FrameSession`."""

    def __init__(self, page: Page, *, dialogs: str = "accept", block_print: bool = True):
        self.page = page
        self.cdp: CDPSession = page.context.new_cdp_session(page)
        self.cdp.send("Page.enable")
        self.frames: dict[str, _FrameSession] = {}  # by session id
        self._frame_by_target: dict[str, _FrameSession] = {}
        self._recording_src: str | None = None
        self._pending_msgs: dict[int, Any] = {}
        self._msg_id = 0
        self.cdp.on("Target.attachedToTarget", self._on_attached)
        self.cdp.on("Target.detachedFromTarget", self._on_detached)
        self.cdp.on("Target.targetInfoChanged", lambda ev: self._on_info_changed(ev))
        self.cdp.on("Target.receivedMessageFromTarget", self._on_child_message)
        try:
            self.cdp.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": False})
        except Exception:
            pass
        if block_print:
            try:
                self.cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": PRINT_STUB, "runImmediately": True})
            except Exception:
                pass
        self._ctx: int | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._nav_listeners: list[Callable[[str], None]] = []
        self._dialog_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._recording_script_id: str | None = None
        self.url: str = page.url
        self.dialogs = dialogs  # accept | dismiss | ignore
        self.cdp.on("Runtime.bindingCalled", self._on_binding)
        self.cdp.on("Page.frameNavigated", self._on_navigated)
        self.cdp.on("Page.javascriptDialogOpening", self._on_dialog)
        # Network activity, so waits are driven by what actually loads rather than fixed delays.
        self._inflight: dict[str, tuple[float, str]] = {}  # requestId -> (started, url)
        self._noise_ids: set[str] = set()
        self._url_hist: dict[str, list[float]] = {}
        self._net_last = time.time()
        self._net_done: list[tuple[float, str]] = []  # (finished, url)
        self.cdp.send("Network.enable")
        self.cdp.on("Network.requestWillBeSent", self._on_req)
        self.cdp.on("Network.loadingFinished", self._on_req_done)
        self.cdp.on("Network.loadingFailed", self._on_req_done)
        self.cdp.on("Network.requestServedFromCache", self._on_req_done)
        # Playwright auto-dismisses dialogs when nobody listens; claim them so our policy wins.
        if dialogs != "ignore":
            try:
                page.on("dialog", self._handle_dialog)
            except Exception:
                pass

    def _handle_dialog(self, d: Any) -> None:
        try:
            d.accept() if self.dialogs == "accept" else d.dismiss()
        except Exception:
            pass  # another World on the same page already handled it

    # -- child targets (cross-origin iframes) ----------------------------------

    def _send_to(self, session_id: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a command to a child session through Target.sendMessageToTarget and wait for its reply.
        Replies arrive as Target.receivedMessageFromTarget events on this session."""
        self._msg_id += 1
        mid = self._msg_id
        self._pending_msgs[mid] = None
        self.cdp.send("Target.sendMessageToTarget", {"sessionId": session_id, "message": json.dumps({"id": mid, "method": method, "params": params or {}})})
        deadline = time.time() + 10
        while self._pending_msgs.get(mid) is None:
            if time.time() > deadline:
                self._pending_msgs.pop(mid, None)
                raise TimeoutError(f"{method} to frame session timed out")
            self.page.wait_for_timeout(5)
        msg = self._pending_msgs.pop(mid)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result", {})

    def _on_child_message(self, ev: dict[str, Any]) -> None:
        try:
            msg = json.loads(ev["message"])
        except (KeyError, ValueError):
            return
        fs = self.frames.get(ev.get("sessionId", ""))
        if "id" in msg:
            if msg["id"] in self._pending_msgs:
                self._pending_msgs[msg["id"]] = msg
            return
        if fs is None:
            return
        m = msg.get("method")
        if m == "Runtime.bindingCalled" and msg["params"].get("name") == BINDING:
            try:
                payload = json.loads(msg["params"]["payload"])
            except (KeyError, ValueError):
                return
            self._refresh_frame_offset(fs)
            payload["_ctx"] = msg["params"].get("executionContextId")
            payload["_frame"] = fs.session_id
            # Lift local coordinates to page coordinates and attach the frame chain.
            self._lift(payload, fs)
            for cb in list(self._listeners):
                cb(payload)
        elif m == "Page.frameNavigated" and not msg["params"].get("frame", {}).get("parentId"):
            fs.ctx = None
            fs.url = msg["params"]["frame"].get("url", fs.url)
        elif m == "Target.attachedToTarget":
            self._attach_child(msg["params"], via=fs)
        elif m == "Target.targetInfoChanged":
            info = msg["params"].get("targetInfo", {})
            f2 = self._frame_by_target.get(info.get("targetId", ""))
            if f2 and info.get("url"):
                f2.url = info["url"]
        elif m == "Target.receivedMessageFromTarget":
            self._on_child_message(msg["params"])  # nested frames: unwrap one level

    def _lift(self, payload: dict[str, Any], fs: _FrameSession) -> None:
        for key in ("target",):
            t = payload.get(key)
            if t and t.get("rect"):
                t["rect"] = {"x": t["rect"]["x"] + fs.offset["x"], "y": t["rect"]["y"] + fs.offset["y"], "w": t["rect"]["w"], "h": t["rect"]["h"]}
                t["frame"] = fs.chain + (t.get("frame") or [])
                t["viewport"] = self._top_viewport()
        for key in ("from", "to"):
            pt = payload.get(key)
            if pt and "x" in pt:
                payload[key] = {"x": pt["x"] + fs.offset["x"], "y": pt["y"] + fs.offset["y"]}

    def _top_viewport(self) -> dict[str, Any]:
        try:
            return self.eval("({w: innerWidth, h: innerHeight, sx: scrollX, sy: scrollY})")
        except Exception:
            return {"w": 1280, "h": 800, "sx": 0, "sy": 0}

    def _on_attached(self, ev: dict[str, Any]) -> None:
        self._attach_child(ev, via=None)

    def _attach_child(self, ev: dict[str, Any], via: _FrameSession | None) -> None:
        info = ev.get("targetInfo", {})
        if info.get("type") != "iframe":
            return
        fs = _FrameSession(self, ev["sessionId"], info.get("targetId", ""), info)
        self.frames[fs.session_id] = fs
        self._frame_by_target[fs.target_id] = fs
        try:
            fs.send("Page.enable")
            fs.send("Runtime.enable")
            fs.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": False})
            if self._recording_src:
                fs.send("Runtime.addBinding", {"name": BINDING, "executionContextName": WORLD})
                fs.send("Page.addScriptToEvaluateOnNewDocument", {"source": self._recording_src, "worldName": WORLD, "runImmediately": True})
                fs.frame_id = fs.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
                ctx = fs.send("Page.createIsolatedWorld", {"frameId": fs.frame_id, "worldName": WORLD})["executionContextId"]
                fs._raw_eval(self._recording_src, ctx)
        except Exception:
            pass
        self._refresh_frame_offset(fs)

    def _on_info_changed(self, ev: dict[str, Any]) -> None:
        info = ev.get("targetInfo", {})
        fs = self._frame_by_target.get(info.get("targetId", ""))
        if fs and info.get("url"):
            fs.url = info["url"]

    def _on_detached(self, ev: dict[str, Any]) -> None:
        fs = self.frames.pop(ev.get("sessionId", ""), None)
        if fs:
            self._frame_by_target.pop(fs.target_id, None)

    def _refresh_frame_offset(self, fs: _FrameSession) -> None:
        """Where the frame element sits in the top page, plus a css path to it. Looked up by the
        frame's url, which is what the top document can see of a cross-origin iframe."""
        try:
            if not fs.url or fs.url == "about:blank":
                try:
                    fs.url = fs.eval("location.href")
                except Exception:
                    pass
            r = self.eval(f"""(() => {{ const want = {json.dumps(fs.url)}; const all = [...document.querySelectorAll('iframe')];
              const f = all.find(i => i.src === want) || all.find(i => want && i.src && (want.startsWith(i.src) || i.src.startsWith(want.split('#')[0])));
              if (!f) return null; const r = f.getBoundingClientRect();
              return {{x: r.left + f.clientLeft, y: r.top + f.clientTop, chain: [__ws.cssPathOf ? __ws.cssPathOf(f) : (f.id ? '#' + CSS.escape(f.id) : 'iframe[src=' + JSON.stringify(f.src) + ']')]}}; }})()""")
            if r:
                fs.offset = {"x": r["x"], "y": r["y"]}
                fs.chain = r["chain"]
        except Exception:
            pass

    def frame_for_chain(self, chain: list[str]) -> _FrameSession | None:
        """The cross-origin frame session whose iframe element matches a recorded frame chain, if any."""
        if not chain:
            return None
        for fs in self.frames.values():
            self._refresh_frame_offset(fs)
            if fs.chain and fs.chain[0] == chain[0]:
                return fs
        # chain may name the iframe by a selector we can resolve in the top page
        try:
            src = self.eval(f"(() => {{ const f = document.querySelector({json.dumps(chain[0])}); return f ? f.src : null; }})()")
        except Exception:
            src = None
        if src:
            for fs in self.frames.values():
                if fs.url == src or src.startswith(fs.url) or fs.url.startswith(src):
                    self._refresh_frame_offset(fs)
                    return fs
        return None

    # -- canvas display list (recording only) ------------------------------------

    def install_canvas_hooks(self) -> None:
        """Replay-time opt-in: install the display-list hook in the main world (detectable; off by default)."""
        if getattr(self, "_canvas_hook_id", None):
            return
        self._canvas_hook_id = self.cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": CANVAS_HOOK_JS, "runImmediately": True})["identifier"]
        try:
            self.cdp.send("Runtime.evaluate", {"expression": CANVAS_HOOK_JS})
        except Exception:
            pass

    def canvas_frame(self, css_selector_chain: list[str]) -> dict[str, Any] | None:
        """The whole current display list of a canvas, in page css px. Forces a repaint first so apps
        that only draw on demand have a fresh frame to read."""
        sel = css_selector_chain[0] if css_selector_chain else "canvas"
        expr = f"""(() => {{ const c = document.querySelector({json.dumps(sel)}); if (!c) return null;
          const all = c[Symbol.for('waystone.all')]; if (!all) return null;
          const prims = all.call(c); if (!prims) return null;
          const r = c.getBoundingClientRect(); const sx = c.width / r.width, sy = c.height / r.height;
          const fix = (p) => ({{ ...p, bbox: [r.left + p.bbox[0] / sx, r.top + p.bbox[1] / sy, p.bbox[2] / sx, p.bbox[3] / sy] }});
          return {{ total: prims.length, hit: [], near: prims.map(fix), canvas: [r.left, r.top, r.width, r.height] }}; }})()"""
        try:
            r = self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return r.get("result", {}).get("value")
        except Exception:
            return None

    def canvas_primitives(self, css_selector_chain: list[str], x: float, y: float, radius: float = 80) -> dict[str, Any] | None:
        """What the page drew under and around page point (x, y) on the canvas matched by the first
        css candidate. Read through the main world's Symbol-keyed reader; returns None if the hook
        did not see any draws (WebGL canvas, or drawn before the hook was installed)."""
        sel = css_selector_chain[0] if css_selector_chain else "canvas"
        expr = f"""(() => {{ const c = document.querySelector({json.dumps(sel)}); if (!c) return null;
          const r = c.getBoundingClientRect(); const sx = c.width / r.width, sy = c.height / r.height;
          const read = c[Symbol.for('waystone.read')]; if (!read) return null;
          const res = read.call(c, ({x} - r.left) * sx, ({y} - r.top) * sy, {radius} * sx);
          if (!res) return null;
          // report boxes in page css px
          const fix = (p) => ({{ ...p, bbox: [r.left + p.bbox[0] / sx, r.top + p.bbox[1] / sy, p.bbox[2] / sx, p.bbox[3] / sy] }});
          return {{ total: res.total, cleared: res.cleared, hit: res.hit.map(fix), near: res.near.map(fix), canvas: [r.left, r.top, r.width, r.height], scale: [sx, sy] }}; }})()"""
        try:
            r = self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return r.get("result", {}).get("value")
        except Exception:
            return None

    # -- closed shadow roots ---------------------------------------------------

    def attach_closed_roots(self) -> int:
        """Find closed shadow roots with DOM.getDocument(pierce), resolve host and root into our
        isolated world, and register them with core.js. Returns how many are registered."""
        try:
            doc = self.cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})["root"]
        except Exception:
            return 0
        pairs: list[tuple[int, int]] = []

        def walk(n: dict[str, Any]) -> None:
            for sr in n.get("shadowRoots", []) or []:
                if sr.get("shadowRootType") == "closed":
                    pairs.append((n["nodeId"], sr["nodeId"]))
            for c in n.get("children", []) or []:
                walk(c)
            for sr in n.get("shadowRoots", []) or []:
                walk(sr)
            if n.get("contentDocument"):
                walk(n["contentDocument"])

        walk(doc)
        if not pairs:
            return 0
        ctx = self._ensure_context()
        ids: list[tuple[str, str]] = []
        for host_id, root_id in pairs:
            try:
                h = self.cdp.send("DOM.resolveNode", {"nodeId": host_id, "executionContextId": ctx})["object"]["objectId"]
                r = self.cdp.send("DOM.resolveNode", {"nodeId": root_id, "executionContextId": ctx})["object"]["objectId"]
                ids.append((h, r))
            except Exception:
                continue
        if not ids:
            return 0
        # callFunctionOn takes the remote objects as arguments, so no serialisation of DOM nodes is needed.
        args = []
        for h, r in ids:
            args.append({"objectId": h}); args.append({"objectId": r})
        fn = "function(...a) { const pairs = []; for (let i = 0; i < a.length; i += 2) pairs.push([a[i], a[i + 1]]); return __ws.addClosedRoots(pairs); }"
        res = self.cdp.send("Runtime.callFunctionOn", {"functionDeclaration": fn, "objectId": ids[0][0], "arguments": args, "returnByValue": True})
        return int(res.get("result", {}).get("value") or 0)

    # -- navigation --------------------------------------------------------

    def navigate(self, url: str) -> None:
        r = self.cdp.send("Page.navigate", {"url": url})
        if r.get("errorText"):
            raise RuntimeError(f"navigation to {url} failed: {r['errorText']}")
        self._ctx = None
        self.url = url

    def on_navigated(self, cb: Callable[[str], None]) -> None:
        self._nav_listeners.append(cb)

    def ready_state(self) -> str:
        try:
            return str(self.eval("document.readyState"))
        except Exception:
            return "loading"

    # -- evaluation ------------------------------------------------------

    def _main_frame_id(self) -> str:
        return self.cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]

    def _ensure_context(self) -> int:
        if self._ctx is not None:
            return self._ctx
        ctx = self.cdp.send(
            "Page.createIsolatedWorld",
            {"frameId": self._main_frame_id(), "worldName": WORLD, "grantUniveralAccess": False},
        )["executionContextId"]
        self._raw_eval(CORE_JS, ctx)
        self._ctx = ctx
        return ctx

    def _raw_eval(self, expression: str, ctx: int, *, by_value: bool = True) -> Any:
        r = self.cdp.send(
            "Runtime.evaluate",
            {"expression": expression, "contextId": ctx, "returnByValue": by_value, "awaitPromise": True},
        )
        if "exceptionDetails" in r:
            ex = r["exceptionDetails"]
            msg = ex.get("exception", {}).get("description") or ex.get("text") or "evaluation failed"
            raise RuntimeError(msg)
        res = r.get("result", {})
        return res.get("value") if by_value else res

    def eval_in(self, ctx: int, expression: str) -> Any:
        """Evaluate in a specific context (e.g. the recorder's world, known from its binding calls)."""
        return self._raw_eval(expression, ctx)

    def eval(self, expression: str, *, by_value: bool = True) -> Any:
        """Evaluate in the isolated world. `__ws` is always defined."""
        for attempt in range(2):
            ctx = self._ensure_context()
            try:
                return self._raw_eval(expression, ctx, by_value=by_value)
            except Exception as e:  # context was destroyed by a navigation; rebuild once
                self._ctx = None
                if attempt == 1 or "context" not in str(e).lower():
                    raise
        raise RuntimeError("unreachable")

    def call(self, fn: str, *args: Any) -> Any:
        return self.eval(f"__ws.{fn}({', '.join(json.dumps(a) for a in args)})")

    def object_id(self, fn: str, *args: Any) -> str | None:
        """Call a __ws function that returns an element; get a CDP remote object id for it."""
        res = self.eval(f"__ws.{fn}({', '.join(json.dumps(a) for a in args)})", by_value=False)
        return res.get("objectId") if res and res.get("subtype") != "null" else None

    def set_files(self, object_id: str, files: list[str]) -> None:
        self.cdp.send("DOM.setFileInputFiles", {"objectId": object_id, "files": files})

    # -- recorder binding -------------------------------------------------

    def listen(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(cb)

    def start_recording(self, *, show: bool = False, panel: bool = True) -> None:
        """Expose the binding to the waystone world and inject the recorder on every new document."""
        # bindingCalled events are only delivered on a session with the Runtime domain enabled.
        # Playwright already enables it on its own session, so this adds no new tell.
        self.cdp.send("Runtime.enable")
        self.cdp.send("Runtime.addBinding", {"name": BINDING, "executionContextName": WORLD})
        # Canvas display list: a main-world hook, recording only. Wrapped CanvasRenderingContext2D
        # methods record what the page draws so a click on a canvas can be identified by the
        # primitive under it. Installed at document start for new documents and evaluated now for
        # the current one (draws made before this point are not seen until the next redraw).
        self._canvas_hook_id = self.cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": CANVAS_HOOK_JS, "runImmediately": True})["identifier"]
        try:
            self.cdp.send("Runtime.evaluate", {"expression": CANVAS_HOOK_JS})
        except Exception:
            pass
        src = f"globalThis.__ws_show = {json.dumps(bool(show))};\n" + CORE_JS + "\n" + RECORDER_JS + ("\n" + PANEL_JS if panel else "")
        self._recording_src = src
        for fs in list(self.frames.values()):
            try:
                fs.send("Runtime.addBinding", {"name": BINDING, "executionContextName": WORLD})
                fs.send("Page.addScriptToEvaluateOnNewDocument", {"source": src, "worldName": WORLD, "runImmediately": True})
                fs.frame_id = fs.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
                ctx = fs.send("Page.createIsolatedWorld", {"frameId": fs.frame_id, "worldName": WORLD})["executionContextId"]
                fs._raw_eval(src, ctx)
            except Exception:
                pass
        self._recording_script_id = self.cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": src, "worldName": WORLD, "runImmediately": True},
        )["identifier"]
        # Arm every frame of the current document, which loaded before the script was registered.
        tree = self.cdp.send("Page.getFrameTree")["frameTree"]
        for frame_id in _frame_ids(tree):
            try:
                ctx = self.cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": WORLD})["executionContextId"]
                self._raw_eval(src, ctx)
            except Exception:
                continue

    def stop_recording(self) -> None:
        self._recording_src = None
        if getattr(self, "_canvas_hook_id", None):
            try:
                self.cdp.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": self._canvas_hook_id})
            except Exception:
                pass
            self._canvas_hook_id = None
        if self._recording_script_id:
            self.cdp.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": self._recording_script_id})
            self._recording_script_id = None
        try:
            self.cdp.send("Runtime.removeBinding", {"name": BINDING})
        except Exception:
            pass

    def _on_binding(self, ev: dict[str, Any]) -> None:
        if ev.get("name") != BINDING:
            return
        try:
            payload = json.loads(ev["payload"])
        except (KeyError, ValueError):
            return
        payload["_ctx"] = ev.get("executionContextId")
        for cb in list(self._listeners):
            cb(payload)

    def _on_navigated(self, ev: dict[str, Any]) -> None:
        frame = ev.get("frame", {})
        if frame.get("parentId"):
            return
        self._ctx = None
        self.url = frame.get("url", self.url)
        for cb in list(self._nav_listeners):
            cb(self.url)

    # -- network / idle ------------------------------------------------------

    _COUNTED = {"XHR", "Fetch", "Document", "Script"}
    # Background traffic that never stops and never means "loading": analytics, beacons, polling.
    _NOISE = ("analytics", "beacon", "collect", "telemetry", "pixel", "track", "metrics", "log.", "/log", "heartbeat", "ping", "sentry", "datadog", "newrelic", "hotjar", "segment.io", "doubleclick", "googletagmanager", "google-analytics", "facebook.com/tr", "optimizely", "clarity.ms", "mparticle", "rum", "events", "stats", "ads", "adsrvr", "/b/ss", "omtrdc", "demdex")

    def _is_noise(self, url: str) -> bool:
        u = url.lower()
        if any(n in u for n in self._NOISE):
            return True
        # the same endpoint hit repeatedly at a steady rate is polling, not loading
        key = u.split("?")[0]
        hist = self._url_hist.setdefault(key, [])
        hist.append(time.time())
        del hist[:-6]
        if len(hist) >= 4 and (hist[-1] - hist[0]) < 30:
            return True
        return False

    def _on_req(self, ev: dict[str, Any]) -> None:
        if ev.get("type") in self._COUNTED:
            url = ev.get("request", {}).get("url", "")
            if self._is_noise(url):
                self._noise_ids.add(ev["requestId"])
                return
            self._inflight[ev["requestId"]] = (time.time(), url)
            self._net_last = time.time()

    def _on_req_done(self, ev: dict[str, Any]) -> None:
        rid = ev.get("requestId", "")
        if rid in self._noise_ids:
            self._noise_ids.discard(rid)
            return
        item = self._inflight.pop(rid, None)
        if item:
            self._net_last = time.time()
            self._net_done.append((time.time(), item[1]))
            del self._net_done[:-200]

    def inflight(self, stale_s: float = 8.0) -> list[str]:
        """URLs still loading (long-polls older than `stale_s` are ignored)."""
        now = time.time()
        return [u for rid, (t0, u) in self._inflight.items() if now - t0 < stale_s]

    def busy(self, since: float | None = None) -> list[str]:
        try:
            return list(self.eval(f"__ws.busy({json.dumps(since) if since is not None else ''})") or [])
        except Exception:
            return []

    def mark_now(self) -> float | None:
        """The page's performance.now(), for 'what appeared after this point' checks."""
        try:
            return float(self.eval("__ws.nowMs()"))
        except Exception:
            return None

    def is_idle(self, quiet_ms: int = 300, *, dom: bool = True, since: float | None = None) -> bool:
        if self.inflight():
            return False
        if time.time() - self._net_last < quiet_ms / 1000:
            return False
        try:
            state = self.eval(f"({{q: __ws.quiet(), b: __ws.busy({json.dumps(since) if since is not None else ''}).length}})") or {}
        except Exception:
            return True
        if state.get("b"):
            return False
        return (not dom) or float(state.get("q", 0)) >= quiet_ms

    def requests_since(self, t: float) -> list[str]:
        return [u for ts, u in self._net_done if ts >= t]

    def wait_idle(self, *, quiet_ms: int = 300, timeout_ms: int = 8000, sleep: Any = None, since: float | None = None) -> dict[str, Any]:
        """Block until no counted request is in flight, no loading indicator that appeared after
        `since` is showing, and the DOM has been still for `quiet_ms`. Returns what it waited on."""
        t0 = time.time()
        sleep = sleep or (lambda ms: time.sleep(ms / 1000))
        if since is None:
            since = self.mark_now()
            if since is not None:
                since -= 150  # indicators that appeared in the last 150ms are the action's
        deadline = t0 + timeout_ms / 1000
        dom_deadline = t0 + min(timeout_ms, 1500) / 1000  # DOM stillness is best-effort: animations never stop
        while time.time() < deadline:
            if self.is_idle(quiet_ms, dom=time.time() < dom_deadline, since=since):
                break
            sleep(50)
        timed_out = time.time() >= deadline
        return {"waited_ms": int((time.time() - t0) * 1000), "requests": len(self.requests_since(t0)), "timed_out": timed_out, "pending": self.inflight()[:3], "busy": self.busy(since) if timed_out else []}

    # -- dialogs -----------------------------------------------------------

    def on_dialog(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._dialog_listeners.append(cb)

    def _on_dialog(self, ev: dict[str, Any]) -> None:
        for cb in list(self._dialog_listeners):
            cb(ev)

    # -- capture ---------------------------------------------------------

    def screenshot(self) -> bytes:
        r = self.cdp.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        return base64.b64decode(r["data"])

    def pdf(self) -> bytes:
        r = self.cdp.send("Page.printToPDF", {"printBackground": True, "preferCSSPageSize": True})
        return base64.b64decode(r["data"])

    def close(self) -> None:
        try:
            self.cdp.detach()
        except Exception:
            pass


def _frame_ids(tree: dict[str, Any]) -> list[str]:
    out = [tree["frame"]["id"]]
    for child in tree.get("childFrames", []):
        out.extend(_frame_ids(child))
    return out
