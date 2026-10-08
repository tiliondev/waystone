// Waystone recording HUD. Lives in the isolated world inside a closed shadow root, so page scripts
// cannot reach it and our own scanners never see it. Python pushes the canonical step list in
// via __ws_panel.set(); the buttons report back through the recorder binding as control events.
//
// Self-contained: Cosmos Oracle (subset, embedded), SF Symbols (as SVG outlines) and the
// @aicss/react orb (bundled with React) travel with the script, so it renders identically in any
// Chromium, including headless Linux.
//
// Motion follows Emil Kowalski's rules: a strong custom ease-out, nothing over ~250ms, CSS
// transitions (interruptible) rather than keyframes for anything the user can retrigger, entries
// from scale(.96)/opacity rather than nothing, exits faster than entries, blur to mask crossfades,
// scale(.97) on press, and no movement at all under prefers-reduced-motion.
(() => {
  if (globalThis.__ws_panel || window !== window.top) return;

  /* ASSETS_PLACEHOLDER */

  const C_BRAND = { ink: '#092328', deep: '#12544F', green: '#2A835F', mint: '#8BBB92' };
  const C = { ink: '#ffffff', deep: '#ececec', green: '#8a8a88', mint: '#000000' };  // light: ink=bg, mint=fg
  // Status pill: green while recording, blue while saving, red on error, grey when paused.
  const STATUS = {
    recording: { fg: 'oklch(.62 .14 158)', bg: 'oklch(.96 .03 158)', text: 'Recording' },
    saving:    { fg: 'oklch(.56 .20 275)', bg: 'oklch(.94 .04 275)', text: 'Saving' },
    saved:     { fg: 'oklch(.62 .14 158)', bg: 'oklch(.96 .03 158)', text: 'Saved' },
    error:     { fg: 'oklch(.58 .19 22)',  bg: 'oklch(.94 .04 22)',  text: 'Error' },
    paused:    { fg: '#6b6b6b',            bg: '#efefef',            text: 'Paused' },
  };
  // Step colours are the Recording pill's oklch(.62 .14 158) with only the hue rotated, so every
  // kind reads as the same weight and the list stays one family with the status pill.
  const hue = (h) => `oklch(.62 .14 ${h})`;
  const KIND_COLOR = {
    click: hue(230), dblclick: hue(230), hover: hue(35),
    type: hue(195), press: hue(330),
    navigate: hue(275), 'new-tab': hue(300), 'switch-tab': hue(255),
    select: hue(350), check: hue(15), set: hue(85),
    scroll: hue(60), drag: hue(110), upload: hue(170), print: hue(0), dialog: hue(22), wait: hue(210),
  };

  const EASE_OUT = 'cubic-bezier(0.23, 1, 0.32, 1)';
  const EASE_IN_OUT = 'cubic-bezier(0.77, 0, 0.175, 1)';
  const EASE_ANTICIPATE = 'cubic-bezier(1, -0.4, 0.35, 0.95)';  // easing.dev/anticipate: pulls back, then goes
  const reduced = () => { try { return matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } };

  const state = { steps: [], t0: Date.now(), paused: false, open: true, stopped: false, saved: false, error: null };
  let host = null, root = null, els = {};
  let rendered = { status: null, open: null, count: 0 };

  const h = (tag, cls, text) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const icon = (name, size = 12, color = C.mint) => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 100 100');
    svg.setAttribute('width', size); svg.setAttribute('height', size);
    svg.style.cssText = 'display:block;flex:none;';
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', ICONS[name]); p.setAttribute('fill', color);
    svg.appendChild(p);
    return svg;
  };

  // Register the embedded font on the document's FontFaceSet (no <style> in the page's head).
  const loadFonts = () => {
    try {
      if (!document.fonts || globalThis.__ws_fonts) return;
      globalThis.__ws_fonts = true;
      for (const [weight, b64] of [[400, FONTS.regular], [500, FONTS.medium]]) {
        const ff = new FontFace('Waystone Cosmos', `url(data:font/woff2;base64,${b64})`, { weight: String(weight) });
        ff.load().then(() => document.fonts.add(ff)).catch(() => {});
      }
    } catch (e) { /* CSP may forbid data: fonts; the fallback stack takes over */ }
  };
  const FONT = '"Waystone Cosmos", "Cosmos Oracle", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif';

  const CSS = `
    :host { all: initial; }
    .box { background: ${C.ink}; border: 1px solid #e3e3e3; /* dev outline */ border-radius: 12px; width: 272px; overflow: hidden; user-select: none;
           font: 12px/1.35 ${FONT}; color: ${C.mint}; -webkit-font-smoothing: antialiased; }
    .bar { display: flex; align-items: center; gap: 7px; height: 40px; padding: 0 6px; cursor: grab; touch-action: none; }
    .bar[data-dragging] { cursor: grabbing; }
    .pill { display: inline-flex; align-items: center; height: 22px; padding: 0 9px 0 5px; border-radius: 11px; overflow: hidden; white-space: nowrap;
            transition: background 220ms ease, color 220ms ease, padding 220ms ${EASE_OUT}; }
    .orb { display: flex; align-items: center; justify-content: center; width: 12px; height: 22px; flex: none; margin-right: 5px; overflow: visible;
           transition: width 220ms ${EASE_OUT}, margin-right 220ms ${EASE_OUT}, opacity 160ms ease, filter 160ms ease; }
    .pill[data-final] .orb { width: 0; margin-right: 0; opacity: 0; filter: blur(2px); }
    .pill[data-final] { padding-left: 9px; }
    .label { font-weight: 500; line-height: 22px; display: inline-block; transition: opacity 120ms ease, filter 120ms ease; }
    .label[data-swap] { opacity: 0; filter: blur(2px); }
    .time { font-variant-numeric: tabular-nums; color: ${C.green}; line-height: 24px; margin-left: 2px; }
    .spacer { flex: 1; }
    .btn { all: unset; cursor: pointer; width: 26px; height: 24px; display: flex; align-items: center; justify-content: center; border-radius: 7px; box-sizing: border-box;
           transition: background 120ms ease, transform 160ms ${EASE_OUT}, opacity 160ms ease, width 200ms ${EASE_OUT}; }
    .btn:hover { background: ${C.deep}; }
    .btn:active { transform: scale(0.97); }
    .btn[data-gone] { width: 0; opacity: 0; pointer-events: none; }
    .btn .glyph { display: block; transition: opacity 120ms ease, filter 120ms ease, transform 160ms ${EASE_OUT}; }
    .btn .glyph[data-swap] { opacity: 0; filter: blur(2px); transform: scale(0.9); }
    .chev .glyph { transition: transform 320ms ${EASE_ANTICIPATE}; }
    .chev[data-closed] .glyph { transform: rotate(180deg); }
    .fold { display: grid; grid-template-rows: 1fr; transition: grid-template-rows 220ms ${EASE_OUT}; }
    .fold[data-closed] { grid-template-rows: 0fr; }
    .fold > div { min-height: 0; overflow: hidden; }
    .list { max-height: 260px; overflow-y: auto; overflow-x: hidden; padding: 2px 0 8px; scrollbar-width: none; }
    .list::-webkit-scrollbar { display: none; width: 0; height: 0; }
    .row { display: grid; grid-template-columns: 66px minmax(0, 1fr); gap: 7px; padding: 3px 8px 3px 12px; align-items: center;
           opacity: 1; transform: translateY(0); transition: opacity 180ms ${EASE_OUT}, transform 180ms ${EASE_OUT}; }
    @starting-style { .row { opacity: 0; transform: translateY(4px); } }
    .kind { font-weight: 500; font-size: 11px; white-space: nowrap; }
    .text { color: ${C.green}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
    .hint { padding: 2px 12px 10px; color: ${C.green}; font-size: 11px; }
    [data-frozen] * { animation-play-state: paused !important; }
    @media (prefers-reduced-motion: reduce) {
      .pill, .orb, .label, .btn, .btn .glyph, .chev .glyph, .fold, .row { transition-property: opacity, background, color; transition-duration: 120ms; }
      .chev[data-closed] .glyph { transform: none; }
      @starting-style { .row { transform: none; } }
    }
  `;

  const button = (name, title, cls) => {
    const b = h('button', 'btn' + (cls ? ' ' + cls : ''));
    b.title = title;
    const g = icon(name, 11); g.classList.add('glyph'); b.appendChild(g);
    b._name = name;
    return b;
  };
  // Swap a button's glyph with a short blur crossfade (exit fast, enter on the next frame).
  const swapIcon = (b, name, color) => {
    const g = b.querySelector('.glyph');
    const same = b._name === name;
    if (same) { g.querySelector('path').setAttribute('fill', color); return; }
    b._name = name;
    if (reduced()) { b.replaceChildren(Object.assign(icon(name, 11, color), { className: 'glyph' })); return; }
    g.setAttribute('data-swap', '');
    setTimeout(() => {
      const n = icon(name, 11, color); n.classList.add('glyph'); n.setAttribute('data-swap', '');
      b.replaceChildren(n);
      requestAnimationFrame(() => requestAnimationFrame(() => n.removeAttribute('data-swap')));
    }, 110);
  };
  // The label morphs (torph): letters shared between "Saving" and "Saved" slide into place, the
  // rest crossfade. Falls back to a blur swap if the bundle is missing or motion is reduced.
  const swapLabel = (text) => {
    if (els.label.textContent === text && !els.morph) return;
    if (els.morph && !reduced()) { els.morph.update(text); return; }
    if (reduced()) { els.label.textContent = text; return; }
    els.label.setAttribute('data-swap', '');
    setTimeout(() => { els.label.textContent = text; requestAnimationFrame(() => els.label.removeAttribute('data-swap')); }, 110);
  };

  const mount = () => {
    if (host && host.isConnected) return;
    loadFonts();
    host = document.createElement('waystone-panel');
    host.style.cssText = 'position:fixed;top:14px;right:14px;z-index:2147483647;pointer-events:auto;';
    root = host.attachShadow({ mode: 'closed' });
    const style = document.createElement('style');
    style.textContent = CSS + '\n' + (typeof ORB_CSS === 'string' ? ORB_CSS : '');
    root.append(style);

    const box = h('div', 'box');
    const bar = h('div', 'bar');

    els.pill = h('span', 'pill');
    els.orbHost = h('span', 'orb');
    els.orbHost.setAttribute('data-theme', 'light');
    els.orbHost.style.setProperty('--orb-fg', 'currentColor');
    els.orb = null;
    try { if (typeof WsOrb !== 'undefined') els.orb = WsOrb.mount(els.orbHost, { variant: 'M2', size: 24 }); } catch (e) { els.orb = null; }
    els.label = h('span', 'label', 'Recording');
    els.pill.append(els.orbHost, els.label);
    try { els.morph = typeof WsOrb !== 'undefined' && WsOrb.morph ? WsOrb.morph(els.label) : null; } catch (e) { els.morph = null; }
    els.time = h('span', 'time', '00:00');
    els.pause = button('pause', 'Pause');
    els.stop = button('stop', 'Stop and save');
    els.toggle = button('up', 'Hide steps', 'chev');
    bar.append(els.pill, els.time, h('span', 'spacer'), els.pause, els.stop, els.toggle);

    els.fold = h('div', 'fold');
    const inner = h('div');
    els.list = h('div', 'list');
    els.hint = h('div', 'hint', 'Everything you do here is captured. Press ■ when you are done.');
    inner.append(els.list, els.hint);
    els.fold.append(inner);

    box.append(bar, els.fold);
    root.append(box);
    document.documentElement.appendChild(host);
    restorePos();

    makeDraggable(bar);
    els.pause.onclick = (e) => { e.stopPropagation(); if (state.stopped) return; setPaused(!state.paused); emit(state.paused ? 'pause' : 'resume'); };
    els.stop.onclick = (e) => { e.stopPropagation(); if (state.stopped) return; state.stopped = true; render(); emit('stop'); };
    els.toggle.onclick = (e) => { e.stopPropagation(); state.open = !state.open; render(); };
    render();
  };

  // Drag the HUD by its top bar. Position is a transform (no layout), clamped to the viewport,
  // and remembered for the tab so it survives navigations within the recording.
  const POS_KEY = 'waystone-hud-pos';
  const pos = { x: 0, y: 0 };
  const applyPos = () => { host.style.transform = `translate(${pos.x}px, ${pos.y}px)`; };
  const clampPos = () => {
    const r = host.getBoundingClientRect();
    const minX = -(innerWidth - r.width - 14), maxX = 14, minY = -14, maxY = innerHeight - r.height - 14;
    pos.x = Math.min(maxX, Math.max(minX, pos.x)); pos.y = Math.min(maxY, Math.max(minY, pos.y));
  };
  const restorePos = () => {
    try { const v = JSON.parse(sessionStorage.getItem(POS_KEY) || 'null'); if (v) { pos.x = v.x; pos.y = v.y; clampPos(); applyPos(); } } catch (e) { /* ignore */ }
  };
  const makeDraggable = (bar) => {
    let start = null;
    bar.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || e.target.closest('button')) return;
      start = { x: e.clientX - pos.x, y: e.clientY - pos.y, moved: false };
      bar.setPointerCapture(e.pointerId);
      bar.setAttribute('data-dragging', '');
      e.preventDefault();
    });
    bar.addEventListener('pointermove', (e) => {
      if (!start) return;
      pos.x = e.clientX - start.x; pos.y = e.clientY - start.y; start.moved = true;
      clampPos(); applyPos();
    });
    const end = (e) => {
      if (!start) return;
      start = null; bar.removeAttribute('data-dragging');
      try { bar.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      try { sessionStorage.setItem(POS_KEY, JSON.stringify(pos)); } catch (err) { /* ignore */ }
    };
    bar.addEventListener('pointerup', end);
    bar.addEventListener('pointercancel', end);
    addEventListener('resize', () => { clampPos(); applyPos(); });
  };

  const emit = (action) => {
    try { globalThis.__waystone_emit(JSON.stringify({ type: 'control', action, t: Date.now() })); } catch (e) { /* no binding */ }
  };
  const setPaused = (p) => { state.paused = p; globalThis.__ws_paused = p; render(); };
  const trunc = (t, n = 30) => { t = String(t || ''); return t.length > n ? t.slice(0, n - 1) + '…' : t; };
  const label = (k) => { const t = String(k).replace(/-/g, ' '); return t.charAt(0).toUpperCase() + t.slice(1); };
  const fmt = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); };

  const render = () => {
    if (!els.label) return;
    const key = state.error ? 'error' : state.saved ? 'saved' : state.stopped ? 'saving' : state.paused ? 'paused' : 'recording';
    const st = STATUS[key];
    const final = key === 'saved' || key === 'error';
    if (key !== rendered.status) {
      rendered.status = key;
      swapLabel(st.text);
      els.pill.style.background = st.bg;
      els.pill.style.color = st.fg;
      els.label.style.color = st.fg;
      els.orbHost.style.color = st.fg;
      els.pill.toggleAttribute('data-final', final);
      // Paused keeps the recording orb but frozen at its compressed (first) keyframe: remount so the
      // animation restarts at 0%, then hold it there.
      if (els.orb && state.paused !== els.orbPaused) {
        els.orbPaused = state.paused;
        els.orb.unmount(); els.orbHost.textContent = '';
        els.orb = WsOrb.mount(els.orbHost, { variant: 'M2', size: 24 });
        els.orbHost.toggleAttribute('data-frozen', state.paused);
      }
      // Controls: pause/play grey, stop red; both leave once saved.
      swapIcon(els.pause, state.paused ? 'play' : 'pause', state.stopped ? C.deep : C.green);
      els.pause.title = state.paused ? 'Resume' : 'Pause';
      swapIcon(els.stop, state.stopped ? 'check' : 'stop', state.stopped ? hue(158) : hue(22));
      els.pause.toggleAttribute('data-gone', key === 'saved');
      els.stop.toggleAttribute('data-gone', key === 'saved');
    }
    if (state.open !== rendered.open) {
      rendered.open = state.open;
      els.toggle.toggleAttribute('data-closed', !state.open);
      els.fold.toggleAttribute('data-closed', !state.open);
      els.toggle.title = (state.open ? 'Hide' : 'Show') + ` steps (${state.steps.length})`;
    }
    renderList();
  };

  // Rows are appended, never rebuilt, so @starting-style animates only the new ones.
  const renderList = () => {
    const steps = state.steps.slice(-40);
    const first = steps.length ? steps[0].i : 0;
    while (els.list.firstChild && Number(els.list.firstChild.dataset.i) < first) els.list.firstChild.remove();
    const have = new Map([...els.list.children].map((r) => [Number(r.dataset.i), r]));
    for (const s of steps) {
      let row = have.get(s.i);
      if (!row) {
        row = h('div', 'row'); row.dataset.i = s.i;
        row.append(h('span', 'kind'), h('span', 'text'));
        els.list.append(row);
      }
      const kind = row.firstChild, text = row.lastChild;
      if (kind.textContent !== label(s.kind)) { kind.textContent = label(s.kind); kind.style.color = KIND_COLOR[s.kind] || C.mint; }
      const t = trunc(s.text); if (text.textContent !== t) text.textContent = t;
    }
    els.hint.style.display = state.steps.length ? 'none' : 'block';
    if (state.open) els.list.scrollTo({ top: els.list.scrollHeight, behavior: reduced() ? 'auto' : 'smooth' });
  };

  // Injected at document start, before <html> exists: mount once there is something to mount into.
  const boot = () => {
    if (!document.documentElement) return false;
    mount();
    setInterval(() => { if (els.time && !state.stopped) els.time.textContent = fmt(Date.now() - state.t0); }, 500);
    // Some pages wipe <html> children on boot; put the HUD back.
    new MutationObserver(() => { if (host && !host.isConnected && !state.stopped) document.documentElement.appendChild(host); }).observe(document.documentElement, { childList: true });
    return true;
  };
  if (!boot()) {
    const tick = setInterval(() => { if (boot()) clearInterval(tick); }, 30);
    document.addEventListener('DOMContentLoaded', () => { clearInterval(tick); boot(); }, { once: true });
  }

  globalThis.__ws_panel = {
    set(payload) {
      if (payload.t0) state.t0 = payload.t0;
      if (payload.steps) state.steps = payload.steps;
      if (payload.paused !== undefined) setPaused(!!payload.paused);
      if (document.documentElement) { mount(); render(); }
    },
    stopped() { state.stopped = true; render(); },
    saved() { state.stopped = true; state.saved = true; state.error = null; render(); },
    error(msg) { state.error = msg || 'Error'; state.saved = false; render(); },
    press(name) { const b = els[name]; if (b) b.click(); return !!b; },
    state() { return { steps: state.steps.length, open: state.open, paused: state.paused, mounted: !!(host && host.isConnected), rows: els.list ? els.list.children.length : -1 }; },
  };
})();
