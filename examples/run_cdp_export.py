"""Reference client for `waystone export -f cdp`: a bare websocket, no Playwright/Puppeteer/Waystone.

    python examples/run_cdp_export.py out/name.cdp.json http://127.0.0.1:9222 [key=value ...]

Implements the four entry kinds: method / resolve / at:point / wait. ~60 lines of logic.
"""
import asyncio, json, sys, time, urllib.request
import websockets


async def main(path: str, cdp: str, params: dict) -> None:
    doc = json.load(open(path))
    resolver = doc["resolver_js"]
    target = json.load(urllib.request.urlopen(urllib.request.Request(f"{cdp}/json/new?about:blank", method="PUT")))
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
        seq = 0

        async def send(method, p=None):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": method, "params": p or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == seq:
                    return msg.get("result", msg)

        async def evaluate(expr):
            r = await send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
            return r.get("result", {}).get("value")

        async def ensure_resolver():
            if await evaluate("typeof __ws") != "object":
                await evaluate(resolver)

        async def wait_idle(ms):
            deadline = time.time() + min(ms * 2 + 2000, 15000) / 1000
            while time.time() < deadline:
                await ensure_resolver()
                if await evaluate("document.readyState === 'complete' && __ws.busy().length === 0 && __ws.quiet() > 300"):
                    return
                await asyncio.sleep(0.15)

        point = {"x": 0, "y": 0}
        for c in doc["commands"]:
            if "resolve" in c:
                r = c["resolve"]
                deadline = time.time() + 10
                res = None
                while time.time() < deadline:
                    await ensure_resolver()
                    res = await evaluate(f"__ws.resolve({json.dumps(r['candidates'])}, {json.dumps(r['fingerprint'])}, {json.dumps(r['opts'])})")
                    if res and res.get("found") and res.get("point"):
                        point = res["point"]
                        break
                    await asyncio.sleep(0.25)
                else:
                    point = r["fallback"]
                    print("  ~ fallback coordinates for", c["meta"]["action"][:60])
                print("  ✓", c["meta"]["action"][:70], f"(via {res.get('kind')})" if res and res.get("found") else "")
            elif "method" in c:
                p = dict(c.get("params") or {})
                if c.get("at") == "point":
                    p["x"], p["y"] = point["x"], point["y"]
                if c["method"] == "Input.insertText":
                    for k, v in params.items():
                        p["text"] = p["text"].replace("{{" + k + "}}", v)
                if c.get("needs_resolver"):
                    await ensure_resolver()
                if c["method"] == "DOM.setFileInputFiles":
                    print("  - skipped upload (needs an objectId; see meta.note)")
                    continue
                r = await send(c["method"], p)
                if "error" in r:
                    print("  !", c["method"], r["error"].get("message"))
                elif c.get("meta") and "resolve" not in c:
                    print("  ✓", c["meta"]["action"][:70])
            elif "wait" in c:
                w = c["wait"]
                if w.get("for") == "load":
                    for _ in range(100):
                        if await evaluate("document.readyState") == "complete":
                            break
                        await asyncio.sleep(0.1)
                    await wait_idle(1000)
                elif w.get("for") == "idle":
                    await wait_idle(w.get("ms", 300))
                elif w.get("for") in ("navigation", "target"):
                    await asyncio.sleep(1.5)
                else:
                    await asyncio.sleep(w.get("ms", 300) / 1000)
            await asyncio.sleep(0.05)
        print("done:", await evaluate("location.href"))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:9222", dict(a.split("=", 1) for a in sys.argv[3:] if "=" in a)))
