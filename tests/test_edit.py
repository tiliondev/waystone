"""Editing a recorded folder keeps it consistent: numbering, screenshots, README, CDP run."""
from __future__ import annotations

import json
from pathlib import Path

from waystone.automation import write_folder
from waystone.edit import apply
from waystone.workflow import Step, Target, Workflow


def _folder(tmp_path: Path) -> Path:
    t = lambda n: Target(candidates=[{"kind": "role", "role": "button", "name": n}], fingerprint={"role": "button", "name": n, "tag": "button"}, rect={"x": 0, "y": 0, "w": 10, "h": 10})
    wf = Workflow(name="e", start_url="https://x.test/", steps=[
        Step(index=0, type="navigate", url="https://x.test/"),
        Step(index=1, type="click", target=t("A"), screenshot="steps/001.png"),
        Step(index=2, type="click", target=t("Oops"), screenshot="steps/002.png"),
        Step(index=3, type="type", target=t("Q"), value="hello", screenshot="steps/003.png"),
        Step(index=4, type="click", target=t("Go"), screenshot="steps/004.png"),
    ])
    f = tmp_path / "e"; (f / "steps").mkdir(parents=True)
    for i in (1, 2, 3, 4):
        (f / "steps" / f"{i:03d}.png").write_bytes(b"png" + bytes([i]))
    write_folder(wf, f, goal="test")
    return f


def test_remove_wait_value_goal(tmp_path):
    f = _folder(tmp_path)
    wf = apply(f, remove_ids=[2], waits=[(3, 500)], values=[(3, "{{query}}")], goal="edited")
    assert [s.type for s in wf.steps] == ["navigate", "click", "type", "wait", "click"]
    assert [s.index for s in wf.steps] == [0, 1, 2, 3, 4]
    assert wf.steps[2].value == "{{query}}" and wf.params == ["query"]
    # screenshots follow their steps: old 003 (type) is now 002, old 004 (Go) is now 004 after the wait; 002 (Oops) is gone
    assert (f / "steps" / "002.png").read_bytes() == b"png\x03"
    assert (f / "steps" / "004.png").read_bytes() == b"png\x04"
    assert sorted(p.name for p in (f / "steps").glob("*.png")) == ["001.png", "002.png", "004.png"]
    doc = json.loads((f / "automation.json").read_text())
    assert doc["goal"] == "edited" and doc["params"] == ["query"]
    assert any(c.get("wait", {}).get("ms") == 500 for c in doc["run"]["commands"])
    assert "Wait 0.5s" in (f / "README.md").read_text()


def test_move(tmp_path):
    f = _folder(tmp_path)
    wf = apply(f, moves=[(4, 1)])
    assert [s.target.fingerprint["name"] if s.target else s.type for s in wf.steps] == ["navigate", "Go", "A", "Oops", "Q"]
    assert (f / "steps" / "001.png").read_bytes() == b"png\x04"
