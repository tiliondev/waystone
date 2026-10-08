"""Screencast → mp4 (ffmpeg) or gif (Pillow), with the clicked element boxed in red at each step."""
from __future__ import annotations

import base64
import io
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .annotate import RED

FPS = 10
LEAD_MS = 600  # box appears this long before the click, while the target is still on screen
HOLD_MS = 250  # and stays briefly after


class Screencast:
    def __init__(self, cdp: Any, *, max_width: int = 1280, max_height: int = 1000, quality: int = 70):
        self.cdp = cdp
        self.frames: list[tuple[float, bytes]] = []  # (unix seconds, jpeg)
        self._opts = {"format": "jpeg", "quality": quality, "maxWidth": max_width, "maxHeight": max_height, "everyNthFrame": 1}
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self._running = False

    def start(self) -> None:
        self.cdp.send("Page.startScreencast", self._opts)
        self._running = True

    def stop(self) -> None:
        if self._running:
            try:
                self.cdp.send("Page.stopScreencast")
            except Exception:
                pass
            self._running = False

    def _on_frame(self, ev: dict[str, Any]) -> None:
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        except Exception:
            pass
        ts = ev.get("metadata", {}).get("timestamp")
        if ts is None:
            import time

            ts = time.time()
        self.frames.append((ts, base64.b64decode(ev["data"])))

    def latest(self) -> bytes | None:
        return self.frames[-1][1] if self.frames else None

    # -- assembly -------------------------------------------------------

    def write(self, path: str | Path, *, t0: float, steps: list[dict[str, Any]] | None = None, fps: int = FPS) -> Path | None:
        """steps: [{t_ms, rect, viewport}] relative to t0 (unix seconds). Writes .mp4 if ffmpeg is available, else .gif."""
        if len(self.frames) < 2:
            return None
        path = Path(path)
        steps = steps or []
        start, end = self.frames[0][0], self.frames[-1][0] + 0.3
        n = max(1, int((end - start) * fps))
        ffmpeg = shutil.which("ffmpeg")
        out = path.with_suffix(".mp4" if ffmpeg else ".gif")

        def frame_at(t: float) -> Image.Image:
            fi = 0
            for i, (ts, _) in enumerate(self.frames):
                if ts <= t:
                    fi = i
                else:
                    break
            img = Image.open(io.BytesIO(self.frames[fi][1])).convert("RGB")
            rel_ms = (t - t0) * 1000
            for s in steps:
                if s.get("rect") and -LEAD_MS <= rel_ms - s["t_ms"] <= HOLD_MS:
                    _box(img, s["rect"], s.get("viewport"))
            return img

        first = frame_at(start)
        scale = min(1.0, 1280 / first.width)  # video stays at 1x; the per-step PNGs keep full resolution
        w, h = (int(first.width * scale) // 2) * 2, (int(first.height * scale) // 2) * 2  # yuv420p needs even dims
        if ffmpeg:
            proc = subprocess.Popen(
                [ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", f"scale={w}:{h}", str(out)],
                stdin=subprocess.PIPE,
            )
            assert proc.stdin
            for i in range(n):
                frame_at(start + i / fps).save(proc.stdin, format="JPEG", quality=85)
            proc.stdin.close()
            proc.wait()
            return out if out.exists() else None
        imgs = [frame_at(start + i / fps).resize((w, h)) for i in range(n)]
        imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=int(1000 / fps), loop=0)
        return out


def _box(img: Image.Image, rect: dict, viewport: dict | None) -> None:
    s = img.width / float(viewport["w"]) if viewport and viewport.get("w") else 1.0
    d = ImageDraw.Draw(img)
    x0, y0 = rect["x"] * s, rect["y"] * s
    d.rectangle((x0, y0, x0 + rect["w"] * s, y0 + rect["h"] * s), outline=RED, width=max(2, round(3 * s)))
