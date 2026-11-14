// Agent House sim panel. No build, no CDN, relative URLs only.
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
let S = null; // last /api/state
let tab = 'house';

async function api(path, method = 'GET', body) {
  const opts = { method, headers: { 'X-Sim-Panel': '1' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = bad ? 'show bad' : 'show';
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.className = ''), 3500);
}

async function act(fn, ok) {
  try {
    const r = await fn();
    if (ok) toast(typeof ok === 'function' ? ok(r) : ok);
    await refresh();
    return r;
  } catch (e) {
    toast(e.message, true);
  }
}

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function time(ts) {
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
  return d.toLocaleTimeString();
}

function fill(form, values) {
  for (const [k, v] of Object.entries(values || {})) {
    const el = form.elements[k];
    if (!el || el === document.activeElement) continue;
    if (el.type === 'checkbox') el.checked = !!v;
    else el.value = v ?? '';
  }
  $$('input[type=range]', form).forEach((r) => {
    const out = r.parentElement.querySelector('output');
    if (out) out.textContent = r.value;
  });
}

function read(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.type === 'number' || el.type === 'range') out[el.name] = el.value === '' ? undefined : Number(el.value);
    else out[el.name] = el.value;
  }
  return out;
}

// ---- tabs ------------------------------------------------------------------------------------
$$('#tabs button').forEach((b) =>
  b.addEventListener('click', () => {
    tab = b.dataset.tab;
    $$('#tabs button').forEach((x) => x.classList.toggle('on', x === b));
    $$('.tab').forEach((s) => s.classList.toggle('on', s.id === `tab-${tab}`));
    poll();
    if (tab === 'entities') renderEntities();
  }),
);

// ---- render ----------------------------------------------------------------------------------
function pill(label, value, cls = '') {
  return `<span class="pill ${cls}"><b>${esc(label)}</b> ${esc(value)}</span>`;
}

function render() {
  const c = S.config;
  $('#app-link').href = S.app_url;
  const life = S.life;
  $('#status').innerHTML = [
    pill('entities', S.house.summary.entities),
    pill('events/min', life.events_last_min),
    pill('rate', life.rate === 0 ? 'paused' : `×${life.effective_rate}`, life.rate === 0 ? 'warn' : ''),
    pill('room LLM', c.room.backend, c.room.backend === 'fake' ? '' : 'live'),
    pill('Claude', c.anthropic.backend, c.anthropic.backend === 'real' ? 'live' : ''),
    pill('OpenAI', c.openai.backend, c.openai.backend === 'real' ? 'live' : ''),
    pill('Jetson metrics', c.metrics_source, c.metrics_source === 'jetson' ? 'live' : ''),
    pill('add-on WS', S.ha.ws_connections, S.ha.ws_connections ? '' : 'warn'),
    life.scenario ? pill('scenario', life.scenario, 'live') : '',
  ].join('');

  // house
  const hf = $('#house-form');
  if (!hf.elements.preset.options.length) {
    for (const p of Object.keys(S.presets)) hf.elements.preset.add(new Option(p, p));
    fill(hf, { ...S.house.spec, backfill_days: c.backfill_days });
  }
  const sm = S.house.summary;
  const built = S.house.built || {};
  const junk = Object.entries(built.junk || {}).map(([k, v]) => `${k.replaceAll('_', ' ')}: ${v}`).join(', ') || 'none';
  const domains = Object.entries(sm.by_domain).map(([d, n]) => `<span class="chip">${esc(d)} ${n}</span>`).join('');
  $('#house-summary').innerHTML = `
    <div class="kv">
      <div><b>${sm.entities}</b> entities with a state</div><div><b>${sm.registry}</b> in the entity registry</div>
      <div><b>${sm.devices}</b> devices</div><div><b>${sm.areas}</b> areas</div>
      <div><b>${sm.unavailable}</b> unavailable</div><div><b>${sm.unknown}</b> unknown</div>
      <div><b>${sm.disabled}</b> disabled (registry only)</div><div><b>${sm.no_area}</b> without an area</div>
    </div>
    <p><b>Junk generated:</b> ${esc(junk)}</p>
    <p><b>History:</b> ${S.house.backfill.days ?? 0} days, ${S.house.backfill.changes ?? 0} changes</p>
    <div class="chips">${domains}</div>`;
  const dom = $('#ent-domain');
  if (dom.options.length === 1) for (const d of Object.keys(sm.by_domain).sort()) dom.add(new Option(d, d));

  // flow
  const rate = $('#rate');
  if (rate !== document.activeElement) rate.value = life.rate;
  $('#rate-out').textContent = life.rate === 0 ? 'paused' : `×${life.rate}`;
  $('#run-automations').checked = life.run_automations;
  $('#flow-stats').innerHTML = `
    <div><b>${life.streams}</b> streaming entities</div><div><b>${life.events_last_min}</b> changes in the last minute</div>
    <div><b>${life.automation_runs}</b> automation runs</div><div>storm: <b>${life.storm_left_s ? life.storm_left_s + ' s left' : 'off'}</b></div>`;

  // scenarios
  const sc = $('#scenarios');
  if (!sc.children.length) {
    sc.innerHTML = Object.entries(S.scenarios)
      .map(([k, d]) => `<button class="scenario" data-scenario="${esc(k)}"><b>${esc(k.replaceAll('_', ' '))}</b><small>${esc(d)}</small></button>`)
      .join('');
  }
  $$('[data-scenario]').forEach((b) => b.classList.toggle('running', b.dataset.scenario === life.scenario));

  // faults
  const hl = $('#ha-latency');
  if (hl !== document.activeElement) hl.value = S.ha.latency_s;
  const ll = $('#llm-latency');
  if (ll !== document.activeElement) ll.value = c.faults.llm.latency_s;
  const jf = $('#jetson-form');
  if (!jf.children.length) {
    jf.innerHTML =
      Object.keys(S.jetson)
        .filter((k) => !['throttled', 'active_profile', 'profiles'].includes(k))
        .map((k) => `<label>${esc(k)} <input name="${esc(k)}" type="number" step="any"></label>`)
        .join('') + '<button type="submit">Apply</button>';
  }
  fill(jf, S.jetson);

  // models
  fill($('#room-form'), c.room);
  fill($('#jetson-real-form'), c.jetson);
  fill($('#cloud-form'), {
    anthropic: c.anthropic.backend, anthropic_model: c.anthropic.model_override,
    openai: c.openai.backend, openai_model: c.openai.model_override,
  });
  $$('[data-saved]').forEach((s) => (s.textContent = c.secrets[s.dataset.saved] ? `saved ${c.secrets[s.dataset.saved]}` : 'not set'));
}

async function renderEvents() {
  const rows = await api('api/events?limit=120');
  $('#events').innerHTML = rows
    .map((e) => `<tr><td>${time(e.ts)}</td><td>${esc(e.entity_id)}</td><td>${esc(e.from)} → <b>${esc(e.to)}</b></td><td class="c-${esc(e.cause)}">${esc(e.cause)}</td></tr>`)
    .join('');
}

async function renderEntities() {
  const q = new URLSearchParams({ q: $('#ent-q').value, domain: $('#ent-domain').value, junk: $('#ent-junk').checked, limit: 300 });
  const rows = await api(`api/entities?${q}`);
  $('#ent-count').textContent = `${rows.length}${rows.length === 300 ? '+' : ''} shown`;
  $('#entities').innerHTML = rows
    .map(
      (e) => `<tr class="${e.junk ? 'junk' : ''}">
        <td><b>${esc(e.entity_id)}</b><br><small>${esc(e.name)}${e.category ? ' · ' + esc(e.category) : ''}</small></td>
        <td>${esc(e.state)}</td><td>${esc(e.area || '—')}</td><td>${esc(e.platform || '')}</td>
        <td class="setter" data-entity="${esc(e.entity_id)}">
          <input value="${esc(e.state)}" size="9">
          <select><option>user</option><option>physical</option><option>automation</option></select>
          <button data-set>Set</button><button data-remove title="remove: the entity disappears">✕</button>
        </td></tr>`,
    )
    .join('');
}

async function renderRequests() {
  const rows = await api('api/requests');
  $('#requests').innerHTML = rows
    .map(
      (r) => `<tr class="${r.status >= 400 ? 'junk' : ''}"><td>${time(r.ts)}</td><td>${esc(r.gateway)}</td><td>${esc(r.backend)}</td>
        <td>${esc(r.model || '')}</td><td>${esc(r.status)}</td><td>${esc(r.ms)}</td>
        <td>${esc(r.tokens_in ?? '')}/${esc(r.tokens_out ?? '')}</td><td>${esc(r.note || (r.tool_calls ? r.tool_calls + ' tool call(s)' : ''))}</td></tr>`,
    )
    .join('');
}

async function refresh() {
  S = await api('api/state');
  render();
}

async function poll() {
  try {
    await refresh();
    if (tab === 'flow') await renderEvents();
    if (tab === 'requests') await renderRequests();
  } catch (e) {
    $('#status').innerHTML = pill('sim', 'unreachable', 'warn');
  }
}

// ---- actions -----------------------------------------------------------------------------------
$('#house-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  toast('Rebuilding…');
  act(() => api('api/house', 'POST', read(ev.target)), (r) => `House rebuilt: ${r.summary.entities} entities`);
});
$('#house-form').elements.preset.addEventListener('change', (ev) => {
  fill($('#house-form'), S.presets[ev.target.value]);
});
$('#house-form').addEventListener('input', (ev) => {
  if (ev.target.type === 'range') ev.target.parentElement.querySelector('output').textContent = ev.target.value;
});

$('#rate').addEventListener('change', (ev) => act(() => api('api/flow', 'POST', { rate: Number(ev.target.value) })));
$('#rate').addEventListener('input', (ev) => ($('#rate-out').textContent = `×${ev.target.value}`));
$$('[data-rate]').forEach((b) => b.addEventListener('click', () => act(() => api('api/flow', 'POST', { rate: Number(b.dataset.rate) }))));
$('#storm').addEventListener('click', () => act(() => api('api/flow', 'POST', { storm: { seconds: 30, factor: 20 } }), 'Storm for 30 s'));
$('#run-automations').addEventListener('change', (ev) => act(() => api('api/flow', 'POST', { run_automations: ev.target.checked })));
$$('[data-backfill]').forEach((b) =>
  b.addEventListener('click', () => act(() => api('api/backfill', 'POST', { days: Number(b.dataset.backfill) }), (r) => `${r.changes} changes over ${r.days} days written`)),
);

let entTimer;
['#ent-q', '#ent-domain', '#ent-junk'].forEach((s) =>
  $(s).addEventListener('input', () => {
    clearTimeout(entTimer);
    entTimer = setTimeout(renderEntities, 250);
  }),
);
$('#entities').addEventListener('click', (ev) => {
  const cell = ev.target.closest('[data-entity]');
  if (!cell) return;
  const id = cell.dataset.entity;
  if (ev.target.matches('[data-set]')) {
    const body = { state: $('input', cell).value, cause: $('select', cell).value };
    act(() => api(`api/entities/${encodeURIComponent(id)}`, 'POST', body), `${id} → ${body.state}`).then(renderEntities);
  } else if (ev.target.matches('[data-remove]')) {
    act(() => api(`api/entities/${encodeURIComponent(id)}`, 'DELETE'), `${id} removed`).then(renderEntities);
  }
});

$('#scenarios').addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-scenario]');
  if (b) act(() => api(`api/scenario/${b.dataset.scenario}`, 'POST'), (r) => `${r.scenario}: ${r.steps} steps`);
});
$('#scenario-stop').addEventListener('click', () => act(() => api('api/scenario-stop', 'POST'), 'Stopped'));

$$('[data-fault]').forEach((b) => b.addEventListener('click', () => act(() => api('api/faults', 'POST', JSON.parse(b.dataset.fault)), b.textContent)));
$('#ha-latency').addEventListener('change', (ev) => act(() => api('api/faults', 'POST', { target: 'ha', latency_s: Number(ev.target.value) })));
$('#llm-latency').addEventListener('change', (ev) => act(() => api('api/faults', 'POST', { target: 'llm', latency_s: Number(ev.target.value) })));
$('#jetson-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  act(() => api('api/faults', 'POST', { target: 'jetson', ...read(ev.target) }), 'Jetson metrics set');
});

$('#room-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  act(() => api('api/config', 'PUT', { room: read(ev.target) }), 'Room-agent backend saved');
});
$('#jetson-real-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  act(() => api('api/config', 'PUT', { jetson: read(ev.target) }), 'Jetson saved');
});
$('#cloud-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  const f = read(ev.target);
  act(
    () => api('api/config', 'PUT', {
      anthropic: { backend: f.anthropic, model_override: f.anthropic_model },
      openai: { backend: f.openai, model_override: f.openai_model },
    }),
    'Orchestrator providers saved',
  );
});
$('#keys-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  const body = {};
  for (const [k, v] of Object.entries(read(ev.target))) if (v) body[k] = v === '-' ? '' : v;
  if (!Object.keys(body).length) return toast('Nothing to save');
  act(() => api('api/secrets', 'PUT', body), 'Keys saved').then(() => ev.target.reset());
});
$$('[data-check]').forEach((b) =>
  b.addEventListener('click', async () => {
    toast(`Testing ${b.dataset.check}…`);
    try {
      const r = await api(`api/check/${b.dataset.check}`, 'POST');
      toast(`${b.dataset.check}: ${r.ok ? 'OK' : 'FAILED'} — ${r.detail}`, !r.ok);
    } catch (e) {
      toast(e.message, true);
    }
  }),
);

poll();
setInterval(() => document.visibilityState === 'visible' && poll(), 2500);
