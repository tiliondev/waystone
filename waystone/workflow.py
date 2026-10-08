"""The recorded artifact: a list of steps, each with ranked selectors and a screenshot."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2
_PARAM = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")

STEP_TYPES = (
    "navigate", "click", "dblclick", "type", "press", "select", "check", "set",
    "hover", "drag", "upload", "scroll", "new-tab", "switch-tab", "dialog", "print", "wait",
)


@dataclass
class Target:
    candidates: list[dict[str, Any]]
    fingerprint: dict[str, Any]
    rect: dict[str, float]
    frame: list[str] = field(default_factory=list)  # css path of each iframe from the top document
    offset: dict[str, float] | None = None  # where inside the element the human clicked (fractions)
    crop: dict[str, Any] | None = None  # visual target for canvas-like elements: crops + recorded point
    canvas: dict[str, Any] | None = None  # what the page drew there: label, row/col anchors, cell (from the display list)

    @classmethod
    def from_js(cls, d: dict[str, Any]) -> "Target":
        return cls(candidates=d["candidates"], fingerprint=d["fingerprint"], rect=d["rect"], frame=d.get("frame") or [], offset=d.get("offset"))

    @property
    def visual(self) -> bool:
        """True when the element has no identity of its own (canvas, image map, big inert surface)."""
        fp = self.fingerprint or {}
        return fp.get("tag") in ("canvas", "svg", "img", "video") or (fp.get("role") in (None, "generic") and not fp.get("name") and not fp.get("text"))

    @property
    def label(self) -> str:
        fp = self.fingerprint or {}
        name = fp.get("name") or fp.get("text") or ""
        kind = fp.get("role") if fp.get("role") not in (None, "generic") else fp.get("tag")
        if not kind and not name and self.candidates:  # imported flows carry selectors only
            c = self.candidates[0]
            s = f'{c.get("role", "element")} "{c["name"]}"' if c["kind"] == "role" else f'`{c.get("value", "")}`'
        else:
            s = f'{kind} "{name}"' if name else str(kind or "element")
        return s + (" (in iframe)" if self.frame else "")


@dataclass
class Step:
    index: int
    type: str
    t: int = 0
    tab: int = 0
    url: str | None = None
    target: Target | None = None
    value: Any = None
    key: str | None = None
    modifiers: list[str] | None = None
    secret: bool = False
    x: float | None = None
    y: float | None = None
    to: dict[str, float] | None = None
    files: list[str] | None = None
    frame: list[str] | None = None
    screenshot: str | None = None
    note: str | None = None
    settle_ms: int | None = None  # measured at record time: how long until network + DOM went quiet
    loaded: list[str] | None = None  # requests that completed during that settle (short urls)

    def describe(self) -> str:
        tl = self.target.label if self.target else "?"
        t = self.type
        if t == "navigate":
            return f"navigate {self.url}"
        if t in ("click", "dblclick", "hover"):
            mods = "+".join(self.modifiers) + "+" if self.modifiers else ""
            tail = f" ({self.note})" if self.note else ""
            return f"{mods}{t} {tl}{tail}"
        if t == "type":
            v = "{{…}}" if self.secret else repr(self.value)
            return f"type {v} into {tl}"
        if t == "press":
            return f"press {self.key}" + (f" in {tl}" if self.target and self.target.fingerprint.get("tag") in ("input", "textarea") else "")
        if t == "select":
            return f"select {self.note or self.value!r} in {tl}"
        if t == "check":
            return f"{'check' if self.value else 'uncheck'} {tl}"
        if t == "set":
            return f"set {tl} to {self.value!r}"
        if t == "drag":
            return f"drag {tl} by ({int((self.to or {}).get('x', 0) - (self.x or 0))}, {int((self.to or {}).get('y', 0) - (self.y or 0))})"
        if t == "upload":
            return f"upload {', '.join(self.files or [])} to {tl}"
        if t == "scroll":
            where = f"inside {tl}" if self.target else "page"
            return f"scroll {where} to ({int(self.x or 0)}, {int(self.y or 0)})"
        if t == "new-tab":
            return f"new tab {self.tab} opened: {self.url}"
        if t == "switch-tab":
            return f"switch to tab {self.tab}"
        if t == "dialog":
            return f"dialog {self.note}: {self.value!r}"
        if t == "print":
            return "print page" + (f" → {self.note}" if self.note else "")
        if t == "wait":
            return (f"wait for {tl}" if self.target else "wait") + (f" ({self.note})" if self.note else "")
        return t


@dataclass
class Workflow:
    name: str
    start_url: str
    steps: list[Step] = field(default_factory=list)
    viewport: dict[str, int] | None = None
    engine: str | None = None
    created_at: str | None = None
    version: int = SCHEMA_VERSION
    _dir: Path | None = field(default=None, repr=False, compare=False)

    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    @property
    def params(self) -> list[str]:
        """Names used as {{param}} in typed values and upload paths."""
        out: list[str] = []
        for s in self.steps:
            vals = [s.value] if isinstance(s.value, str) else []
            vals += s.files or []
            for v in vals:
                for m in _PARAM.finditer(v):
                    if m.group(1) not in out:
                        out.append(m.group(1))
        return out

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_dir", None)
        d["steps"] = [{k: v for k, v in s.items() if v not in (None, [], False) or k in ("index", "type", "t")} for s in d["steps"]]
        return d

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        self._dir = p.parent
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Workflow":
        p = Path(path)
        if p.is_dir():
            p = p / "automation.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        if "workflow" in d and "steps" not in d.get("workflow", {}).get("steps", []):
            d = d["workflow"]  # an automation folder's automation.json wraps the workflow
        steps = []
        for s in d.get("steps", []):
            t = s.pop("target", None)
            steps.append(Step(**s, target=Target(**t) if t else None))
        wf = cls(
            name=d["name"],
            start_url=d["start_url"],
            steps=steps,
            viewport=d.get("viewport"),
            engine=d.get("engine"),
            created_at=d.get("created_at"),
            version=d.get("version", SCHEMA_VERSION),
        )
        wf._dir = p.parent
        return wf

    def summary(self) -> str:
        lines = [f"{self.name}  ({len(self.steps)} steps)  start: {self.start_url}"]
        for s in self.steps:
            tab = f" [tab {s.tab}]" if s.tab else ""
            lines.append(f"  {s.index:>3}. {s.describe()}{tab}")
        if self.params:
            lines.append(f"  params: {', '.join(self.params)}")
        return "\n".join(lines)


def fill_params(value: str, params: dict[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        k = m.group(1)
        if k not in params:
            raise KeyError(f"missing workflow param {{{{{k}}}}}")
        return str(params[k])

    return _PARAM.sub(sub, value)
