# waystone-mcp

MCP server for [Waystone](https://github.com/tiliondev/waystone), the browser action recorder. A person does a task once in a browser; Waystone writes a folder with step-by-step instructions, ranked selectors, screenshots and a replayable workflow. This package launches `waystone mcp` so MCP clients need nothing but Node and Python 3.11+.

```json
{ "mcpServers": { "waystone": { "command": "npx", "args": ["-y", "waystone-mcp"] } } }
```

On first run it creates `~/.waystone/venv` and installs `waystone-browser[fortress,mcp]` into it. If `waystone` or `uvx` is already on PATH, it uses that instead.

Tools: `record_start`, `record_status`, `record_stop`, `run`, `show`, `edit`, `export`, `page_open`, `page_mark`, `page_act`, `page_close`.

Environment: `WAYSTONE_BIN` (use this `waystone` executable), `WAYSTONE_PYTHON` (interpreter for the venv), `WAYSTONE_HOME` (where the venv lives), `WAYSTONE_PIP_SPEC` (what to install).
