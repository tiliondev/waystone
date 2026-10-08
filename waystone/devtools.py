"""Chrome DevTools Recorder interop (the @puppeteer/replay UserFlow schema).

Export: a Waystone workflow becomes a user flow that DevTools imports, @puppeteer/replay runs, and
TestCafe / Sauce Labs / Cypress / Nightwatch / WebdriverIO converters understand.
Import: a flow recorded in DevTools becomes a Waystone workflow, so it gets self-healing selectors,
load-aware waits, and the Fortress-native CDP replay.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .workflow import SCHEMA_VERSION, Step, Target, Workflow

KEY_NAMES = {"Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Space"}


# --- selectors --------------------------------------------------------------

def _to_devtools_selector(c: dict[str, Any]) -> str | list[str] | None:
    k = c["kind"]
    if k == "role":
        return f'aria/{c["name"]}[role="{c["role"]}"]'
    if k == "text":
        return f'text/{c["value"]}'
    if k == "xpath":
        return f'xpath/{c["value"]}'
    if k == "label":
        return None  # no DevTools equivalent; the css/role alternatives cover it
    if k == "css":
        v = c["value"]
        if " >>> " in v:
            parts = v.split(" >>> ")
            return [f"pierce/{p}" for p in parts]  # ancestor chain, each descending into a shadow root
        return v
    return None


def _from_devtools_selector(sel: str | list[str]) -> dict[str, Any] | None:
    if isinstance(sel, list):
        inner = [s[len("pierce/"):] if s.startswith("pierce/") else s for s in sel]
        return {"kind": "css", "value": " >>> ".join(inner)}
    if sel.startswith("aria/"):
        m = re.fullmatch(r"aria/(.*?)(?:\[role=\"([^\"]+)\"\])?", sel)
        if m:
            name, role = m.group(1), m.group(2) or "generic"
            return {"kind": "role", "role": role, "name": name}
    if sel.startswith("text/"):
        return {"kind": "text", "value": sel[5:]}
    if sel.startswith("xpath/"):
        return {"kind": "xpath", "value": sel[6:]}
    if sel.startswith("pierce/"):
        return {"kind": "css", "value": sel[7:]}
    return {"kind": "css", "value": sel}


def _selectors(t: Target) -> list[str | list[str]]:
    out: list[str | list[str]] = []
    for c in t.candidates:
        s = _to_devtools_selector(c)
        if s is not None and s not in out:
            out.append(s)
    return out


# --- export -------------------------------------------------------------------

def to_devtools(wf: Workflow) -> str:
    steps: list[dict[str, Any]] = []
    vp = wf.viewport or {"width": 1280, "height": 800}
    steps.append({"type": "setViewport", "width": vp["width"], "height": vp["height"], "deviceScaleFactor": 1, "isMobile": False, "hasTouch": False, "isLandscape": True})
    tab_urls: dict[int, str] = {0: "main"}
    pending_target: str | None = None

    def base(s: Step) -> dict[str, Any]:
        d: dict[str, Any] = {}
        target = tab_urls.get(s.tab, "main")
        if target != "main":
            d["target"] = target
        return d

    def with_offset(d: dict[str, Any], t: Target) -> dict[str, Any]:
        if t.offset and t.rect:
            d["offsetX"] = round(t.rect["w"] * t.offset["ox"], 1)
            d["offsetY"] = round(t.rect["h"] * t.offset["oy"], 1)
        else:
            d["offsetX"] = round(t.rect["w"] / 2, 1)
            d["offsetY"] = round(t.rect["h"] / 2, 1)
        return d

    for s in wf.steps:
        t = s.target
        if s.type == "navigate":
            if s.note == "after-action" and steps:
                steps[-1].setdefault("assertedEvents", []).append({"type": "navigation", "url": s.url})
            else:
                steps.append({**base(s), "type": "navigate", "url": s.url, "assertedEvents": [{"type": "navigation", "url": s.url}]})
        elif s.type == "new-tab":
            tab_urls[s.tab] = s.url or f"tab-{s.tab}"
        elif s.type == "switch-tab":
            continue
        elif s.type in ("click", "check") and t:
            if s.note == "background":
                continue
            steps.append(with_offset({**base(s), "type": "click", "selectors": _selectors(t)}, t))
        elif s.type == "dblclick" and t:
            steps.append(with_offset({**base(s), "type": "doubleClick", "selectors": _selectors(t)}, t))
        elif s.type == "hover" and t:
            steps.append({**base(s), "type": "hover", "selectors": _selectors(t)})
        elif s.type in ("type", "select", "set") and t:
            steps.append({**base(s), "type": "change", "selectors": _selectors(t), "value": str(s.value or "")})
        elif s.type == "press":
            combo = (s.key or "Enter").split("+")
            for m in combo[:-1]:
                steps.append({**base(s), "type": "keyDown", "key": m})
            steps.append({**base(s), "type": "keyDown", "key": combo[-1]})
            steps.append({**base(s), "type": "keyUp", "key": combo[-1]})
            for m in reversed(combo[:-1]):
                steps.append({**base(s), "type": "keyUp", "key": m})
        elif s.type == "scroll":
            d = {**base(s), "type": "scroll", "x": int(s.x or 0), "y": int(s.y or 0)}
            if t:
                d["selectors"] = _selectors(t)
            steps.append(d)
        elif s.type == "wait" and t:
            steps.append({**base(s), "type": "waitForElement", "selectors": _selectors(t), "visible": True})
        elif s.type == "wait":
            steps.append({**base(s), "type": "waitForExpression", "expression": f"new Promise(r => setTimeout(() => r(true), {s.settle_ms or 1000}))"})
        elif s.type in ("upload", "drag", "print", "dialog"):
            steps.append({**base(s), "type": "customStep", "name": f"waystone:{s.type}", "parameters": {"description": s.describe(), "files": s.files, "to": s.to, "selectors": _selectors(t) if t else None}})
    return json.dumps({"title": wf.name, "steps": steps}, indent=2, ensure_ascii=False)


# --- import -------------------------------------------------------------------

def from_devtools(text: str, name: str | None = None) -> Workflow:
    flow = json.loads(text)
    steps: list[Step] = []
    start_url = ""
    tabs: dict[str, int] = {"main": 0}
    pending_mods: list[str] = []
    viewport = None

    def target_for(st: dict[str, Any]) -> Target | None:
        sels = st.get("selectors")
        if not sels:
            return None
        cands = [c for c in (_from_devtools_selector(s) for s in sels) if c]
        fp: dict[str, Any] = {}
        for c in cands:
            if c["kind"] == "role":
                fp = {"role": c["role"], "name": c["name"], "tag": None}
                break
        if not fp:
            for c in cands:
                if c["kind"] == "text":
                    fp = {"text": c["value"], "name": c["value"]}
                    break
        return Target(candidates=cands, fingerprint=fp, rect={"x": 0, "y": 0, "w": 0, "h": 0})

    def tab_of(st: dict[str, Any]) -> int:
        tg = st.get("target", "main")
        if tg not in tabs:
            tabs[tg] = len(tabs)
            steps.append(Step(index=0, type="new-tab", tab=tabs[tg], url=tg))
        return tabs[tg]

    for st in flow.get("steps", []):
        ty = st.get("type")
        if ty == "setViewport":
            viewport = {"width": st["width"], "height": st["height"]}
            continue
        if ty == "navigate":
            if not start_url:
                start_url = st["url"]
            steps.append(Step(index=0, type="navigate", url=st["url"], tab=tab_of(st)))
            continue
        tab = tab_of(st)
        if ty in ("click", "doubleClick"):
            steps.append(Step(index=0, type="click" if ty == "click" else "dblclick", tab=tab, target=target_for(st), modifiers=list(pending_mods) or None))
        elif ty == "hover":
            steps.append(Step(index=0, type="hover", tab=tab, target=target_for(st)))
        elif ty == "change":
            steps.append(Step(index=0, type="type", tab=tab, target=target_for(st), value=st.get("value", "")))
        elif ty == "keyDown":
            k = st.get("key", "")
            if k in ("Meta", "Control", "Alt", "Shift"):
                pending_mods.append(k)
            elif k in KEY_NAMES or len(k) > 1:
                steps.append(Step(index=0, type="press", tab=tab, key="+".join(pending_mods + [k])))
        elif ty == "keyUp":
            k = st.get("key", "")
            if k in pending_mods:
                pending_mods.remove(k)
        elif ty == "scroll":
            steps.append(Step(index=0, type="scroll", tab=tab, target=target_for(st), x=st.get("x", 0), y=st.get("y", 0)))
        elif ty == "waitForElement":
            steps.append(Step(index=0, type="wait", tab=tab, target=target_for(st), note=f"waitForElement {st.get('operator', '==')} {st.get('count', 1)}"))
        elif ty == "customStep" and str(st.get("name", "")).startswith("waystone:"):
            kind = st["name"].split(":", 1)[1]
            p = st.get("parameters") or {}
            steps.append(Step(index=0, type=kind, tab=tab, target=Target(candidates=[c for c in (_from_devtools_selector(s) for s in (p.get("selectors") or [])) if c], fingerprint={}, rect={"x": 0, "y": 0, "w": 0, "h": 0}) if p.get("selectors") else None, files=p.get("files"), to=p.get("to")))
        for ev in st.get("assertedEvents") or []:
            if ev.get("type") == "navigation" and ty != "navigate":
                steps.append(Step(index=0, type="navigate", tab=tab, url=ev.get("url"), note="after-action"))
    for i, s in enumerate(steps):
        s.index = i
    return Workflow(name=name or re.sub(r"[^\w-]+", "-", flow.get("title") or "devtools").strip("-").lower(), start_url=start_url, steps=steps, viewport=viewport, engine="devtools-recorder", version=SCHEMA_VERSION)
