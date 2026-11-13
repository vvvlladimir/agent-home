// Shared state for every page: status, rooms, agents, decisions waiting, the live feed, toasts.
// Pages load their own lists; what the header and the overview need lives here.
import { reactive } from 'vue';
import { api, type Action, type AgentSummary, type Area, type Entity, type FeedItem, type Finding, type Proposal, type Status } from './api';

export const state = reactive({
  status: null as Status | null,
  apiError: '',
  areas: [] as Area[],
  agents: [] as AgentSummary[],
  names: {} as Record<string, string>, // entity_id → friendly name
  pending: [] as Action[],
  drafts: [] as Proposal[],
  findings: [] as Finding[],
  asksLoaded: false,
  feed: [] as FeedItem[],
  feedPaused: false,
  streamOk: false,
  polling: false,
  toasts: [] as { id: number; text: string; kind: string }[],
  busy: {} as Record<string, boolean>, // id → a request runs (button spinner)
  leaving: {} as Record<string, boolean>, // id → the resolved card animates out
  dict: {} as Record<string, string>,
  lang: 'en',
});

// Cross-page intents: "open this run / proposal / agent" from another page.
export const nav = reactive({
  runId: '' as string,
  proposalId: '' as string,
  agentId: '' as string,
  areaSel: '' as string,
});

// ── i18n, toasts, theme ──────────────────────────────────────────────────────
export const t = (text: string) => state.dict[text] || text;

function stored(key: string) {
  try { return localStorage.getItem('ah.' + key); } catch { return null; }
}
function store(key: string, value: string) {
  try { localStorage.setItem('ah.' + key, value); } catch { /* private mode */ }
}

export async function setLang(lang: string) {
  state.lang = lang;
  store('lang', lang);
  try { state.dict = await (await fetch(`static/i18n/${lang}.json`)).json(); } catch { state.dict = {}; }
  document.documentElement.lang = lang;
}

function applyTheme(theme: string | null) {
  if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
export function toggleTheme() {
  const set = document.documentElement.dataset.theme;
  const dark = set ? set === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  const next = dark ? 'light' : 'dark';
  applyTheme(next);
  store('theme', next);
}

let toastSeq = 0;
export function toast(text: string, kind = '') {
  const id = ++toastSeq;
  state.toasts.push({ id, text, kind });
  setTimeout(() => { state.toasts = state.toasts.filter((x) => x.id !== id); }, kind === 'bad' ? 6000 : 3500);
}
export const fail = (e: unknown) => toast(e instanceof Error ? e.message : String(e), 'bad');

// Runs fn with a spinner on the control keyed by id; on success the card keyed by id slides out first.
// Errors become a toast and the result is undefined.
export async function withBusy<T>(id: string, fn: () => Promise<T>, leave = false): Promise<T | undefined> {
  state.busy[id] = true;
  try {
    const out = await fn();
    if (leave) {
      state.leaving[id] = true;
      await new Promise((r) => setTimeout(r, 300));
    }
    return out;
  } catch (e) {
    fail(e);
    return undefined;
  } finally {
    delete state.busy[id];
    delete state.leaving[id];
  }
}

// ── loaders ──────────────────────────────────────────────────────────────────
export async function refresh() {
  try {
    state.status = await api.get<Status>('api/status');
    state.apiError = '';
  } catch (e) {
    state.apiError = (e as Error).message;
  }
}
export async function loadAreas() {
  try { state.areas = await api.get<Area[]>('api/areas'); } catch { /* the banner shows it */ }
}
export async function loadAgents() {
  try { state.agents = await api.get<AgentSummary[]>('api/agents'); } catch { /* banner */ }
}
export async function loadNames() {
  try {
    const names: Record<string, string> = {};
    for (const e of await api.get<Entity[]>('api/entities')) if (e.name) names[e.entity_id] = e.name;
    state.names = names;
  } catch { /* titles fall back to entity ids */ }
}
export async function loadPending() {
  try { state.pending = await api.get<Action[]>('api/actions?verdict=pending'); } catch { /* banner */ }
}
export async function loadAsks() {
  try {
    const [drafts, findings] = await Promise.all([
      api.get<Proposal[]>('api/proposals?status=draft'),
      api.get<Finding[]>('api/findings'),
    ]);
    state.drafts = drafts;
    state.findings = findings.filter((f) => f.status !== 'dismissed');
    state.asksLoaded = true;
  } catch { /* banner */ }
}

// ── live feed: SSE with a polling fallback (some proxies buffer or drop SSE) ─
export function addFeed(items: FeedItem[]) {
  if (state.feedPaused) return;
  const seen = new Set(state.feed.map((e) => e.id));
  const fresh = items.filter((e) => !seen.has(e.id));
  if (!fresh.length) return;
  state.feed = [...fresh, ...state.feed].sort((a, b) => b.id - a.id).slice(0, 500);
}
let pollTimer: number | undefined;
function startPolling() {
  if (pollTimer) return;
  state.polling = true;
  const tick = () => api.get<FeedItem[]>('api/events?limit=100').then(addFeed).catch(() => {});
  tick();
  pollTimer = setInterval(tick, 3000);
}
function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = undefined;
  state.polling = false;
}
function connectStream() {
  const stream = new EventSource('api/events/stream');
  stream.onopen = () => { state.streamOk = true; stopPolling(); };
  stream.onerror = () => { state.streamOk = false; }; // EventSource reconnects by itself
  stream.onmessage = (m) => addFeed([JSON.parse(m.data)]);
  setTimeout(() => { if (!state.streamOk) startPolling(); }, 8000);
  setInterval(() => { if (!state.streamOk) startPolling(); }, 15000);
}

export async function start() {
  applyTheme(stored('theme'));
  await setLang(stored('lang') || 'en');
  await refresh();
  loadAreas(); loadAgents(); loadNames(); loadPending(); loadAsks();
  api.get<FeedItem[]>('api/events?limit=200').then((items) => { state.feed = items; }).catch(() => {});
  connectStream();
  setInterval(refresh, 5000);
  setInterval(() => { loadPending(); loadAgents(); }, 5000);
  setInterval(loadAsks, 20000);
  setInterval(() => { loadAreas(); loadNames(); }, 60000);
}

// ── global actions ───────────────────────────────────────────────────────────
export async function setMode(mode: 'shadow' | 'live') {
  if (!state.status || state.status.mode === mode) return;
  if (mode === 'live' && !confirm(t('Switch the global mode to Live? Agents whose own mode is live will be allowed to actuate.'))) return;
  try {
    state.status = await api.put<Status>('api/mode', { mode });
    toast(mode === 'live' ? t('Live mode on') : t('Shadow mode on'));
  } catch (e) { fail(e); }
}
// Stopping is instant; resuming asks.
export async function stopAll() {
  try {
    state.status = await api.put<Status>('api/kill-switch', { engaged: true, reason: 'stopped from the panel' });
    toast(t('All agents stopped'));
  } catch (e) { fail(e); }
}
export async function resumeAll() {
  if (!confirm(t('Resume the agents? They act again within their tiers and modes.'))) return;
  try {
    state.status = await api.put<Status>('api/kill-switch', { engaged: false });
    toast(t('Agents resumed'));
  } catch (e) { fail(e); }
}
export async function decide(id: string, approve: boolean) {
  const done = await withBusy(id, () => api.post(`api/approvals/${encodeURIComponent(id)}`, { approve }), true);
  if (done !== undefined) toast(approve ? t('Approved') : t('Denied'));
  await loadPending();
}
export const killed = () => !!state.status?.kill_switch?.engaged;
