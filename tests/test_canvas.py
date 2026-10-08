"""Canvas apps: no DOM per control, so clicks are recorded as a crop and relocated visually on replay."""
from __future__ import annotations

import io
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from waystone import Page, Waystone, visual
from waystone.engine import probe

CDP = os.environ.get("WAYSTONE_CDP_URL", "http://127.0.0.1:9222")


def _scene(x0: int) -> bytes:
    im = Image.new("RGB", (640, 360), "#fafafa"); d = ImageDraw.Draw(im)
    d.rectangle((x0, 40, x0 + 140, 88), fill="#2a6"); d.text((x0 + 50, 56), "Save", fill="white")
    d.rectangle((x0 + 180, 40, x0 + 320, 88), fill="#26a"); d.text((x0 + 230, 56), "Export", fill="white")
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()


def test_template_match_finds_shifted_crop_and_rejects_absent_one():
    vp = {"w": 640, "h": 360}
    vt = visual.capture(_scene(40), 110, 64, vp)
    assert not vt["ambiguous"]
    r = visual.locate(_scene(130), vt, vp, near=(110, 64))
    assert r["found"] and abs(r["x"] - 200) < 2 and abs(r["y"] - 64) < 2
    blank = io.BytesIO(); Image.new("RGB", (640, 360), "white").save(blank, "PNG")
    assert not visual.locate(blank.getvalue(), vt, vp, near=(110, 64))["found"]


@pytest.fixture(scope="module")
def canvas_url():
    h = partial(SimpleHTTPRequestHandler, directory=str(Path(__file__).parent)); h.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h); threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/canvas.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def ws():
    if probe(CDP) is None:
        pytest.skip(f"no CDP endpoint at {CDP}")
    with Waystone(CDP) as w:
        yield w


def test_canvas_clicks_record_crops_and_replay_after_layout_shift(ws, canvas_url, tmp_path):
    rec = ws.recorder("cv", out_dir=tmp_path, video=False)
    rec.start(canvas_url); human = Page(rec.pw_page); human._sleep(600)
    for cx, cy in ((110, 64), (470, 64)):   # Save, Delete
        human.input.click(cx, cy); human._sleep(400)
    human.world.close(); wf = rec.stop()
    clicks = [s for s in wf.steps if s.type == "click" and s.target and s.target.crop]
    assert len(clicks) == 2 and all(s.target.crop and (tmp_path / "cv" / s.target.crop["path"]).exists() for s in clicks)
    wf.start_url = canvas_url + "?shift=90"; wf.steps[0].url = wf.start_url
    kinds = []
    res = ws.replay(wf, timeout_ms=5000, on_step=lambda s, i: kinds.append((s.type, (i or {}).get("kind"))), keep_open=True)
    assert res.ok and [k for t, k in kinds if t == "click"] == ["visual", "visual"]
    p = ws.attach("canvas.html")
    assert p.world.eval("document.getElementById('log').textContent") == "canvas:delete"
    p.close()


@pytest.fixture(scope="module")
def grid_url(canvas_url):
    return canvas_url.replace("canvas.html", "canvas_grid.html")


def test_grid_cell_relocates_on_shift_and_reports_ambiguity_on_scroll(ws, grid_url, tmp_path):
    rec = ws.recorder("grid", out_dir=tmp_path, video=False)
    rec.start(grid_url); human = Page(rec.pw_page); human._sleep(600)
    human.input.click(60 + 2 * 100 + 50, 40 + 4 * 28 + 14); human._sleep(400)   # cell C5
    human.world.close(); wf = rec.stop()
    crop = wf.steps[-1].target.crop
    assert crop and not crop["ambiguous"] and crop["context"]["half"] > 24  # grown until unique

    def run(variant):
        wf.start_url = grid_url + variant; wf.steps[0].url = wf.start_url
        info = []
        ws.replay(wf, timeout_ms=5000, keep_open=True, on_step=lambda s, i: info.append(i))
        p = ws.attach("canvas_grid"); hit = p.world.eval("document.getElementById('log').textContent"); p.close()
        return hit, info[-1]

    hit, i = run("?shift=120")
    assert hit == "cell:C5" and i["kind"] == "visual" and i["via"] == "context"
    hit, i = run("?scroll=3")   # C5 is off-screen; every candidate is a different cell
    assert i["fallback"] == "recorded-coordinates" and "ambiguous" in i["why"]


def test_display_list_identifies_cell_by_headers_and_relocates_after_scroll(ws, grid_url, tmp_path):
    from waystone.canvas import phrase

    rec = ws.recorder("dl", out_dir=tmp_path, video=False)
    rec.start(grid_url); human = Page(rec.pw_page); human._sleep(600)
    human.input.click(310, 166); human._sleep(400)                       # C5
    human.input.click(60 + 7 * 100 + 50, 40 + 11 * 28 + 14); human._sleep(400)   # H12
    human.world.close(); wf = rec.stop()
    descs = [s.target.canvas for s in wf.steps if s.target and s.target.canvas]
    assert [phrase(d) for d in descs] == ["the item reading “34” in row “5”, column “C”", "the item reading “1” in row “12”, column “H”"]
    assert "row header “5” is to its left" in (tmp_path / "dl" / "README.md").read_text()

    def run(variant):
        wf.start_url = grid_url + variant; wf.steps[0].url = wf.start_url
        res = ws.replay(wf, timeout_ms=5000, keep_open=True, canvas_hooks=True, until=2)
        p = ws.attach("canvas_grid"); hit = p.world.eval("document.getElementById('log').textContent"); p.close()
        return res, hit

    for variant in ("?scroll=3", "?scroll=2&shift=60"):      # pixels could not do these
        res, hit = run(variant)
        assert res.ok and hit == "cell:C5", (variant, res.error, hit)
    res, hit = run("?scroll=8")                                # C5 is not on screen: refuse, do not guess
    assert not res.ok and hit == "" and "not drawn on the canvas" in str(res.error)
