"""Turn a Workflow into things people and agents can use directly:

  agent              a compact Markdown brief: every step, its target, selectors, waits
  playwright-python  a runnable Playwright script
  playwright-js      the same in JavaScript
  puppeteer          a runnable Puppeteer script

The generated scripts use the best recorded selector per step (role+name when available, then
test ids / css / xpath). They are a starting point you can read and edit, not the self-healing
replayer: for that use `waystone replay`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .workflow import Step, Target, Workflow
from .world import CORE_JS

FORMATS = ("agent", "playwright-python", "playwright-js", "puppeteer", "cdp", "devtools")


def export(wf: Workflow, fmt: str) -> str:
    if fmt == "agent":
        return to_agent(wf)
    if fmt == "playwright-python":
        return to_playwright_python(wf)
    if fmt == "playwright-js":
        return to_playwright_js(wf)
    if fmt == "puppeteer":
        return to_puppeteer(wf)
    if fmt == "cdp":
        return to_cdp(wf)
    if fmt == "devtools":
        from .devtools import to_devtools
        return to_devtools(wf)
    raise ValueError(f"unknown format {fmt!r}; choose from {', '.join(FORMATS)}")


def ext(fmt: str) -> str:
    return {"agent": ".agent.md", "playwright-python": ".playwright.py", "playwright-js": ".playwright.mjs", "puppeteer": ".puppeteer.mjs", "cdp": ".cdp.json", "devtools": ".devtools.json"}[fmt]


# --- selector choice --------------------------------------------------------

def best(t: Target) -> dict[str, Any]:
    """The candidate to lead with: role+name reads best and survives restyling; test ids next; then the rest."""
    cands = t.candidates
    for c in cands:
        if c["kind"] == "role":
            return c
    for c in cands:
        if c["kind"] == "css" and c["value"].startswith("[data-"):
            return c
    for c in cands:
        if c["kind"] in ("css", "label", "text"):
            return c
    return cands[-1]


def _css_fallback(t: Target) -> str | None:
    for c in t.candidates:
        if c["kind"] == "css" and " >>> " not in c["value"]:
            return c["value"]
    return None


def describe_sel(c: dict[str, Any]) -> str:
    if c["kind"] == "role":
        return f'role={c["role"]} name="{c["name"]}"'
    if c["kind"] == "text":
        return f'text="{c["value"]}"'
    if c["kind"] == "label":
        return f'label="{c["value"]}"'
    return f'{c["kind"]}={c["value"]}'


# --- agent brief ---------------------------------------------------------------

def to_agent(wf: Workflow) -> str:
    out = [f"# {wf.name}", "", f"Recorded workflow, {len(wf.steps)} steps. Start at `{wf.start_url}`.", ""]
    if wf.params:
        out += ["Parameters to supply: " + ", ".join(f"`{{{{{p}}}}}`" for p in wf.params), ""]
    out += [
        "Each step lists the action, the element as a user would describe it, then the selectors that",
        "identified it at recording time, most stable first. Prefer the role/name selector; fall back",
        "down the list if the page has changed. `settle` is how long the page took to go quiet after",
        "the action and what it loaded, so wait for the same before the next step.",
        "",
    ]
    for s in wf.steps:
        tab = f" (tab {s.tab})" if s.tab else ""
        out.append(f"## {s.index}. {s.describe()}{tab}")
        if s.target:
            fp = s.target.fingerprint
            el = fp.get("role") if fp.get("role") not in (None, "generic") else fp.get("tag")
            out.append(f"- element: {el} \"{fp.get('name') or fp.get('text') or ''}\"" + (f", type={fp['type']}" if fp.get("type") else ""))
            if s.target.frame:
                out.append(f"- inside iframe: `{' → '.join(s.target.frame)}`")
            sels = [describe_sel(c) for c in s.target.candidates[:4]]
            out.append("- selectors: " + " · ".join(f"`{x}`" for x in sels))
            if s.target.offset:
                out.append(f"- clicked at {int(s.target.offset['ox'] * 100)}%,{int(s.target.offset['oy'] * 100)}% of the element")
        if s.type == "type":
            out.append(f"- value: `{s.value}`" + (" (secret, supplied as a parameter)" if s.secret else ""))
        if s.type in ("select", "set"):
            out.append(f"- value: `{s.value}`" + (f" ({s.note})" if s.note else ""))
        if s.type == "upload":
            out.append(f"- files: {', '.join(f'`{f}`' for f in s.files or [])}" + (f" (recorded as {s.note})" if s.note else ""))
        if s.type == "navigate" and s.note == "after-action":
            out.append("- this navigation was caused by the previous action; wait for it rather than navigating")
        if s.settle_ms:
            loaded = f", loaded {len(s.loaded)} request(s): " + ", ".join(f"`{u}`" for u in s.loaded[:3]) if s.loaded else ""
            out.append(f"- settle: {s.settle_ms / 1000:.1f}s{loaded}")
        if s.screenshot:
            out.append(f"- screenshot: `{s.screenshot}`")
        out.append("")
    return "\n".join(out)


# --- code generation -------------------------------------------------------

def _py(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _param_sub_py(value: str) -> str:
    """'{{email}}' -> params["email"], mixed text -> f-string."""
    if not value:
        return '""'
    parts = re.split(r"(\{\{\s*[\w.-]+\s*\}\})", value)
    if len(parts) == 1:
        return _py(value)
    pieces = []
    for p in parts:
        if not p:
            continue
        m = re.fullmatch(r"\{\{\s*([\w.-]+)\s*\}\}", p)
        pieces.append(f'params[{_py(m.group(1))}]' if m else _py(p))
    return " + ".join(pieces)


def _param_sub_js(value: str) -> str:
    if not value:
        return '""'
    parts = re.split(r"(\{\{\s*[\w.-]+\s*\}\})", value)
    if len(parts) == 1:
        return json.dumps(value, ensure_ascii=False)
    pieces = []
    for p in parts:
        if not p:
            continue
        m = re.fullmatch(r"\{\{\s*([\w.-]+)\s*\}\}", p)
        pieces.append(f"params[{json.dumps(m.group(1))}]" if m else json.dumps(p, ensure_ascii=False))
    return " + ".join(pieces)


def _pw_locator_py(scope: str, t: Target) -> str:
    c = best(t)
    if c["kind"] == "role":
        return f'{scope}.get_by_role({_py(c["role"])}, name={_py(c["name"])}, exact=True).first'
    if c["kind"] == "text":
        return f'{scope}.get_by_text({_py(c["value"])}, exact=True).first'
    if c["kind"] == "label":
        return f'{scope}.get_by_label({_py(c["value"])}).first'
    if c["kind"] == "xpath":
        return f'{scope}.locator("xpath=" + {_py(c["value"])}).first'
    return f'{scope}.locator({_py(c["value"])}).first'


def _pw_locator_js(scope: str, t: Target) -> str:
    c = best(t)
    j = lambda v: json.dumps(v, ensure_ascii=False)
    if c["kind"] == "role":
        return f'{scope}.getByRole({j(c["role"])}, {{ name: {j(c["name"])}, exact: true }}).first()'
    if c["kind"] == "text":
        return f'{scope}.getByText({j(c["value"])}, {{ exact: true }}).first()'
    if c["kind"] == "label":
        return f'{scope}.getByLabel({j(c["value"])}).first()'
    if c["kind"] == "xpath":
        return f'{scope}.locator("xpath=" + {j(c["value"])}).first()'
    return f'{scope}.locator({j(c["value"])}).first()'


def _pw_scope_py(t: Target, page: str) -> str:
    scope = page
    for sel in t.frame:
        scope = f"{scope}.frame_locator({_py(sel)})"
    return scope


def _pw_scope_js(t: Target, page: str) -> str:
    scope = page
    for sel in t.frame:
        scope = f"{scope}.frameLocator({json.dumps(sel)})"
    return scope


def to_playwright_python(wf: Workflow) -> str:
    L: list[str] = []
    L += [
        f'"""{wf.name}: generated by waystone from a recorded session. Edit freely."""',
        "import sys",
        "from playwright.sync_api import sync_playwright",
        "",
        f"START_URL = {_py(wf.start_url)}",
        "PARAMS = " + _py({p: "" for p in wf.params}) + "  # fill these in, or pass key=value on the command line",
        "",
        "",
        "def run(page, context, params):",
    ]
    cur = "page"
    tabs = {0: "page"}
    for s in wf.steps:
        if s.tab in tabs:
            cur = tabs[s.tab]
        ind = "    "
        t = s.target
        if s.type == "navigate":
            if s.note == "after-action":
                L.append(f'{ind}{cur}.wait_for_load_state("domcontentloaded")  # caused by the previous step')
            else:
                L.append(f"{ind}{cur}.goto({_py(s.url)})")
        elif s.type == "new-tab":
            name = f"page{s.tab}"
            tabs[s.tab] = name
            L.append(f'{ind}{name} = context.wait_for_event("page")  # opened by the previous step')
            L.append(f'{ind}{name}.wait_for_load_state("domcontentloaded")')
        elif s.type == "switch-tab":
            L.append(f"{ind}# switch to tab {s.tab}")
        elif s.type == "dialog":
            L.append(f'{ind}{cur}.once("dialog", lambda d: d.accept())')
        elif s.type == "print":
            L.append(f'{ind}{cur}.pdf(path={_py(f"{wf.name}-{s.index:03d}.pdf")})  # the human printed here')
        elif s.type == "scroll":
            if t:
                L.append(f"{ind}{_pw_locator_py(_pw_scope_py(t, cur), t)}.evaluate({_py(f'el => el.scrollTo({int(s.x or 0)}, {int(s.y or 0)})')})")
            else:
                L.append(f"{ind}{cur}.evaluate({_py(f'() => window.scrollTo({int(s.x or 0)}, {int(s.y or 0)})')})")
        elif s.type == "press":
            L.append(f"{ind}{cur}.keyboard.press({_py(s.key or 'Enter')})")
        elif s.type == "wait":
            if t is not None:
                L.append(f"{ind}{_pw_locator_py(_pw_scope_py(t, cur), t)}.wait_for()  # page ready")
            else:
                L.append(f"{ind}{cur}.wait_for_timeout({s.settle_ms or 1000})")
        elif t is not None:
            loc = _pw_locator_py(_pw_scope_py(t, cur), t)
            mods = f", modifiers={_py(s.modifiers)}" if s.modifiers else ""
            if s.type == "click":
                if s.note == "background":
                    L.append(f"{ind}# background click (dismiss) skipped")
                    continue
                L.append(f"{ind}{loc}.click({mods[2:] if mods else ''})")
            elif s.type == "dblclick":
                L.append(f"{ind}{loc}.dblclick()")
            elif s.type == "hover":
                L.append(f"{ind}{loc}.hover()")
            elif s.type == "type":
                L.append(f"{ind}{loc}.fill({_param_sub_py(str(s.value or ''))})")
            elif s.type == "select":
                L.append(f"{ind}{loc}.select_option({_py(str(s.value or ''))})")
            elif s.type == "check":
                L.append(f"{ind}{loc}.set_checked({_py(bool(s.value))})")
            elif s.type == "set":
                L.append(f"{ind}{loc}.fill({_py(str(s.value or ''))})")
            elif s.type == "drag":
                to = s.to or {}
                L.append(f"{ind}{loc}.drag_to({cur}.locator('body'), target_position={{'x': {int(to.get('x', 0))}, 'y': {int(to.get('y', 0))}}})")
            elif s.type == "upload":
                files = [f"params[{_py(re.sub(r'[{} ]', '', f))}]" if f.startswith("{{") else _py(f) for f in (s.files or [])]
                L.append(f"{ind}{loc}.set_input_files([{', '.join(files)}])")
            else:
                L.append(f"{ind}# {s.describe()} (not generated)")
        if s.settle_ms and s.settle_ms > 400 and s.type in ("click", "press", "select", "type"):
            L.append(f'{ind}{cur}.wait_for_load_state("networkidle")  # recorded settle {s.settle_ms / 1000:.1f}s')
    L += [
        "",
        "",
        'if __name__ == "__main__":',
        "    params = {**PARAMS, **dict(a.split('=', 1) for a in sys.argv[1:] if '=' in a)}",
        "    with sync_playwright() as p:",
        '        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")  # Fortress; or p.chromium.launch(headless=False)',
        "        context = browser.contexts[0] if browser.contexts else browser.new_context()",
        "        page = context.new_page()",
        "        run(page, context, params)",
        "        print('done:', page.url)",
    ]
    return "\n".join(L) + "\n"


def to_playwright_js(wf: Workflow) -> str:
    j = lambda v: json.dumps(v, ensure_ascii=False)
    L: list[str] = [
        f"// {wf.name}: generated by waystone from a recorded session. Edit freely.",
        'import { chromium } from "playwright";',
        "",
        f"const START_URL = {j(wf.start_url)};",
        "const PARAMS = " + j({p: "" for p in wf.params}) + ";  // fill these in",
        "",
        "export async function run(page, context, params) {",
    ]
    cur = "page"
    tabs = {0: "page"}
    for s in wf.steps:
        if s.tab in tabs:
            cur = tabs[s.tab]
        ind = "  "
        t = s.target
        if s.type == "navigate":
            L.append(f'{ind}await {cur}.waitForLoadState("domcontentloaded");  // caused by the previous step' if s.note == "after-action" else f"{ind}await {cur}.goto({j(s.url)});")
        elif s.type == "new-tab":
            name = f"page{s.tab}"
            tabs[s.tab] = name
            L.append(f'{ind}const {name} = await context.waitForEvent("page");  // opened by the previous step')
            L.append(f'{ind}await {name}.waitForLoadState("domcontentloaded");')
        elif s.type == "switch-tab":
            L.append(f"{ind}// switch to tab {s.tab}")
        elif s.type == "dialog":
            L.append(f'{ind}{cur}.once("dialog", (d) => d.accept());')
        elif s.type == "print":
            L.append(f'{ind}await {cur}.pdf({{ path: {j(f"{wf.name}-{s.index:03d}.pdf")} }});  // the human printed here')
        elif s.type == "scroll":
            if t:
                L.append(f"{ind}await {_pw_locator_js(_pw_scope_js(t, cur), t)}.evaluate((el) => el.scrollTo({int(s.x or 0)}, {int(s.y or 0)}));")
            else:
                L.append(f"{ind}await {cur}.evaluate(() => window.scrollTo({int(s.x or 0)}, {int(s.y or 0)}));")
        elif s.type == "press":
            L.append(f"{ind}await {cur}.keyboard.press({j(s.key or 'Enter')});")
        elif s.type == "wait":
            if t is not None:
                L.append(f"{ind}await {_pw_locator_js(_pw_scope_js(t, cur), t)}.waitFor();  // page ready")
            else:
                L.append(f"{ind}await {cur}.waitForTimeout({s.settle_ms or 1000});")
        elif t is not None:
            loc = _pw_locator_js(_pw_scope_js(t, cur), t)
            opts = f"{{ modifiers: {j(s.modifiers)} }}" if s.modifiers else ""
            if s.type == "click":
                if s.note == "background":
                    L.append(f"{ind}// background click (dismiss) skipped")
                    continue
                L.append(f"{ind}await {loc}.click({opts});")
            elif s.type == "dblclick":
                L.append(f"{ind}await {loc}.dblclick();")
            elif s.type == "hover":
                L.append(f"{ind}await {loc}.hover();")
            elif s.type == "type":
                L.append(f"{ind}await {loc}.fill({_param_sub_js(str(s.value or ''))});")
            elif s.type == "select":
                L.append(f"{ind}await {loc}.selectOption({j(str(s.value or ''))});")
            elif s.type == "check":
                L.append(f"{ind}await {loc}.setChecked({j(bool(s.value))});")
            elif s.type == "set":
                L.append(f"{ind}await {loc}.fill({j(str(s.value or ''))});")
            elif s.type == "drag":
                to = s.to or {}
                L.append(f"{ind}await {loc}.dragTo({cur}.locator('body'), {{ targetPosition: {{ x: {int(to.get('x', 0))}, y: {int(to.get('y', 0))} }} }});")
            elif s.type == "upload":
                files = [f"params[{j(re.sub(r'[{} ]', '', f))}]" if f.startswith("{{") else j(f) for f in (s.files or [])]
                L.append(f"{ind}await {loc}.setInputFiles([{', '.join(files)}]);")
            else:
                L.append(f"{ind}// {s.describe()} (not generated)")
        if s.settle_ms and s.settle_ms > 400 and s.type in ("click", "press", "select", "type"):
            L.append(f'{ind}await {cur}.waitForLoadState("networkidle");  // recorded settle {s.settle_ms / 1000:.1f}s')
    L += [
        "}",
        "",
        "if (import.meta.url === `file://${process.argv[1]}`) {",
        "  const params = { ...PARAMS, ...Object.fromEntries(process.argv.slice(2).filter((a) => a.includes('=')).map((a) => a.split('=', 2))) };",
        '  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");  // Fortress; or chromium.launch({ headless: false })',
        "  const context = browser.contexts()[0] ?? (await browser.newContext());",
        "  const page = await context.newPage();",
        "  await run(page, context, params);",
        "  console.log('done:', page.url());",
        "}",
    ]
    return "\n".join(L) + "\n"


def _pup_selector(t: Target) -> str:
    """Puppeteer's p-selectors cover role/name and text; css and xpath pass through."""
    c = best(t)
    if c["kind"] == "role":
        name = c["name"].replace('"', '\\"')
        return f'::-p-aria([name="{name}"][role="{c["role"]}"])'
    if c["kind"] == "text":
        return f'::-p-text({c["value"]})'
    if c["kind"] == "xpath":
        return f'::-p-xpath({c["value"]})'
    if c["kind"] == "label":
        css = _css_fallback(t)
        return css or f'::-p-text({c["value"]})'
    return c["value"]


def to_puppeteer(wf: Workflow) -> str:
    j = lambda v: json.dumps(v, ensure_ascii=False)
    L: list[str] = [
        f"// {wf.name}: generated by waystone from a recorded session. Edit freely.",
        'import puppeteer from "puppeteer-core";',
        "",
        f"const START_URL = {j(wf.start_url)};",
        "const PARAMS = " + j({p: "" for p in wf.params}) + ";  // fill these in",
        "",
        "export async function run(page, browser, params) {",
        "  const settle = (ms) => new Promise((r) => setTimeout(r, ms));",
    ]
    cur = "page"
    tabs = {0: "page"}
    for s in wf.steps:
        if s.tab in tabs:
            cur = tabs[s.tab]
        ind = "  "
        t = s.target

        def scope() -> str:
            if not t or not t.frame:
                return cur
            return f"{cur}_f{s.index}"

        if t and t.frame:
            L.append(f"{ind}const {scope()} = await (await {cur}.waitForSelector({j(t.frame[-1])})).contentFrame();")
        if s.type == "navigate":
            L.append(f'{ind}await {cur}.waitForNavigation({{ waitUntil: "domcontentloaded" }}).catch(() => {{}});  // caused by the previous step' if s.note == "after-action" else f'{ind}await {cur}.goto({j(s.url)}, {{ waitUntil: "domcontentloaded" }});')
        elif s.type == "new-tab":
            name = f"page{s.tab}"
            tabs[s.tab] = name
            L.append(f'{ind}const {name} = await (await browser.waitForTarget((t) => t.opener() && t.type() === "page")).page();')
            L.append(f"{ind}await {name}.bringToFront();")
        elif s.type == "switch-tab":
            L.append(f"{ind}// switch to tab {s.tab}")
        elif s.type == "dialog":
            L.append(f'{ind}{cur}.once("dialog", (d) => d.accept());')
        elif s.type == "print":
            L.append(f'{ind}await {cur}.pdf({{ path: {j(f"{wf.name}-{s.index:03d}.pdf")} }});  // the human printed here')
        elif s.type == "scroll":
            if t:
                L.append(f"{ind}await {scope()}.locator({j(_pup_selector(t))}).scroll({{ scrollLeft: {int(s.x or 0)}, scrollTop: {int(s.y or 0)} }});")
            else:
                L.append(f"{ind}await {cur}.evaluate(() => window.scrollTo({int(s.x or 0)}, {int(s.y or 0)}));")
        elif s.type == "press":
            L.append(f"{ind}await {cur}.keyboard.press({j(s.key or 'Enter')});")
        elif s.type == "wait":
            if t is not None:
                L.append(f"{ind}await {scope()}.waitForSelector({j(_pup_selector(t))});  // page ready")
            else:
                L.append(f"{ind}await settle({s.settle_ms or 1000});")
        elif t is not None:
            sel = j(_pup_selector(t))
            sc = scope()
            if s.type == "click":
                if s.note == "background":
                    L.append(f"{ind}// background click (dismiss) skipped")
                    continue
                if s.modifiers:
                    L.append(f"{ind}await {cur}.keyboard.down({j(s.modifiers[0])}); await {sc}.locator({sel}).click(); await {cur}.keyboard.up({j(s.modifiers[0])});")
                else:
                    L.append(f"{ind}await {sc}.locator({sel}).click();")
            elif s.type == "dblclick":
                L.append(f"{ind}await {sc}.locator({sel}).click({{ count: 2 }});")
            elif s.type == "hover":
                L.append(f"{ind}await {sc}.locator({sel}).hover();")
            elif s.type == "type":
                L.append(f"{ind}await {sc}.locator({sel}).fill({_param_sub_js(str(s.value or ''))});")
            elif s.type == "select":
                L.append(f"{ind}await {sc}.locator({sel}).fill({j(str(s.value or ''))});")
            elif s.type == "check":
                L.append(f"{ind}await {sc}.locator({sel}).click();  // sets checked={j(bool(s.value))}")
            elif s.type == "set":
                L.append(f"{ind}await {sc}.locator({sel}).fill({j(str(s.value or ''))});")
            elif s.type == "upload":
                files = [f"params[{j(re.sub(r'[{} ]', '', f))}]" if f.startswith("{{") else j(f) for f in (s.files or [])]
                L.append(f"{ind}await (await {sc}.waitForSelector({sel})).uploadFile({', '.join(files)});")
            elif s.type == "drag":
                to = s.to or {}
                L.append(f"{ind}await (await {sc}.waitForSelector({sel})).drag({{ x: {int(to.get('x', 0))}, y: {int(to.get('y', 0))} }});")
            else:
                L.append(f"{ind}// {s.describe()} (not generated)")
        if s.settle_ms and s.settle_ms > 400 and s.type in ("click", "press", "select", "type"):
            L.append(f"{ind}await settle({min(s.settle_ms, 8000)});  // recorded settle {s.settle_ms / 1000:.1f}s")
    L += [
        "}",
        "",
        "if (import.meta.url === `file://${process.argv[1]}`) {",
        "  const params = { ...PARAMS, ...Object.fromEntries(process.argv.slice(2).filter((a) => a.includes('=')).map((a) => a.split('=', 2))) };",
        '  const browser = await puppeteer.connect({ browserURL: "http://127.0.0.1:9222" });  // Fortress',
        "  const page = await browser.newPage();",
        "  await run(page, browser, params);",
        "  console.log('done:', page.url());",
        "}",
    ]
    return "\n".join(L) + "\n"


def write_exports(wf: Workflow, out_dir: str | Path, formats: tuple[str, ...] = ("agent", "playwright-python")) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for fmt in formats:
        p = Path(out_dir) / f"{wf.name}{ext(fmt)}"
        p.write_text(export(wf, fmt), encoding="utf-8")
        out[fmt] = p
    return out


# --- raw CDP ---------------------------------------------------------------

def to_cdp(wf: Workflow) -> str:
    """The session as Chrome DevTools Protocol commands, for Fortress or any Chromium, from any
    CDP client. No Playwright, no Puppeteer, no Waystone at runtime.

    Robust, not coordinate replay: the export carries `resolver_js` (Waystone's selector engine)
    and each action is preceded by a `resolve` entry. The client evaluates the resolver once per
    document (it is idempotent), then `__ws.resolve(candidates, fingerprint, opts)` returns the
    live `point` to click, scrolled into view, with hit-testing. The recorded coordinates are kept
    as `fallback` for clients that cannot evaluate JS.

    Entry kinds:
      {"method", "params"}            send as-is
      {"resolve": {...}}              evaluate resolver_js if needed, then __ws.resolve(...) -> point
      {"method", "params", "at": "point"}  substitute params.x/y with the last resolved point
      {"wait": {...}}                 poll: "load" (readyState), "idle" (__ws.busy() empty + ms), "target"
    """
    mods = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
    keys = {"Enter": ("Enter", 13, "\r"), "Tab": ("Tab", 9, "\t"), "Escape": ("Escape", 27, None), "Backspace": ("Backspace", 8, None),
            "ArrowUp": ("ArrowUp", 38, None), "ArrowDown": ("ArrowDown", 40, None), "ArrowLeft": ("ArrowLeft", 37, None), "ArrowRight": ("ArrowRight", 39, None)}
    cmds: list[dict[str, Any]] = [
        {"method": "Page.enable"},
        {"method": "Runtime.enable"},
        {"method": "Page.navigate", "params": {"url": wf.start_url}},
        {"wait": {"for": "load"}},
    ]

    def fallback_point(t: Target) -> dict[str, float]:
        r = t.rect
        if t.offset:
            return {"x": r["x"] + r["w"] * t.offset["ox"], "y": r["y"] + r["h"] * t.offset["oy"]}
        return {"x": r["x"] + r["w"] / 2, "y": r["y"] + r["h"] / 2}

    def resolve(s: Step, **extra: Any) -> dict[str, Any]:
        t = s.target
        assert t is not None
        opts: dict[str, Any] = {"frame": t.frame}
        if t.offset:
            opts["offset"] = t.offset
        opts.update(extra)
        return {"resolve": {"candidates": t.candidates, "fingerprint": t.fingerprint, "opts": opts, "fallback": fallback_point(t)}, "meta": {"step": s.index, "action": s.describe()}}

    def mouse(kind: str, m: int = 0, count: int = 1, **extra: Any) -> dict[str, Any]:
        return {"method": "Input.dispatchMouseEvent", "params": {"type": kind, "x": 0, "y": 0, "button": "left", "clickCount": count, "modifiers": m, **extra}, "at": "point"}

    def click(m: int = 0, count: int = 1) -> list[dict[str, Any]]:
        out = [mouse("mouseMoved")]
        for i in range(1, count + 1):
            out += [mouse("mousePressed", m, i), mouse("mouseReleased", m, i)]
        return out

    for s in wf.steps[1:]:
        t = s.target
        md = {"step": s.index, "action": s.describe()}
        if s.type == "navigate":
            if s.note == "after-action":
                cmds.append({"wait": {"for": "navigation", "url": s.url}, "meta": md})
            else:
                cmds.append({"method": "Page.navigate", "params": {"url": s.url}, "meta": md})
                cmds.append({"wait": {"for": "load"}})
        elif s.type in ("new-tab", "switch-tab"):
            cmds.append({"wait": {"for": "target", "tab": s.tab, "url": s.url}, "meta": md})
        elif s.type == "dialog":
            cmds.append({"method": "Page.handleJavaScriptDialog", "params": {"accept": True}, "meta": md})
        elif s.type == "wait":
            cmds.append({"wait": {"ms": s.settle_ms or 1000}, "meta": md} if not t else resolve(s, scroll=False))
        elif s.type == "print":
            cmds.append({"method": "Page.printToPDF", "params": {"printBackground": True}, "meta": md})
        elif s.type == "scroll":
            if t:
                cmds.append(resolve(s, scroll=False))
                cmds.append({"method": "Runtime.evaluate", "params": {"expression": f"__ws.act({json.dumps(t.candidates)}, {json.dumps(t.fingerprint)}, {json.dumps({'frame': t.frame})}, 'scroll', {json.dumps({'x': s.x or 0, 'y': s.y or 0})})"}})
            else:
                cmds.append({"method": "Runtime.evaluate", "params": {"expression": f"window.scrollTo({int(s.x or 0)}, {int(s.y or 0)})"}, "meta": md})
        elif s.type == "press":
            combo = (s.key or "Enter").split("+")
            key = combo[-1]
            m = sum(mods.get(k, 0) for k in combo[:-1])
            code, vk, text = keys.get(key, (key, 0, key if len(key) == 1 else None))
            down = {"type": "keyDown" if text and not m else "rawKeyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk, "modifiers": m}
            if text and not m:
                down["text"] = text
            cmds.append({"method": "Input.dispatchKeyEvent", "params": down, "meta": md})
            cmds.append({"method": "Input.dispatchKeyEvent", "params": {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk, "modifiers": m}})
        elif t is not None:
            if s.type == "click" and s.note == "background":
                continue
            m = sum(mods.get(k, 0) for k in (s.modifiers or []))
            if s.type in ("click", "check"):
                cmds.append(resolve(s)); cmds += click(m)
            elif s.type == "dblclick":
                cmds.append(resolve(s)); cmds += click(m, 2)
            elif s.type == "hover":
                cmds.append(resolve(s)); cmds.append(mouse("mouseMoved")); cmds.append({"wait": {"ms": 350}})
            elif s.type == "type":
                cmds.append(resolve(s)); cmds += click()
                cmds.append({"method": "Input.dispatchKeyEvent", "params": {"type": "rawKeyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 4}})
                cmds.append({"method": "Input.dispatchKeyEvent", "params": {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 4}})
                cmds.append({"method": "Input.dispatchKeyEvent", "params": {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8}})
                cmds.append({"method": "Input.dispatchKeyEvent", "params": {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8}})
                cmds.append({"method": "Input.insertText", "params": {"text": str(s.value or "")}})
            elif s.type in ("select", "set", "upload"):
                action = "select" if s.type == "select" else "setValue"
                if s.type == "upload":
                    cmds.append({"method": "DOM.setFileInputFiles", "params": {"files": s.files or []}, "resolve_object": {"candidates": t.candidates, "fingerprint": t.fingerprint, "opts": {"frame": t.frame}}, "meta": {**md, "note": "evaluate __ws.handle(...) with returnByValue=false and pass its objectId; files are {{params}}"}})
                else:
                    cmds.append({"method": "Runtime.evaluate", "params": {"expression": f"__ws.act({json.dumps(t.candidates)}, {json.dumps(t.fingerprint)}, {json.dumps({'frame': t.frame})}, {json.dumps(action)}, {json.dumps(str(s.value or ''))})", "returnByValue": True}, "meta": md, "needs_resolver": True})
            elif s.type == "drag":
                to = s.to or {}
                cmds.append(resolve(s))
                cmds.append(mouse("mouseMoved")); cmds.append(mouse("mousePressed", 0, 1))
                cmds.append({"method": "Input.dispatchMouseEvent", "params": {"type": "mouseMoved", "x": to.get("x", 0), "y": to.get("y", 0), "button": "left", "buttons": 1}})
                cmds.append({"method": "Input.dispatchMouseEvent", "params": {"type": "mouseReleased", "x": to.get("x", 0), "y": to.get("y", 0), "button": "left", "clickCount": 1}})
        if s.type in ("click", "dblclick", "press", "select", "type", "check") or (s.type == "navigate" and s.note != "after-action"):
            cmds.append({"wait": {"for": "idle", "ms": max(300, min(s.settle_ms or 0, 8000)), "loaded": s.loaded or []}})

    doc = {
        "waystone": wf.version, "name": wf.name, "start_url": wf.start_url, "viewport": wf.viewport, "params": wf.params,
        "resolver_js": CORE_JS,
        "notes": [
            "Open a page target's websocket and send `method` entries in order.",
            "For a `resolve` entry: if `__ws` is undefined in the page (new document), Runtime.evaluate `resolver_js` first; then evaluate __ws.resolve(candidates, fingerprint, opts) with returnByValue and keep its `point`. If `found` is false, retry for up to ~10s; use `fallback` only as a last resort.",
            "Entries with \"at\": \"point\" take params.x/params.y from the last resolved point.",
            "`wait`: \"load\" = document.readyState complete; \"idle\" = __ws.busy() empty and no in-flight XHR for 300ms, cap at `ms`*2+2000; \"target\" = a new page target appeared.",
            "Substitute {{name}} in Input.insertText text with `params`.",
            "See examples/run_cdp_export.py in the waystone repo for a 60-line reference client.",
        ],
        "commands": cmds,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)
