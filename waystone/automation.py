"""The deliverable: an automation folder.

    name/
      README.md         what this does and how to do it, written for an agent
      automation.json   summary + instructional steps + full workflow + deterministic CDP `run`
      resolver.js       selector engine `run` depends on (nothing else)
      steps/NNN.png     screenshots with the target boxed and the action captioned on the image
      video.mp4         the session

A person records once; an agent reads README.md and knows exactly what to do; a runner executes
`run` deterministically. Repeating patterns in the steps are called out so the agent can generalise
("steps 4-7 repeat for each meeting").
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .export import best, describe_sel, to_cdp
from .workflow import Step, Target, Workflow
from .world import CORE_JS


# --- instructional phrasing ---------------------------------------------------

def _elem(t: Target) -> str:
    fp = t.fingerprint or {}
    kind = fp.get("role") if fp.get("role") not in (None, "generic") else fp.get("tag") or "element"
    name = fp.get("name") or fp.get("text") or ""
    where = " inside the iframe" if t.frame else ""
    return (f'the {kind} “{name}”' if name else f"the {kind}") + where


def instruction(s: Step) -> str:
    """One imperative line per step, the way you would tell a person."""
    t = s.target
    if s.type == "navigate":
        return "Wait for the page to load" if s.note == "after-action" else f"Open {s.url}"
    if s.type == "click":
        if s.note == "background":
            return "Click on empty space to dismiss"
        if t and t.canvas:
            from .canvas import phrase
            return f"On the {t.fingerprint.get('tag', 'canvas')}, click {phrase(t.canvas)}"
        if t and t.crop:
            amb = " This spot was not visually unique when recorded; check the surrounding context before clicking." if t.crop.get("ambiguous") else ""
            return f"Click the {t.fingerprint.get('tag', 'canvas')} at ({int(t.crop['x'])}, {int(t.crop['y'])}), see the crop.{amb}"
        mods = "+".join(s.modifiers) + "+" if s.modifiers else ""
        tail = " (opens a new tab)" if s.note == "open in new tab" else ""
        return f"{mods and 'Hold ' + mods[:-1] + ' and '}Click {_elem(t)}{tail}" if t else "Click"
    if s.type == "dblclick":
        return f"Double-click {_elem(t)}"
    if s.type == "hover":
        return f"Hover over {_elem(t)} to reveal its menu"
    if s.type == "type":
        v = f"the value of {{{{{s.value.strip('{}')}}}}}" if s.secret or (s.value or "").startswith("{{") else f"“{s.value}”"
        return f"Type {v} into {_elem(t)}"
    if s.type == "press":
        return f"Press {s.key}"
    if s.type == "select":
        return f"Choose “{s.note or s.value}” in {_elem(t)}"
    if s.type == "check":
        return f"{'Tick' if s.value else 'Untick'} {_elem(t)}"
    if s.type == "set":
        return f"Set {_elem(t)} to “{s.value}”"
    if s.type == "drag":
        return f"Drag {_elem(t)} by ({int((s.to or {}).get('x', 0) - (s.x or 0))}, {int((s.to or {}).get('y', 0) - (s.y or 0))}) px"
    if s.type == "upload":
        return f"Upload {', '.join(s.files or [])} via {_elem(t)}"
    if s.type == "scroll":
        return f"Scroll {_elem(t) if t else 'the page'} to y={int(s.y or 0)}"
    if s.type == "new-tab":
        return f"A new tab opens: {s.url}. Continue there"
    if s.type == "switch-tab":
        return f"Switch to tab {s.tab}"
    if s.type == "dialog":
        return f"A {s.note} dialog appears (“{s.value}”): accept it"
    if s.type == "print":
        return "Print the page (save as PDF)"
    if s.type == "wait":
        if t:
            return f"Wait until {_elem(t)} is on the page" + (" (this is how you know the page is ready)" if s.note == "page ready" else "")
        return f"Wait {s.settle_ms / 1000:.1f}s" if s.settle_ms else "Wait"
    return s.describe()


# --- repeating patterns -------------------------------------------------------------

def _shape(s: Step) -> str:
    """What a step looks like with the specifics removed: same action on the same kind of element,
    found the same way. Two steps with equal shape are 'the same move on a different item'."""
    t = s.target
    if not t:
        return s.type
    fp = t.fingerprint or {}
    css = next((c["value"] for c in t.candidates if c["kind"] == "css" and ":nth-of-type" in c["value"]), "")
    css = re.sub(r":nth-of-type\(\d+\)", ":nth-of-type(N)", css)
    return f'{s.type}|{fp.get("role")}|{fp.get("tag")}|{css[:80]}'


def patterns(wf: Workflow) -> list[dict[str, Any]]:
    """Find runs of steps that repeat as a block (A B C A' B' C' ...). Small-scale by design: this
    is for 'do the same thing for each row', not for program synthesis."""
    steps = [s for s in wf.steps if s.type not in ("scroll", "wait")]
    shapes = [_shape(s) for s in steps]
    found: list[dict[str, Any]] = []
    n = len(shapes)
    for size in range(1, min(6, n // 2) + 1):
        i = 0
        while i + 2 * size <= n:
            block = shapes[i:i + size]
            reps = 1
            while i + (reps + 1) * size <= n and shapes[i + reps * size:i + (reps + 1) * size] == block:
                reps += 1
            if reps >= 2 and any(st.type in ("click", "type", "select", "check") for st in steps[i:i + size]):
                items = []
                for r in range(reps):
                    first = steps[i + r * size]
                    items.append((first.target.fingerprint.get("name") or first.target.fingerprint.get("text") or "") if first.target else "")
                # How to enumerate the rest: the varying item's structural selector with its index generalised.
                first_t = steps[i].target
                sel = next((c["value"] for c in (first_t.candidates if first_t else []) if c["kind"] == "css" and ":nth-of-type(" in c["value"]), None)
                collection = re.sub(r":nth-of-type\(\d+\)(?!.*:nth-of-type)", "", sel) if sel else None  # drop the LAST nth → all siblings
                fp = first_t.fingerprint if first_t else {}
                found.append({"from": steps[i].index, "to": steps[i + reps * size - 1].index, "size": size, "repeats": reps, "items": items,
                              "varies": {"step": steps[i].index, "role": fp.get("role"), "collection_css": collection}})
                i += reps * size
            else:
                i += 1
    # keep the largest non-overlapping ones
    found.sort(key=lambda f: (-(f["to"] - f["from"]), f["from"]))
    kept: list[dict[str, Any]] = []
    for f in found:
        if all(f["to"] < k["from"] or f["from"] > k["to"] for k in kept):
            kept.append(f)
    return sorted(kept, key=lambda f: f["from"])


# --- README -------------------------------------------------------------------------

def summary_line(wf: Workflow, goal: str | None) -> str:
    if goal:
        return goal.strip()
    host = urlparse(wf.start_url).netloc.replace("www.", "")
    acts = [s for s in wf.steps if s.type in ("click", "type", "select", "upload", "print")]
    if not acts:
        return f"Open {host}"
    last = acts[-1]
    return f"On {host}: " + ", then ".join(instruction(s).lower() for s in acts[:3]) + (f" … ending with {instruction(last).lower()}" if len(acts) > 3 else "") + "."


def readme(wf: Workflow, goal: str | None) -> str:
    L: list[str] = [f"# {wf.name}", "", summary_line(wf, goal), "", f"Start at `{wf.start_url}`."]
    if wf.params:
        L += ["", "Inputs you must supply: " + ", ".join(f"`{{{{{p}}}}}`" for p in wf.params) + "."]
    L += ["", "## How to do it", ""]
    pats = patterns(wf)
    pat_at = {p["from"]: p for p in pats}
    skip_until = -1
    for s in wf.steps:
        if s.index in pat_at:
            p = pat_at[s.index]
            v = p.get("varies") or {}
            L.append(f"**Steps {p['from']} to {p['to']} repeat {p['repeats']}x: the same {p['size']}-step move for each item** ({', '.join(f'“{i}”' for i in p['items'] if i)}).")
            if v.get("collection_css"):
                L.append(f"The item that varies is step {v['step']}'s target, a {v.get('role') or 'element'}. All such items on the page: `css={v['collection_css']}`. Enumerate those and repeat the block once per item, substituting its name into step {v['step']}.")
            else:
                L.append(f"To do it for other items, repeat the block with a different item in step {v.get('step', p['from'])}.")
            L.append("")
        line = f"{s.index}. {instruction(s)}"
        if s.target and s.target.canvas:
            d = s.target.canvas
            bits = []
            if d.get("label"): bits.append(f"it reads “{d['label']}”")
            if d.get("row_anchor"): bits.append(f"row header “{d['row_anchor']['s']}” is to its left")
            if d.get("col_anchor"): bits.append(f"column header “{d['col_anchor']['s']}” is above it")
            if d.get("image"): bits.append(f"it is the image `{str(d['image']).rsplit('/', 1)[-1][:40]}`")
            line += "  \n   drawn on a canvas, not an element: " + "; ".join(bits) + (f". Recorded position ({int(s.target.crop['x'])}, {int(s.target.crop['y'])}); crop `{s.target.crop['context']['path']}`." if s.target.crop else ".")
        elif s.target and s.target.crop:
            c = s.target.crop
            line += f"  \n   drawn on a canvas, not an element. Find `{c['context']['path']}` in a screenshot (the click is at offset {int(c['context']['ox'])},{int(c['context']['oy'])} inside it); the tight target is `{c['target']['path']}`. Recorded position ({int(c['x'])}, {int(c['y'])})."
        elif s.target:
            line += f"  \n   find it with: " + " · ".join(f"`{describe_sel(c)}`" for c in s.target.candidates[:3])
        if s.settle_ms and s.settle_ms >= 500:
            line += f"  \n   then wait ~{s.settle_ms / 1000:.1f}s for the page to settle" + (f" (it loads {len(s.loaded)} request(s))" if s.loaded else "")
        if s.screenshot:
            line += f"  \n   see `{s.screenshot}`"
        L.append(line)
    L += ["", "## Running it", "", "- `waystone run .` replays this deterministically on any Chromium with a CDP port, Fortress included (self-healing selectors, load-aware waits).",
          "- `automation.json` → `run` is the same thing as raw CDP commands for any client; `resolver.js` is the only dependency.",
          "- `waystone export -f playwright-python automation.json` for stock Chromium.", ""]
    L += ["## Notes for an agent", "",
          "- Selectors are ranked; the first that matches exactly one visible element is the one to use. Prefer role+name; fall back down the list.",
          "- `settle` times are what the page needed after each action at recording time. Wait for the page to be idle, not for a fixed time.",
          "- Values written as `{{name}}` are parameters: ask for them or read them from the task.",
          "- Each screenshot in `steps/` has the target boxed in red and the action written at the top.", ""]
    return "\n".join(L)


# --- folder --------------------------------------------------------------------------

def write_folder(wf: Workflow, folder: str | Path, *, goal: str | None = None, video: Path | None = None) -> dict[str, Path]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    run = json.loads(to_cdp(wf))
    run.pop("resolver_js", None)
    run["resolver"] = "resolver.js"
    doc = {
        "waystone": wf.version,
        "name": wf.name,
        "summary": summary_line(wf, goal),
        "goal": goal,
        "start_url": wf.start_url,
        "params": wf.params,
        "patterns": patterns(wf),
        "steps": [{"index": s.index, "do": instruction(s), "type": s.type, "tab": s.tab,
                   "element": (_elem(s.target) if s.target else None),
                   "selectors": ([describe_sel(c) for c in s.target.candidates[:4]] if s.target else None),
                   "value": s.value, "key": s.key, "settle_ms": s.settle_ms, "screenshot": s.screenshot} for s in wf.steps],
        "workflow": wf.to_dict(),
        "run": run,
    }
    (folder / "automation.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    out["automation"] = folder / "automation.json"
    (folder / "resolver.js").write_text(CORE_JS, encoding="utf-8")
    out["resolver"] = folder / "resolver.js"
    (folder / "README.md").write_text(readme(wf, goal), encoding="utf-8")
    out["readme"] = folder / "README.md"
    if video:
        out["video"] = video
    return out
