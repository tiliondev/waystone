// Waystone recorder. Injected into the isolated world of every document (including same-origin
// iframes) while a session is active. Capture-phase listeners see events before page handlers
// can stop propagation or navigate away; results go to Python through a CDP binding that exists
// only in this world.
//
// Captures: click, dblclick, drag, type (coalesced per field), select, check, press (incl. modifier
// combos and arrows), hover (only when it revealed the thing that was clicked next), upload,
// scroll (window and scrollable elements), and the moment before each click for screenshots.
(() => {
  if (globalThis.__ws_rec || !globalThis.__ws) return;
  globalThis.__ws_rec = true;

  const BINDING = '__waystone_emit';
  const ws = globalThis.__ws;
  const SHOW = !!globalThis.__ws_show;
  const now = () => Date.now();
  const emit = (o) => {
    try { globalThis[BINDING](JSON.stringify(o)); } catch (e) { /* binding not attached */ }
  };
  // Any action other than typing itself first flushes the field being typed into, so steps stay ordered.
  const emitAction = (o) => { if (o.type !== 'type' && o.type !== 'pre-click') flush(); emit(o); };
  const tgt = (e) => (e.composedPath ? e.composedPath()[0] : e.target);
  const OURS = new Set(['WAYSTONE-PANEL', 'WAYSTONE-MARKS']);
  const ours = (e) => e.composedPath && e.composedPath().some((n) => n && OURS.has(n.nodeName));
  // Page-generated events, clicks on our own HUD, and anything while paused are not the human's workflow.
  const skip = (e) => e.isTrusted === false || ours(e) || globalThis.__ws_paused;
  // Where inside the element the pointer was, as fractions, so replay clicks the same spot.
  const withOffset = (d, el, e) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && e && e.clientX != null) d.offset = { ox: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), oy: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
    return d;
  };
  const modsOf = (e) => [e.metaKey && 'Meta', e.ctrlKey && 'Control', e.altKey && 'Alt', e.shiftKey && 'Shift'].filter(Boolean);
  const isEl = (n) => !!n && n.nodeType === 1;
  const show = (el) => { if (SHOW && el && window === window.top) { try { ws.flash(ws.rectOf(el)); } catch (e) { /* ignore */ } } };

  // --- typing: coalesced per field --------------------------------------------------

  let pending = null, pendingT = 0, pendingDesc = null;
  let flushTimer = null;
  const flush = () => {
    clearTimeout(flushTimer);
    if (!pending) return;
    const el = pending, t0 = pendingT, desc = pendingDesc;
    pending = null; pendingDesc = null;
    const editable = el.isContentEditable;
    // The node may have been replaced by its framework since the first keystroke (typeahead
    // inputs do this); read the value from whichever node is live, keep the description we took
    // when it was.
    const live = el.isConnected ? el : (desc ? (ws.resolveEl(desc.candidates, desc.fingerprint, {}) || {}).el : null) || el;
    emit({
      type: 'type',
      target: desc || ws.describe(el),
      value: editable ? live.textContent : (live.value !== undefined ? live.value : el.value),
      secret: (el.type || '').toLowerCase() === 'password' || (el.autocomplete || '').includes('password'),
      t: t0,
    });
  };

  addEventListener('input', (e) => {
    if (skip(e)) return;
    const el = tgt(e);
    if (!isEl(el)) return;
    if (!(el.matches('input, textarea') || el.isContentEditable)) return;
    const type = (el.type || '').toLowerCase();
    if (['checkbox', 'radio', 'file', 'range', 'color'].includes(type)) return;
    // Same field if same node, or same identity (frameworks swap the node but keep its id/name).
    const sameField = pending && (pending === el || (pendingDesc && JSON.stringify(pendingDesc.candidates[0]) === JSON.stringify(ws.describe(el).candidates[0])));
    if (pending && !sameField) flush();
    if (!sameField) { pendingT = now(); pendingDesc = ws.describe(el); }
    pending = el;
    clearTimeout(flushTimer);
    flushTimer = setTimeout(flush, 700);
  }, true);

  // --- hover detection: did hovering X reveal the thing that was clicked next? --------------

  let hover = null; // {el, t, appeared:Set<Element>}
  const bigArea = () => innerWidth * innerHeight * 0.5;
  const mo = new MutationObserver((muts) => {
    if (!hover) return;
    for (const m of muts) {
      if (m.type === 'childList') m.addedNodes.forEach((n) => n.nodeType === 1 && !OURS.has(n.nodeName) && hover.appeared.add(n));
      else if (m.target && m.target.nodeType === 1) hover.appeared.add(m.target);
    }
  });
  try {
    mo.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden', 'aria-expanded', 'aria-hidden', 'open', 'data-state'] });
  } catch (e) { /* no documentElement yet */ }

  const revealedBy = (h, el) => {
    if (!h || h.el === el || h.el.contains(el)) return false;
    for (const n of h.appeared) {
      if (n === h.el || n.contains(h.el)) continue;
      const r = n.getBoundingClientRect();
      if (r.width * r.height > bigArea()) continue;
      if (n === el || n.contains(el)) return true;
    }
    return false;
  };

  addEventListener('mouseover', (e) => {
    if (ours(e)) return;
    const el = ws.pick(tgt(e));
    if (!el) return;
    if (hover && (hover.el === el || hover.el.contains(el))) return;
    if (hover && revealedBy(hover, el)) return; // moving within the revealed menu: keep the hover
    hover = { el, t: now(), appeared: new Set() };
  }, true);

  // CSS-only menus (`.menu:hover .items { display:block }`) never mutate the DOM. Find a stylesheet
  // rule with :hover that reveals the target, and return the ancestor that must be hovered.
  let hoverRules = null, hoverRulesT = 0;
  const REVEAL = /display|visibility|opacity|max-height|height|transform|pointer-events|clip|left|top/;
  const collectRules = (rules, out) => {
    for (const r of rules) {
      try {
        if (r.cssRules && !r.selectorText) collectRules(r.cssRules, out);
        else if (r.selectorText && r.selectorText.includes(':hover') && REVEAL.test(r.style && r.style.cssText || '')) {
          for (const sel of r.selectorText.split(',')) if (sel.includes(':hover')) out.push(sel.trim());
        }
      } catch (e) { /* ignore */ }
    }
  };
  const getHoverRules = () => {
    if (hoverRules && now() - hoverRulesT < 5000) return hoverRules;
    const out = [];
    for (const ss of document.styleSheets) {
      let rules = null;
      try { rules = ss.cssRules; } catch (e) { continue; } // cross-origin sheet
      if (rules) collectRules(rules, out);
    }
    hoverRules = out; hoverRulesT = now();
    return out;
  };
  const cssRevealer = (target) => {
    for (const sel of getHoverRules()) {
      const i = sel.lastIndexOf(':hover');
      const prefix = sel.slice(0, i).replace(/:hover/g, '');
      const suffix = sel.slice(i + 6);
      if (!suffix.trim()) continue; // the hovered element is itself the styled one: no reveal
      const clean = (prefix + suffix).replace(/:hover/g, '');
      let revealed = null;
      try { revealed = target.closest(clean); } catch (e) { continue; }
      if (!revealed) continue;
      for (let e = target.parentElement; e; e = e.parentElement) {
        try { if (e.matches(prefix.trim() || '*') && e.contains(revealed) && e !== revealed) return e; } catch (err) { break; }
      }
    }
    return null;
  };

  const maybeHover = (el) => {
    let revealer = null;
    if (hover && revealedBy(hover, el) && now() - hover.t > 120) revealer = hover.el;
    else revealer = cssRevealer(el);
    if (revealer && revealer !== el && !el.contains(revealer)) {
      emitAction({ type: 'hover', target: ws.describe(revealer), t: hover ? hover.t : now() });
    }
  };

  // --- mouse: click / dblclick / drag ----------------------------------------------------

  let down = null; // {x, y, el, t}
  let suppressClick = false;

  addEventListener('mousedown', (e) => {
    if (skip(e)) return;
    if (e.button !== 0) return;
    const el = ws.pick(tgt(e));
    if (!el) return;
    if (pending && pending !== el) flush();
    down = { x: e.clientX, y: e.clientY, el, t: now() };
    suppressClick = false;
    emit({ type: 'pre-click', target: withOffset(ws.describe(el), el, e), t: now() });
  }, true);

  addEventListener('mouseup', (e) => {
    if (skip(e)) return;
    if (e.button !== 0 || !down) return;
    const dx = e.clientX - down.x, dy = e.clientY - down.y;
    if (Math.hypot(dx, dy) > 6 && now() - down.t > 80) {
      suppressClick = true;
      const off = ws.rectOf(down.el);
      const r0 = down.el.getBoundingClientRect();
      const fx = off.x - r0.left, fy = off.y - r0.top; // frame offset
      emitAction({ type: 'drag', target: ws.describe(down.el), from: { x: down.x + fx, y: down.y + fy }, to: { x: e.clientX + fx, y: e.clientY + fy }, t: now() });
      show(down.el);
    }
    down = null;
  }, true);

  addEventListener('click', (e) => {
    if (skip(e)) return;
    if (e.button !== 0) return;
    if (suppressClick) { suppressClick = false; return; }
    const raw = tgt(e);
    const el = ws.pick(raw);
    if (!el) return;
    if (pending && pending !== el) flush();
    maybeHover(el);
    const type = (el.type || '').toLowerCase();
    if (el.tagName === 'INPUT' && (type === 'checkbox' || type === 'radio')) {
      // `click` fires before the checked state settles in some frameworks; report intent.
      emitAction({ type: 'check', target: withOffset(ws.describe(el), el, e), value: !el.checked ? true : el.checked, t: now() });
    } else {
      emitAction({ type: 'click', target: withOffset(ws.describe(el), el, e), modifiers: modsOf(e), t: now() });
    }
    show(el);
    hover = null;
  }, true);

  // Right-click on a link: if a new tab opens right after, that was "Open link in new tab".
  addEventListener('contextmenu', (e) => {
    if (skip(e)) return;
    const el = ws.pick(tgt(e));
    const link = el && (el.closest('a[href]') || (el.querySelector && el.querySelector('a[href]')));
    emitAction({ type: 'contextmenu', target: withOffset(ws.describe(link || el), link || el, e), href: link ? link.href : null, t: now() });
  }, true);

  addEventListener('dblclick', (e) => {
    if (skip(e)) return;
    const el = ws.pick(tgt(e));
    if (el) emitAction({ type: 'dblclick', target: withOffset(ws.describe(el), el, e), t: now() });
  }, true);

  // --- change: select / file / checkbox state --------------------------------------------

  addEventListener('change', (e) => {
    if (ours(e) || globalThis.__ws_paused) return;
    // Untrusted change events are kept: custom dropdowns set a hidden <select> and dispatch change,
    // and recording that is a harmless, idempotent state set (unlike a page-generated click).
    const el = tgt(e);
    if (!isEl(el)) return;
    const type = (el.type || '').toLowerCase();
    if (el.tagName === 'SELECT') {
      emitAction({ type: 'select', target: ws.describe(el), value: el.value, label: el.selectedOptions[0] ? el.selectedOptions[0].textContent.trim() : '', t: now() });
      show(el);
    } else if (type === 'file') {
      emitAction({ type: 'upload', target: ws.describe(el), files: [...(el.files || [])].map((f) => f.name), t: now() });
    } else if (type === 'checkbox' || type === 'radio') {
      emitAction({ type: 'check-state', target: ws.describe(el), value: el.checked, t: now() });
    } else if (type === 'range' || type === 'color' || type === 'date' || type === 'time') {
      emitAction({ type: 'set', target: ws.describe(el), value: el.value, t: now() });
    } else {
      flush();
    }
  }, true);

  // --- keyboard ------------------------------------------------------------------------------

  const NAV_KEYS = new Set(['Enter', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown', 'Home', 'End', 'Delete']);
  addEventListener('keydown', (e) => {
    if (skip(e)) return;
    const mods = [e.metaKey && 'Meta', e.ctrlKey && 'Control', e.altKey && 'Alt', e.shiftKey && 'Shift'].filter(Boolean);
    const combo = mods.length && e.key.length === 1 ? [...mods, e.key.toLowerCase()].join('+') : null;
    const isNav = NAV_KEYS.has(e.key);
    if (!isNav && !combo) return;
    if (combo && !e.metaKey && !e.ctrlKey) return; // shift+letter is just typing
    if (e.key === 'Backspace') return;
    flush();
    const el = ws.pick(tgt(e)) || document.activeElement;
    const key = combo || (e.shiftKey && isNav ? `Shift+${e.key}` : e.key);
    emitAction({ type: 'press', key, target: el ? ws.describe(el) : null, t: now() });
  }, true);

  addEventListener('blur', () => flush(), true);
  addEventListener('focusout', () => flush(), true);
  addEventListener('submit', () => flush(), true);
  addEventListener('beforeunload', () => flush(), true);
  addEventListener('pagehide', () => flush(), true);

  // --- scroll: window and scrollable elements --------------------------------------------

  // Pages scroll themselves: carousels, scroll restoration, scrollIntoView on focus, lazy loaders.
  // Only a scroll that follows a person's input counts: a wheel/trackpad gesture, a scrolling key,
  // or a mousedown in a scrollbar gutter (drag). Momentum scrolling keeps firing wheel events, so the window
  // stays open as long as the gesture does.
  const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown', 'Home', 'End', ' ']);
  let lastUserScrollInput = -Infinity;
  const noteScrollInput = () => { lastUserScrollInput = performance.now(); };
  addEventListener('wheel', noteScrollInput, { capture: true, passive: true });
  addEventListener('touchmove', noteScrollInput, { capture: true, passive: true });
  const onScrollbar = (e) => {  // a mousedown in a scrollbar gutter: clientWidth/Height exclude the bars
    const de = document.documentElement;
    if (e.clientX >= de.clientWidth || e.clientY >= de.clientHeight) return true;
    const el = e.target;
    if (!isEl(el)) return false;
    const r = el.getBoundingClientRect();
    return (el.scrollHeight > el.clientHeight && e.clientX - r.left >= el.clientWidth) ||
           (el.scrollWidth > el.clientWidth && e.clientY - r.top >= el.clientHeight);
  };
  addEventListener('mousedown', (e) => { if (onScrollbar(e)) noteScrollInput(); }, true);
  addEventListener('keydown', (e) => { if (SCROLL_KEYS.has(e.key)) noteScrollInput(); }, true);
  const userScrolled = () => performance.now() - lastUserScrollInput < 1500;

  const scrollTimers = new Map();
  addEventListener('scroll', (e) => {
    if (globalThis.__ws_paused || ours(e) || !userScrolled()) return;
    const el = tgt(e);
    const key = isEl(el) ? el : 'window';
    clearTimeout(scrollTimers.get(key));
    scrollTimers.set(key, setTimeout(() => {
      scrollTimers.delete(key);
      if (key === 'window') emitAction({ type: 'scroll', x: scrollX, y: scrollY, frame: ws.frameChain(window), t: now() });
      else emitAction({ type: 'scroll', target: ws.describe(el), x: el.scrollLeft, y: el.scrollTop, t: now() });
    }, 400));
  }, true);

  // window.print() is stubbed in the main world to dispatch this instead of opening the native dialog.
  document.addEventListener('waystone:print', () => emitAction({ type: 'print', t: now() }), true);

  emit({ type: 'ready', url: location.href, title: document.title, top: window === window.top, t: now() });
})();
