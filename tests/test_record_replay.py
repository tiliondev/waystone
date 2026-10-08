"""Record a session with real input events, then replay it blind. Needs a headed Fortress on CDP."""
from __future__ import annotations

import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from waystone import Page, Waystone, Workflow
from waystone.engine import probe

CDP = os.environ.get("WAYSTONE_CDP_URL", "http://127.0.0.1:9222")


@pytest.fixture(scope="module")
def fixture_url():
    handler = partial(SimpleHTTPRequestHandler, directory=str(Path(__file__).parent))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/fixture.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def ws():
    if probe(CDP) is None:
        pytest.skip(f"no CDP endpoint at {CDP}")
    with Waystone(CDP) as w:
        yield w


def types(wf: Workflow) -> list[str]:
    return [s.type for s in wf.steps]


def test_record_everything_then_replay(ws, fixture_url, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("hello")

    rec = ws.recorder("edge", out_dir=tmp_path, video=True)
    rec.start(fixture_url)
    human = Page(rec.pw_page)  # drives the recorder's tab with real CDP input events

    m = human.mark(viewport_only=False)
    assert not [x for x in m if (x.name or "") in ("Pause", "Stop and save") or (x.name or "").startswith("Hide steps")], "HUD controls leaked into marks"
    human.type(next(x for x in m if x.type == "email"), "a@b.co")
    human.type(next(x for x in m if x.type == "password"), "hunter2")
    human.select("select[name=plan]", "pro")
    human.click("#tos")
    human.upload("#doc", [str(doc)])
    human.hover(m.find("Products"))
    human.click("#hidden-item")
    human.dblclick(m.find("Card title"))
    human.click(m.find("Shadow button"))
    human.click(m.find("Frame button"))
    r = human.locate("#far")
    x, y = r["rect"]["x"] + 5, r["rect"]["y"] + 5
    human.input.drag(x, y, x + 60, y + 40)
    human.input.press("Escape")
    human.world.eval("setTimeout(() => confirm('sure?'), 10)")
    human._sleep(400)
    human.click(m.find("Open popup"))
    popup = human.wait_for_popup(5000)
    assert popup is not None, "popup did not open"
    human._sleep(600)
    pop = Page(popup)
    pop.click("#pbtn")
    human._sleep(1200)  # let debounced type/scroll flushes land
    human.world.close()
    pop.world.close()
    wf = rec.stop()

    print(wf.summary())
    t = types(wf)
    for kind in ("type", "select", "check", "upload", "hover", "dblclick", "click", "drag", "press", "dialog", "new-tab", "switch-tab"):
        assert kind in t, f"missing {kind} step in {t}"

    folder = tmp_path / "edge"
    pw = next(s for s in wf.steps if s.type == "type" and s.secret)
    assert pw.value == "{{password}}" and "hunter2" not in (folder / "automation.json").read_text()
    assert wf.params == ["password", "doc_txt"]
    # the deliverable: README, automation.json with a deterministic run, resolver, captioned steps, video
    readme = (folder / "README.md").read_text()
    assert "## How to do it" in readme and "Type the value of {{password}}" in readme and "A new tab opens" in readme
    auto = json.loads((folder / "automation.json").read_text())
    assert auto["run"]["resolver"] == "resolver.js" and (folder / "resolver.js").exists()
    assert any(c.get("resolve") for c in auto["run"]["commands"])
    assert any(st["do"].startswith(("Type", "Click")) for st in auto["steps"][1:3])
    assert any(s.type == "click" and s.target and s.target.frame for s in wf.steps), "iframe click lost its frame chain"
    assert any(s.type == "click" and s.target and any(" >>> " in c.get("value", "") for c in s.target.candidates) for s in wf.steps), "shadow click lost its deep selector"
    assert sum(1 for s in wf.steps if s.type == "dblclick") == 1 and not any(s.type == "click" and s.target and s.target.fingerprint.get("name") == "Card title" for s in wf.steps)
    assert "video" in rec.artifacts and rec.artifacts["video"].stat().st_size > 10000
    shots = list((folder / "steps").glob("*.png"))
    assert len(shots) >= 8

    # --- replay blind, on a fresh page ---
    res = ws.replay(wf, params={"password": "s3cret", "doc_txt": str(doc)}, timeout_ms=5000)
    assert res.ok, f"replay failed: {res.error}"
    assert res.steps_run == len(wf.steps)


def test_replay_fails_loudly_with_screenshot(ws, fixture_url, tmp_path):
    from waystone.workflow import Step, Target

    wf = Workflow(name="bad", start_url=fixture_url, steps=[
        Step(index=0, type="navigate", url=fixture_url),
        Step(index=1, type="click", target=Target(candidates=[{"kind": "css", "value": "#does-not-exist"}], fingerprint={"tag": "button", "name": "Nope"}, rect={"x": 0, "y": 0, "w": 1, "h": 1})),
    ])
    res = ws.replay(wf, timeout_ms=800)
    assert not res.ok and res.error is not None
    assert res.error.step.index == 1 and res.error.screenshot


def test_only_scrolls_a_person_made_are_recorded(ws, fixture_url, tmp_path):
    """Pages scroll themselves (carousels, scroll restoration, scrollIntoView on focus). Those are
    not steps. A wheel gesture is."""
    rec = ws.recorder("scrolls", out_dir=tmp_path, video=False)
    rec.start(fixture_url)
    human = Page(rec.pw_page)
    human.world.eval("scrollTo(0, 400)"); human._sleep(700)   # the page moved itself
    human.click("#far"); human._sleep(700)                     # click scrolls the target into view itself
    assert "scroll" not in types(rec.workflow), rec.workflow.summary()
    human.scroll(300); human._sleep(700)                       # a real wheel gesture
    human.world.close()
    wf = rec.stop()
    assert types(wf).count("scroll") == 1, wf.summary()
