'use strict';
/* Sensors page: ISM-band environment sensors via a server-side rtl_433 job (S.job). Receive only. Only environment
   sensors are listed. There is deliberately no pager decoder (display-pager content is private and, in the US,
   generally off-limits under ECPA); the Radio survey only shows whether paging channels are busy. */

const AUTO_MHZ = 'auto';
const DP = { sensors: { mhz: AUTO_MHZ } };
const fmtJobMhz = m => (m === AUTO_MHZ ? '433.92 / 315 / 915 MHz (auto)' : `${m} MHz`);

function jobIs(kind) { return S.job && S.job.kind === kind; }
function jobRunning(kind) { return jobIs(kind) && S.job.enabled; }

function decoderControls(kind, presetKey) {
  const presets = (S.job?.presets || {})[presetKey] || [];
  const input = el('input', { class: 'field', id: `${kind}-mhz`, style: 'width:110px', inputmode: 'decimal', value: String(DP[kind].mhz), 'aria-label': 'Frequency in MHz, or auto',
    oninput: e => { DP[kind].mhz = e.target.value.replace(',', '.').trim().toLowerCase(); e.target.setCustomValidity(''); } });
  const sel = el('select', { class: 'field', id: `${kind}-preset`, 'aria-label': 'Preset frequency',
    onchange: e => { if (e.target.value) { DP[kind].mhz = e.target.value; input.value = e.target.value; input.setCustomValidity(''); } } });
  const fill = list => sel.replaceChildren(el('option', { value: '', text: 'Presets…' }), ...list.map(([f, t]) => el('option', { value: String(f), text: t })));
  fill(presets);
  const btn = el('button', { class: 'btn primary', id: `${kind}-toggle`, onclick: () => {
    if (jobRunning(kind)) radioSend('job_stop');
    else if (sdrCapability(kind).available) {
      const mhz = DP[kind].mhz === AUTO_MHZ ? AUTO_MHZ : frequencyValue(DP[kind].mhz);
      if (mhz == null) { input.setCustomValidity('Enter a frequency from 24 to 1766 MHz, or auto.'); input.reportValidity(); return; }
      if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'radio', cmd: 'job_start', kind, mhz }));
    }
  } });
  return { input, sel, btn, fill };
}

function decoderStatus(kind, dongle, btn, sel, statusEl) {
  const j = S.job || {};
  dongle.update();
  const running = jobRunning(kind);
  const cap = sdrCapability(kind);
  btn.disabled = !S.connected || (!running && !cap.available);
  btn.title = running ? '' : cap.available ? sdrTakeoverHint(kind) : cap.error;
  btn.textContent = running ? '■ Stop' : '▶ Start';
  btn.classList.toggle('danger', running); btn.classList.toggle('primary', !running);
  if (!sel.dataset.filled && j.presets?.[kind]) {
    sel.dataset.filled = '1';
    sel.replaceChildren(el('option', { value: '', text: 'Presets…' }), ...j.presets[kind].map(([f, t]) => el('option', { value: String(f), text: t })));
  }
  statusEl.textContent = !S.connected ? 'server disconnected' : running ? `running on ${fmtJobMhz(j.mhz)}` : !cap.available ? cap.error : 'idle';
}

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
    if (!devs.length) grid.append(el('div', { class: 'empty-state', style: 'grid-column:1/-1', text: !sdrCapability('sensors').available ? sdrCapability('sensors').error : jobRunning('sensors') ? `Listening on ${fmtJobMhz(j.mhz)}… sensors transmit every 30–60 seconds, so the first ones appear within a minute or two.` : 'Press ▶ Start to look for sensors around you.' }));
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
