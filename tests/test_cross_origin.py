"""A cross-site iframe (localhost vs 127.0.0.1 are different sites, so Chromium puts it in its own
process). Marks, actions, recording and replay must all work inside it."""
from __future__ import annotations

import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from waystone import Page, Waystone
from waystone.engine import probe

CDP = os.environ.get("WAYSTONE_CDP_URL", "http://127.0.0.1:9222")
HERE = Path(__file__).parent


@pytest.fixture(scope="module")
def host_url():
    quiet = partial(SimpleHTTPRequestHandler, directory=str(HERE)); quiet.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    frame_srv = ThreadingHTTPServer(("127.0.0.1", 0), quiet)
    threading.Thread(target=frame_srv.serve_forever, daemon=True).start()
    frame_url = f"http://localhost:{frame_srv.server_address[1]}/xframe.html"

    class Host(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/xorigin.html"):
                body = (HERE / "xorigin.html").read_text().replace("XFRAME_URL", frame_url).encode()
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body); return
            return super().do_GET()
        def log_message(self, *a, **k): pass

    host_srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Host, directory=str(HERE)))
    threading.Thread(target=host_srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{host_srv.server_address[1]}/xorigin.html"
    host_srv.shutdown(); frame_srv.shutdown()


@pytest.fixture(scope="module")
def ws():
    if probe(CDP) is None:
        pytest.skip(f"no CDP endpoint at {CDP}")
    with Waystone(CDP) as w:
        yield w


def test_marks_and_actions_inside_cross_origin_iframe(ws, host_url):
    p = ws.page(host_url); p._sleep(1200)
    assert p.world.eval("document.getElementById('pay').contentDocument === null"), "fixture is not cross-origin"
    m = p.mark(viewport_only=False)
    btn = m.find("Pay now"); card = next(x for x in m if x.tag == "input")
    assert btn and btn.frame == ["#pay"] and btn.rect["y"] > 100  # lifted to page coordinates
    assert p.type(card, "4242").get("_frame")
    assert p.click(btn).get("_frame")
    p._sleep(300)
    fs = next(iter(p.world.frames.values()))
    assert fs.eval("document.getElementById('flog').textContent") == "paid:4242"
    p.close()


def test_record_and_replay_through_cross_origin_iframe(ws, host_url, tmp_path):
    rec = ws.recorder("xo", out_dir=tmp_path, video=False)
    rec.start(host_url)
    human = Page(rec.pw_page); human._sleep(1200)
    m = human.mark(viewport_only=False)
    human.type(next(x for x in m if x.tag == "input"), "4242"); human.click(m.find("Pay now"))
    human._sleep(1200); human.world.close()
    wf = rec.stop()
    assert [s.type for s in wf.steps if s.target and s.target.frame and s.type != "wait"] == ["click", "press", "type", "click"]
    assert all(s.target.frame == ["#pay"] for s in wf.steps if s.target)
    res = ws.replay(wf, timeout_ms=6000)
    assert res.ok, res.error


def test_enter_that_navigates_to_another_site_returns(ws, host_url):
    """Enter in a form whose action is on another site starts a cross-process navigation between
    the keyDown and the keyUp. The release must not block on an acknowledgement from a renderer
    that no longer exists."""
    import time
    p = ws.page(host_url); p._sleep(800)
    box = next(x for x in p.mark(viewport_only=False) if x.tag == "input" and x.frame == [])
    p.type(box, "tacos")
    t = time.time(); p.press("Enter")
    assert time.time() - t < 6, "press(Enter) blocked across a cross-site navigation"
    assert "q=tacos" in p.url and "xframe" in p.url
    p.close()
