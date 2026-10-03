'use strict';

/* ---------- helpers ---------- */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const main = () => $('#main');

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch('/api' + path, opts);
  if (r.status === 401 && !['/login', '/session', '/setup'].includes(path)) {
    showAuth(false);
    throw new Error('Session expired - please sign in again');
  }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.blob();
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === 'string' ? d : d ? JSON.stringify(d) : `HTTP ${r.status}`);
  }
  return data;
}

function toast(msg, kind = '') {
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 3500);
}

async function busy(btn, fn) {
  if (btn) { btn.classList.add('busy'); btn.disabled = true; }
  try { return await fn(); }
  catch (e) { toast(e.message, 'bad'); }
  finally { if (btn) { btn.classList.remove('busy'); btn.disabled = false; } }
}

let modalCleanup = null;
function openModal(title, html, onMount, wide = false) {
  if (modalCleanup) { const c = modalCleanup; modalCleanup = null; c(); }
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = html;
  $('.modal-card').classList.toggle('wide', wide);
  $('#modal').classList.remove('hidden');
  if (onMount) onMount($('#modal-body'));
  const first = $('#modal-body input:not([type=hidden]):not([type=checkbox]), #modal-body select');
  if (first) first.focus();
}
function closeModal() {
  if (modalCleanup) { const c = modalCleanup; modalCleanup = null; c(); }
  $('#modal').classList.add('hidden');
  $('#modal-body').innerHTML = '';
}
const modalOpen = () => !$('#modal').classList.contains('hidden');
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal' || e.target.closest('[data-close]')) closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

function readForm(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name || el.disabled) continue;
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.type === 'radio') { if (el.checked) out[el.name] = el.value; }
    else if (el.type === 'number') out[el.name] = el.value === '' ? null : Number(el.value);
    else if (el.dataset.type === 'list') out[el.name] = el.value.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    else out[el.name] = el.value.trim();
  }
  return out;
}

const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString() : '-';
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}
function ago(ts) {
  if (!ts) return 'never';
  const d = Date.now() / 1000 - ts;
  if (d < 60) return `${Math.round(d)}s ago`;
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  return `${Math.round(d / 3600)}h ago`;
}
const badge = (text, kind = '', plain = false) => `<span class="badge ${kind} ${plain ? 'plain' : ''}">${esc(text)}</span>`;
const fmtNumber = n => n ? `+${n}` : '';

function extBadge(st) {
  const s = (st && st.state) || 'disabled';
  const map = { registered: 'ok', registering: 'warn', failed: 'bad', stopped: '', disabled: '' };
  return badge(s, map[s] ?? '');
}
const WA_STATES = {
  ready: ['connected', 'ok'], qr: ['scan the QR code', 'warn'], loading: ['loading', 'warn'], starting: ['starting', 'warn'],
  authenticated: ['signing in', 'warn'], disconnected: ['disconnected', 'bad'], failed: ['failed', 'bad'], stopped: ['stopped', ''],
  disabled: ['disabled', ''],
};
function waBadge(st) {
  const [text, kind] = WA_STATES[st?.state || 'disabled'] || [st?.state || '?', ''];
  return badge(text, kind);
}

/* ---------- state ---------- */
const state = { pbxs: [], extensions: [], wa: [], bridges: [], status: null, driver: 'chromium', contacts: {} };
async function loadAll() {
  const [pbxs, extensions, wa, bridges, status] = await Promise.all([
    api('GET', '/pbxs'), api('GET', '/extensions'), api('GET', '/wa'), api('GET', '/bridges'), api('GET', '/status')]);
  Object.assign(state, { pbxs, extensions, wa, bridges, status });
}
const pbxById = id => state.pbxs.find(p => p.id === id);
const extById = id => state.extensions.find(e => e.id === id);
const waById = id => state.wa.find(a => a.id === id);
const extLabel = e => e ? `${e.username}${pbxById(e.pbx_id) ? ` @ ${pbxById(e.pbx_id).name || pbxById(e.pbx_id).host}` : ''}` : 'missing';
const waLabel = a => a ? (a.name + (a.status?.me?.number ? ` (+${a.status.me.number})` : '')) : 'missing';

async function contactsOf(accountId, refresh = false) {
  const hit = state.contacts[accountId];
  if (hit && !refresh) return hit;
  const list = await api('GET', `/wa/${accountId}/contacts${refresh ? '?refresh=true' : ''}`);
  state.contacts[accountId] = list;
  return list;
}

/* ---------- text-to-speech voices ---------- */
let voicesAt = 0;
async function ensureVoices(force = false) {
  if (!force && state.voices && Date.now() - voicesAt < 60000) return state.voices;
  try { state.voices = await api('GET', '/tts/voices'); voicesAt = Date.now(); }
  catch (e) { state.voices = state.voices || { piper: { available: false }, espeak: { voices: [] } }; }
  return state.voices;
}
const QUALITY_RANK = { high: 0, medium: 1, low: 2, x_low: 3 };
function voiceFamilies() {
  const v = state.voices || { piper: {}, espeak: {} };
  const fams = {};
  const fam = id => (fams[id] ||= { id, label: '', natural: [], basic: [] });
  const installed = new Set((v.piper.installed || []).map(x => x.key));
  const seen = new Set();
  for (const c of [...(v.piper.installed || []), ...(v.piper.catalog || [])]) {
    if (seen.has(c.key)) continue;
    seen.add(c.key);
    const f = fam(c.family);
    if (!f.label && c.language) f.label = c.native && c.native !== c.language ? `${c.language} (${c.native})` : c.language;
    f.natural.push(Object.assign({}, c, { installed: installed.has(c.key) }));
  }
  for (const e of (v.espeak && v.espeak.voices) || []) {
    const f = fam(e.id.split('-')[0]);
    if (!f.label) f.label = e.name;
    f.basic.push(e);
  }
  for (const f of Object.values(fams)) {
    f.natural.sort((a, b) => (b.installed - a.installed) || ((QUALITY_RANK[a.quality] ?? 9) - (QUALITY_RANK[b.quality] ?? 9)) || a.key.localeCompare(b.key));
  }
  return fams;
}
const voiceFamilyOf = value => value.startsWith('piper:') ? value.slice(6).split('_')[0] : value.split('-')[0];
function voiceOptions(f, value, naturalOnly) {
  let html = '';
  if (f && f.natural.length) {
    html += '<optgroup label="Natural voices (Piper)">' + f.natural.map(c => {
      const id = 'piper:' + c.key;
      return `<option value="${esc(id)}" ${id === value ? 'selected' : ''}>${c.installed ? '✓' : '⬇'} ${esc(c.name)} · ${esc(c.country || c.code)} · ${esc(c.quality)}${c.installed ? '' : ` · ${c.size_mb} MB`}</option>`;
    }).join('') + '</optgroup>';
  }
  if (f && !naturalOnly && f.basic.length) {
    html += '<optgroup label="Basic voices (espeak-ng)">' + f.basic.map(e => `<option value="${esc(e.id)}" ${e.id === value ? 'selected' : ''}>${esc(e.name)}</option>`).join('') + '</optgroup>';
  }
  if (value && !html.includes(`value="${esc(value)}"`)) html = `<option value="${esc(value)}" selected>${esc(value)}</option>` + html;
  return html;
}
// allowDefault: a leading "use the default voice" choice (value "")
function voicePicker(name, value, { naturalOnly = false, allowDefault = false } = {}) {
  const fams = voiceFamilies();
  const list = Object.values(fams).filter(f => !naturalOnly || f.natural.length).sort((a, b) => (a.label || a.id).localeCompare(b.label || b.id));
  const def = state.voices?.default_voice || 'piper:en_US-lessac-medium';
  const shown = value || def;
  const cur = voiceFamilyOf(shown);
  const defOpt = allowDefault ? `<option value="" ${!value ? 'selected' : ''}>Default voice (${esc(def)})</option>` : '';
  return `<div class="voice-picker" data-voice-picker ${naturalOnly ? 'data-natural-only' : ''} ${allowDefault ? 'data-allow-default' : ''}>
      <select data-voice-lang aria-label="Language">${list.map(f => `<option value="${esc(f.id)}" ${f.id === cur ? 'selected' : ''}>${esc(f.label || f.id)}${f.natural.length ? ' ★' : ''}</option>`).join('')}</select>
      <select ${name ? `name="${name}"` : ''} data-voice aria-label="Voice">${defOpt}${voiceOptions(fams[cur], value, naturalOnly)}</select>
    </div><span class="hint" data-voice-hint></span>`;
}
function bindVoicePickers(root) {
  $$('[data-voice-picker]', root).forEach(p => {
    const lang = $('[data-voice-lang]', p), voice = $('[data-voice]', p), hint = p.nextElementSibling;
    const naturalOnly = p.hasAttribute('data-natural-only');
    const allowDefault = p.hasAttribute('data-allow-default');
    const showHint = () => {
      const v = voice.value || '';
      const f = voiceFamilies()[voiceFamilyOf(v)];
      const c = f && v.startsWith('piper:') ? f.natural.find(x => 'piper:' + x.key === v) : null;
      if (!hint) return;
      hint.textContent = !v ? 'Uses the default voice from the Voices page.'
        : c && !c.installed ? `Natural voice, downloads automatically (${c.size_mb} MB) when saved or previewed.`
        : v.startsWith('piper:') ? 'Natural voice (Piper), runs offline.'
        : 'Basic voice (espeak-ng). Languages marked ★ have natural voices.';
    };
    lang.onchange = () => {
      const f = voiceFamilies()[lang.value];
      const pick = f.natural.find(c => c.installed) || f.natural.find(c => c.quality === 'medium') || f.natural[0];
      const val = pick ? 'piper:' + pick.key : (naturalOnly ? '' : (f.basic[0] || {}).id || '');
      const def = state.voices?.default_voice || '';
      voice.innerHTML = (allowDefault ? `<option value="">Default voice (${esc(def)})</option>` : '') + voiceOptions(f, val, naturalOnly);
      voice.value = val;
      showHint();
    };
    voice.onchange = showHint;
    showHint();
  });
}
function voiceNeedsDownload(value) {
  if (!value || !value.startsWith('piper:')) return false;
  return !((state.voices && state.voices.piper.installed) || []).some(x => 'piper:' + x.key === value);
}
async function playPreview(container, payload) {
  const blob = await api('POST', '/tts/preview', payload);
  const audio = $('audio', container);
  audio.src = URL.createObjectURL(blob);
  audio.classList.remove('hidden');
  audio.play().catch(() => {});
}

/* ---------- router ---------- */
let pollTimer = null;
let pageCleanup = null;
function every(ms, fn) { clearInterval(pollTimer); pollTimer = setInterval(() => { if (!document.hidden) fn(); }, ms); }
const pages = {};

async function route() {
  clearInterval(pollTimer);
  if (pageCleanup) { const c = pageCleanup; pageCleanup = null; c(); }
  const [name, arg] = location.hash.replace(/^#\/?/, '').split('/');
  const page = pages[name || 'dashboard'] ? (name || 'dashboard') : 'dashboard';
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
  try { await pages[page](arg ? decodeURIComponent(arg) : undefined); }
  catch (e) { main().innerHTML = `<div class="empty"><p>${esc(e.message)}</p></div>`; }
}
window.addEventListener('beforeunload', () => { if (pageCleanup) pageCleanup(); });
window.addEventListener('hashchange', route);

/* ---------- calls (shared) ---------- */
const PHASES = {
  menu: ['caller is in the menu', 'info'], 'dial-number': ['caller is entering a number', 'info'], pin: ['entering the PIN', 'info'],
  dialing: ['WhatsApp is ringing', 'warn'], ringing: ['ringing extensions', 'warn'], accepting: ['answering WhatsApp', 'warn'],
  connected: ['connected', 'ok'], starting: ['starting', ''], ended: ['ended', ''],
};
function sessionCard(s) {
  const [ptext, pkind] = PHASES[s.phase] || [s.phase, ''];
  const dur = s.connected_at ? ` ${fmtDur(Date.now() / 1000 - s.connected_at)}` : '';
  const toWa = s.kind === 'pbx-to-wa';
  const sip = s.sip || {};
  const who = s.peer.name || fmtNumber(s.peer.number) || '…';
  const pbxSide = toWa ? (sip.remote_display || sip.remote || 'PBX caller') : (sip.remote || (s.ringing || []).join(', ') || (s.targets || []).join(', '));
  return `<div class="card">
    <div class="card-row"><h2>${esc(toWa ? pbxSide : who)} → ${esc(toWa ? who : pbxSide)}</h2>${badge(ptext + dur, pkind)}</div>
    <div class="muted">${toWa ? 'PBX → WhatsApp' : 'WhatsApp → PBX'} · bridge ${esc(s.bridge.name || s.bridge.id)}</div>
    <dl class="kv">
      <dt>WhatsApp</dt><dd>${s.wa_call ? `${esc(s.wa_call.peer_label)} · ${esc(s.wa_call.state)}` : '-'}</dd>
      <dt>Codec</dt><dd>${esc(sip.codec || '-')}</dd>
      <dt>RTP</dt><dd class="mono">${sip.rtp ? `${sip.rtp.rx_packets} in / ${sip.rtp.tx_packets} out` : '-'}</dd>
      ${s.audio ? `<dt>Audio</dt><dd class="mono">${Math.round(s.audio.rx_bytes / 8000)} s from WhatsApp · ${Math.round(s.audio.tx_bytes / 8000)} s to WhatsApp${s.audio.error ? ` · <span class="error">${esc(s.audio.error)}</span>` : ''}</dd>` : ''}
    </dl>
    <div class="card-actions"><button class="btn danger" data-hangup="${esc(s.id)}">Hang up</button></div>
  </div>`;
}
function bindHangups(root) {
  $$('[data-hangup]', root).forEach(b => b.onclick = () => busy(b, async () => {
    await api('POST', `/calls/${b.dataset.hangup}/hangup`);
    toast('Call ended');
  }));
}

/* ---------- dashboard ---------- */
pages.dashboard = async () => {
  const render = () => {
    const st = state.status;
    const extStates = Object.values(st.extensions);
    const registered = extStates.filter(e => e.state === 'registered').length;
    const linked = state.wa.filter(a => a.status?.state === 'ready').length;
    const steps = [
      [state.wa.length && linked, `<a href="#/whatsapp">Link WhatsApp</a>: add an account and scan its QR code with WhatsApp on your phone (Linked devices).`],
      [state.extensions.length, `<a href="#/pbx">Add your PBX and an extension</a> for wa2sip to register (e.g. 1009).`],
      [state.bridges.length, `<a href="#/bridges">Create a bridge</a>: pick contacts (or all of WhatsApp), which extensions ring for their calls, and the menu you hear when you call the extension.`],
    ];
    main().innerHTML = `
      <div class="page-head"><div><h1>Dashboard</h1><p class="muted">WhatsApp calls ⇄ your PBX</p></div></div>
      <div class="tiles">
        <div class="card tile"><div class="label">WhatsApp linked</div><div class="value">${linked}/${state.wa.length}</div></div>
        <div class="card tile"><div class="label">Extensions registered</div><div class="value">${registered}/${extStates.length}</div></div>
        <div class="card tile"><div class="label">Bridges</div><div class="value">${state.bridges.filter(b => b.enabled).length}</div></div>
        <div class="card tile"><div class="label">Active calls</div><div class="value">${st.sessions.length}</div></div>
      </div>
      ${steps.some(s => !s[0]) ? `<div class="card section" style="margin-top:0"><h2>Getting started</h2>
        <ol class="steps">${steps.map(([done, html]) => `<li class="${done ? 'done' : ''}"><span>${html}</span></li>`).join('')}</ol></div>` : ''}
      <div class="section"><h2>Active calls</h2>
        ${st.sessions.length ? `<div class="grid">${st.sessions.map(sessionCard).join('')}</div>` : '<div class="empty"><p>No calls right now.</p></div>'}</div>
      <div class="section"><h2>WhatsApp</h2>
        ${state.wa.length ? `<div class="grid">${state.wa.map(a => `<div class="card"><div class="card-row"><h2>${esc(a.name)}</h2>${waBadge(a.status)}</div>
          <div class="muted">${a.status?.me ? esc(`${a.status.me.name || ''} +${a.status.me.number || ''}`) : esc(a.status?.detail || '')}</div>
          ${a.status?.state === 'qr' ? '<div class="card-actions"><a class="btn primary small" href="#/whatsapp">Show QR code</a></div>' : ''}</div>`).join('')}</div>`
          : '<div class="empty"><p>No WhatsApp account linked yet.</p><a class="btn primary" href="#/whatsapp">Link WhatsApp</a></div>'}</div>
      <div class="section"><h2>Extensions</h2>
        ${state.extensions.length ? `<div class="table-wrap"><table><tr><th>Extension</th><th>PBX</th><th>Status</th><th>Bridge</th></tr>
          ${state.extensions.map(e => {
            const s = st.extensions[e.id] || {};
            const b = state.bridges.find(x => x.extension_id === e.id);
            return `<tr><td>${esc(e.username)}</td><td>${esc(pbxById(e.pbx_id)?.name || pbxById(e.pbx_id)?.host || '?')}</td>
              <td>${extBadge(s)} ${s.error ? `<span class="small error">${esc(s.error)}</span>` : ''}</td><td>${b ? esc(b.name || b.id) : '<span class="muted">-</span>'}</td></tr>`;
          }).join('')}</table></div>` : '<div class="empty"><p>No extensions yet.</p><a class="btn primary" href="#/pbx">Add PBX</a></div>'}</div>`;
    bindHangups(main());
  };
  await loadAll();
  render();
  every(2500, async () => {
    if (modalOpen()) return;
    [state.status, state.wa] = await Promise.all([api('GET', '/status'), api('GET', '/wa')]);
    render();
  });
};

/* ---------- WhatsApp accounts ---------- */
function waCard(a) {
  const st = a.status || {};
  const me = st.me;
  let body = '';
  if (st.state === 'qr' && st.qr) {
    body = `<div class="qr-box"><img src="${esc(st.qr)}" alt="WhatsApp QR code" data-qr="${esc(a.id)}">
      <div><ol><li>Open WhatsApp on your phone</li><li>Settings → <b>Linked devices</b> → <b>Link a device</b></li><li>Scan this code</li></ol>
      <p class="muted small">Or <a href="#" data-pair="${esc(a.id)}">link with your phone number</a> instead.</p></div></div>`;
    if (st.pairing_code) body += `<p>Pairing code: <span class="pairing-code">${esc(st.pairing_code)}</span></p>`;
  } else if (st.state === 'ready') {
    body = `<dl class="kv"><dt>Account</dt><dd>${esc(me?.name || '')} ${me?.number ? esc('+' + me.number) : ''}</dd>
      <dt>Linked since</dt><dd>${esc(ago(st.since))}</dd>
      ${st.calls?.length ? `<dt>Calls</dt><dd>${st.calls.map(c => `${esc(c.peer_label)} · ${esc(c.state)}`).join('<br>')}</dd>` : ''}
      ${st.diag && st.diag.enable_web_calling === false ? `<dt>Calling</dt><dd>${badge('not enabled by WhatsApp for this session', 'bad')}</dd>` : ''}
      </dl>`;
  } else {
    body = `<p class="muted">${esc(st.detail || '')}${['loading', 'starting', 'authenticated'].includes(st.state) ? ' <span class="pulse">…</span>' : ''}</p>`;
  }
  const bridges = state.bridges.filter(b => b.wa_account_id === a.id);
  return `<div class="card" data-wa="${esc(a.id)}">
    <div class="card-row"><h2>${esc(a.name)}</h2>${a.enabled ? waBadge(st) : badge('disabled')}</div>
    <div class="muted small">${bridges.length ? `Bridges: ${bridges.map(b => esc(b.name || b.id)).join(', ')}` : 'No bridge uses this account yet'}
      · unbridged calls: ${a.unrouted === 'reject' ? 'declined' : 'keep ringing on the phone'}</div>
    ${body}
    <div class="card-actions">
      ${st.state === 'ready' ? `<button class="btn small" data-act="contacts">Contacts</button>` : ''}
      ${state.driver === 'fake' && st.state === 'ready' ? `<button class="btn small" data-act="simulate">Simulate incoming call</button>` : ''}
      <button class="btn small" data-act="view">Browser view</button>
      <button class="btn small" data-act="diag">Diagnostics</button>
      <button class="btn small" data-act="edit">Edit</button>
      <button class="btn small" data-act="restart">Restart</button>
      ${st.state === 'ready' ? `<button class="btn small danger" data-act="logout">Unlink</button>` : ''}
      <button class="btn small danger" data-act="delete">Delete</button>
    </div></div>`;
}

pages.whatsapp = async () => {
  await loadAll();
  const draw = () => {
    main().innerHTML = `
      <div class="page-head"><div><h1>WhatsApp</h1><p class="muted">Each account runs its own WhatsApp Web (a linked device, like WhatsApp on a computer).</p></div>
        <button class="btn primary" id="wa-add">+ Add WhatsApp account</button></div>
      ${state.wa.length ? `<div class="grid">${state.wa.map(waCard).join('')}</div>`
        : '<div class="empty"><p>Add an account, then scan its QR code with WhatsApp on your phone.</p></div>'}
      <div class="callout small section">Calls use WhatsApp Web's own calling (WhatsApp Web supports voice calls since July 2026). Only 1:1 voice calls are bridged;
        group calls and a second call while one is running are left to your phone.</div>`;
    $('#wa-add').onclick = () => waForm();
    $$('[data-wa]').forEach(card => {
      const a = waById(card.dataset.wa);
      $$('[data-act]', card).forEach(btn => btn.onclick = () => waAction(btn, a));
      const pair = $('[data-pair]', card);
      if (pair) pair.onclick = e => { e.preventDefault(); pairingDialog(a); };
    });
  };
  draw();
  let last = JSON.stringify(state.wa.map(a => [a.status?.state, a.status?.qr, a.status?.pairing_code, a.status?.calls?.length, a.status?.me]));
  every(2000, async () => {
    if (modalOpen()) return;
    state.wa = await api('GET', '/wa');
    const now = JSON.stringify(state.wa.map(a => [a.status?.state, a.status?.qr, a.status?.pairing_code, a.status?.calls?.length, a.status?.me]));
    if (now !== last) { last = now; draw(); }
  });
};

function waForm(a) {
  openModal(a ? 'Edit WhatsApp account' : 'Add WhatsApp account', `
    <form id="wa-form">
      <label>Name<input name="name" required value="${esc(a?.name || 'WhatsApp')}" placeholder="My phone"></label>
      <label>Incoming WhatsApp calls that no bridge takes
        <select name="unrouted">
          <option value="ignore" ${a?.unrouted !== 'reject' ? 'selected' : ''}>Leave them alone (they ring on your phone as usual)</option>
          <option value="reject" ${a?.unrouted === 'reject' ? 'selected' : ''}>Decline them</option>
        </select></label>
      <label class="check"><input type="checkbox" name="enabled" ${a?.enabled !== false ? 'checked' : ''}> Enabled</label>
      ${a ? '' : '<p class="muted small">After saving, a QR code appears on this page within a few seconds. Scan it in WhatsApp → Linked devices.</p>'}
      <div class="error" id="wa-error"></div>
      <div class="form-actions"><button class="btn primary" type="submit">${a ? 'Save' : 'Add account'}</button></div>
    </form>`, body => {
    const form = $('#wa-form', body);
    form.onsubmit = e => {
      e.preventDefault();
      busy($('[type=submit]', form), async () => {
        try { a ? await api('PUT', `/wa/${a.id}`, readForm(form)) : await api('POST', '/wa', readForm(form)); }
        catch (err) { $('#wa-error').textContent = err.message; return; }
        closeModal();
        pages.whatsapp();
      });
    };
  });
}

function pairingDialog(a) {
  openModal('Link with phone number', `
    <form id="pair-form">
      <p class="muted" style="margin-top:0">WhatsApp sends a notification to your phone; enter the 8-character code shown here in WhatsApp → Linked devices → Link with phone number.</p>
      <label>Your WhatsApp phone number <span class="hint">with country code, e.g. 905321234567</span><input name="phone" required inputmode="tel"></label>
      <div id="pair-result"></div>
      <div class="form-actions"><button class="btn primary" type="submit">Get code</button></div>
    </form>`, body => {
    const form = $('#pair-form', body);
    form.onsubmit = e => {
      e.preventDefault();
      busy($('[type=submit]', form), async () => {
        const r = await api('POST', `/wa/${a.id}/pairing-code`, { phone: form.phone.value });
        $('#pair-result').innerHTML = `<p>Enter this code on your phone:</p><span class="pairing-code">${esc(r.code)}</span>`;
      });
    };
  });
}

async function waAction(btn, a) {
  const act = btn.dataset.act;
  if (act === 'edit') return waForm(a);
  if (act === 'contacts') return contactsDialog(a);
  if (act === 'view') return browserView(a);
  if (act === 'diag') return busy(btn, async () => {
    const d = await api('GET', `/wa/${a.id}/diagnostics`);
    openModal(`Diagnostics · ${a.name}`, `<p class="muted" style="margin-top:0">What WhatsApp Web reports about calling in this browser. <b>enable_web_calling</b> must be true after linking.</p>
      <pre class="json">${esc(JSON.stringify(d, null, 2))}</pre>`, null, true);
  });
  if (act === 'restart') return busy(btn, async () => { await api('POST', `/wa/${a.id}/restart`); toast('Restarting WhatsApp Web…'); });
  if (act === 'simulate') return simulateDialog(a);
  if (act === 'logout') {
    if (!confirm(`Unlink "${a.name}" from WhatsApp? You will need to scan a new QR code.`)) return;
    return busy(btn, async () => { await api('POST', `/wa/${a.id}/logout`); toast('Unlinked'); });
  }
  if (act === 'delete') {
    if (!confirm(`Delete "${a.name}"? It is unlinked from WhatsApp and its browser profile is removed.`)) return;
    return busy(btn, async () => { await api('DELETE', `/wa/${a.id}`); toast('Account deleted'); pages.whatsapp(); });
  }
}

function contactsDialog(a) {
  openModal(`Contacts · ${a.name}`, `
    <div class="toolbar"><input id="c-search" placeholder="Search name or number…" style="flex:1"><button class="btn small" id="c-refresh">Refresh</button></div>
    <div id="c-list" class="section" style="margin-top:12px"><p class="muted">Loading…</p></div>`, async body => {
    const draw = list => {
      const q = $('#c-search', body).value.toLowerCase();
      const rows = list.filter(c => !q || `${c.name} ${c.pushname} ${c.number}`.toLowerCase().includes(q)).slice(0, 300);
      $('#c-list', body).innerHTML = `<div class="table-wrap"><table><tr><th>Name</th><th>Number</th><th>WhatsApp id</th></tr>
        ${rows.map(c => `<tr><td>${esc(c.name || c.pushname || '-')}${c.isBusiness ? ' ' + badge('business', 'info', true) : ''}</td><td>${esc(fmtNumber(c.number))}</td><td class="mono small">${esc(c.id)}</td></tr>`).join('')}
        </table></div><p class="muted small">${list.length} contacts${rows.length < list.length ? `, ${rows.length} shown` : ''}</p>`;
    };
    let list = [];
    const load = async refresh => { list = await contactsOf(a.id, refresh); draw(list); };
    $('#c-search', body).oninput = () => draw(list);
    $('#c-refresh', body).onclick = e => busy(e.target, () => load(true));
    try { await load(false); } catch (e) { $('#c-list', body).innerHTML = `<p class="error">${esc(e.message)}</p>`; }
  }, true);
}

function browserView(a) {
  openModal(`Browser view · ${a.name}`, `
    <p class="muted" style="margin-top:0">What WhatsApp Web currently shows in wa2sip's browser (for troubleshooting).</p>
    <img class="shot" id="shot" alt="WhatsApp Web screenshot">
    <div class="form-actions"><label class="check" style="margin:0 auto 0 0"><input type="checkbox" id="shot-auto"> Auto refresh</label><button class="btn" id="shot-refresh">Refresh</button></div>`, body => {
    const img = $('#shot', body);
    const load = () => { img.src = `/api/wa/${a.id}/screenshot.jpg?t=${Date.now()}`; };
    img.onerror = () => toast('No browser view available (is the account running?)', 'bad');
    $('#shot-refresh', body).onclick = load;
    let t = null;
    $('#shot-auto', body).onchange = e => { clearInterval(t); t = e.target.checked ? setInterval(load, 2000) : null; };
    modalCleanup = () => clearInterval(t);
    load();
  }, true);
}

function simulateDialog(a) {
  openModal('Simulate an incoming WhatsApp call', `
    <form id="sim-form">
      <p class="muted" style="margin-top:0">Simulated accounts only (WA2SIP_WA_DRIVER=fake). The fake caller echoes what it hears.</p>
      <label>Caller number<input name="number" value="491701111001"></label>
      <div class="form-actions"><button class="btn" type="button" id="sim-hangup">Caller hangs up</button><button class="btn primary" type="submit">Call</button></div>
    </form>`, body => {
    const form = $('#sim-form', body);
    $('#sim-hangup', body).onclick = e => busy(e.target, () => api('POST', `/wa/${a.id}/simulate-hangup`));
    form.onsubmit = e => { e.preventDefault(); busy($('[type=submit]', form), async () => { await api('POST', `/wa/${a.id}/simulate-call`, readForm(form)); toast('Ringing…', 'ok'); }); };
  });
}

/* ---------- PBX & extensions ---------- */
pages.pbx = async () => {
  await loadAll();
  const render = () => {
    const st = state.status;
    main().innerHTML = `
      <div class="page-head"><div><h1>PBX &amp; extensions</h1><p class="muted">wa2sip registers these extensions on your PBX (FreePBX, Asterisk, …) like a desk phone would.</p></div>
        <button class="btn primary" id="pbx-add">+ Add PBX</button></div>
      ${state.pbxs.length ? state.pbxs.map(p => {
        const exts = state.extensions.filter(e => e.pbx_id === p.id);
        return `<div class="card section" style="margin-top:0;margin-bottom:16px" data-pbx="${esc(p.id)}">
          <div class="card-row"><div><h2>${esc(p.name || p.host)}</h2><div class="muted small mono">${esc(p.host)}:${esc(p.port)}${p.domain ? ` · domain ${esc(p.domain)}` : ''}</div></div>
            <div class="toolbar">${p.enabled ? '' : badge('disabled')}<button class="btn small" data-pact="ext">+ Extension</button><button class="btn small" data-pact="edit">Edit</button><button class="btn small danger" data-pact="delete">Delete</button></div></div>
          ${exts.length ? `<div class="table-wrap" style="margin-top:12px"><table><tr><th>Extension</th><th>Status</th><th>Registered until</th><th>Used by</th><th></th></tr>
            ${exts.map(e => {
              const s = st.extensions[e.id] || {};
              const b = state.bridges.find(x => x.extension_id === e.id);
              return `<tr data-ext="${esc(e.id)}"><td><b>${esc(e.username)}</b> ${e.display_name ? `<span class="muted">${esc(e.display_name)}</span>` : ''}</td>
                <td>${e.enabled ? extBadge(s) : badge('disabled')} ${s.error ? `<div class="small error">${esc(s.error)}</div>` : ''}</td>
                <td class="small">${s.registered_until ? esc(new Date(s.registered_until * 1000).toLocaleTimeString()) : '-'}</td>
                <td>${b ? esc(b.name || b.id) : '<span class="muted">-</span>'}</td>
                <td><button class="btn small" data-eact="register">Re-register</button> <button class="btn small" data-eact="edit">Edit</button> <button class="btn small danger" data-eact="delete">Delete</button></td></tr>`;
            }).join('')}</table></div>` : '<p class="muted">No extensions on this PBX yet.</p>'}
        </div>`;
      }).join('') : '<div class="empty"><p>Add the PBX that wa2sip should register its extensions on.</p></div>'}
      <div class="callout small">Create the extensions on your PBX first (e.g. FreePBX → Applications → Extensions → PJSIP). Turn on <b>Trust RPID/PAI</b> (Asterisk: <code>trust_id_inbound=yes</code>)
        so phones show the WhatsApp caller's name and number instead of the extension's own.</div>`;
    $('#pbx-add').onclick = () => pbxForm();
    $$('[data-pbx]').forEach(card => {
      const p = pbxById(card.dataset.pbx);
      $$('[data-pact]', card).forEach(btn => btn.onclick = () => {
        if (btn.dataset.pact === 'edit') return pbxForm(p);
        if (btn.dataset.pact === 'ext') return extForm(null, p.id);
        if (confirm(`Delete PBX ${p.name || p.host}?`)) busy(btn, async () => { await api('DELETE', `/pbxs/${p.id}`); toast('PBX deleted'); pages.pbx(); });
      });
      $$('[data-ext]', card).forEach(row => {
        const e = extById(row.dataset.ext);
        $$('[data-eact]', row).forEach(btn => btn.onclick = () => {
          if (btn.dataset.eact === 'edit') return extForm(e, e.pbx_id);
          if (btn.dataset.eact === 'register') return busy(btn, async () => { await api('POST', `/extensions/${e.id}/register`); toast('Registering…'); });
          if (confirm(`Delete extension ${e.username}?`)) busy(btn, async () => { await api('DELETE', `/extensions/${e.id}`); toast('Extension deleted'); pages.pbx(); });
        });
      });
    });
  };
  render();
  every(3000, async () => { if (modalOpen()) return; state.status = await api('GET', '/status'); render(); });
};

function pbxForm(p) {
  openModal(p ? 'Edit PBX' : 'Add PBX', `
    <form id="pbx-form">
      <label>Name <span class="hint">optional</span><input name="name" value="${esc(p?.name)}" placeholder="FreePBX"></label>
      <div class="row">
        <label>Host / IP<input name="host" required value="${esc(p?.host)}" placeholder="192.168.1.10"></label>
        <label>SIP port (UDP)<input name="port" type="number" min="1" max="65535" value="${esc(p?.port ?? 5060)}"></label>
      </div>
      <div class="row">
        <label>SIP domain <span class="hint">optional, defaults to the host</span><input name="domain" value="${esc(p?.domain)}"></label>
        <label>Registration expiry (s)<input name="expires" type="number" min="60" max="3600" value="${esc(p?.expires ?? 300)}"></label>
      </div>
      <label class="check"><input type="checkbox" name="enabled" ${p?.enabled !== false ? 'checked' : ''}> Enabled</label>
      <div class="error" id="pbx-error"></div>
      <div class="form-actions"><button class="btn primary" type="submit">${p ? 'Save' : 'Add PBX'}</button></div>
    </form>`, body => {
    const form = $('#pbx-form', body);
    form.onsubmit = e => {
      e.preventDefault();
      busy($('[type=submit]', form), async () => {
        let saved;
        try { saved = p ? await api('PUT', `/pbxs/${p.id}`, readForm(form)) : await api('POST', '/pbxs', readForm(form)); }
        catch (err) { $('#pbx-error').textContent = err.message; return; }
        closeModal();
        await pages.pbx();
        if (!p) extForm(null, saved.id);
      });
    };
  });
}

function extForm(e, pbxId) {
  openModal(e ? `Edit extension ${e.username}` : 'Add extension', `
    <form id="ext-form" autocomplete="off">
      <label>PBX<select name="pbx_id">${state.pbxs.map(p => `<option value="${esc(p.id)}" ${p.id === (e?.pbx_id || pbxId) ? 'selected' : ''}>${esc(p.name || p.host)}</option>`).join('')}</select></label>
      <div class="row">
        <label>Extension number<input name="username" required value="${esc(e?.username)}" placeholder="1009"></label>
        <label>Password / secret<input name="password" type="password" autocomplete="new-password" placeholder="${e?.password_set ? '(unchanged)' : ''}"></label>
      </div>
      <div class="row">
        <label>Auth user <span class="hint">optional, defaults to the number</span><input name="auth_username" value="${esc(e?.auth_username)}"></label>
        <label>Display name <span class="hint">optional</span><input name="display_name" value="${esc(e?.display_name)}" placeholder="WhatsApp"></label>
      </div>
      <label class="check"><input type="checkbox" name="enabled" ${e?.enabled !== false ? 'checked' : ''}> Enabled</label>
      <div class="error" id="ext-error"></div>
      <div class="form-actions"><button class="btn primary" type="submit">${e ? 'Save' : 'Add extension'}</button></div>
    </form>`, body => {
    const form = $('#ext-form', body);
    form.onsubmit = ev => {
      ev.preventDefault();
      busy($('[type=submit]', form), async () => {
        try { e ? await api('PUT', `/extensions/${e.id}`, readForm(form)) : await api('POST', '/extensions', readForm(form)); }
        catch (err) { $('#ext-error').textContent = err.message; return; }
        closeModal();
        toast(e ? 'Extension saved' : 'Extension added - registering…', 'ok');
        pages.pbx();
      });
    };
  });
}

/* ---------- bridges ---------- */
// Same rules as Bridge.menu_codes() on the server: explicit keys first, then 1-9 (10-99 for big menus).
function menuCodes(contacts, dialNumber, dialDigit) {
  const menu = contacts.filter(c => c.in_menu);
  const taken = new Set(menu.filter(c => c.digit).map(c => c.digit));
  if (dialNumber) taken.add(dialDigit);
  const two = menu.length + (dialNumber ? 1 : 0) > 9;
  const pool = two ? Array.from({ length: 90 }, (_, i) => String(i + 10)) : '123456789'.split('');
  const free = pool.filter(p => ![...taken].some(t => t.startsWith(p) || p.startsWith(t)));
  return contacts.map(c => !c.in_menu ? '' : c.digit || free.shift() || '');
}

function bridgeFlows(b) {
  const ext = extById(b.extension_id);
  const lines = [];
  if (b.outbound_enabled) {
    const menu = b.menu.map(m => `<span class="badge plain">${esc(m.code)}</span> ${esc(m.name)}`).join(' &nbsp;');
    const direct = b.menu.length === 1 && !b.dial_number && !b.menu_always;
    const lockOut = b.pin && b.pin_outbound ? '🔒 PIN, then ' : '';
    lines.push(`<div><span class="dir">Call ${esc(ext?.username || '?')}</span><span>${lockOut}${direct ? `calls ${esc(b.menu[0].name)} on WhatsApp` : (menu || '<span class="muted">empty menu</span>') + (b.dial_number ? ` &nbsp;<span class="badge plain">${esc(b.dial_digit)}</span> any number` : '')}</span></div>`);
  }
  if (b.inbound_enabled) {
    const who = b.all_contacts ? 'Any WhatsApp caller' : b.contacts.filter(c => c.inbound).length ? b.contacts.filter(c => c.inbound).map(c => esc(c.name || fmtNumber(c.number))).join(', ') : '';
    if (who) lines.push(`<div><span class="dir">WhatsApp call</span><span>${who}${b.all_contacts && b.contacts.some(c => c.inbound) ? ' (listed contacts first)' : ''} → rings <b>${esc((b.ring_targets || []).join(', ') || '?')}</b>${b.pin && b.pin_inbound ? ' · 🔒 PIN to answer' : ''}</span></div>`);
    const overrides = b.contacts.filter(c => c.inbound && c.ring.length);
    if (overrides.length) lines.push(`<div><span class="dir"></span><span class="small muted">${overrides.map(c => `${esc(c.name || c.number)} → ${esc(c.ring.join(', '))}`).join(' · ')}</span></div>`);
  }
  return `<div class="flow">${lines.join('') || '<span class="muted">Both directions are turned off.</span>'}</div>`;
}

pages.bridges = async () => {
  await loadAll();
  const render = () => {
    const sessions = state.status.sessions;
    main().innerHTML = `
      <div class="page-head"><div><h1>Bridges</h1><p class="muted">A bridge connects a WhatsApp account (all of it or chosen contacts) with an extension.</p></div>
        <button class="btn primary" id="bridge-add" ${!state.wa.length || !state.extensions.length ? 'disabled title="Add a WhatsApp account and an extension first"' : ''}>+ New bridge</button></div>
      ${state.bridges.length ? `<div class="grid">${state.bridges.map(b => {
        const active = sessions.filter(s => s.bridge.id === b.id).length;
        const wa = waById(b.wa_account_id);
        return `<div class="card" data-bridge="${esc(b.id)}">
          <div class="card-row"><h2>${esc(b.name || 'Bridge')}</h2>${!b.enabled ? badge('disabled') : active ? badge(`${active} call${active > 1 ? 's' : ''}`, 'info') : badge('ready', 'ok')}</div>
          <div class="muted small">${esc(waLabel(wa))} ⇄ extension ${esc(extLabel(extById(b.extension_id)))}</div>
          ${bridgeFlows(b)}
          <div class="card-actions"><button class="btn small" data-bact="edit">Edit</button><button class="btn small" data-bact="preview">▶ Menu</button><button class="btn small danger" data-bact="delete">Delete</button></div>
          <audio controls class="hidden" style="width:100%;height:32px;margin-top:8px"></audio>
        </div>`;
      }).join('')}</div>` : `<div class="empty"><p>No bridges yet.</p>${state.wa.length && state.extensions.length ? '' : '<p class="small">You need a WhatsApp account and an extension first.</p>'}</div>`}`;
    const add = $('#bridge-add');
    if (add) add.onclick = () => bridgeForm();
    $$('[data-bridge]').forEach(card => {
      const b = state.bridges.find(x => x.id === card.dataset.bridge);
      $$('[data-bact]', card).forEach(btn => btn.onclick = () => {
        if (btn.dataset.bact === 'edit') return bridgeForm(b);
        if (btn.dataset.bact === 'preview') return busy(btn, async () => {
          if (voiceNeedsDownload(b.voice || state.voices?.default_voice)) toast('Downloading the natural voice first - this can take a minute…');
          await playPreview(card, { bridge: b });
        });
        if (confirm(`Delete bridge ${b.name || b.id}?`)) busy(btn, async () => { await api('DELETE', `/bridges/${b.id}`); toast('Bridge deleted'); pages.bridges(); });
      });
    });
  };
  await ensureVoices();
  render();
  every(3000, async () => { if (modalOpen()) return; state.status = await api('GET', '/status'); render(); });
};

const BRIDGE_DEFAULTS = {
  name: '', enabled: true, all_contacts: false, contacts: [],
  inbound_enabled: true, ring_targets: [], ring_timeout: 30, caller_name: 'WA {name}', caller_number: 'whatsapp',
  announce: true, announce_text: 'WhatsApp call from {name}.', reject_unanswered: true,
  outbound_enabled: true, allowed_callers: [], menu_always: false, dial_number: false, dial_digit: '0',
  national_prefix: '0', country_code: '', ringback: true, dial_timeout: 60, after_call: 'hangup', menu_digit: '*',
  max_call_seconds: 14400, ivr_greeting: 'Welcome.', ivr_option_text: 'Press {digit} for {name}.',
  ivr_dial_text: 'Press {digit} to dial a phone number.', ivr_enter_text: 'Enter the phone number with the country code, then press the hash key.',
  ivr_invalid_text: 'Sorry, that is not a valid choice.', ivr_calling_text: 'Calling {name}.', ivr_failed_text: '{name} is not available right now.',
  ivr_busy_text: '{name} is busy.', ivr_not_on_wa_text: 'This number is not on WhatsApp.',
  ivr_wa_busy_text: 'WhatsApp is already in another call. Please try again later.', ivr_offline_text: 'WhatsApp is not connected right now.',
  ivr_goodbye_text: 'Goodbye.', ivr_repeats: 3, ivr_timeout: 6, voice: '', speed: 0,
  pin: '', pin_outbound: true, pin_inbound: true, pin_attempts: 3,
  pin_prompt_text: 'Please enter your PIN, then press the hash key.', pin_wrong_text: 'Wrong PIN.',
};

function contactRow(c, code) {
  return `<div class="contact-row" data-contact>
    <input data-f="digit" maxlength="3" inputmode="numeric" value="${esc(c.digit)}" placeholder="${esc(code || '-')}" title="Menu key (empty = automatic)" aria-label="Menu key">
    <input data-f="name" value="${esc(c.name)}" placeholder="name spoken in the menu" aria-label="Name">
    <span class="who" title="${esc(c.wa_id)}">${esc(fmtNumber(c.number) || c.wa_id)}</span>
    <input data-f="ring" value="${esc((c.ring || []).join(', '))}" placeholder="default" title="Extensions to ring for this contact's WhatsApp calls (empty = the bridge's)" aria-label="Rings">
    <input type="checkbox" data-f="inbound" ${c.inbound !== false ? 'checked' : ''} title="Ring the PBX when this contact calls on WhatsApp" aria-label="Incoming">
    <input type="checkbox" data-f="in_menu" ${c.in_menu !== false ? 'checked' : ''} title="Offer this contact in the menu" aria-label="In menu">
    <span class="tools"><button type="button" class="icon-btn" data-move="-1" aria-label="Move up">↑</button><button type="button" class="icon-btn" data-move="1" aria-label="Move down">↓</button><button type="button" class="icon-btn" data-rm aria-label="Remove">&times;</button></span>
    <input type="hidden" data-f="wa_id" value="${esc(c.wa_id)}"><input type="hidden" data-f="number" value="${esc(c.number)}">
  </div>`;
}

const textField = (label, name, value, hint = '') =>
  `<label>${label}${hint ? ` <span class="hint">${hint}</span>` : ''}<input name="${name}" value="${esc(value)}"></label>`;

async function bridgeForm(b) {
  await ensureVoices();
  const x = Object.assign({}, BRIDGE_DEFAULTS, b || {});
  x.contacts = (x.contacts || []).map(c => Object.assign({ digit: '', ring: [], inbound: true, in_menu: true, wa_id: '', number: '', name: '' }, c));
  const waId = x.wa_account_id || state.wa[0]?.id;
  const usedExt = new Set(state.bridges.filter(o => o.id !== b?.id).map(o => o.extension_id));
  const extId = x.extension_id || (state.extensions.find(e => !usedExt.has(e.id)) || state.extensions[0])?.id;
  openModal(b ? `Edit bridge ${b.name || ''}` : 'New bridge', `
    <form id="bridge-form" autocomplete="off">
      <div class="row">
        <label>Name <span class="hint">optional</span><input name="name" value="${esc(x.name)}" placeholder="Family"></label>
        <label class="check" style="align-self:end"><input type="checkbox" name="enabled" ${x.enabled ? 'checked' : ''}> Enabled</label>
      </div>
      <div class="row">
        <label>WhatsApp account<select name="wa_account_id">${state.wa.map(a => `<option value="${esc(a.id)}" ${a.id === waId ? 'selected' : ''}>${esc(waLabel(a))}</option>`).join('')}</select></label>
        <label>Extension <span class="hint">the number you call to reach WhatsApp</span><select name="extension_id">${state.extensions.map(e => `<option value="${esc(e.id)}" ${e.id === extId ? 'selected' : ''}>${esc(extLabel(e))}${usedExt.has(e.id) ? ' · used by another bridge' : ''}</option>`).join('')}</select></label>
      </div>

      <fieldset><legend>WhatsApp contacts</legend>
        <label class="check"><input type="checkbox" name="all_contacts" ${x.all_contacts ? 'checked' : ''}> Bridge the whole WhatsApp account
          <span class="hint">(every incoming WhatsApp call that no other bridge lists)</span></label>
        <p class="hint muted small" style="margin:0 0 8px">Contacts listed here ring their extensions when they call you on WhatsApp, and appear in the menu when you call the extension, in this order.</p>
        <div class="contact-list">
          <div class="contact-head"><span>Key</span><span>Name</span><span>WhatsApp</span><span>Rings</span><span title="Incoming WhatsApp calls">In</span><span title="In the menu">Menu</span><span></span></div>
          <div id="contact-rows"></div>
        </div>
        <div class="picker">
          <div class="toolbar"><input id="pick-q" placeholder="Search your WhatsApp contacts…" style="flex:1;min-width:200px">
            <input id="pick-num" placeholder="…or a number: 905321234567" style="width:210px" inputmode="tel"><button type="button" class="btn small" id="pick-add-num">Add number</button></div>
          <div class="picker-results" id="pick-results"></div>
        </div>
      </fieldset>

      <fieldset><legend>PIN</legend>
        <div class="row">
          <label>PIN <span class="hint">4-16 digits; empty = no PIN</span><input name="pin" value="${esc(x.pin)}" inputmode="numeric" pattern="[0-9]{4,16}" maxlength="16" autocomplete="off" placeholder="none"></label>
          <label>Attempts per call<input name="pin_attempts" type="number" min="1" max="10" value="${esc(x.pin_attempts)}"></label>
        </div>
        <div data-pin>
          <label class="check"><input type="checkbox" name="pin_outbound" ${x.pin_outbound ? 'checked' : ''}> Ask callers of the extension, before the menu</label>
          <label class="check"><input type="checkbox" name="pin_inbound" ${x.pin_inbound ? 'checked' : ''}> Ask whoever picks up an incoming WhatsApp call, before it is answered
            <span class="hint">(the other extensions keep ringing meanwhile)</span></label>
          <div class="row">
            ${textField('PIN prompt', 'pin_prompt_text', x.pin_prompt_text)}
            ${textField('Wrong PIN', 'pin_wrong_text', x.pin_wrong_text)}
          </div>
          <p class="hint muted small" style="margin:0 0 8px">Callers type the PIN and press #. After 10 wrong PINs in a row the bridge's PIN locks for a minute, doubling up to an hour.</p>
        </div>
      </fieldset>

      <fieldset><legend>Incoming WhatsApp calls → ring extensions</legend>
        <label class="check"><input type="checkbox" name="inbound_enabled" ${x.inbound_enabled ? 'checked' : ''}> Ring the PBX when a bridged contact calls on WhatsApp</label>
        <div data-in>
          <div class="row">
            <label>Ring these extensions <span class="hint">comma separated; the first to answer gets the call</span><input name="ring_targets" data-type="list" value="${esc(x.ring_targets.join(', '))}" placeholder="1001, 1002"></label>
            <label>Ring for (s)<input name="ring_timeout" type="number" min="5" max="300" value="${esc(x.ring_timeout)}"></label>
          </div>
          <div class="row">
            ${textField('Caller name shown', 'caller_name', x.caller_name, '{name}, {number}')}
            <label>Caller number shown<select name="caller_number">
              <option value="whatsapp" ${x.caller_number === 'whatsapp' ? 'selected' : ''}>The WhatsApp number</option>
              <option value="extension" ${x.caller_number === 'extension' ? 'selected' : ''}>The bridge's extension</option></select></label>
          </div>
          <label class="check"><input type="checkbox" name="announce" ${x.announce ? 'checked' : ''}> Announce the caller when you answer</label>
          ${textField('Announcement', 'announce_text', x.announce_text, '{name}, {number}')}
          <label class="check"><input type="checkbox" name="reject_unanswered" ${x.reject_unanswered ? 'checked' : ''}> Decline the WhatsApp call when nobody answers (otherwise it keeps ringing on the phone)</label>
        </div>
      </fieldset>

      <fieldset><legend>Calling the extension → menu → WhatsApp</legend>
        <label class="check"><input type="checkbox" name="outbound_enabled" ${x.outbound_enabled ? 'checked' : ''}> Answer calls to the extension and connect them to WhatsApp</label>
        <div data-out>
          <p class="hint muted small" style="margin:0 0 10px">You hear e.g. <i>"Welcome. Press 1 for Ali. Press 2 for Ayşe."</i>, press a key and wa2sip calls that contact on WhatsApp.
            With only one contact, it is called directly.</p>
          <div class="row">
            <label>Voice / language ${voicePicker('voice', x.voice, { allowDefault: true })}</label>
            <label>Speed (words per minute) <span class="hint">0 = default</span><input name="speed" type="number" min="0" max="400" step="5" value="${esc(x.speed)}"></label>
          </div>
          ${textField('Greeting', 'ivr_greeting', x.ivr_greeting)}
          ${textField('Each contact', 'ivr_option_text', x.ivr_option_text, '{digit} and {name} are filled in')}
          <div class="row three">
            <label class="check" style="align-self:end"><input type="checkbox" name="dial_number" ${x.dial_number ? 'checked' : ''}> Dial any number</label>
            <label>with key<input name="dial_digit" maxlength="1" value="${esc(x.dial_digit)}"></label>
            <label class="check" style="align-self:end"><input type="checkbox" name="menu_always" ${x.menu_always ? 'checked' : ''}> Menu even for one contact</label>
          </div>
          <div class="row" data-dialnum>
            <label>Your country code <span class="hint">e.g. 90, 49, 1</span><input name="country_code" value="${esc(x.country_code)}" inputmode="numeric"></label>
            <label>National prefix <span class="hint">a number starting with it gets the country code instead</span><input name="national_prefix" value="${esc(x.national_prefix)}" inputmode="numeric"></label>
          </div>
          <details class="more"><summary>More prompts, timing and access</summary>
            ${textField('Calling', 'ivr_calling_text', x.ivr_calling_text, '{name}; empty = silent')}
            ${textField('Not reachable / declined', 'ivr_failed_text', x.ivr_failed_text, '{name}')}
            ${textField('Busy', 'ivr_busy_text', x.ivr_busy_text, '{name}')}
            ${textField('Dial a number', 'ivr_dial_text', x.ivr_dial_text, '{digit}')}
            ${textField('Enter the number', 'ivr_enter_text', x.ivr_enter_text)}
            ${textField('Number not on WhatsApp', 'ivr_not_on_wa_text', x.ivr_not_on_wa_text)}
            ${textField('Invalid key', 'ivr_invalid_text', x.ivr_invalid_text)}
            ${textField('WhatsApp in another call', 'ivr_wa_busy_text', x.ivr_wa_busy_text)}
            ${textField('WhatsApp not connected', 'ivr_offline_text', x.ivr_offline_text)}
            ${textField('Goodbye', 'ivr_goodbye_text', x.ivr_goodbye_text)}
            <div class="row three">
              <label>Wait for a key (s)<input name="ivr_timeout" type="number" min="2" max="30" value="${esc(x.ivr_timeout)}"></label>
              <label>Repeat menu (times)<input name="ivr_repeats" type="number" min="1" max="10" value="${esc(x.ivr_repeats)}"></label>
              <label>WhatsApp rings for (s)<input name="dial_timeout" type="number" min="10" max="180" value="${esc(x.dial_timeout)}"></label>
            </div>
            <div class="row three">
              <label>Back-to-menu key <span class="hint">during a call</span><input name="menu_digit" maxlength="1" value="${esc(x.menu_digit)}"></label>
              <label>When the WhatsApp call ends<select name="after_call">
                <option value="hangup" ${x.after_call === 'hangup' ? 'selected' : ''}>Hang up</option>
                <option value="menu" ${x.after_call === 'menu' ? 'selected' : ''}>Back to the menu</option></select></label>
              <label class="check" style="align-self:end"><input type="checkbox" name="ringback" ${x.ringback ? 'checked' : ''}> Ring tone while WhatsApp rings</label>
            </div>
            <div class="row">
              <label>Allowed callers <span class="hint">PBX extensions that may use this bridge; empty = everyone</span><input name="allowed_callers" data-type="list" value="${esc(x.allowed_callers.join(', '))}" placeholder="1001, 1002"></label>
              <label>Max call length (s)<input name="max_call_seconds" type="number" min="60" max="86400" value="${esc(x.max_call_seconds)}"></label>
            </div>
          </details>
          <div class="toolbar" style="margin:4px 0 12px"><button type="button" class="btn small" id="menu-preview">▶ Preview menu</button><audio controls class="hidden" style="height:32px;flex:1;min-width:200px"></audio></div>
        </div>
      </fieldset>
      <div class="error" id="bridge-error"></div>
      <div class="form-actions"><button type="submit" class="btn primary">${b ? 'Save' : 'Create bridge'}</button></div>
    </form>`, body => {
    const form = $('#bridge-form', body);
    let contacts = x.contacts.slice();
    const collect = () => {
      contacts = $$('[data-contact]', form).map(r => ({
        wa_id: $('[data-f=wa_id]', r).value, number: $('[data-f=number]', r).value,
        name: $('[data-f=name]', r).value.trim(), digit: $('[data-f=digit]', r).value.trim(),
        ring: $('[data-f=ring]', r).value.split(/[\s,]+/).map(s => s.trim()).filter(Boolean),
        inbound: $('[data-f=inbound]', r).checked, in_menu: $('[data-f=in_menu]', r).checked,
      }));
      return contacts;
    };
    const drawContacts = () => {
      const codes = menuCodes(contacts, form.dial_number.checked, form.dial_digit.value || '0');
      $('#contact-rows', form).innerHTML = contacts.length ? contacts.map((c, i) => contactRow(c, codes[i])).join('')
        : '<p class="muted small" style="margin:4px 0">No contacts yet - search below, or tick "whole WhatsApp account".</p>';
      $$('[data-contact]', form).forEach((row, i) => {
        $('[data-rm]', row).onclick = () => { collect(); contacts.splice(i, 1); drawContacts(); drawPicker(); };
        $$('[data-move]', row).forEach(btn => btn.onclick = () => {
          collect();
          const j = i + Number(btn.dataset.move);
          if (j < 0 || j >= contacts.length) return;
          [contacts[i], contacts[j]] = [contacts[j], contacts[i]];
          drawContacts();
        });
        $$('input', row).forEach(inp => inp.onchange = () => { collect(); drawContacts(); });
      });
    };
    const has = c => contacts.some(o => (c.id && (o.wa_id === c.id || (c.lid && o.wa_id === c.lid))) || (c.number && o.number === c.number));
    let all = [];
    const drawPicker = () => {
      const q = $('#pick-q', form).value.trim().toLowerCase();
      const box = $('#pick-results', form);
      if (!all.length) return;
      const hits = all.filter(c => !q || `${c.name || ''} ${c.pushname || ''} ${c.number || ''}`.toLowerCase().includes(q)).slice(0, q ? 50 : 8);
      box.innerHTML = hits.map((c, i) => `<button type="button" data-i="${i}" ${has(c) ? 'disabled' : ''}><span>${esc(c.name || c.pushname || '?')}</span><span class="muted small">${esc(fmtNumber(c.number) || c.id)}${has(c) ? ' · added' : ''}</span></button>`).join('')
        || '<p class="muted small" style="margin:4px">No match. Add the number on the right instead.</p>';
      $$('[data-i]', box).forEach(btn => btn.onclick = () => {
        const c = hits[Number(btn.dataset.i)];
        collect();
        contacts.push({ wa_id: c.id, number: c.number || '', name: c.name || c.pushname || '', digit: '', ring: [], inbound: true, in_menu: true });
        drawContacts(); drawPicker();
      });
    };
    const loadContacts = async () => {
      const box = $('#pick-results', form);
      const acc = waById(form.wa_account_id.value);
      if (acc?.status?.state !== 'ready') { all = []; box.innerHTML = '<p class="muted small" style="margin:4px">Link this WhatsApp account to search its contacts. You can still add numbers.</p>'; return; }
      box.innerHTML = '<p class="muted small" style="margin:4px">Loading contacts…</p>';
      try { all = await contactsOf(acc.id); drawPicker(); }
      catch (e) { box.innerHTML = `<p class="error small" style="margin:4px">${esc(e.message)}</p>`; }
    };
    $('#pick-q', form).oninput = drawPicker;
    $('#pick-add-num', form).onclick = () => {
      const n = $('#pick-num', form).value.replace(/\D/g, '');
      if (n.length < 7) return toast('Enter the full number with country code', 'bad');
      collect();
      if (contacts.some(c => c.number === n)) return toast('Already added');
      const known = all.find(c => c.number === n);
      contacts.push({ wa_id: known?.id || '', number: n, name: known?.name || known?.pushname || '', digit: '', ring: [], inbound: true, in_menu: true });
      $('#pick-num', form).value = '';
      drawContacts(); drawPicker();
    };
    form.wa_account_id.onchange = loadContacts;
    const sync = () => {
      $('[data-in]', form).classList.toggle('hidden', !form.inbound_enabled.checked);
      $('[data-out]', form).classList.toggle('hidden', !form.outbound_enabled.checked);
      $('[data-dialnum]', form).classList.toggle('hidden', !form.dial_number.checked);
      form.announce_text.disabled = !form.announce.checked;
      $('[data-pin]', form).classList.toggle('hidden', !form.pin.value.trim());
    };
    ['inbound_enabled', 'outbound_enabled', 'dial_number', 'announce'].forEach(n => form[n].addEventListener('change', sync));
    form.pin.addEventListener('input', sync);
    form.dial_number.addEventListener('change', () => { collect(); drawContacts(); });
    form.dial_digit.addEventListener('change', () => { collect(); drawContacts(); });
    sync();
    drawContacts();
    loadContacts();
    bindVoicePickers(form);
    const payload = () => {
      const data = readForm(form);
      data.contacts = collect();
      return data;
    };
    $('#menu-preview', form).onclick = e => busy(e.target, async () => {
      const data = payload();
      if (voiceNeedsDownload(data.voice || state.voices?.default_voice)) toast('Downloading the natural voice first - this can take a minute…');
      await playPreview(e.target.parentElement, { bridge: data });
    });
    form.onsubmit = e => {
      e.preventDefault();
      const data = payload();
      busy($('[type=submit]', form), async () => {
        try {
          if (b) await api('PUT', `/bridges/${b.id}`, data);
          else await api('POST', '/bridges', data);
        } catch (err) { $('#bridge-error', form).textContent = err.message; return; }
        closeModal();
        toast(b ? 'Bridge saved' : 'Bridge created', 'ok');
        pages.bridges();
      });
    };
  }, true);
}

/* ---------- calls ---------- */
pages.calls = async () => {
  const render = data => {
    main().innerHTML = `
      <div class="page-head"><div><h1>Calls</h1></div><button class="btn small" id="clear-history">Clear history</button></div>
      <div class="section" style="margin-top:0"><h2>Active</h2>
        ${data.active.length ? `<div class="grid">${data.active.map(sessionCard).join('')}</div>` : '<div class="empty"><p>No active calls.</p></div>'}</div>
      <div class="section"><h2>History</h2>
        ${data.history.length ? `<div class="table-wrap"><table>
          <tr><th>Time</th><th>Direction</th><th>WhatsApp</th><th>PBX</th><th>Bridge</th><th>Duration</th><th>Result</th></tr>
          ${data.history.map(h => `<tr><td>${esc(fmtTime(h.started_at))}</td><td>${h.direction === 'wa-to-pbx' ? '↓ from WhatsApp' : '↑ to WhatsApp'}</td>
            <td>${esc(h.wa_peer || '-')}${h.wa_number && !String(h.wa_peer).includes(h.wa_number) ? ` <span class="muted small">+${esc(h.wa_number)}</span>` : ''}</td>
            <td>${esc(h.pbx_party || '-')}</td><td>${esc(h.bridge || '-')}</td>
            <td>${h.connected_at ? fmtDur(h.duration) : '-'}</td><td>${esc(h.result)}</td></tr>`).join('')}
        </table></div>` : '<div class="empty"><p>No calls yet.</p></div>'}</div>`;
    bindHangups(main());
    $('#clear-history').onclick = e => { if (confirm('Clear the call history?')) busy(e.target, async () => { await api('DELETE', '/calls/history'); render(await api('GET', '/calls')); }); };
  };
  render(await api('GET', '/calls'));
  every(2000, async () => render(await api('GET', '/calls')));
};

/* ---------- voices ---------- */
pages.voices = async () => {
  await ensureVoices(true);
  const s = await api('GET', '/settings');
  main().innerHTML = `
    <div class="page-head"><div><h1>Voices</h1><p class="muted">The menu and announcements are spoken by Piper, a natural-sounding neural voice that runs offline on this server.</p></div></div>
    <div class="card"><h2>Default voice</h2>
      <form id="voice-form" style="margin-top:10px">
        <div class="row">
          <label>Voice / language ${voicePicker('default_voice', s.default_voice)}</label>
          <label>Speed (words per minute)<input name="default_speed" type="number" min="60" max="400" step="5" value="${esc(s.default_speed)}"></label>
        </div>
        <label>Ring tone callers hear while WhatsApp rings<select name="ringback_style">
          <option value="eu" ${s.ringback_style === 'eu' ? 'selected' : ''}>Europe / Turkey (425 Hz)</option>
          <option value="us" ${s.ringback_style === 'us' ? 'selected' : ''}>North America (440+480 Hz)</option>
          <option value="uk" ${s.ringback_style === 'uk' ? 'selected' : ''}>UK (400+450 Hz, double ring)</option></select></label>
        <div class="form-actions"><button class="btn primary" type="submit">Save</button></div>
      </form></div>
    <div class="card section"><h2>Natural voices</h2>
      <p class="muted" style="margin-top:4px">Each voice is downloaded once (usually 20-120 MB). Voices chosen for a bridge or as the default download automatically. Languages marked ★ have natural voices.</p>
      <div id="voice-installed"></div>
      <div class="voice-add">
        <label style="flex:1;min-width:280px;margin:0">Add a natural voice ${voicePicker('', 'piper:tr_TR-dfki-medium', { naturalOnly: true })}</label>
        <button class="btn primary small" id="voice-download">⬇ Download</button>
      </div>
      <label style="margin-top:12px">Test sentence<input id="voice-sample" value="Welcome. Press 1 for Ali. Press 2 for Ayşe."></label>
      <div class="toolbar"><audio id="voice-audio" controls class="hidden" style="height:32px;width:100%"></audio></div>
    </div>`;
  let voiceTimer = null;
  const drawVoices = () => {
    const pv = state.voices.piper || {};
    const box = $('#voice-installed');
    if (!pv.available) { box.innerHTML = '<p class="muted">Natural voices are not available in this installation (Piper is missing); espeak-ng is used.</p>'; return; }
    const rows = (pv.installed || []).map(v => `<tr data-voice-key="${esc(v.key)}"><td>${esc(v.name)} · ${esc(v.country || v.code)} · ${esc(v.quality)}</td>
        <td>${esc(v.language)}</td><td>${v.size_mb} MB</td><td class="small">${v.used_by.length ? esc(v.used_by.join(', ')) : '<span class="muted">unused</span>'}</td>
        <td><button class="btn small" data-vact="test">▶ Test</button> <button class="btn small danger" data-vact="delete">Delete</button></td></tr>`);
    const dl = Object.entries(pv.downloads || {}).map(([k, d]) => `<tr><td>${esc(k)}</td><td colspan="3">${d.error ? `<span class="error">${esc(d.error)}</span>`
        : `<div class="meter"><span>${Math.round(d.progress * 100)}%</span><div class="bar"><i style="width:${Math.round(d.progress * 100)}%"></i></div></div>`}</td><td>${d.error ? '' : 'downloading…'}</td></tr>`);
    box.innerHTML = rows.length || dl.length ? `<div class="table-wrap"><table><tr><th>Installed voice</th><th>Language</th><th>Size</th><th>Used by</th><th></th></tr>${rows.join('')}${dl.join('')}</table></div>`
      : '<p class="muted">No natural voices installed yet.</p>';
    if (pv.catalog_error) box.insertAdjacentHTML('beforeend', `<p class="error small">${esc(pv.catalog_error)}</p>`);
    $$('[data-vact]', box).forEach(btn => btn.onclick = () => {
      const key = btn.closest('tr').dataset.voiceKey;
      if (btn.dataset.vact === 'test') return testVoice(btn, 'piper:' + key);
      if (confirm(`Delete voice ${key}?`)) busy(btn, async () => { await api('DELETE', `/tts/voices/${encodeURIComponent(key)}`); await refreshVoices(); toast('Voice deleted'); });
    });
    const downloading = Object.values(pv.downloads || {}).some(d => !d.error);
    if (downloading && !voiceTimer) voiceTimer = setInterval(refreshVoices, 1000);
    if (!downloading && voiceTimer) { clearInterval(voiceTimer); voiceTimer = null; }
  };
  const refreshVoices = async () => { await ensureVoices(true); drawVoices(); };
  const testVoice = (btn, voice) => busy(btn, async () => {
    const blob = await api('POST', '/tts/preview', { text: $('#voice-sample').value, voice, speed: Number($('#voice-form').default_speed.value) || 150 });
    const audio = $('#voice-audio');
    audio.src = URL.createObjectURL(blob);
    audio.classList.remove('hidden');
    audio.play().catch(() => {});
  });
  bindVoicePickers(main());
  drawVoices();
  $('#voice-download').onclick = e => busy(e.target, async () => {
    const v = $('.voice-add [data-voice]').value;
    if (!v.startsWith('piper:')) return;
    await api('POST', `/tts/voices/${encodeURIComponent(v.slice(6))}`);
    await refreshVoices();
    toast('Download started', 'ok');
  });
  const form = $('#voice-form');
  form.onsubmit = e => {
    e.preventDefault();
    busy($('[type=submit]', form), async () => {
      const data = readForm(form);
      await api('PUT', '/settings', data);
      if (voiceNeedsDownload(data.default_voice)) toast('Saved - the voice is downloading in the background', 'ok');
      else toast('Saved', 'ok');
      await refreshVoices();
    });
  };
  pageCleanup = () => { if (voiceTimer) clearInterval(voiceTimer); };
};

/* ---------- logs ---------- */
pages.logs = async () => {
  main().innerHTML = `
    <div class="page-head"><div><h1>Logs</h1></div>
      <div class="toolbar">
        <select id="log-level"><option value="0">All levels</option><option value="1">Warnings &amp; errors</option></select>
        <input id="log-filter" placeholder="Filter…">
        <label class="check" style="margin:0"><input type="checkbox" id="log-follow" checked> Follow</label>
        <button class="btn small" id="log-clear">Clear view</button>
      </div></div>
    <div class="logbox" id="logbox"></div>`;
  const box = $('#logbox');
  let seq = 0, records = [];
  const draw = () => {
    const lvl = $('#log-level').value, f = $('#log-filter').value.toLowerCase();
    const rows = records.filter(r => (lvl === '0' || ['WARNING', 'ERROR', 'CRITICAL'].includes(r.level))
      && (!f || r.msg.toLowerCase().includes(f) || r.logger.includes(f)));
    box.innerHTML = rows.map(r => `<div class="l ${r.level}"><span class="t">${esc(new Date(r.ts * 1000).toLocaleTimeString())} ${esc(r.level.padEnd(7))} ${esc(r.logger)}</span> ${esc(r.msg)}</div>`).join('');
    if ($('#log-follow').checked) box.scrollTop = box.scrollHeight;
  };
  const poll = async () => {
    const r = await api('GET', `/logs?after=${seq}`);
    if (r.records.length) {
      records = records.concat(r.records).slice(-2000);
      seq = r.records[r.records.length - 1].seq;
      draw();
    }
  };
  $('#log-level').onchange = draw;
  $('#log-filter').oninput = draw;
  $('#log-clear').onclick = () => { records = []; draw(); };
  await poll();
  every(1500, poll);
};

/* ---------- settings ---------- */
pages.settings = async () => {
  const s = await api('GET', '/settings');
  const origin = location.origin;
  main().innerHTML = `
    <div class="page-head"><div><h1>Settings</h1></div></div>
    <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(340px,1fr))">
      <div class="card"><h2>System</h2><dl class="kv">
        <dt>Version</dt><dd>${esc(s.version)}</dd>
        <dt>Web UI port</dt><dd>${esc(s.web_port)}</dd>
        <dt>SIP (UDP)</dt><dd>${esc(s.sip_port)}</dd>
        <dt>RTP ports</dt><dd>${esc(s.rtp_ports)}</dd>
        <dt>Advertised IP</dt><dd>${esc(s.advertise_ip)}</dd>
        <dt>WhatsApp driver</dt><dd>${esc(s.wa_driver)}${s.wa_driver === 'fake' ? ' ' + badge('simulated', 'warn') : ''}</dd>
        <dt>Browser sandbox</dt><dd>${s.chromium_sandbox ? badge('on', 'ok') : badge('off', 'warn')}</dd>
        <dt>Data dir</dt><dd class="mono">${esc(s.data_dir)}</dd>
      </dl><p class="muted small">These come from environment variables (see .env / docker-compose.yml).</p></div>
      <div class="card"><h2>Change admin password</h2>
        <form id="pw-form" style="margin-top:10px">
          <label>Current password<input name="current" type="password" required autocomplete="current-password"></label>
          <label>New password<input name="new" type="password" required minlength="10" autocomplete="new-password"></label>
          <div class="error" id="pw-error"></div>
          <div class="form-actions"><button class="btn primary" type="submit">Change password</button></div>
        </form></div>
    </div>
    <div class="card section"><h2>Automation API</h2>
      <p class="muted">Use this token for scripts and monitoring. Full API reference: <a href="/api/docs" target="_blank">/api/docs</a>.</p>
      <div class="toolbar"><input id="token" class="mono" readonly value="${esc(s.api_token)}" style="flex:1;min-width:240px">
        <button class="btn small" id="copy-token">Copy</button><button class="btn small danger" id="regen-token">Regenerate</button></div>
      <h3 style="margin-top:16px">Example: status of all WhatsApp accounts</h3>
      <pre class="logbox" style="height:auto;min-height:0">curl ${esc(origin)}/api/wa -H "Authorization: Bearer ${esc(s.api_token)}"</pre>
    </div>`;
  $('#copy-token').onclick = () => { navigator.clipboard?.writeText($('#token').value); toast('Copied'); };
  $('#regen-token').onclick = e => {
    if (!confirm('Regenerate the API token? Existing integrations will stop working.')) return;
    busy(e.target, async () => { const r = await api('POST', '/settings/api-token'); $('#token').value = r.api_token; toast('New token generated', 'ok'); });
  };
  const form = $('#pw-form');
  form.onsubmit = e => {
    e.preventDefault();
    busy($('[type=submit]', form), async () => {
      try { await api('POST', '/settings/password', readForm(form)); }
      catch (err) { $('#pw-error').textContent = err.message; return; }
      form.reset(); $('#pw-error').textContent = '';
      toast('Password changed', 'ok');
    });
  };
};

/* ---------- auth ---------- */
function showAuth(setup) {
  $('#app').classList.add('hidden');
  $('#auth').classList.remove('hidden');
  $('#auth-hint').textContent = setup ? 'Welcome! Choose an admin password for the web UI.' : 'Sign in to manage your WhatsApp bridges.';
  $('#auth-confirm-wrap').classList.toggle('hidden', !setup);
  $('#auth-submit').textContent = setup ? 'Create password' : 'Sign in';
  $('#auth-form').dataset.setup = setup ? '1' : '';
  // the minimum applies to new passwords only; older, shorter ones can still sign in
  const pw = $('#auth-password');
  if (setup) pw.minLength = 10; else pw.removeAttribute('minlength');
  pw.autocomplete = setup ? 'new-password' : 'current-password';
  pw.focus();
}

$('#auth-form').onsubmit = async e => {
  e.preventDefault();
  const setup = !!e.target.dataset.setup;
  const pw = $('#auth-password').value;
  $('#auth-error').textContent = '';
  if (setup && pw !== $('#auth-confirm').value) { $('#auth-error').textContent = 'Passwords do not match'; return; }
  try {
    await api('POST', setup ? '/setup' : '/login', { password: pw });
    $('#auth-password').value = ''; $('#auth-confirm').value = '';
    start();
  } catch (err) { $('#auth-error').textContent = err.message; }
};

$('#logout').onclick = async () => { await api('POST', '/logout'); showAuth(false); };

async function start() {
  const s = await api('GET', '/session');
  state.driver = s.driver;
  $('#version').textContent = `v${s.version}`;
  if (!s.authenticated) return showAuth(s.setup_required);
  $('#auth').classList.add('hidden');
  $('#app').classList.remove('hidden');
  await loadAll().catch(() => {});
  ensureVoices().catch(() => {});
  route();
}
start();
