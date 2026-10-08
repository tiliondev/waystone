"""waystone record <url> | replay <workflow.json> | mark <url> | show <workflow.json>"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import HOME_URL, Waystone, __version__
from .export import FORMATS, export, ext
from .workflow import Workflow


def _kv(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--param expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def cmd_record(a: argparse.Namespace) -> int:
    with Waystone(a.cdp, engine=a.engine, headless=False, profile=a.profile) as ws:
        print(f"engine {ws.engine.version} @ {ws.engine.cdp_url}")
        what = f"the open tab matching '{a.attach}'" if a.attach else (a.url or HOME_URL)
        print(f"recording '{a.name}' on {what}. drive the browser; press ■ in the panel (top right) or Ctrl+C to stop.\n", flush=True)
        rec = ws.record(
            a.name, None if a.attach else a.url, out_dir=a.out, video=not a.no_video, show=not a.no_show, attach=a.attach, goal=a.goal,
            on_step=lambda s: print(f"  {s.index:>3}. {s.describe()}" + (f"  [tab {s.tab}]" if s.tab else ""), flush=True),
        )
    wf = rec.workflow
    print(f"\n{len(wf)} steps recorded → {rec.folder}/")
    for name in ("README.md", "automation.json", "resolver.js", "steps/", "video.mp4"):
        if (rec.folder / name.rstrip("/")).exists():
            print(f"  {name}")
    if wf.params:
        print(f"params: {', '.join(wf.params)}  (pass with --param name=value on run)")
    print(f"\nrun it:  waystone run {rec.folder}")
    return 0


def cmd_replay(a: argparse.Namespace) -> int:
    wf = Workflow.load(a.workflow)
    print(wf.summary(), "\n")
    with Waystone(a.cdp, engine=a.engine, headless=a.headless, profile=a.profile) as ws:
        def on_step(s, info):
            via = ""
            if info and info.get("skipped"):
                via = f"  (skipped: {info['skipped']})"
            elif info and info.get("fallback") == "recorded-coordinates":
                via = f"  (clicked recorded coordinates; {info.get('why')})"
            elif info and info.get("pdf"):
                via = f"  (saved {info['pdf']})"
            elif info and info.get("fallback"):
                via = f"  (fallback: {info['fallback']})"
            elif info and info.get("kind") == "canvas":
                via = f"  (display list, via {info.get('via')})"
            elif info and info.get("kind") == "visual":
                via = f"  (visual match {info['score']:.2f} via {info.get('via')}" + (f" at {info['zoom']}x" if info.get('zoom', 1.0) != 1.0 else "") + ")"
            elif info and info.get("kind"):
                via = f"  (via {info['kind']}, score {info['score']:.2f})"
            w = (info or {}).get("wait") or {}
            if w.get("waited_ms", 0) > 400 or w.get("requests"):
                via += f"  [waited {w['waited_ms'] / 1000:.1f}s, {w.get('requests', 0)} req" + (f", still busy: {w['busy']}" if w.get("busy") else (", timed out" if w.get("timed_out") else "")) + "]"
            print(f"  ✓ {s.index:>3}. {s.describe()}{via}", flush=True)

        res = ws.replay(wf, _kv(a.param), on_step=on_step, shots_dir=a.shots, keep_open=a.keep_open, timeout_ms=a.timeout * 1000, show=a.show, until=a.until, canvas_hooks=a.canvas_hooks)
    if res.ok:
        print(f"\ndone in {res.duration_s}s. {res.steps_run} steps, final url {res.final_url}")
        if res.healed:
            print(f"self-healed {len(res.healed)} step(s): {res.healed}")
        return 0
    print(f"\nfailed at {res.error}", file=sys.stderr)
    if res.error and res.error.screenshot:
        p = Path(a.out or ".") / "waystone-failure.png"
        p.write_bytes(res.error.screenshot)
        print(f"screenshot: {p}", file=sys.stderr)
    return 1


def cmd_mark(a: argparse.Namespace) -> int:
    with Waystone(a.cdp, engine=a.engine, headless=a.headless, profile=a.profile) as ws:
        page = ws.attach(a.attach) if a.attach is not None else ws.page(a.url)
        marks = page.mark(viewport_only=not a.all, overlay=a.overlay)
        out = Path(a.out or "marks.png")
        marks.save(out)
        if a.json:
            print(json.dumps([m.__dict__ for m in marks], indent=2))
        else:
            print(marks.to_prompt())
        print(f"\nscreenshot: {out}", file=sys.stderr)
        if a.hold:
            input("press enter to close…")
        if a.attach is None:
            page.close()
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    wf = Workflow.load(a.workflow)
    text = export(wf, a.format)
    if a.out == "-":
        print(text)
        return 0
    out = Path(a.out) if a.out else Path(a.workflow).with_suffix("").with_name(wf.name + ext(a.format))
    out.write_text(text, encoding="utf-8")
    print(out)
    return 0


def cmd_import(a: argparse.Namespace) -> int:
    from .devtools import from_devtools

    wf = from_devtools(Path(a.file).read_text(encoding="utf-8"), name=a.name)
    out = Path(a.out) if a.out else Path(a.file).with_name(wf.name + ".json")
    wf.save(out)
    print(wf.summary())
    print(f"\nsaved {out}; replay with: waystone replay {out}")
    return 0


def cmd_edit(a: argparse.Namespace) -> int:
    from .edit import apply

    waits = [(int(x), int(ms)) for x, ms in (a.wait or [])]
    moves = [(int(x), int(y)) for x, y in (a.move or [])]
    values = [(int(x), v) for x, v in (a.set_value or [])]
    wf = apply(a.folder, remove_ids=[int(x) for x in (a.remove or [])], waits=waits, moves=moves, values=values, goal=a.goal)
    print(wf.summary())
    print(f"\nrewrote {Path(a.folder)}/ (automation.json, README.md, steps/)")
    return 0


def cmd_mcp(a: argparse.Namespace) -> int:
    from waystone.mcp import serve
    serve()
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    print(Workflow.load(a.workflow).summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="waystone", description="Record once, mark for agents, replay anywhere.")
    p.add_argument("--version", action="version", version=f"waystone {__version__}")
    p.add_argument("--cdp", default=None, help="attach to a running browser at this CDP url instead of launching one")
    p.add_argument("--profile", default=os.environ.get("WAYSTONE_PROFILE"), help="browser profile directory to keep cookies and logins between runs (default: a fresh temporary profile)")
    p.add_argument("--engine", choices=["fortress", "chrome"], default=os.environ.get("WAYSTONE_ENGINE", "fortress"), help="which browser to launch when --cdp is not given (default: fortress; $WAYSTONE_ENGINE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record a human session into a workflow")
    r.add_argument("url", nargs="?", help=f"start url (default: {HOME_URL}, or $WAYSTONE_HOME)")
    r.add_argument("-n", "--name", default="workflow")
    r.add_argument("-o", "--out", default=".", help="output directory")
    r.add_argument("--attach", metavar="URL_SUBSTR", help="record an already-open tab instead of opening a new one")
    r.add_argument("--no-video", action="store_true")
    r.add_argument("--no-show", action="store_true", help="do not flash recorded elements in the live page")
    r.add_argument("-g", "--goal", default=None, help="one line: what this automation achieves (goes at the top of README.md)")
    r.set_defaults(fn=cmd_record)

    rp = sub.add_parser("run", aliases=["replay"], help="run an automation folder (or workflow json) deterministically on Fortress")
    rp.add_argument("workflow", help="automation folder, automation.json, or a workflow json")
    rp.add_argument("-p", "--param", action="append", default=[], help="key=value for {{key}} placeholders")
    rp.add_argument("--headless", action="store_true")
    rp.add_argument("--shots", default=None, help="save a screenshot per step into this dir")
    rp.add_argument("--keep-open", action="store_true")
    rp.add_argument("--timeout", type=int, default=10, help="seconds to wait for each element")
    rp.add_argument("--show", action="store_true", help="flash each target in the live page")
    rp.add_argument("--until", type=int, default=None, help="stop after this step index (inclusive)")
    rp.add_argument("--canvas-hooks", action="store_true", help="relocate canvas clicks by reading what the page draws (installs a main-world hook; detectable by the page)")
    rp.add_argument("-o", "--out", default=".")
    rp.set_defaults(fn=cmd_replay)

    m = sub.add_parser("mark", help="number every actionable element on a page")
    m.add_argument("url", nargs="?")
    m.add_argument("-o", "--out", default="marks.png")
    m.add_argument("--attach", metavar="URL_SUBSTR", help="mark an already-open tab")
    m.add_argument("--all", action="store_true", help="include elements outside the viewport")
    m.add_argument("--overlay", action="store_true", help="also paint marks into the live page (headed only)")
    m.add_argument("--json", action="store_true")
    m.add_argument("--headless", action="store_true")
    m.add_argument("--hold", action="store_true", help="keep the page open until enter")
    m.set_defaults(fn=cmd_mark)

    e = sub.add_parser("export", help="agent brief or a Playwright / Puppeteer script from a workflow")
    e.add_argument("workflow")
    e.add_argument("-f", "--format", choices=FORMATS, default="agent")
    e.add_argument("-o", "--out", default=None, help="output file ('-' for stdout; default: next to the workflow)")
    e.set_defaults(fn=cmd_export)

    im = sub.add_parser("import", help="import a Chrome DevTools Recorder JSON user flow as a workflow")
    im.add_argument("file")
    im.add_argument("-n", "--name", default=None)
    im.add_argument("-o", "--out", default=None)
    im.set_defaults(fn=cmd_import)

    ed = sub.add_parser("edit", help="remove, reorder or adjust steps in a recorded folder")
    ed.add_argument("folder")
    ed.add_argument("--remove", nargs="+", metavar="STEP", help="step indices to drop")
    ed.add_argument("--wait", nargs=2, action="append", metavar=("AFTER", "MS"), help="insert a pause after a step")
    ed.add_argument("--move", nargs=2, action="append", metavar=("STEP", "TO"), help="move a step before another")
    ed.add_argument("--set-value", nargs=2, action="append", metavar=("STEP", "VALUE"), help="change a typed/selected value (use {{name}} for a parameter)")
    ed.add_argument("--goal", default=None, help="replace the goal line")
    ed.set_defaults(fn=cmd_edit)

    s = sub.add_parser("show", help="print a workflow's steps")
    s.add_argument("workflow")

    mc = sub.add_parser("mcp", help="run the MCP server on stdio (for Claude Code, Claude Desktop, Cursor, Codex)")
    mc.set_defaults(fn=cmd_mcp)
    s.set_defaults(fn=cmd_show)

    a = p.parse_args(argv)
    if a.cmd == "mark" and not a.url and a.attach is None:
        p.error("url is required unless --attach is given")
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
