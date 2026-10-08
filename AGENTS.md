# Waystone for agents

This file is for a coding agent (Claude Code, Codex, Cursor and the like) that has been asked to set Waystone up for a user, help them record a browser automation, or run one.

Waystone records a person doing a browser task once and writes a folder with step-by-step instructions, ranked selectors, screenshots, the workflow as JSON and a CDP command list. `waystone run` replays the folder. An agent can follow the folder's README directly.

## As an MCP server

If your client speaks MCP, this is the simplest route: install with the `mcp` extra and register `waystone mcp`.

```
.venv/bin/pip install "waystone-browser[fortress,mcp]"
```

```json
{ "mcpServers": { "waystone": { "command": "npx", "args": ["-y", "waystone-mcp"] } } }
```

`npx waystone-mcp` creates `~/.waystone/venv` on first run and installs Waystone into it (Python 3.11+ required). With a venv of your own: `"command": "/path/to/.venv/bin/waystone", "args": ["mcp"]`.

Tools: `record_start` / `record_status` / `record_stop` (the recording flow below, as calls), `run`, `show`, `edit`, `export`, and `page_open` / `page_mark` / `page_act` / `page_close` for a page with no recording. `record_start` returns a line to say to the user; `record_stop` returns the folder's README.

## Setup

Requirements: Python 3.11+, a machine with a display (recording opens a visible browser window).

```
python -m venv .venv
.venv/bin/pip install "waystone-browser[fortress]"
.venv/bin/waystone --version
```

- Fortress, the default browser engine, is a prebuilt Chromium downloaded on first launch into `~/.tilion`. The first `waystone record` or `waystone run` pays that download once.
- No Fortress, or no network for the download: add `--engine chrome` to use a stock Chromium that Playwright can find, or `--cdp http://127.0.0.1:9222` to attach to a browser the user already runs with `--remote-debugging-port=9222`.
- Put the venv's `bin` on `PATH` or call `.venv/bin/waystone` explicitly. The examples below assume `waystone` resolves.

## Recording for a user

Start the recording and hand the browser to the user:

```
waystone record https://example.com/start -n <name> -g "<one line: what this automation achieves>" -o automations/
```

Tell the user, in these words or close to them:

- A browser window opened with a small panel in the top right. That panel shows the steps as they are recorded.
- Do the task the way you normally would. Click, type, press Enter, open menus, switch tabs. Nothing special is needed.
- Wait for pages to finish loading before the next click. The recording measures that wait and uses it on replay.
- Typing into a password field is stored as a placeholder, not the value.
- When the task is done, press **Stop and save** in the panel.

Do not drive the browser yourself while the user is recording. The recording is of the person.

Picking a start URL: the page where the task begins, after login if the site needs one. If the user has to log in as part of the task, that is fine; the password becomes a `{{password}}` parameter.

## After the recording

The folder is at `<out>/<name>/`:

```
README.md         numbered instructions, one per step, with selectors and settle time
automation.json   workflow, steps and the CDP command list
resolver.js       selector engine the CDP list depends on
steps/NNN.png     one screenshot per step, element boxed in red, instruction at the top
video.mp4         screencast
```

Check it:

1. Read `README.md`. Each step should name the element the user used. Open a few `steps/*.png` to confirm the red box is on the right thing.
2. Replay it blind: `waystone run <folder>`. It prints which selector resolved each step and what it waited for, and exits non-zero on a miss with `waystone-failure.png` and the step number.
3. Fix with `waystone edit`, not by hand:

```
waystone edit <folder> --remove 6 7              # drop stray steps
waystone edit <folder> --wait 5 2000             # 2s pause after step 5
waystone edit <folder> --move 9 3                # reorder
waystone edit <folder> --set-value 4 "{{query}}" # make a typed value a parameter
waystone edit <folder> --goal "..."              # replace the goal line
```

`waystone show <folder>` prints the steps. Editing `automation.json` directly desynchronises the README, the screenshots and the CDP list; the `edit` command keeps them together.

## Running

```
waystone run <folder>
waystone run <folder> -p password=... -p query=...     # fill {{parameters}}
waystone run <folder> --headless                       # no window
waystone run <folder> --until 7                        # stop after step 7
waystone run <folder> --shots out/shots/               # screenshot per step
waystone run <folder> --timeout 20                     # seconds per element (default 10)
waystone run <folder> --canvas-hooks                   # canvas targets by what the page draws
```

- Replay is deterministic. Selectors are tried in recorded order; the first that matches exactly one visible element wins. There is no model in the loop and no guessing: a step that cannot be resolved fails loudly.
- Each run launches a fresh temporary browser profile. For a site that needs the user's logged-in session, pass the same `--profile <dir>` to `record` and `run` (the user signs in during the recording; the cookies stay in that directory), or attach to the user's own browser with `--cdp`.
- Scheduling is the user's cron or task scheduler calling `waystone run`. Exit code 0 means every step resolved and ran.

## Following a folder as an agent

If you are the one performing the task rather than `waystone run`:

- Read `README.md` top to bottom. Each step gives the action, the element, the selectors in order of stability, and how long the page took to settle. Use the first selector that matches one element; fall through in order.
- A block marked as repeating per item gives the selector for all such items. Enumerate those and repeat the block once per item, including items that were not in the recording.
- Compare against `steps/NNN.png` when a page looks different from what the step describes.
- When no selector matches, inspect the live page with `page.mark()` (below) rather than guessing from the screenshot, then tell the user the step needs re-recording.

## Python API

```python
from waystone import Waystone, Workflow

with Waystone() as ws:                       # Waystone("http://127.0.0.1:9222") to attach
    res = ws.replay(Workflow.load("automations/<name>"), params={"password": "..."})
    print(res.ok, res.error, res.final_url)

    page = ws.page("https://example.com")    # a page with no recording
    marks = page.mark()                      # every actionable element, numbered
    print(marks.to_prompt())                 # [1] link "new"  [5] textbox "search" ...
    page.click(marks.find("Sign in"))
    page.type(5, "query")
    page.press("Enter")
```

## Exporting

```
waystone export <folder>/automation.json -f playwright-python   # also playwright-js, puppeteer
waystone export <folder>/automation.json -f devtools            # Chrome DevTools Recorder flow
waystone export <folder>/automation.json -f agent               # plain-text brief
waystone import recording.json                                  # DevTools Recorder flow in
```

Exported scripts use the top-ranked selector per step and do not need Waystone installed.

## When something goes wrong

- `Fortress CDP endpoint did not come up`: an earlier browser is still running or the port is taken. `pkill -f fortress` (or close the orphaned Chromium windows), then retry. Launched browsers are closed with the process on normal exit and on SIGTERM.
- The browser opens but the panel does not appear: the page blocked the start URL, or it is a `chrome://` page. Record from an `http(s)` URL.
- A step fails on replay with "no element matched": open `waystone-failure.png` and the step's `steps/NNN.png`. The site changed, a dialog or cookie banner is in the way, or the page needed longer. Add a wait with `waystone edit --wait`, or re-record that part.
- A step waits the full timeout on every run: the site polls or streams. Known polling and analytics are ignored; report the URL pattern if one gets through.
- The site shows a human-verification challenge during recording: the user completes it as part of the recording. If it appears on every replay, that site cannot be replayed unattended.
- Recording a `target=_blank` link: give the new tab a second to open before pressing Stop. It is recorded as a `new-tab` step and the replay switches to it.
