'use strict';
/* Pager and 433 MHz sensor pages. Both use the SDR dongle through a server-side decoder job (S.job). Receive only.
   Pager text lives in server memory only and is never saved or forwarded. Only environment sensors are listed. */

const DP = { pager: { mhz: 929.6125, q: '', addr: null, paused: false, frozen: null }, sensors: { mhz: 433.92 } };
const JOB_TITLE = { pager: 'Pagers', sensors: 'Sensors' };

function jobIs(kind) { return S.job && S.job.kind === kind; }
function jobRunning(kind) { return jobIs(kind) && S.job.enabled; }

function decoderControls(kind, presetKey) {
  const presets = (S.job?.presets || {})[presetKey] || [];
  const input = el('input', { class: 'field', id: `${kind}-mhz`, style: 'width:110px', inputmode: 'decimal', value: String(DP[kind].mhz), 'aria-label': 'Frequency in MHz',
    oninput: e => { DP[kind].mhz = e.target.value.replace(',', '.'); e.target.setCustomValidity(''); } });
  const sel = el('select', { class: 'field', id: `${kind}-preset`, 'aria-label': 'Preset frequency',
    onchange: e => { if (e.target.value) { DP[kind].mhz = e.target.value; input.value = e.target.value; input.setCustomValidity(''); } } });
  const fill = list => sel.replaceChildren(el('option', { value: '', text: 'Presets…' }), ...list.map(([f, t]) => el('option', { value: String(f), text: t })));
  fill(presets);
  const btn = el('button', { class: 'btn primary', id: `${kind}-toggle`, onclick: () => {
    if (jobRunning(kind)) radioSend('job_stop');
    else if (sdrCapability(kind).available) {
      const mhz = frequencyValue(DP[kind].mhz);
      if (mhz == null) { input.setCustomValidity('Enter a frequency from 24 to 1766 MHz.'); input.reportValidity(); return; }
      if (kind === 'pager') { DP.pager.paused = false; DP.pager.frozen = null; DP.pager.addr = null; }
      if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'radio', cmd: 'job_start', kind, mhz }));
    }
  } });
  return { input, sel, btn, fill };
}

function decoderStatus(kind, dongle, btn, sel, statusEl) {
  const r = S.radio, j = S.job || {};
  dongle.update();
  const running = jobRunning(kind);
  const cap = sdrCapability(kind);
  btn.disabled = !S.connected || (!running && !cap.available);
  btn.title = cap.available || running ? '' : cap.error;
  btn.textContent = running ? '■ Stop' : '▶ Start';
  btn.classList.toggle('danger', running); btn.classList.toggle('primary', !running);
  if (!sel.dataset.filled && j.presets?.[kind === 'pager' ? 'pager' : 'sensors']) {
    sel.dataset.filled = '1';
    sel.replaceChildren(el('option', { value: '', text: 'Presets…' }), ...j.presets[kind].map(([f, t]) => el('option', { value: String(f), text: t })));
  }
  statusEl.textContent = !S.connected ? 'server disconnected' : running ? `running on ${j.mhz} MHz` : !cap.available ? cap.error : jobIs(kind) && j.count ? `stopped · showing ${j.count} earlier` : 'idle';
}

// ---- Pagers ----------------------------------------------------------------------------------------
const pagerPage = {
  build(sec) {
    const c = decoderControls('pager', 'pager');
    pagerPage.c = c;
    pagerPage.dongle = dongleChip();
    const sh = pageShell('Pagers', c.btn, c.input, el('span', { class: 'dim', text: 'MHz' }), c.sel, pagerPage.dongle, el('span', { id: 'pg-status', class: 'dim' }));
    const filter = el('input', { class: 'field', id: 'pg-q', type: 'search', placeholder: 'Filter text or address', style: 'flex:1', oninput: e => { DP.pager.q = e.target.value; pagerPage.render(); } });
    const pause = el('button', { class: 'btn small', id: 'pg-pause', onclick: () => { DP.pager.paused = !DP.pager.paused; DP.pager.frozen = DP.pager.paused ? (S.job?.messages || []).slice() : null; pagerPage.render(); }, text: '⏸ Pause' });
    sh.body.append(
      el('div', { class: 'note', style: 'margin-bottom:12px' }, el('b', {}, 'Handle with care. '),
        'Pager messages are broadcast in the clear, but they often contain private details (hospitals and clinics use pagers). They are kept in memory only, are not saved, are never sent over the mesh and are not passed to Claude. Do not act on or share what you read here.'),
      el('div', { id: 'pg-err' }),
      el('div', { class: 'decoder-page' },
        box('Live messages', el('div', {}, el('div', { class: 'row-flex', style: 'margin-bottom:8px' }, filter, pause,
          el('button', { class: 'btn small hidden', id: 'pg-clear', text: 'Clear address filter', onclick: () => { DP.pager.addr = null; pagerPage.render(); } }),
          el('span', { id: 'pg-count', class: 'dim' })), el('div', { class: 'feedlist', id: 'pg-feed' })), {}),
        el('div', { class: 'stack' },
          box('Busiest pagers', el('div', { id: 'pg-top', class: 'presets' })),
          box('Pager channels found', el('div', { id: 'pg-chans' })),
          box('How it works', el('div', { class: 'dim', style: 'font-size:12.5px', text: 'The SDR listens on one paging channel and decodes POCSAG (512/1200/2400 baud) and FLEX. Run a survey on the Radio page first: the “Pagers 929-932” band shows which channels are active near you. An address is just a pager’s number.' })))));
    sec.append(sh.root);
  },
  render() {
    const j = S.job || {};
    decoderStatus('pager', pagerPage.dongle, pagerPage.c.btn, pagerPage.c.sel, $('#pg-status'));
    $('#pg-err').replaceChildren(...(j.error && !j.enabled ? [el('div', { class: 'err-note', role: 'alert', style: 'margin-bottom:12px', text: j.error })] : []));
    const d = DP.pager, mine = jobIs('pager');
    const all = d.paused && d.frozen ? d.frozen : (mine ? j.messages || [] : []);
    const q = d.q.trim().toLowerCase();
    const msgs = all.filter(m => (!d.addr || m.address === d.addr) && (!q || `${m.text} ${m.address}`.toLowerCase().includes(q)));
    $('#pg-pause').textContent = d.paused ? '▶ Resume' : '⏸ Pause';
    $('#pg-clear').classList.toggle('hidden', !d.addr);
    $('#pg-count').textContent = mine ? `${j.count} decoded${d.addr ? ` · address ${d.addr}` : ''}` : '';
    const feed = $('#pg-feed');
    feed.replaceChildren();
    if (!msgs.length) feed.append(el('div', { class: 'empty-state', text: q || d.addr ? 'No messages match these filters.' : d.paused ? 'Feed paused. Resume to see new messages.' : !sdrCapability('pager').available ? sdrCapability('pager').error : jobRunning('pager') ? 'Listening… pager traffic is bursty; messages appear when someone is paged.' : 'Press ▶ Start to decode a paging channel.' }));
    for (const m of msgs) {
      feed.append(el('div', { class: 'pmsg' }, el('time', { text: new Date(m.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) }),
        el('span', { class: 'addr', title: 'Filter to this address', onclick: () => { d.addr = d.addr === m.address ? null : m.address; pagerPage.render(); } }, m.address, el('div', { class: 'dim', style: 'font-size:10.5px', text: `${m.proto} · ${m.kind}` })),
        el('span', { class: 'body' + (m.text ? '' : ' tone'), text: m.text || '(tone only, no text)' })));
    }
    const top = $('#pg-top');
    top.replaceChildren();
    for (const a of (mine ? j.top_addresses : []) || []) top.append(el('button', { class: 'chip' + (d.addr === a.address ? ' active' : ''), onclick: () => { d.addr = d.addr === a.address ? null : a.address; pagerPage.render(); }, text: `${a.address} · ${a.n}` }));
    if (!top.children.length) top.append(el('span', { class: 'dim', text: 'none yet' }));
    const ch = $('#pg-chans');
    ch.replaceChildren();
    const found = (S.radio?.channels || []).filter(c => c.band === 'Pagers 929-932').slice(0, 8);
    for (const c of found) ch.append(el('div', { class: 'crow', onclick: () => { d.mhz = String(c.mhz); $('#pager-mhz').value = c.mhz; }, title: 'Use this frequency' }, el('b', { text: c.mhz.toFixed(4) }), el('span', { class: 'x', text: 'paging channel' }), el('span', { class: 't', text: `${Math.round(c.duty * 100)}% · +${c.peak_db} dB` })));
    if (!found.length) ch.append(el('div', { class: 'empty-state' }, 'None surveyed yet. Start a survey on the ', el('a', { href: '#/radio', text: 'Radio' }), ' page.'));
  },
};
registerPage('pager', pagerPage);

// ---- 433 MHz sensors ------------------------------------------------------------------------------------
const sensorsPage = {
  build(sec) {
    const c = decoderControls('sensors', 'sensors');
    sensorsPage.c = c;
    sensorsPage.dongle = dongleChip();
    const sh = pageShell('Sensors', c.btn, c.input, el('span', { class: 'dim', text: 'MHz' }), c.sel, sensorsPage.dongle, el('span', { id: 'sn-status', class: 'dim' }));
    sh.body.append(
      el('div', { class: 'note', style: 'margin-bottom:12px' }, el('b', {}, 'Environment sensors only. '),
        'Backyard weather stations, thermometers and similar devices near you (rtl_433). Devices that identify vehicles (tyre-pressure sensors), remotes or door/alarm sensors are counted but never listed, because they would reveal what other people are doing.'),
      el('div', { id: 'sn-err' }), el('div', { class: 'grid', id: 'sn-grid' }));
    sec.append(sh.root);
  },
  render() {
    const j = S.job || {};
    decoderStatus('sensors', sensorsPage.dongle, sensorsPage.c.btn, sensorsPage.c.sel, $('#sn-status'));
    $('#sn-err').replaceChildren(...(j.error && !j.enabled ? [el('div', { class: 'err-note', role: 'alert', style: 'margin-bottom:12px', text: j.error })] : []));
    const grid = $('#sn-grid');
    grid.replaceChildren();
    const devs = jobIs('sensors') ? j.devices || [] : [];
    if (!devs.length) grid.append(el('div', { class: 'empty-state', style: 'grid-column:1/-1', text: !sdrCapability('sensors').available ? sdrCapability('sensors').error : jobRunning('sensors') ? `Listening on ${j.mhz} MHz… sensors transmit every 30–60 seconds, so the first ones appear within a minute or two.` : 'Press ▶ Start to look for sensors around you.' }));
    for (const v of devs) {
      const t = v.readings.temperature_C ?? v.readings.temperature_1_C;
      const f = t ? Math.round(t.value * 9 / 5 + 32) : null;
      const chips = el('div', { class: 'chips-r' });
      for (const [key, r] of Object.entries(v.readings)) if (key !== 'temperature_C') chips.append(el('span', {}, `${r.label} `, el('b', { text: `${r.value}${r.unit ? ' ' + r.unit : ''}` })));
      const hist = v.history?.temperature_C || v.history?.temperature_1_C || [];
      grid.append(el('div', { class: 'box dev', dataset: { key: v.key } },
        el('h3', {}, el('span', { text: `${v.model}${v.id ? ` · ${v.id}` : ''}${v.channel ? ` ch ${v.channel}` : ''}` }), el('span', { style: 'text-transform:none;letter-spacing:0', text: `${ago(v.last_seen)} ago` })),
        f != null ? el('div', { class: 'temp', style: `color:${tempColor(f)}` }, String(f), el('small', { text: `°F  (${t.value.toFixed(1)} °C)` })) : null,
        chips, hist.length > 2 ? spark(hist) : null,
        el('div', { class: 'dim', style: 'font-size:12px', text: `${v.count} reports${v.battery_ok === false ? ' · ⚠ low battery' : ''}${v.rssi != null ? ` · ${v.rssi} dB` : ''}` })));
    }
    if (jobIs('sensors') && j.ignored) grid.append(el('div', { class: 'dim', style: 'grid-column:1/-1;font-size:12px', text: `${j.ignored} transmissions from other kinds of devices were ignored (not listed, not stored).` }));
  },
};
registerPage('sensors', sensorsPage);
