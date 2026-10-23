'use strict';
/* SignalForge UI debug reporter. Does nothing unless the server was started with --debug-ui (the state frame
   then says debug_ui: true). It reports clicks, page/view changes, JavaScript errors, CSP violations and failed
   map tiles over the socket so an operator at the terminal can follow along. It never sends the token, the
   query string, or anything typed into fields. */
(() => {
  const queue = [];
  let lastKey = '';
  const clip = (v, n = 80) => String(v ?? '').replace(/\s+/g, ' ').trim().slice(0, n);
  const safeUrl = u => { try { const x = new URL(u, location.href); return x.origin === location.origin ? x.pathname : `${x.host}${x.pathname}`; } catch { return clip(u); } };

  function send(ev, data = {}) {
    const frame = { type: 'uidebug', ev, page: S.page, view: S.view, ...data };
    if (S.debugUi === false) return;
    if (S.debugUi !== true || ws?.readyState !== 1) { if (queue.length < 60) queue.push(frame); return; }
    ws.send(JSON.stringify(frame));
  }
  function flush() {
    if (S.debugUi !== true || ws?.readyState !== 1) { if (S.debugUi === false) queue.length = 0; return; }
    while (queue.length) ws.send(JSON.stringify(queue.shift()));
  }
  function describe(t) {
    const e = t.closest('button, a, [role=tab], tr[data-id], li[data-id], .leaflet-interactive, .chip, th, input, select, label') || t;
    const inMap = e.closest('.leaflet-container');
    return {
      tag: e.tagName.toLowerCase(), id: e.id || undefined, cls: clip(typeof e.className === 'string' ? e.className : e.getAttribute('class'), 60) || undefined,
      text: e.matches('input, textarea, select') ? undefined : clip(e.textContent, 50) || undefined,
      node: e.dataset?.id || e.getAttribute?.('data-id') || undefined,
      within: e.closest('[id]')?.id || undefined, map: inMap ? inMap.id : undefined,
    };
  }
  function mapInfo(sel) {
    const m = document.querySelector(sel);
    if (!m) return { present: false };
    const r = m.getBoundingClientRect(), imgs = [...m.querySelectorAll('.leaflet-tile')];
    return { present: true, visible: !!(r.width && r.height && m.offsetParent), w: Math.round(r.width), h: Math.round(r.height),
      tiles: imgs.length, loaded: imgs.filter(i => i.complete && i.naturalWidth).length,
      bases: [...new Set(imgs.map(i => safeUrl(i.src).split('/')[0]))] };
  }
  function snapshot() {
    return {
      hash: location.hash, w: innerWidth, h: innerHeight, dpr: devicePixelRatio,
      newNodesPage: typeof nodesMapInit === 'function', // false = the browser is running a cached old pages.js
      nodesMap: mapInfo('#np-map'), mainMap: mapInfo('#map'),
      nodes: S.nodes?.size, banner: clip($('#banner')?.textContent, 120) || undefined,
      pageTitle: clip(document.querySelector(`#page-${S.page} h1`)?.textContent, 40) || undefined,
    };
  }

  document.addEventListener('click', e => send('click', { x: e.clientX, y: e.clientY, target: describe(e.target) }), true);
  addEventListener('error', e => {
    const t = e.target;
    if (t && t.tagName === 'IMG') send('img-error', { src: safeUrl(t.src), cls: clip(t.className, 40) });
    else if (t === window || !t?.tagName) send('js-error', { msg: clip(e.message, 300), at: `${safeUrl(e.filename)}:${e.lineno}:${e.colno}`, stack: clip(e.error?.stack, 600) });
  }, true);
  addEventListener('unhandledrejection', e => send('js-error', { msg: clip(e.reason?.message || e.reason, 300), stack: clip(e.reason?.stack, 600) }));
  document.addEventListener('securitypolicyviolation', e => send('csp', { blocked: safeUrl(e.blockedURI), directive: e.violatedDirective }));
  const origError = console.error;
  console.error = (...a) => { send('console-error', { msg: clip(a.map(String).join(' '), 300) }); origError.apply(console, a); };

  setInterval(() => {
    flush();
    const key = `${S.page}|${S.view}|${location.hash}`;
    if (key !== lastKey && S.debugUi) { lastKey = key; setTimeout(() => send('page', snapshot()), 1500); }
  }, 1000);
  addEventListener('resize', () => { clearTimeout(send.rt); send.rt = setTimeout(() => send('resize', { w: innerWidth, h: innerHeight }), 800); });
  window.uiDebugSnapshot = () => send('snapshot', snapshot());
})();
