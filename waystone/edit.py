"""Edit a recorded automation folder without re-recording.

    waystone edit board-minutes/ --remove 6 7
    waystone edit board-minutes/ --wait 5 2000
    waystone edit board-minutes/ --move 9 3
    waystone edit board-minutes/ --set-value 4 "{{query}}"
    waystone edit board-minutes/ --goal "Download every meeting's minutes"

Each edit rewrites automation.json, renumbers steps, renames the screenshots to match, and
regenerates README.md and the CDP run so the folder stays consistent.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .automation import write_folder
from .workflow import Step, Workflow


def load_folder(folder: str | Path) -> tuple[Workflow, Path, str | None]:
    f = Path(folder)
    if f.is_file():
        f = f.parent
    doc = json.loads((f / "automation.json").read_text(encoding="utf-8"))
    wf = Workflow.load(f / "automation.json")
    wf._dir = f
    return wf, f, doc.get("goal")


def remove(wf: Workflow, indices: list[int]) -> None:
    drop = set(indices)
    wf.steps = [s for s in wf.steps if s.index not in drop]


def move(wf: Workflow, src: int, dst: int) -> None:
    """Move step `src` so it sits at position `dst` (before the step currently there)."""
    s = next(st for st in wf.steps if st.index == src)
    wf.steps.remove(s)
    pos = next((i for i, st in enumerate(wf.steps) if st.index >= dst), len(wf.steps))
    wf.steps.insert(pos, s)


def add_wait(wf: Workflow, after: int, ms: int) -> None:
    pos = next(i for i, st in enumerate(wf.steps) if st.index == after) + 1
    wf.steps.insert(pos, Step(index=0, type="wait", tab=wf.steps[pos - 1].tab, note=f"{ms}ms", settle_ms=ms))


def set_value(wf: Workflow, index: int, value: str) -> None:
    s = next(st for st in wf.steps if st.index == index)
    if s.type not in ("type", "select", "set"):
        raise ValueError(f"step {index} is a {s.type}; only type/select/set steps take a value")
    s.value = value


def renumber(wf: Workflow, folder: Path) -> None:
    """Renumber steps and rename screenshots in two passes so swaps cannot clobber each other."""
    steps_dir = folder / "steps"
    moves: list[tuple[Path, Path]] = []
    for new_i, s in enumerate(wf.steps):
        if s.screenshot:
            old = folder / s.screenshot
            new = steps_dir / f"{new_i:03d}.png"
            if old.exists() and old != new:
                moves.append((old, new))
            s.screenshot = f"steps/{new_i:03d}.png"
        s.index = new_i
    tmp = []
    for old, new in moves:
        t = old.with_name(old.name + ".moving")
        shutil.move(old, t)
        tmp.append((t, new))
    for t, new in tmp:
        shutil.move(t, new)
    # drop screenshots of removed steps
    keep = {folder / s.screenshot for s in wf.steps if s.screenshot}
    for png in steps_dir.glob("*.png"):
        if png not in keep:
            png.unlink()


def apply(folder: str | Path, *, remove_ids: list[int] | None = None, waits: list[tuple[int, int]] | None = None,
          moves: list[tuple[int, int]] | None = None, values: list[tuple[int, str]] | None = None, goal: str | None = None) -> Workflow:
    wf, f, old_goal = load_folder(folder)
    for after, ms in waits or []:
        add_wait(wf, after, ms)
    for i, v in values or []:
        set_value(wf, i, v)
    for src, dst in moves or []:
        move(wf, src, dst)
    if remove_ids:
        remove(wf, remove_ids)
    renumber(wf, f)
    write_folder(wf, f, goal=goal if goal is not None else old_goal, video=(f / "video.mp4") if (f / "video.mp4").exists() else None)
    return wf
