// Waystone canvas display list. Installed in the MAIN world at document start during recording
// only (never on replay). Wraps CanvasRenderingContext2D so every text, rect, path and image the
// page draws is kept, with the transform in effect, as a list of primitives per canvas. The
// recorder reads it when the person clicks a canvas and identifies the primitive under the pointer.
//
// Kept small and quiet: non-enumerable, names/lengths copied, originals in a closure, the record
// stored under a Symbol so page code cannot stumble on it by enumeration.
(() => {
  if (globalThis.__ws_canvas_hooked) return;
  globalThis.__ws_canvas_hooked = true;
  const KEY = Symbol('waystone-display-list');
  const MAX = 20000;                      // primitives kept per canvas per frame
  const P = CanvasRenderingContext2D.prototype;
  const lists = new WeakMap();            // canvas -> { frame: [...], prev: [...], t }

  const listFor = (ctx) => {
    const c = ctx.canvas;
    if (!c) return null;
    let l = lists.get(c);
    if (!l) { l = { frame: [], prev: [], t: 0, cleared: 0 }; lists.set(c, l); c[KEY] = l; }
    return l;
  };
  // A frame boundary: the app cleared the canvas (clearRect covering it) or a new animation frame began.
  const rotate = (l) => { l.prev = l.frame; l.frame = []; l.t = performance.now(); l.cleared++; };

  const tf = (ctx) => { const m = ctx.getTransform(); return [m.a, m.b, m.c, m.d, m.e, m.f]; };
  const apply = (m, x, y) => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];
  const bboxOf = (m, x, y, w, h) => {
    const pts = [apply(m, x, y), apply(m, x + w, y), apply(m, x, y + h), apply(m, x + w, y + h)];
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)];
  };
  const push = (ctx, prim) => {
    const l = listFor(ctx);
    if (!l || l.frame.length >= MAX) return;
    prim.i = l.frame.length;
    l.frame.push(prim);
  };

  const wrap = (name, fn) => {
    const orig = P[name];
    if (typeof orig !== 'function') return;
    const w = function (...a) { try { fn(this, a); } catch (e) { /* never break the page */ } return orig.apply(this, a); };
    try {
      Object.defineProperty(w, 'name', { value: orig.name, configurable: true });
      Object.defineProperty(w, 'length', { value: orig.length, configurable: true });
      Object.defineProperty(w, 'toString', { value: () => orig.toString(), configurable: true, writable: true });
    } catch (e) { /* ignore */ }
    Object.defineProperty(P, name, { value: w, writable: true, configurable: true, enumerable: false });
  };

  // text: the most useful primitive. Measure with the context's current font to get a box.
  const text = (ctx, a, kind) => {
    const [str, x, y] = a;
    const s = String(str);
    let w = 0;
    try { w = ctx.measureText(s).width; } catch (e) { w = s.length * 7; }
    const size = parseFloat(ctx.font) || 12;
    const align = ctx.textAlign, base = ctx.textBaseline;
    let x0 = x; if (align === 'center') x0 = x - w / 2; else if (align === 'right' || align === 'end') x0 = x - w;
    let y0 = y - size * 0.8; if (base === 'middle') y0 = y - size / 2; else if (base === 'top' || base === 'hanging') y0 = y;
    const m = tf(ctx);
    push(ctx, { k: 'text', s, bbox: bboxOf(m, x0, y0, w, size), font: ctx.font, fill: kind === 'fill' ? String(ctx.fillStyle) : undefined, stroke: kind === 'stroke' ? String(ctx.strokeStyle) : undefined });
  };
  wrap('fillText', (ctx, a) => text(ctx, a, 'fill'));
  wrap('strokeText', (ctx, a) => text(ctx, a, 'stroke'));

  const rect = (ctx, a, kind) => {
    const [x, y, w, h] = a;
    const m = tf(ctx);
    if (kind === 'clear') {
      const c = ctx.canvas;
      if (w >= c.width * 0.9 && h >= c.height * 0.9) { rotate(listFor(ctx)); return; }
      push(ctx, { k: 'clear', bbox: bboxOf(m, x, y, w, h) });
      return;
    }
    push(ctx, { k: 'rect', bbox: bboxOf(m, x, y, w, h), fill: kind === 'fill' ? String(ctx.fillStyle) : undefined, stroke: kind === 'stroke' ? String(ctx.strokeStyle) : undefined });
  };
  wrap('fillRect', (ctx, a) => rect(ctx, a, 'fill'));
  wrap('strokeRect', (ctx, a) => rect(ctx, a, 'stroke'));
  wrap('clearRect', (ctx, a) => rect(ctx, a, 'clear'));

  // images: identity is the source (url, or a hash of the first pixels for bitmaps/canvases).
  const srcId = (img) => {
    try {
      if (img instanceof HTMLImageElement) return img.currentSrc || img.src || 'img';
      if (img instanceof HTMLVideoElement) return img.currentSrc || 'video';
      if (img instanceof HTMLCanvasElement) return 'canvas:' + (img.id || img.width + 'x' + img.height);
      if (typeof ImageBitmap !== 'undefined' && img instanceof ImageBitmap) return 'bitmap:' + img.width + 'x' + img.height;
    } catch (e) { /* ignore */ }
    return 'image';
  };
  wrap('drawImage', (ctx, a) => {
    const img = a[0];
    let dx, dy, dw, dh, sx = null, sy = null, sw = null, sh = null;
    if (a.length >= 9) { [sx, sy, sw, sh, dx, dy, dw, dh] = a.slice(1, 9); }
    else if (a.length >= 5) { [dx, dy, dw, dh] = a.slice(1, 5); }
    else { dx = a[1]; dy = a[2]; dw = img.width || img.videoWidth || 0; dh = img.height || img.videoHeight || 0; }
    const m = tf(ctx);
    push(ctx, { k: 'image', src: srcId(img), sprite: sx !== null ? [sx, sy, sw, sh] : null, bbox: bboxOf(m, dx, dy, dw, dh) });
  });

  // paths: track the current path's extent; fill/stroke commits it as one primitive.
  const paths = new WeakMap();
  const pathOf = (ctx) => { let p = paths.get(ctx); if (!p) { p = { pts: [] }; paths.set(ctx, p); } return p; };
  wrap('beginPath', (ctx) => { paths.set(ctx, { pts: [] }); });
  const addPt = (ctx, x, y) => { const p = pathOf(ctx); if (p.pts.length < 4000) p.pts.push(apply(tf(ctx), x, y)); };
  wrap('moveTo', (ctx, a) => addPt(ctx, a[0], a[1]));
  wrap('lineTo', (ctx, a) => addPt(ctx, a[0], a[1]));
  wrap('arc', (ctx, a) => { const [x, y, r] = a; addPt(ctx, x - r, y - r); addPt(ctx, x + r, y + r); });
  wrap('arcTo', (ctx, a) => { addPt(ctx, a[0], a[1]); addPt(ctx, a[2], a[3]); });
  wrap('ellipse', (ctx, a) => { const [x, y, rx, ry] = a; addPt(ctx, x - rx, y - ry); addPt(ctx, x + rx, y + ry); });
  wrap('quadraticCurveTo', (ctx, a) => { addPt(ctx, a[0], a[1]); addPt(ctx, a[2], a[3]); });
  wrap('bezierCurveTo', (ctx, a) => { addPt(ctx, a[0], a[1]); addPt(ctx, a[2], a[3]); addPt(ctx, a[4], a[5]); });
  wrap('rect', (ctx, a) => { addPt(ctx, a[0], a[1]); addPt(ctx, a[0] + a[2], a[1] + a[3]); });
  wrap('roundRect', (ctx, a) => { addPt(ctx, a[0], a[1]); addPt(ctx, a[0] + a[2], a[1] + a[3]); });
  const commit = (ctx, kind) => {
    const p = pathOf(ctx);
    if (!p.pts.length) return;
    const xs = p.pts.map((q) => q[0]), ys = p.pts.map((q) => q[1]);
    push(ctx, { k: 'path', n: p.pts.length, bbox: [Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)], fill: kind === 'fill' ? String(ctx.fillStyle) : undefined, stroke: kind === 'stroke' ? String(ctx.strokeStyle) : undefined });
  };
  wrap('fill', (ctx, a) => { if (!(a[0] instanceof Path2D)) commit(ctx, 'fill'); });
  wrap('stroke', (ctx, a) => { if (!(a[0] instanceof Path2D)) commit(ctx, 'stroke'); });

  // Frame boundaries for apps that never clearRect but redraw per rAF: rotate at each animation frame
  // if the canvas received draws since the last one.
  const raf = globalThis.requestAnimationFrame;
  if (typeof raf === 'function') {
    const tick = () => {
      try {
        for (const c of document.querySelectorAll('canvas')) {
          const l = c[KEY];
          if (l && l.frame.length && performance.now() - l.t > 0) { /* keep; cleared by clearRect or next large draw */ }
        }
      } catch (e) { /* ignore */ }
      raf.call(globalThis, tick);
    };
    raf.call(globalThis, tick);
  }

  // Reader for the isolated world: expose one function on the canvas element, under the symbol-keyed
  // record, that returns the current frame (or the previous one if the current is mid-draw).
  const read = (canvas, x, y, radius) => {
    const l = canvas[KEY];
    if (!l) return null;
    const frame = l.frame.length ? l.frame : l.prev;
    const hit = [];
    const near = [];
    for (const p of frame) {
      const [bx, by, bw, bh] = p.bbox;
      const inside = x >= bx && x <= bx + bw && y >= by && y <= by + bh;
      const d = inside ? 0 : Math.hypot(Math.max(bx - x, 0, x - (bx + bw)), Math.max(by - y, 0, y - (by + bh)));
      if (inside) hit.push(p);
      else if (d <= radius) near.push({ ...p, d });
    }
    near.sort((a, b) => a.d - b.d);
    // Everything else in the frame too (texts especially): headers can be far from the click.
    const rest = frame.filter((p) => p.k === 'text' && !hit.includes(p) && !near.includes(p));
    return { total: frame.length, hit, near: near.slice(0, 60).concat(rest), size: [canvas.width, canvas.height], cleared: l.cleared };
  };
  const all = (canvas) => { const l = canvas[KEY]; return l ? (l.frame.length ? l.frame : l.prev) : null; };
  Object.defineProperty(HTMLCanvasElement.prototype, Symbol.for('waystone.read'), { value: function (x, y, r) { return read(this, x, y, r || 60); }, configurable: true, enumerable: false });
  Object.defineProperty(HTMLCanvasElement.prototype, Symbol.for('waystone.all'), { value: function () { return all(this); }, configurable: true, enumerable: false });
})();
