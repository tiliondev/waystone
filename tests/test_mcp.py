"""The MCP tools, called directly (no transport). Needs a headed Fortress on CDP like the other tests;
the server launches its own browser, so this only checks a browser can be launched at all."""
from __future__ import annotations

import asyncio
import os

import pytest

from waystone.engine import probe

CDP = os.environ.get("WAYSTONE_CDP_URL", "http://127.0.0.1:9222")
pytest.importorskip("mcp")


def test_mcp_tools_list_and_agent_mode(tmp_path):
    if probe(CDP) is None:
        pytest.skip(f"no CDP endpoint at {CDP}")
    import waystone.mcp as m

    async def go():
        names = {t.name for t in await m.mcp.list_tools()}
        assert {"record_start", "record_status", "record_stop", "run", "show", "edit", "export", "page_open", "page_mark", "page_act", "page_close"} <= names
        r = await m.page_open("https://example.com")
        pid = r["page_id"]
        text, image = await m.page_mark(pid)
        assert "actionable elements" in text and type(image).__name__ == "Image"
        r = await m.page_act(pid, "click", ref=0)
        assert "iana.org" in r["url"]
        assert (await m.page_close(pid))["closed"]

    asyncio.run(go())
