"""Selector engine + Page actions against a local fixture. Needs a Fortress/Chromium on CDP."""
from __future__ import annotations

import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from waystone import Waystone
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


@pytest.fixture()
def page(ws, fixture_url):
    p = ws.page()
    p.goto(fixture_url)
    yield p
    p.close()


def log(page):
    return page.world.eval("document.getElementById('log').textContent")


# --- scan / marks ------------------------------------------------------------

def test_scan_skips_hidden_and_numbers_in_reading_order(page):
    marks = page.mark()
    names = [m.name for m in marks]
    assert "Hidden" not in names
    assert names[0] == "header" or names[0] == "Home"
    assert marks[0].index == 0


def test_candidates_prefer_test_ids_then_role(page):
    btn = page.mark().find("Sign in")
    kinds = [(c["kind"], c.get("value") or c.get("name")) for c in btn.candidates]
    assert kinds[0] == ("css", '[data-testid="submit-btn"]')
    assert ("role", "Sign in") in kinds


def test_auto_generated_classes_are_ignored(page):
    close = page.mark().find("Close dialog")
    css = [c["value"] for c in close.candidates if c["kind"] == "css"]
    assert not any("css-1x2y3z" in v for v in css)


def test_nested_link_collapses_to_one_mark(page):
    cards = [m for m in page.mark() if "Nested link title" in (m.name or "")]
    assert len(cards) == 1 and cards[0].role == "link"


def test_cursor_pointer_div_is_a_mark(page):
    card = page.mark().find("Card title")
    assert card is not None and card.tag == "div"


def test_huge_focusable_section_is_not_a_mark(page):
    assert page.mark().find("Some text in a huge") is None


def test_shadow_dom_button_is_found_with_deep_selector(page):
    m = page.mark(viewport_only=False).find("Shadow button")
    assert m is not None
    assert any(" >>> " in c["value"] for c in m.candidates if c["kind"] == "css")
    page.click(m)


def test_iframe_button_is_found_with_frame_chain(page):
    m = page.mark(viewport_only=False).find("Frame button")
    assert m is not None and m.frame and ("#frame" in m.frame[0] or "iframe" in m.frame[0])
    r = page.click(m)
    assert r["found"]


# --- resolution / self-heal --------------------------------------------------

def test_label_resolves_input(page):
    email = next(m for m in page.mark() if m.tag == "input" and m.type == "email")
    assert email.name == "Email"
    r = page.world.call("resolve", [{"kind": "label", "value": "Email"}], email.fingerprint, {})
    assert r["found"] and r["tag"] == "input"


def test_self_heal_falls_back_when_testid_disappears(page):
    btn = page.mark().find("Sign in")
    page.world.eval("document.querySelector('[data-testid]').removeAttribute('data-testid')")
    r = page.world.call("resolve", btn.candidates, btn.fingerprint, {})
    assert r["found"] and r["kind"] in ("role", "text")


def test_locate_waits_for_late_element(page):
    page.world.eval("setTimeout(() => { const b = document.createElement('button'); b.textContent = 'Late'; b.id = 'late'; document.body.prepend(b); }, 800)")
    r = page.locate("#late", timeout_ms=3000)
    assert r["found"]


def test_locate_raises_with_reason(page):
    from waystone import NotFound

    with pytest.raises(NotFound):
        page.locate("#nope", timeout_ms=500)


# --- actions -----------------------------------------------------------------

def test_click_scrolls_far_element_into_view(page):
    page.click(page.mark(viewport_only=False).find("Far below"))
    assert log(page) == "far-clicked"


def test_covered_element_falls_back_to_js_click(page):
    r = page.click("#covered")
    assert log(page) == "covered-clicked"
    assert r.get("fallback") == "js-click"


def test_hover_reveals_menu_then_click(page):
    page.hover(page.mark().find("Products"))
    page.click("#hidden-item")
    assert log(page) == "widgets-clicked"


def test_type_select_check_set(page):
    marks = page.mark()
    email = next(m for m in marks if m.tag == "input" and m.type == "email")
    page.type(email, "a@b.co")
    assert page.world.eval("document.getElementById('email').value") == "a@b.co"
    page.select("select[name=plan]", "pro")
    assert page.world.eval("document.querySelector('select').value") == "pro"
    page.select("select[name=plan]", "Free")  # by label
    assert page.world.eval("document.querySelector('select').value") == "free"
    page.check("#tos", True)
    assert page.world.eval("document.getElementById('tos').checked") is True
    page.check("#tos", True)  # idempotent
    assert page.world.eval("document.getElementById('tos').checked") is True
    page.set_value("input[name=vol]", "7")
    assert page.world.eval("document.querySelector('input[name=vol]').value") == "7"


def test_upload(page, tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("hi")
    page.upload("#doc", [str(f)])
    assert page.world.eval("document.getElementById('doc').files[0].name") == "doc.txt"


def test_dialog_is_auto_accepted(page):
    page.world.eval("setTimeout(() => { window.__r = confirm('ok?'); }, 50)")
    page._sleep(400)
    assert page.world.eval("window.__r") is True


def test_find_prefers_exact_match(page):
    marks = page.mark(viewport_only=False)
    assert marks.find("Home").name == "Home"
    assert marks.find("new", role="link") is None or marks.find("new", role="link").name.lower().startswith("new")


def test_closed_shadow_root_button_is_found_and_clicked(page):
    assert page.world.eval("document.getElementById('closedw').shadowRoot") is None  # really closed
    m = page.mark(viewport_only=False).find("Closed button")
    assert m is not None and any(" >>> " in c["value"] for c in m.candidates if c["kind"] == "css")
    page.click(m)
    assert log(page) == "closed-clicked"
