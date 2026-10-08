// Waystone core. Runs in an isolated world: it can read the DOM but page scripts cannot see it.
// Pure (no DOM mutation) except overlay()/flash()/clearOverlay(), which are opt-in visuals.
//
// Handles: open shadow DOM (">>>" selectors, deep queries, composedPath targets), same-origin
// iframes (frame chains + top-level rect offsets), cursor:pointer pseudo-buttons, occlusion,
// framework-generated ids/classes, and fingerprint-scored self-healing resolution.
(() => {
  if (globalThis.__ws) return;

  const INTERACTIVE =
    'a[href], button, input:not([type=hidden]), select, textarea, summary, label, ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
    '[role=menuitemcheckbox], [role=menuitemradio], [role=option], [role=switch], [role=textbox], ' +
    '[role=combobox], [role=searchbox], [role=slider], [role=treeitem], [role=gridcell], ' +
    '[contenteditable=""], [contenteditable=true], [onclick], [tabindex]:not([tabindex="-1"]), ' +
    '[jsaction*="click"], [ng-click], [data-action], [data-toggle], [data-bs-toggle], [data-href], [data-url], [data-link], [data-ved][jsaction], ' +
    'canvas, video, svg[onclick], svg[tabindex]';  // canvas/video: the element is the surface the user acts on
  const POINTER_TAGS = new Set(['DIV', 'SPAN', 'LI', 'IMG', 'SVG', 'TD', 'TR', 'P', 'H1', 'H2', 'H3', 'H4', 'ARTICLE', 'SECTION', 'FIGURE', 'I']);

  const IMPLICIT_ROLE = {
    A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox', SUMMARY: 'button', LABEL: 'label',
    H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading', H5: 'heading', H6: 'heading',
    IMG: 'img', NAV: 'navigation', MAIN: 'main', FORM: 'form', TABLE: 'table', OPTION: 'option',
  };
  const INPUT_ROLE = {
    button: 'button', submit: 'button', reset: 'button', image: 'button', file: 'button',
    checkbox: 'checkbox', radio: 'radio', range: 'slider', search: 'searchbox',
    email: 'textbox', tel: 'textbox', text: 'textbox', url: 'textbox', password: 'textbox', number: 'spinbutton',
    date: 'textbox', time: 'textbox', 'datetime-local': 'textbox', month: 'textbox', week: 'textbox', color: 'button',
  };

  const isEl = (n) => !!n && n.nodeType === 1; // cross-realm safe (iframes have their own Element)
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const short = (s, n = 80) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
  const esc = (s) => (globalThis.CSS && CSS.escape ? CSS.escape(s) : s.replace(/([^\w-])/g, '\\$1'));
  const q = (s) => JSON.stringify(s);
  const winOf = (el) => (el.ownerDocument && el.ownerDocument.defaultView) || window;
  const style = (el) => winOf(el).getComputedStyle(el);
  const area = (r) => Math.max(0, r.w) * Math.max(0, r.h);

  // --- deep DOM (shadow roots) ---------------------------------------------

  // Closed shadow roots are invisible to JS. Python finds them via DOM.getDocument(pierce) and
  // hands the root nodes in through addClosedRoots(); from then on they are walked like open ones.
  const closedRoots = new Map(); // host element -> ShadowRoot
  const addClosedRoots = (pairs) => { for (const [host, root] of pairs) if (host && root && !OUR_HOSTS.has(host.nodeName)) closedRoots.set(host, root); return closedRoots.size; };
  const rootOf = (el) => el.shadowRoot || closedRoots.get(el) || null;

  const OUR_HOSTS = new Set(['WAYSTONE-PANEL', 'WAYSTONE-MARKS']);
  function* walk(root) {
    const els = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of els) {
      if (OUR_HOSTS.has(el.nodeName)) continue;  // never look inside our own overlays
      yield el;
      const sr = rootOf(el);
      if (sr) yield* walk(sr);
    }
  }
  const deepAll = (root) => [...walk(root || document)];

  // querySelectorAll that understands "host >>> inner" for shadow roots.
  const deepQuery = (sel, root) => {
    const parts = sel.split(' >>> ');
    let roots = [root || document];
    for (let i = 0; i < parts.length; i++) {
      const next = [];
      for (const r of roots) {
        let found = [];
        try { found = [...r.querySelectorAll(parts[i])]; } catch (e) { /* bad selector */ }
        if (i === parts.length - 1) next.push(...found);
        else for (const f of found) { const sr = rootOf(f); if (sr) next.push(sr); }
      }
      roots = next;
    }
    return roots;
  };

  const closestDeep = (el, sel) => {
    let cur = el;
    while (cur) {
      if (isEl(cur)) {
        const hit = cur.closest(sel);
        if (hit) return hit;
      }
      const root = cur.getRootNode ? cur.getRootNode() : null;
      cur = root && root.host ? root.host : null;
    }
    return null;
  };

  // --- roles and names -------------------------------------------------------

  // DOM text of the visible nodes only. Not innerText: that returns the *rendered* string, so a
  // link styled text-transform:uppercase would be named "MEETINGS" while Playwright, Puppeteer
  // and the accessibility tree all see "Meetings". Skips <style>/<script> and display:none parts.
  const SKIP_TAGS = new Set(['STYLE', 'SCRIPT', 'TEMPLATE', 'NOSCRIPT']);
  const visText = (el) => {
    const doc = el.ownerDocument || document;
    const walker = doc.createTreeWalker(el, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode(n) {
        if (n.nodeType === 1) {
          if (SKIP_TAGS.has(n.tagName) || n.hidden) return NodeFilter.FILTER_REJECT;
          const st = style(n);
          return st.display === 'none' || st.visibility === 'hidden' ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let out = '';
    for (let n = walker.nextNode(); n; n = walker.nextNode()) out += n.nodeValue + ' ';
    return clean(out);
  };

  const role = (el) => {
    const r = el.getAttribute && el.getAttribute('role');
    if (r) return r.split(/\s+/)[0];
    if (el.tagName === 'INPUT') return INPUT_ROLE[(el.type || 'text').toLowerCase()] || 'textbox';
    if (el.tagName === 'A' && !el.hasAttribute('href')) return 'generic';
    return IMPLICIT_ROLE[el.tagName] || 'generic';
  };

  const labelFor = (el) => {
    const doc = el.ownerDocument || document;
    if (el.id) {
      const l = doc.querySelector(`label[for="${esc(el.id)}"]`);
      if (l) return visText(l);
    }
    const wrap = closestDeep(el, 'label');
    if (wrap) return visText(wrap);
    return '';
  };

  const accName = (el) => {
    const doc = el.ownerDocument || document;
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map((id) => doc.getElementById(id)).filter(Boolean).map((n) => n.textContent).join(' ');
      if (clean(t)) return clean(t);
    }
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) {
      return labelFor(el) || clean(el.placeholder) || clean(el.getAttribute('title')) || clean(el.name) || '';
    }
    if (el.tagName === 'IMG') return clean(el.alt);
    if (el.tagName === 'SVG' || el.tagName === 'svg') {
      const t = el.querySelector('title');
      if (t) return clean(t.textContent);
    }
    const img = el.querySelector && el.querySelector('img[alt]');
    const txt = visText(el);
    return txt || (img ? clean(img.alt) : '') || clean(el.getAttribute('title')) || '';
  };

  // --- geometry and visibility -----------------------------------------------

  const rectLocal = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  };

  // Offset from this frame's viewport to the top document's viewport (same-origin frames only).
  const frameOffset = (win) => {
    let x = 0, y = 0, w = win || window;
    for (let i = 0; i < 8 && w !== w.top; i++) {
      let fe = null;
      try { fe = w.frameElement; } catch (e) { break; }
      if (!fe) break;
      const r = fe.getBoundingClientRect();
      x += r.left + fe.clientLeft;
      y += r.top + fe.clientTop;
      w = w.parent;
    }
    return { x, y };
  };

  const frameChain = (win) => {
    const chain = [];
    let w = win || window;
    for (let i = 0; i < 8 && w !== w.top; i++) {
      let fe = null;
      try { fe = w.frameElement; } catch (e) { break; }
      if (!fe) break;
      chain.unshift(cssPath(fe));
      w = w.parent;
    }
    return chain;
  };

  const topViewport = () => {
    let w = window;
    try { if (w.top && w.top.innerWidth) w = w.top; } catch (e) { /* cross-origin */ }
    return { w: w.innerWidth, h: w.innerHeight, sx: w.scrollX, sy: w.scrollY };
  };

  const rectOf = (el) => {
    const r = rectLocal(el);
    const off = frameOffset(winOf(el));
    return { x: r.x + off.x, y: r.y + off.y, w: r.w, h: r.h };
  };

  const isVisible = (el) => {
    if (!isEl(el)) return false;
    const st = style(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    if (closestDeep(el, '[aria-hidden="true"]')) return false;
    if (el.closest('details:not([open])') && el.tagName !== 'SUMMARY' && !el.closest('summary')) return false;
    return true;
  };

  const inViewport = (r, vp) => {
    vp = vp || { w: innerWidth, h: innerHeight };
    return r.x + r.w > 0 && r.y + r.h > 0 && r.x < vp.w && r.y < vp.h;
  };

  // Is this element what the user would hit at its centre? Walks shadow roots and frames.
  const hittable = (el) => {
    const win = winOf(el), doc = el.ownerDocument || document;
    const r = el.getBoundingClientRect();
    const cx = Math.min(win.innerWidth - 1, Math.max(0, r.left + r.width / 2));
    const cy = Math.min(win.innerHeight - 1, Math.max(0, r.top + r.height / 2));
    let top = fromPoint(doc, cx, cy);
    while (top && rootOf(top)) {
      const inner = rootOf(top).elementFromPoint(cx, cy);
      if (!inner || inner === top) break;
      top = inner;
    }
    if (!top) return false;
    if (top === el || el.contains(top) || top.contains(el)) return true;
    const topRoot = top.getRootNode();
    if (topRoot && topRoot.host && el.contains(topRoot.host)) return true;
    return false;
  };

  // Our own overlays (HUD, marks) must never count as covering the page for hit tests.
  const withoutOurs = (fn) => {
    const ours = document.querySelectorAll('waystone-panel, waystone-marks');
    const prev = [];
    ours.forEach((h) => { prev.push(h.style.pointerEvents); h.style.pointerEvents = 'none'; });
    try { return fn(); } finally { ours.forEach((h, i) => { h.style.pointerEvents = prev[i]; }); }
  };
  const fromPoint = (doc, x, y) => withoutOurs(() => doc.elementFromPoint(x, y));

  // Does a click at local viewport point (x, y) land on el (or inside it)?
  const hitsAt = (el, x, y) => {
    const win = winOf(el), doc = el.ownerDocument || document;
    if (x < 0 || y < 0 || x >= win.innerWidth || y >= win.innerHeight) return false;
    let top = fromPoint(doc, x, y);
    while (top && rootOf(top)) {
      const inner = rootOf(top).elementFromPoint(x, y);
      if (!inner || inner === top) break;
      top = inner;
    }
    if (!top) return false;
    if (top === el || el.contains(top)) return true;
    const topRoot = top.getRootNode();
    if (topRoot && topRoot.host && el.contains(topRoot.host)) return true;
    // A label/wrapper that contains the element: clicking it still reaches the element.
    return top.contains(el) && (top.tagName === 'LABEL' || area(rectLocal(top)) < area(rectLocal(el)) * 1.5);
  };

  // Find a point inside el that actually hits it: the preferred offset first (where the human
  // clicked), then the centre, then a grid. Stretched links and overlapping cards need this.
  const hitPoint = (el, prefer) => {
    const r = el.getBoundingClientRect();
    const pts = [];
    if (prefer && prefer.ox != null) pts.push([r.left + r.width * prefer.ox, r.top + r.height * prefer.oy]);
    pts.push([r.left + r.width / 2, r.top + r.height / 2]);
    for (const fy of [0.5, 0.25, 0.75, 0.1, 0.9]) for (const fx of [0.5, 0.25, 0.75, 0.1, 0.9]) pts.push([r.left + r.width * fx, r.top + r.height * fy]);
    for (const [x, y] of pts) {
      const cx = Math.min(r.right - 1, Math.max(r.left + 1, x)), cy = Math.min(r.bottom - 1, Math.max(r.top + 1, y));
      if (hitsAt(el, cx, cy)) {
        const off = frameOffset(winOf(el));
        return { x: cx + off.x, y: cy + off.y };
      }
    }
    return null;
  };

  const isPointer = (el) => POINTER_TAGS.has(el.tagName.toUpperCase()) && style(el).cursor === 'pointer';
  // cursor is inherited: climb to the outermost contiguous pointer element (bounded by maxArea).
  const pointerRoot = (el, maxArea) => {
    let cur = el;
    while (cur.parentElement && isPointer(cur.parentElement) && (!maxArea || area(rectOf(cur.parentElement)) <= maxArea)) cur = cur.parentElement;
    return cur;
  };

  // --- selectors ----------------------------------------------------------------

  const AUTO_PREFIX = /^(ui-id-|ember|react-|radix|mui|headlessui|cdk-|ng-|:r|__|css-|sc-|jsx-|svelte-|emotion-|chakra-|data-v-|v-|styled)/i;
  const isAuto = (s) => {
    if (!s) return true;
    if (AUTO_PREFIX.test(s) || /\d{3,}|[0-9a-f]{8,}/i.test(s)) return true;
    return s.split(/[-_]/).some((seg) => seg.length >= 5 && /[a-z]/i.test(seg) && (seg.match(/\d/g) || []).length >= 2);
  };
  const uniq = (sel, root) => {
    try { return root.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  };

  // CSS path within the element's own root (document or shadow root).
  const cssPathLocal = (el) => {
    const root = el.getRootNode();
    const parts = [];
    let cur = el;
    for (let depth = 0; cur && cur.nodeType === 1 && depth < 10; depth++) {
      let part = cur.tagName.toLowerCase();
      if (cur.id && !isAuto(cur.id) && uniq(`#${esc(cur.id)}`, root)) {
        parts.unshift(`#${esc(cur.id)}`);
        break;
      }
      const stable = [...cur.classList].filter((c) => !isAuto(c) && c.length < 32).slice(0, 2);
      if (stable.length) part += '.' + stable.map(esc).join('.');
      const parent = cur.parentElement;
      const sibs = parent ? [...parent.children].filter((s) => s.tagName === cur.tagName) : [];
      if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      parts.unshift(part);
      if (uniq(parts.join(' > '), root)) break;
      if (!parent) break;
      cur = parent;
    }
    return parts.join(' > ');
  };

  // Full path, composed through shadow hosts with ">>>".
  const cssPath = (el) => {
    const segs = [];
    let cur = el;
    for (let i = 0; cur && i < 6; i++) {
      segs.unshift(cssPathLocal(cur));
      const root = cur.getRootNode();
      cur = root && root.host ? root.host : null;
    }
    return segs.join(' >>> ');
  };

  const xpath = (el) => {
    if (el.getRootNode() !== (el.ownerDocument || document)) return null; // not meaningful inside shadow roots
    const segs = [];
    for (let cur = el; cur && cur.nodeType === 1; cur = cur.parentNode) {
      const tag = cur.tagName.toLowerCase();
      const sibs = cur.parentNode ? [...cur.parentNode.children].filter((s) => s.tagName === cur.tagName) : [cur];
      segs.unshift(sibs.length > 1 ? `${tag}[${sibs.indexOf(cur) + 1}]` : tag);
    }
    return '/' + segs.join('/');
  };

  const TEST_ATTRS = ['data-testid', 'data-test', 'data-test-id', 'data-cy', 'data-qa', 'data-automation-id', 'data-tracking-id', 'data-id', 'name'];
  const TEXT_TAGS = ['A', 'BUTTON', 'SUMMARY', 'LABEL', 'OPTION', 'LI', 'TD', 'TH', 'SPAN', 'DIV'];

  // Ordered selector candidates, most stable first. Replay walks this list.
  const candidates = (el) => {
    const out = [];
    const tag = el.tagName.toLowerCase();
    const hostPrefix = (() => {
      const root = el.getRootNode();
      return root && root.host ? cssPath(root.host) + ' >>> ' : '';
    })();
    for (const a of TEST_ATTRS) {
      const v = el.getAttribute(a);
      if (v && !isAuto(v)) out.push({ kind: 'css', value: `${hostPrefix}${a === 'name' ? tag : ''}[${a}=${q(v)}]` });
    }
    if (el.id && !isAuto(el.id)) out.push({ kind: 'css', value: `${hostPrefix}#${esc(el.id)}` });
    const r = role(el), name = accName(el);
    if (name && r !== 'generic' && name.length <= 120) out.push({ kind: 'role', role: r, name });
    // Playwright's ranking: placeholder and label beat raw text for form fields.
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) {
      if (el.placeholder) out.push({ kind: 'css', value: `${hostPrefix}${tag}[placeholder=${q(el.placeholder)}]` });
      const l = labelFor(el);
      if (l && l.length <= 120) out.push({ kind: 'label', value: l });
    }
    if (TEXT_TAGS.includes(el.tagName) || r === 'button' || r === 'link' || r === 'tab' || r === 'menuitem' || r === 'option') {
      const t = visText(el);
      if (t && t.length <= 80) out.push({ kind: 'text', value: t, tag });
    }
    const aria = el.getAttribute('aria-label');
    if (aria) out.push({ kind: 'css', value: `${hostPrefix}${tag}[aria-label=${q(aria)}]` });
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) && el.type && el.type !== 'text') out.push({ kind: 'css', value: `${hostPrefix}${tag}[type=${q(el.type)}]` });
    if (el.tagName === 'A' && el.getAttribute('href')) {
      const href = el.getAttribute('href');
      if (href.length < 200 && !/^(#|javascript:)/.test(href)) out.push({ kind: 'css', value: `${hostPrefix}a[href=${q(href)}]` });
    }
    const title = el.getAttribute('title');
    if (title) out.push({ kind: 'css', value: `${hostPrefix}${tag}[title=${q(title)}]` });
    out.push({ kind: 'css', value: cssPath(el) });
    const xp = xpath(el);
    if (xp) out.push({ kind: 'xpath', value: xp });
    return out;
  };



  const fingerprint = (el) => {
    const r = role(el);
    const name = accName(el);
    return {
      tag: el.tagName.toLowerCase(),
      role: r,
      name: short(name, r === 'generic' ? 60 : 120),
      text: short(visText(el), 80),
      type: el.type || null,
      href: el.tagName === 'A' ? short(el.getAttribute('href') || '', 200) : null,
      classes: [...el.classList].filter((c) => !isAuto(c)).slice(0, 4),
      pointer: isPointer(el),
    };
  };

  const describe = (el) => ({
    candidates: candidates(el),
    fingerprint: fingerprint(el),
    rect: rectOf(el),
    frame: frameChain(winOf(el)),
    dpr: devicePixelRatio,
    viewport: topViewport(),
  });

  // --- picking the element the user meant ------------------------------------------

  const viewportArea = () => { const v = topViewport(); return v.w * v.h; };

  // From a raw event target (possibly inside a shadow root or a text node), find the thing the
  // user meant: the nearest interactive ancestor, unless that ancestor is a huge container, in
  // which case prefer the smallest clickable-looking element at or above the raw target.
  const pick = (node) => {
    let raw = isEl(node) ? node : node && node.parentElement;
    if (!raw) return null;
    const rawRect = rectOf(raw);
    const big = Math.max(viewportArea() * 0.3, area(rawRect) * 12);
    const hit = closestDeep(raw, INTERACTIVE);
    if (hit && (area(rectOf(hit)) <= big || ['CANVAS', 'VIDEO'].includes(hit.tagName))) return hit;
    // Walk up from raw looking for a reasonably sized pointer/interactive element.
    let cur = raw;
    for (let i = 0; cur && i < 6; i++) {
      if (isEl(cur)) {
        const rr = rectOf(cur);
        if (area(rr) > big) break;
        if (cur.matches(INTERACTIVE)) return cur;
        if (isPointer(cur)) return pointerRoot(cur, big);
      }
      const root = cur.getRootNode();
      cur = cur.parentElement || (root && root.host ? root.host : null);
    }
    // Nothing clickable-looking: the smallest element with text at or above raw.
    cur = raw;
    for (let i = 0; cur && i < 4; i++) {
      if (clean(cur.textContent) && area(rectOf(cur)) <= big) return cur;
      cur = cur.parentElement;
    }
    return raw;
  };

  // --- set-of-marks scan --------------------------------------------------------------

  const scan = (opts = {}) => {
    const viewportOnly = opts.viewportOnly !== false;
    const vp = topViewport();
    const items = [];
    const seen = new Set();
    const consider = (el, doc) => {
      if (seen.has(el) || !isVisible(el)) return;
      const r = rectOf(el);
      if (viewportOnly && (!inViewport(r, vp) || !hittable(el))) return;
      if (area(r) > vp.w * vp.h * 0.5 && !['CANVAS', 'VIDEO'].includes(el.tagName)) return;
      seen.add(el);
      items.push({ el, r });
    };
    const scanDoc = (doc) => {
      for (const el of deepAll(doc)) {
        if (el.tagName === 'LABEL') {
          // A label stands in for its control: clicking it focuses the control, and the control is
          // what a person means. Keep the label only when it has no visible control.
          const ctl = el.control || (el.htmlFor ? doc.getElementById(el.htmlFor) : el.querySelector('input, select, textarea'));
          if (ctl && isVisible(ctl)) { consider(ctl, doc); continue; }
        }
        if (el.matches(INTERACTIVE)) consider(el, doc);
        else if (isPointer(el) && !closestDeep(el, INTERACTIVE) && !(el.parentElement && isPointer(el.parentElement))) consider(el, doc);
        else if (el.tagName === 'IFRAME') {
          try { if (el.contentDocument) scanDoc(el.contentDocument); } catch (e) { /* cross-origin */ }
        }
      }
    };
    scanDoc(document);
    // Drop an interactive parent when an interactive child covers (nearly) the same box.
    const kept = items.filter(({ el, r }) =>
      !items.some((o) => o.el !== el && el.contains(o.el) &&
        Math.abs(o.r.x - r.x) < 4 && Math.abs(o.r.y - r.y) < 4 && Math.abs(o.r.w - r.w) < 4 && Math.abs(o.r.h - r.h) < 4));
    // And drop a pointer-styled wrapper whose only interactive content is a single child mark.
    const final = kept.filter(({ el }) => !(isPointer(el) && !el.matches(INTERACTIVE) && kept.filter((o) => o.el !== el && el.contains(o.el)).length === 1));
    final.sort((a, b) => (Math.round(a.r.y / 12) - Math.round(b.r.y / 12)) || (a.r.x - b.r.x));
    const out = final.map(({ el, r }, i) => {
      const fp = fingerprint(el);
      return { index: i, role: fp.role, name: fp.name, tag: fp.tag, type: fp.type, rect: r, candidates: candidates(el), fingerprint: fp, frame: frameChain(winOf(el)) };
    });
    out.push({ _meta: true, dpr: devicePixelRatio, viewport: vp, url: location.href, title: document.title });
    return out;
  };

  // --- resolution (replay / self-heal) ------------------------------------------------

  const byRole = (r, name, doc) => {
    const want = clean(name).toLowerCase();
    return deepAll(doc).filter((el) => role(el) === r && isVisible(el) && clean(accName(el)).toLowerCase() === want);
  };
  const byText = (value, tag, doc) => {
    const want = clean(value);
    return deepAll(doc).filter((el) => (!tag || el.tagName.toLowerCase() === tag) && isVisible(el) && visText(el) === want);
  };
  const byLabel = (value, doc) => {
    const want = clean(value).toLowerCase();
    const out = [];
    for (const l of deepAll(doc).filter((e) => e.tagName === 'LABEL')) {
      if (clean(l.textContent).toLowerCase() !== want) continue;
      const target = l.htmlFor ? doc.getElementById(l.htmlFor) : l.querySelector('input, textarea, select');
      if (target && isVisible(target)) out.push(target);
    }
    return out;
  };
  const byXpath = (xp, doc) => {
    try {
      const r = doc.evaluate(xp, doc, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      const out = [];
      for (let i = 0; i < r.snapshotLength; i++) out.push(r.snapshotItem(i));
      return out;
    } catch (e) { return []; }
  };

  const query = (c, doc) => {
    switch (c.kind) {
      case 'css': return deepQuery(c.value, doc);
      case 'role': return byRole(c.role, c.name, doc);
      case 'text': return byText(c.value, c.tag, doc);
      case 'label': return byLabel(c.value, doc);
      case 'xpath': return byXpath(c.value, doc);
      default: return [];
    }
  };

  const score = (el, fp) => {
    if (!fp) return 1;
    const cur = fingerprint(el);
    let s = 0, n = 0;
    const cmp = (a, b, w) => { n += w; if (a && b && String(a).toLowerCase() === String(b).toLowerCase()) s += w; };
    cmp(cur.tag, fp.tag, 2);
    cmp(cur.role, fp.role, 2);
    cmp(cur.name, fp.name, 3);
    cmp(cur.text, fp.text, 2);
    cmp(cur.type, fp.type, 1);
    if (fp.href) cmp(cur.href, fp.href, 2);
    return n ? s / n : 0;
  };

  // Descend into the recorded iframe chain. Returns the document to query, or null.
  const frameDoc = (chain) => {
    let doc = document;
    for (const sel of chain || []) {
      const fe = deepQuery(sel, doc)[0];
      let inner = null;
      try { inner = fe && fe.contentDocument; } catch (e) { /* cross-origin */ }
      if (!inner) return null;
      doc = inner;
    }
    return doc;
  };

  // Walk candidates in order; accept the first that yields exactly one visible element, or the
  // best fingerprint match among several. Returns the element (internal) or a serialisable locator.
  const resolveEl = (cands, fp, opts = {}) => {
    const doc = frameDoc(opts.frame);
    if (!doc) return { found: false, reason: 'frame not found' };
    const minScore = opts.minScore ?? 0.5;
    let best = null;
    for (const c of cands) {
      // Hidden inputs (custom checkboxes/radios/file pickers) are legitimate targets when allowed.
      const all = query(c, doc);
      const els = opts.allowHidden ? (all.filter(isVisible).length ? all.filter(isVisible) : all.filter((e) => e.isConnected)) : all.filter(isVisible);
      if (!els.length) continue;
      for (const el of els) {
        const sc = els.length === 1 ? Math.max(score(el, fp), 0.6) : score(el, fp);
        if (!best || sc > best.score) best = { el, score: sc, kind: c.kind, value: c.value || `${c.role}:${c.name}` };
      }
      if (best && best.score >= 0.9) break;
    }
    if (!best || best.score < minScore) return { found: false, best: best ? { score: best.score, kind: best.kind } : null };
    return { found: true, ...best };
  };

  const resolve = (cands, fp, opts = {}) => {
    const r = resolveEl(cands, fp, opts);
    if (!r.found) return r;
    const el = r.el;
    if (opts.scroll !== false) {
      const vp = topViewport();
      const rr = rectOf(el);
      if (!inViewport(rr, vp) || rr.y < 0 || rr.y + rr.h > vp.h) el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    }
    const point = hitPoint(el, opts.offset);
    return { found: true, score: r.score, kind: r.kind, value: r.value, rect: rectOf(el), point, hittable: !!point, dpr: devicePixelRatio, tag: el.tagName.toLowerCase(), checked: typeof el.checked === 'boolean' ? el.checked : null, viewport: topViewport() };
  };

  // Used by replay for a last-resort programmatic click / value set when the element is covered.
  const act = (cands, fp, opts, action, value) => {
    const r = resolveEl(cands, fp, opts);
    if (!r.found) return r;
    const el = r.el;
    const fire = (type, init) => el.dispatchEvent(new (type.startsWith('mouse') ? MouseEvent : Event)(type, { bubbles: true, cancelable: true, composed: true, ...(init || {}) }));
    if (action === 'click') { el.focus && el.focus(); fire('mousedown'); fire('mouseup'); el.click ? el.click() : fire('click'); }
    else if (action === 'select') { el.value = value; fire('input'); fire('change'); return { found: true, ok: el.value === value }; }
    else if (action === 'setValue') {
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value');
      if (setter && setter.set) setter.set.call(el, value); else el.value = value;
      fire('input'); fire('change');
    }
    else if (action === 'scroll') { el.scrollTo(value.x || 0, value.y || 0); }
    else if (action === 'check') {
      if (el.checked !== !!value) {
        const lbl = el.labels && el.labels[0];
        if (lbl && isVisible(lbl)) lbl.click(); else el.click();
      }
      return { found: true, ok: el.checked === !!value, rect: rectOf(el) };
    }
    return { found: true, ok: true, rect: rectOf(el) };
  };

  // Element handle for CDP (DOM.setFileInputFiles etc): caller evaluates with returnByValue=false.
  const handle = (cands, fp, opts) => { const r = resolveEl(cands, fp, opts || {}); return r.found ? r.el : null; };

  const value = (cands, fp, opts) => { const r = resolveEl(cands, fp, opts || {}); return r.found ? (r.el.isContentEditable ? r.el.textContent : r.el.value) : null; };

  // --- optional visuals (these DO touch the DOM) ------------------------------------------

  let host = null;
  const ensureHost = () => {
    if (host && host.isConnected) return host._root;
    host = document.createElement('waystone-marks');
    host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
    host._root = host.attachShadow({ mode: 'closed' });
    document.documentElement.appendChild(host);
    return host._root;
  };
  const boxEl = (rect, color, label) => {
    const box = document.createElement('div');
    box.style.cssText = `position:absolute;left:${rect.x}px;top:${rect.y}px;width:${rect.w}px;height:${rect.h}px;border:2px solid ${color};box-sizing:border-box;border-radius:2px;`;
    if (label !== undefined) {
      const tag = document.createElement('span');
      tag.textContent = String(label);
      tag.style.cssText = `position:absolute;left:-2px;top:-18px;background:${color};color:#fff;font:bold 11px/16px system-ui,sans-serif;padding:0 4px;border-radius:2px;`;
      box.appendChild(tag);
    }
    return box;
  };
  const overlay = (marks, color = '#e5322d') => {
    clearOverlay();
    const root = ensureHost();
    for (const m of marks) if (!m._meta) root.appendChild(boxEl(m.rect, color, m.index));
    return marks.length;
  };
  const flash = (rect, ms = 500, color = '#e5322d') => {
    const root = ensureHost();
    const box = boxEl(rect, color);
    box.style.boxShadow = `0 0 0 3px ${color}55`;
    root.appendChild(box);
    setTimeout(() => box.remove(), ms);
  };
  const clearOverlay = () => { if (host) { host.remove(); host = null; } };

  // How long since the DOM last changed (ms). Replay waits for this to settle after an action.
  let lastMut = performance.now();
  try {
    new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.target && (m.target.nodeName === 'WAYSTONE-MARKS' || m.target.nodeName === 'WAYSTONE-PANEL')) continue; // our own overlays
        // Many apps rewrite attributes every frame with the same value; that is not "loading".
        if (m.type === 'attributes' && m.oldValue === m.target.getAttribute(m.attributeName)) continue;
        lastMut = performance.now();
        return;
      }
    }).observe(document, { subtree: true, childList: true, attributes: true, attributeOldValue: true, characterData: true });
  } catch (e) { /* no document yet */ }
  const quiet = () => performance.now() - lastMut;

  // Visible loading indicators: the page is telling us it is not ready.
  const BUSY = '[aria-busy="true"], progress, [role="progressbar"], .loading, .loader, .spinner, .skeleton, ' +
    '[class*="spinner" i], [class*="loading" i], [class*="skeleton" i], [id*="loading" i], [id*="spinner" i], [class*="shimmer" i]';
  // Loading indicators that were already showing before an action started are decoration (a
  // permanent brand animation, a skeleton for an unrelated panel), not evidence that the action
  // is still loading. We remember when each one was first seen visible.
  const busySeen = new WeakMap();
  const busy = (sinceMs) => {
    const out = [];
    const now = performance.now();
    for (const el of document.querySelectorAll(BUSY)) {
      if (!isVisible(el)) { busySeen.delete(el); continue; }
      const r = rectLocal(el);
      if (area(r) < 64) continue;
      const st = style(el);
      if (st.pointerEvents === 'none' && parseFloat(st.opacity) < 0.05) continue;
      if (!busySeen.has(el)) busySeen.set(el, now);
      if (sinceMs !== undefined && busySeen.get(el) < sinceMs) continue;  // was already there
      out.push((el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '')).slice(0, 60));
      if (out.length >= 5) break;
    }
    return out;
  };
  const nowMs = () => performance.now();

  const inertAt = (x, y) => {
    const top = fromPoint(document, x, y);
    return !top || !closestDeep(top, INTERACTIVE) && !isPointer(top);
  };

  // Let dispatched input pass through our overlays (a driven click under the HUD must reach the page).
  const ghost = (on) => { document.querySelectorAll('waystone-panel, waystone-marks').forEach((h) => { h.style.pointerEvents = on ? 'none' : (h.tagName === 'WAYSTONE-PANEL' ? 'auto' : 'none'); }); return true; };

  globalThis.__ws = { quiet, busy, nowMs, inertAt, ghost, cssPathOf: cssPath, addClosedRoots, scan, describe, resolve, resolveEl, act, handle, value, pick, rectOf, hittable, isVisible, overlay, flash, clearOverlay, frameChain, INTERACTIVE };
})();
