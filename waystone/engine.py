"""Connect to a Fortress engine over CDP, launching one if nothing is listening."""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright

DEFAULT_CDP_URL = os.environ.get("WAYSTONE_CDP_URL", "http://127.0.0.1:9222")


def _http_base(cdp_url: str) -> str:
    """ws://host:port/devtools/browser/… -> http://host:port"""
    from urllib.parse import urlparse

    u = urlparse(cdp_url)
    scheme = "https" if u.scheme in ("https", "wss") else "http"
    return f"{scheme}://{u.netloc}"


def probe(cdp_url: str, timeout: float = 1.0) -> dict[str, Any] | None:
    """Return /json/version if a CDP endpoint answers at cdp_url (http or ws form), else None."""
    try:
        with urllib.request.urlopen(f"{_http_base(cdp_url)}/json/version", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


@dataclass
class Engine:
    """A live CDP connection. Use `Engine.connect()` or the `Waystone` facade."""

    cdp_url: str
    browser: Browser
    version: str
    _pw: Playwright = field(repr=False)
    _owned: Any = field(default=None, repr=False)

    @classmethod
    def connect(
        cls,
        cdp_url: str | None = None,
        *,
        engine: str = "fortress",
        headless: bool = False,
        launch: bool = True,
        port: int = 9222,
        profile: str | Path | None = None,
        fortress_kwargs: dict[str, Any] | None = None,
    ) -> "Engine":
        """Get a browser.

        - `cdp_url` given: attach to it, whatever it is.
        - otherwise `engine="fortress"` (default): launch Fortress via tilion-fortress on a free port.
        - `engine="chrome"`: launch a stock Chromium (Playwright's, or $WAYSTONE_CHROME) with a CDP port.
        - `launch=False`: only attach to $WAYSTONE_CDP_URL / 127.0.0.1:9222, never launch.
        - `profile`: browser profile directory for a launched browser. Default is a fresh temporary
          one per launch (deleted on close), so several Waystone processes never collide. Give a
          path to keep cookies and logins between runs.
        """
        owned = None
        if cdp_url:
            url = cdp_url
            info = probe(url)
            if info is None:
                raise ConnectionError(f"No CDP endpoint at {url}.")
        elif not launch:
            url = DEFAULT_CDP_URL
            info = probe(url)
            if info is None:
                raise ConnectionError(f"No CDP endpoint at {url}. Start a browser with --remote-debugging-port, or drop launch=False.")
        else:
            if engine == "chrome":
                owned, url = _launch_chrome(port=port, headless=headless, profile=profile)
            elif engine == "fortress":
                owned, url = _launch_fortress(port=port, headless=headless, profile=profile, **(fortress_kwargs or {}))
            else:
                raise ValueError(f"unknown engine {engine!r}: use 'fortress' or 'chrome'")
            info = probe(url, timeout=5) or {}
        _ensure_window(url)
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(url)
        except Exception:
            pw.stop()
            if owned is not None:
                owned.close()
            raise
        eng = cls(cdp_url=url, browser=browser, version=info.get("Browser", "?"), _pw=pw, _owned=owned)
        if owned is not None:
            _reap_at_exit(owned)  # a launched browser must not outlive this process, crash or not
        return eng

    @property
    def context(self) -> BrowserContext:
        ctxs = self.browser.contexts
        return ctxs[0] if ctxs else self.browser.new_context()

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self._pw.stop()
            if self._owned is not None:
                self._owned.close()
                if self._owned in _OWNED:
                    _OWNED.remove(self._owned)

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _ensure_window(cdp_url: str) -> None:
    """Playwright cannot attach to a browser with zero windows (e.g. after the user closed the last tab)."""
    base = _http_base(cdp_url)
    try:
        with urllib.request.urlopen(f"{base}/json/list", timeout=2) as r:
            targets = json.load(r)
        if any(t.get("type") == "page" for t in targets):
            return
        req = urllib.request.Request(f"{base}/json/new?about:blank", method="PUT")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


_OWNED: list[Any] = []


def _reap_all() -> None:
    for o in _OWNED:
        try:
            o.close()
        except Exception:
            pass
    _OWNED.clear()


def _reap_at_exit(owned: Any) -> None:
    import atexit
    import signal

    if not _OWNED:
        atexit.register(_reap_all)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                prev = signal.getsignal(sig)

                def handler(signum, frame, prev=prev):
                    _reap_all()
                    if callable(prev):
                        prev(signum, frame)
                    else:
                        raise SystemExit(128 + signum)

                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not the main thread, or unsupported platform
    _OWNED.append(owned)


def _free_port(preferred: int) -> int:
    import socket

    for p in (preferred, 0):
        with socket.socket() as sk:
            try:
                sk.bind(("127.0.0.1", p))
                return sk.getsockname()[1]
            except OSError:
                continue
    return preferred


class _Proc:
    """A browser we started: close() ends it."""

    def __init__(self, proc: Any, cdp_url: str, temp: str | None = None):
        self.proc, self.cdp_url, self.temp = proc, cdp_url, temp

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        _rm_temp(self.temp)


def _find_chrome() -> str | None:
    import glob
    import shutil
    import sys

    env = os.environ.get("WAYSTONE_CHROME")
    if env and os.path.exists(env):
        return env
    pw = glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")) + \
         glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    if pw:
        return sorted(pw)[-1]
    for c in (["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium"] if sys.platform == "darwin" else []):
        if os.path.exists(c):
            return c
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _launch_chrome(*, port: int, headless: bool, profile: str | Path | None = None) -> tuple[Any, str]:
    """Stock Chromium with a CDP port: Playwright's download, $WAYSTONE_CHROME, or the system Chrome."""
    import subprocess
    import tempfile
    import time

    exe = _find_chrome()
    if not exe:
        raise ConnectionError("No Chromium found. Install one (`playwright install chromium`) or set WAYSTONE_CHROME=/path/to/chrome.")
    port = _free_port(port)
    profile_dir, temp = _profile_dir(profile, "waystone-chrome-")
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check", "--window-size=1280,900"]
    if headless:
        args.append("--headless=new")
    args.append("about:blank")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if probe(url):
            return _Proc(proc, url, temp), url
        time.sleep(0.1)
    proc.kill()
    raise ConnectionError(f"Chromium at {exe} did not open a CDP port")


def _profile_dir(profile: str | Path | None, prefix: str) -> tuple[str, str | None]:
    """(user-data-dir, temp dir to delete on close or None)."""
    import tempfile

    if profile:
        p = Path(profile).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return str(p), None
    d = tempfile.mkdtemp(prefix=prefix)
    return d, d


def _launch_fortress(*, port: int, headless: bool, profile: str | Path | None = None, **kwargs: Any) -> tuple[Any, str]:
    try:
        from tillion_fortress import Fortress  # the wheel ships the module with two l's
    except ImportError:
        try:
            from tilion_fortress import Fortress  # type: ignore[no-redef]
        except ImportError as e:
            raise ConnectionError(
                "No Fortress engine is running and `tilion-fortress` is not installed. "
                "Either start one (`tilion --remote-debugging-port=9222`) or `pip install waystone-browser[fortress]`."
            ) from e
    port = _free_port(port)
    # The launcher always passes its one shared profile; Chromium takes the last --user-data-dir on
    # the command line, so ours wins. A shared profile means a second launch fails on the lock.
    profile_dir, temp = _profile_dir(profile, "waystone-fortress-")
    kwargs["extra_args"] = list(kwargs.get("extra_args") or []) + [f"--user-data-dir={profile_dir}"]
    f = Fortress(port=port, headless=headless, **kwargs)
    try:
        f.start()
    except Exception as e:
        raise ConnectionError(f"Fortress did not start ({e}). Profile: {profile_dir}. If another browser is using that profile, close it or pass a different one.") from e
    return _Fortress(f, temp), f.cdp_url or f"http://127.0.0.1:{port}"


class _Fortress:
    """A launched Fortress plus the temp profile to remove when it goes."""

    def __init__(self, f: Any, temp: str | None):
        self.f, self.temp = f, temp

    @property
    def proc(self) -> Any:
        return getattr(self.f, "_proc", None)

    def close(self) -> None:
        try:
            self.f.close()
        finally:
            _rm_temp(self.temp)


def _rm_temp(d: str | None) -> None:
    if d:
        import shutil

        shutil.rmtree(d, ignore_errors=True)
