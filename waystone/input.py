"""Raw CDP input. Independent of Playwright's frame tracking, which stalls on Fortress after click navigations."""
from __future__ import annotations

import sys
import asyncio
import time

from playwright.sync_api import CDPSession

# key -> (code, windowsVirtualKeyCode, text)
KEYS: dict[str, tuple[str, int, str | None]] = {
    "Enter": ("Enter", 13, "\r"),
    "Tab": ("Tab", 9, "\t"),
    "Escape": ("Escape", 27, None),
    "Backspace": ("Backspace", 8, None),
    "Delete": ("Delete", 46, None),
    "ArrowUp": ("ArrowUp", 38, None),
    "ArrowDown": ("ArrowDown", 40, None),
    "ArrowLeft": ("ArrowLeft", 37, None),
    "ArrowRight": ("ArrowRight", 39, None),
    "Home": ("Home", 36, None),
    "End": ("End", 35, None),
    "PageUp": ("PageUp", 33, None),
    "PageDown": ("PageDown", 34, None),
    "Space": ("Space", 32, " "),
    "a": ("KeyA", 65, "a"),
}
MODS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}


class Input:
    def __init__(self, cdp: CDPSession, sleep=None):
        self.cdp = cdp
        self._sleep = sleep or (lambda ms: time.sleep(ms / 1000))

    def _release(self, method: str, params: dict, timeout_s: float = 1.5) -> None:
        """Send a keyUp/mouseReleased with a bounded wait. When the matching keyDown/mousePressed
        starts a cross-process navigation, the renderer that would acknowledge the release is gone.
        Stock Chrome still answers; Fortress does not, and an unbounded send never returns. Nothing
        downstream depends on that acknowledgement."""
        try:
            impl = self.cdp._impl_obj
            self.cdp._sync(asyncio.wait_for(impl.send(method, params), timeout_s))
        except asyncio.TimeoutError:
            pass
        except AttributeError:
            self.cdp.send(method, params)

    # -- mouse ------------------------------------------------------------

    def move(self, x: float, y: float) -> None:
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    def click(self, x: float, y: float, *, button: str = "left", count: int = 1, modifiers: list[str] | None = None) -> None:
        mods = sum(MODS.get(m, 0) for m in (modifiers or []))
        self.move(x, y)
        self._sleep(30)
        for i in range(1, count + 1):
            self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": button, "clickCount": i, "modifiers": mods})
            self._sleep(40)
            self._release("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": button, "clickCount": i, "modifiers": mods})
            if i < count:
                self._sleep(60)

    def hover(self, x: float, y: float, settle_ms: int = 350) -> None:
        # A couple of intermediate moves so hover-intent menus (which watch velocity) open.
        self.move(x - 12, y - 8)
        self._sleep(40)
        self.move(x, y)
        self._sleep(settle_ms)

    def drag(self, x0: float, y0: float, x1: float, y1: float, steps: int = 12) -> None:
        self.move(x0, y0)
        self._sleep(40)
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x0, "y": y0, "button": "left", "clickCount": 1})
        self._sleep(80)
        for i in range(1, steps + 1):
            f = i / steps
            self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0 + (x1 - x0) * f, "y": y0 + (y1 - y0) * f, "button": "left", "buttons": 1})
            self._sleep(16)
        self._sleep(60)
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x1, "y": y1, "button": "left", "clickCount": 1})

    def wheel(self, dx: float, dy: float, x: float = 400, y: float = 300) -> None:
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy})

    # -- keyboard ---------------------------------------------------------

    def type(self, text: str, delay_ms: int = 20) -> None:
        for ch in text:
            if ch == "\n":
                self.press("Enter")
                continue
            self.cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "unmodifiedText": ch, "key": ch})
            self._release("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})
            if delay_ms:
                self._sleep(delay_ms)

    def press(self, combo: str) -> None:
        """'Enter', 'Tab', 'Meta+a', 'Control+a', 'Shift+Tab'…"""
        parts = combo.split("+")
        key = parts[-1]
        mods = sum(MODS[m] for m in parts[:-1])
        code, vk, text = KEYS.get(key, (key, 0, key if len(key) == 1 else None))
        if len(key) == 1 and key not in KEYS:
            code, vk = f"Key{key.upper()}", ord(key.upper())
        down = {"type": "keyDown" if (text and not mods) else "rawKeyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": mods}
        if text and not mods:
            down["text"] = text
            down["unmodifiedText"] = text
        self.cdp.send("Input.dispatchKeyEvent", down)
        self._release("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": mods})

    def select_all(self) -> None:
        self.press("Meta+a" if sys.platform == "darwin" else "Control+a")
