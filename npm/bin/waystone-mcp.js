#!/usr/bin/env node
// Launches `waystone mcp` over stdio. Stdout is the MCP transport, so every message here goes to stderr.
//
// Order: $WAYSTONE_BIN, a `waystone` on PATH, `uvx`, then a private venv at ~/.waystone/venv that is
// created and populated on first run (python3 >= 3.11 required).
"use strict";
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const PKG = process.env.WAYSTONE_PIP_SPEC || "waystone-browser[fortress,mcp]";
const HOME = process.env.WAYSTONE_HOME || path.join(os.homedir(), ".waystone");
const VENV = path.join(HOME, "venv");
const WIN = process.platform === "win32";
const venvBin = (name) => path.join(VENV, WIN ? "Scripts" : "bin", WIN ? `${name}.exe` : name);

const log = (m) => process.stderr.write(`[waystone-mcp] ${m}\n`);
const has = (cmd, args = ["--version"]) => spawnSync(cmd, args, { stdio: "ignore" }).status === 0;

function run(cmd, args) {
  const child = spawn(cmd, args, { stdio: "inherit" });
  child.on("exit", (code, sig) => process.exit(code ?? (sig ? 1 : 0)));
  for (const s of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(s, () => child.kill(s));
}

function findPython() {
  const named = ["3.16", "3.15", "3.14", "3.13", "3.12", "3.11"].map((v) => `python${v}`);
  for (const c of [process.env.WAYSTONE_PYTHON, ...named, "python3", "python", "py"].filter(Boolean)) {
    const r = spawnSync(c, ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"], { stdio: "ignore" });
    if (r.status === 0) return c;
  }
  return null;
}

function ensureVenv() {
  const ws = venvBin("waystone");
  if (fs.existsSync(ws)) return ws;
  const py = findPython();
  if (!py) {
    log("Python 3.11 or newer is required and was not found. Install it (https://www.python.org/downloads/) or set WAYSTONE_PYTHON.");
    process.exit(1);
  }
  log(`first run: creating ${VENV} and installing ${PKG} (this happens once)`);
  fs.mkdirSync(HOME, { recursive: true });
  let r = spawnSync(py, ["-m", "venv", VENV], { stdio: ["ignore", "inherit", "inherit"] });
  if (r.status !== 0) { log("could not create the virtual environment"); process.exit(1); }
  r = spawnSync(venvBin("python"), ["-m", "pip", "install", "--quiet", "--upgrade", "pip", PKG], { stdio: ["ignore", process.stderr, "inherit"] });
  if (r.status !== 0) { log("pip install failed"); process.exit(1); }
  return ws;
}

if (process.env.WAYSTONE_BIN) {
  run(process.env.WAYSTONE_BIN, ["mcp"]);
} else if (has("waystone")) {
  run("waystone", ["mcp"]);
} else if (has("uvx")) {
  run("uvx", ["--from", PKG, "waystone", "mcp"]);
} else {
  run(ensureVenv(), ["mcp"]);
}
