"""Minimal agent loop: mark the page, ask a model which number to click, act, repeat.

Swap `decide()` for a real model call. The prompt it receives is `marks.to_prompt()`
plus the annotated screenshot bytes in `marks.png`.
"""
from __future__ import annotations

import sys

from waystone import Marks, Waystone

GOAL = "open the 'new' page, then open the first story"


def decide(goal: str, marks: Marks, step: int) -> str:
    # Stand-in for an LLM. Returns an action like "click 3", "type 5 hello", "press Enter", "done".
    if step == 0:
        m = marks.find("new")
        return f"click {m.index}" if m else "done"
    if step == 1:
        for m in marks:
            if m.role == "link" and m.name and m.name not in ("new", "past", "comments", "ask", "show", "jobs", "submit", "login"):
                return f"click {m.index}"
    return "done"


def main() -> int:
    with Waystone() as ws:
        page = ws.page("https://news.ycombinator.com")
        for step in range(6):
            marks = page.mark()
            marks.save(f"out/step-{step}.png")
            print(f"\n--- step {step}: {len(marks)} marks on {marks.url}")
            action = decide(GOAL, marks, step)
            print("model says:", action)
            if action == "done":
                break
            verb, *rest = action.split(" ", 2)
            if verb == "click":
                page.click(int(rest[0]))
            elif verb == "type":
                page.type(int(rest[0]), rest[1])
            elif verb == "press":
                page.press(rest[0])
        print("final url:", page.url)
        page.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
