<p align="center">
  <img src="https://raw.githubusercontent.com/tiliondev/waystone/main/docs/assets/banner-waystone.png" alt="Waystone" width="100%">
</p>

<h3 align="center">Browser action recorder for automation</h3>

<p align="center">
  <a href="https://github.com/tiliondev/fortress"><img src="https://img.shields.io/badge/engine-Fortress-24292f?logo=googlechrome&logoColor=white" alt="Fortress"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-24292f?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-24292f" alt="MIT">
</p>

Waystone records a person doing a task in a browser, such as filing a form or opening each item in a listing, and writes a folder that describes exactly what was done:

- numbered instructions per step, with ranked selectors for the element used
- a screenshot per step with that element boxed
- the workflow as JSON
- a Chrome DevTools Protocol command list that performs the same actions

The folder is used in three ways:

- `waystone run` replays the steps in a browser.
- An agent reads the instructions and performs the task itself, including for items that were not in the recording.
- `waystone export` writes the steps as a Playwright or Puppeteer script. The script runs with those libraries alone; Waystone does not need to be installed.

<p align="center">
  <img src="https://raw.githubusercontent.com/tiliondev/waystone/main/docs/assets/demo-boarddocs.gif" alt="Recording a BoardDocs task, then replaying it" width="100%">
</p>

## What it is used for

- **Web scraping.** Record opening a listing, paging through results, opening each item. Replay it on a schedule.
- **Data collection from sites without an API.** Government portals, school boards, court records, property listings, supplier catalogues.
- **Repetitive work.** The same form, the morning report download, an order status check, a CRM update. One recording, a parameter for the parts that change.
- **Teaching an agent a site.** Browser agents fail on dense or unusual layouts. The recording tells the agent which element to use at each step, how to find it, and when the page is ready.
- **Test and automation scripts.** Export Playwright (Python or JavaScript), Puppeteer, or a DevTools Recorder flow for Cypress, TestCafe, Nightwatch and WebdriverIO.
- **Documenting a process.** The generated README and screenshots are a step-by-step guide a person can follow.

## The problem it addresses

An agent working a site from scratch makes three decisions per step, each a model call:

- which of the clickable elements is the right one
- whether the page has finished loading
- what to do on the page it reached

That makes runs slow and different each time, and on dense layouts (BoardDocs, OpenTable, most enterprise software) the agent regularly picks the wrong element or acts too early. A recording of a person doing the task replaces all three decisions with facts: the element used, several ways to find it, how long the page took to settle, and a screenshot.

## How it compares

Chrome DevTools has a recorder built in, Playwright ships `codegen`, Selenium IDE is a browser extension, and LLM browser agents skip recording and decide every step at run time. ✓ handled, ◐ partly or depends on the harness, ✗ not handled.

| | Waystone | DevTools Recorder | Playwright codegen | Selenium IDE | LLM browser agents |
|---|:-:|:-:|:-:|:-:|:-:|
| Fields inside cross-origin iframes | ✓ | ✓ | ✓ | ✓ | ◐ |
| Closed shadow roots | ✓ | ✗ | ✗ | ✗ | ◐ |
| Canvas clicks identified by content, not coordinates | ✓ | ✗ | ✗ | ✗ | ◐ |
| Settle time measured per step | ✓ | ✗ | ✗ | ✗ | ✗ |
| Analytics and polling ignored when waiting | ✓ | ✗ | ✗ | ✗ | ✗ |
| New tabs and popups | ✓ | ✗ | ✓ | ✓ | ◐ |
| Right-click → open in new tab | ✓ | ✗ | ✗ | ✗ | ✗ |
| JavaScript dialogs | ✓ | ✗ | ✓ | ✓ | ◐ |
| Print dialog | ✓ | ✗ | ✗ | ✗ | ✗ |
| Hover menus recorded | ✓ | ✗ | ✗ | ✓ | ◐ |
| Drag | ✓ | ✗ | ✗ | ✓ | ◐ |
| File uploads | ✓ | ✗ | ✓ | ✓ | ◐ |
| Secrets stored as parameters, not values | ✓ | ✗ | ✗ | ✗ | ◐ |
| Several ranked selectors per element | ✓ | ✓ | ✗ | ✓ | ✗ |
| Repeated blocks detected, per-item selector given | ✓ | ✗ | ✗ | ✗ | ◐ |
| Screenshot per step with the element boxed | ✓ | ✗ | ✗ | ✗ | ◐ |
| Instructions an agent can follow | ✓ | ✗ | ✗ | ✗ | — |
| Deterministic replay | ✓ | ✓ | ✓ | ✓ | ✗ |
| Failure reports step, reason and screenshot | ✓ | ✗ | ◐ | ✗ | ✗ |
| Exports to other tools' formats | ✓ | ✓ | ✗ | ✓ | ✗ |

## Recording

```
pip install "waystone-browser[fortress]"
waystone record https://go.boarddocs.com/ca/dublinusd/Board.nsf/Public -n board-minutes -g "Open the minutes of the two most recent board meetings"
```

A browser opens with a panel in the top right showing the recording state and the steps so far. Do the task, press stop.

Captured:

- clicks, double-clicks, modifier-clicks, right-click → open in new tab
- typing (one step per field), Enter, Tab, Escape, arrow keys, key combinations
- select elements, checkboxes, radios, range and date inputs
- hover menus, drags, file uploads, window and element scrolling
- new tabs and popups, JavaScript dialogs, print
- iframes (same-origin and cross-origin), shadow DOM (open and closed), clicks on canvas

Passwords and upload paths are stored as `{{parameters}}`, never as values.

## Output

```
board-minutes/
  README.md          goal, then one numbered instruction per step with the element, its selectors and the settle time
  automation.json    the same steps as data, the full workflow, and the CDP command list
  resolver.js        the selector engine the command list depends on
  steps/005.png      one screenshot per step; the element is boxed in red and the instruction is written at the top
  video.mp4          screencast of the session
```

From the generated README for the recording above:

> **Steps 2 to 12 repeat 2x: the same 3-step move for each item** (“Aug 12, 2025 (Tue) Regular Meeting…”, “Jun 10, 2025 (Tue) Regular Meeting…”).
> The item that varies is step 2's target, a link. All such items on the page: `css=div.wrap-year.ui-accordion-content:nth-of-type(1) > a.icon.prevnext`. Enumerate those and repeat the block once per item.
>
> 5. Click the link “View Minutes”
>    find it with: `role=link name="View Minutes"` · `css=#btn-view-minutes-id` · `text="View Minutes"`
>    then wait ~1.5s for the page to settle (it loads 1 request)

Each instruction has the element, the selectors that matched it in order of stability, and the settle time. A block that repeats per item is called out with the selector for all such items.

## Running

```
waystone run board-minutes/
waystone run board-minutes/ -p password=… --until 7
```

- Selectors are tried in recorded order; the first that matches exactly one visible element is used.
- Before each action the runner waits for in-flight requests and loading indicators to clear. Analytics, beacons and polling do not count, nor do indicators that were already on screen.
- A step whose element cannot be found fails with the step number, the reason and a screenshot. Nothing is clicked.
- Each step prints which selector resolved it and what it waited for. `--until` stops after a step.

Canvas clicks have no element. During recording a hook on `CanvasRenderingContext2D` records what the page drew, so the click is stored as the primitive under it with its text and the row and column headers around it (“the item reading 34 in row 5, column C”), plus crops of the screenshot.

- default replay relocates the crops by template match
- `--canvas-hooks` reads the display list again and finds the same text anchors, which survives scrolling, zoom and theme changes
- a target whose anchors are not drawn is reported as not on screen; nothing is clicked

The command list in `automation.json` is plain CDP (`Page.navigate`, `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`, `Runtime.evaluate`) with a `resolve` entry before each action that returns the element's position via `resolver.js`. `examples/run_cdp_export.py` runs it over a websocket in about sixty lines with no dependency on Waystone.

## Editing

```
waystone edit board-minutes/ --remove 6 7            # drop steps
waystone edit board-minutes/ --wait 5 2000           # pause 2s after step 5
waystone edit board-minutes/ --move 9 3              # move step 9 before step 3
waystone edit board-minutes/ --set-value 4 "{{query}}"
```

Steps are renumbered, screenshots renamed, README.md and the CDP run regenerated.

## Engines

```
waystone record <url> -n flow                                 # launches Fortress
waystone record <url> -n flow --engine chrome                 # launches a stock Chromium
waystone record <url> -n flow --cdp http://127.0.0.1:9222     # attaches to a running browser
```

- Default is [Fortress](https://github.com/tiliondev/fortress), a Chromium build whose fingerprint reads as an ordinary Chrome install. Stock Chromium works too.
- Each launch gets a fresh temporary profile, so several Waystone processes can run at once. `--profile <dir>` (or `WAYSTONE_PROFILE`) keeps cookies and logins between runs, for sites that need the user signed in.
- Everything Waystone injects runs in a CDP isolated world; the panel is in a closed shadow root; marks are drawn on screenshots, not added to the DOM. Page scripts cannot see any of it.

## Other formats

```
waystone export board-minutes/automation.json -f playwright-python
waystone export … -f playwright-js | puppeteer | devtools | cdp | agent
waystone import recording.json
```

- `playwright-python`, `playwright-js`, `puppeteer`: runnable scripts using the top-ranked selector per step
- `devtools`: a Chrome DevTools Recorder flow, which DevTools imports and `@puppeteer/replay`, TestCafe, Cypress, Nightwatch and WebdriverIO run or convert
- `waystone import`: a DevTools Recorder flow in, a Waystone workflow out

## MCP server

```
pip install "waystone-browser[fortress,mcp]"
waystone mcp
```

Claude Code, Claude Desktop, Cursor and Codex configuration. With `npx`, nothing has to be installed first (Python 3.11+ must be present; a private environment is created on first run):

```json
{ "mcpServers": { "waystone": { "command": "npx", "args": ["-y", "waystone-mcp"] } } }
```

With `uvx`: `"command": "uvx", "args": ["--from", "waystone-browser[fortress,mcp]", "waystone", "mcp"]`. With it installed: `"command": "waystone", "args": ["mcp"]`.

Tools:

- `record_start`, `record_status`, `record_stop`: open a browser for the user, watch steps arrive, get the folder and its README when they press stop
- `run`: replay a folder; on a miss returns the step, the reason and a screenshot path
- `show`, `edit`, `export`: read, change and convert a folder
- `page_open`, `page_mark`, `page_act`, `page_close`: drive a page with no recording by element index (returns the numbered screenshot)

## Agent mode

For a page with no recording, `page.mark()` returns every actionable element with an index and a screenshot with the indices drawn on it. The agent acts by index.

```python
from waystone import Waystone

with Waystone() as ws:
    page = ws.page("https://news.ycombinator.com")
    marks = page.mark()
    print(marks.to_prompt())       # [1] link "new"  [5] textbox "search" …
    page.click(marks.find("new"))
    page.type(5, "fortress")
    page.press("Enter")
```

## Roadmap

Done

- [x] Cross-origin iframes (one CDP session per frame)
- [x] Closed shadow roots (`DOM.getDocument` with `pierce: true`)
- [x] Step editing in the generated folder (`waystone edit`: remove, reorder, add a wait, change a value)
- [x] Wait-for-element steps recorded after navigations
- [x] Canvas clicks by template match (context crop grown until unique; ambiguity reported rather than guessed)
- [x] 2D canvas display list: a main-world hook during recording records what the app drew; a click is identified as “the item reading 34 in row 5, column C”; `--canvas-hooks` relocates by the same reading
- [x] Identity through draw arguments: image sources and sprite rects recorded with the click, so content placed differently on each load is found by what it is

Next

- [ ] Pointer paths: full mousedown, moves and mouseup with timing for draw, paint, select-box, slider and drag
- [ ] Pixel cascade for WebGL and WebGPU: multi-scale template match, OCR anchors, ORB keypoints with homography, scroll-to-find
- [ ] "Any one of these": a click on one of several identical items, replayed by picking one
- [ ] Optional element-detection model for screens with no DOM and no text
- [ ] Rule inference across repeated blocks (nearest, first, largest)

## Development

```
python -m venv .venv && .venv/bin/pip install -e ".[dev,fortress]"
.venv/bin/pytest -q
```

`tests/test_record_replay.py` records a session covering every action type above and replays it on a fresh page.

## License

MIT. Fortress is a separate dependency under its own [source-available license](https://github.com/tiliondev/fortress/blob/main/LICENSE).
