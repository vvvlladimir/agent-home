// Agent House GUI. No build step. Ingress rule: every URL is relative (no leading slash).

const PAGES = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'feed', label: 'Live feed & traces' },
  { id: 'agents', label: 'Agents' },
  { id: 'proposals', label: 'Proposals', milestone: 'M4',
    todo: 'Automation drafts with YAML diff, rationale and shadow-mode evidence. Approve, reject or edit.' },
  { id: 'automations', label: 'Deployed automations', milestone: 'M4',
    todo: 'Everything Agent House deployed into Home Assistant: versions, diff, one-click rollback, enable / disable.' },
  { id: 'migration', label: 'Migration', milestone: 'M5',
    todo: 'Your existing automations and Node-RED flows next to their proposed replacements, with a parity report.' },
  { id: 'chat', label: 'Chat', milestone: 'M5',
    todo: 'Talk to the orchestrator or a room agent; tool calls are shown inline.' },
  { id: 'models', label: 'Models & Benchmarks', milestone: 'M2',
    todo: 'Model profiles, the benchmark runner and its history: tok/s, time to first token, memory, tool-call accuracy.' },
  { id: 'settings', label: 'Settings & Logs' },
];

const TIER_HELP = {
  T0: 'free: the agent may act',
  T1: 'the agent may act and you are notified',
  T2: 'the agent may act only after your approval',
  T3: 'never: agents cannot actuate this entity',
};

function app() {
  return {
    pages: PAGES,
    page: 'dashboard',
    lang: 'en',
    dict: {},
    status: null,
    apiError: '',
    areas: [],
    entities: [],
    areaSel: '',
    entityFilter: '',
    feed: [],
    feedKind: '',
    feedFilter: '',
    feedPaused: false,
    streamOk: false,
    settings: null,
    logs: [],
    logLevel: 'info',
    _stream: null,

    async init() {
      this.applyTheme(this.stored('theme'));
      await this.setLang(this.stored('lang') || 'en');
      window.addEventListener('hashchange', () => this.route());
      this.route();
      await this.refresh();
      this.loadAreas();
      setInterval(() => this.refresh(), 5000);
      setInterval(() => { if (this.page === 'dashboard') this.loadAreas(); }, 30000);
      this.api('api/events?limit=200').then((items) => { this.feed = items; }).catch(() => {});
      this.connectStream();
    },

    // ── helpers ────────────────────────────────────────────────────────────────
    stored(key) { try { return localStorage.getItem('ah.' + key); } catch (e) { return null; } },
    store(key, value) { try { localStorage.setItem('ah.' + key, value); } catch (e) { /* private mode */ } },
    t(text) { return this.dict[text] || text; },
    async setLang(lang) {
      this.lang = lang;
      this.store('lang', lang);
      try { this.dict = await (await fetch('static/i18n/' + lang + '.json')).json(); } catch (e) { this.dict = {}; }
      document.documentElement.lang = lang;
    },
    applyTheme(theme) {
      if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
      else delete document.documentElement.dataset.theme;
    },
    toggleTheme() {
      const dark = document.documentElement.dataset.theme
        ? document.documentElement.dataset.theme === 'dark'
        : window.matchMedia('(prefers-color-scheme: dark)').matches;
      const next = dark ? 'light' : 'dark';
      this.applyTheme(next);
      this.store('theme', next);
    },
    async api(path, options) {
      const response = await fetch(path, options);
      if (!response.ok) {
        let detail = response.status + ' ' + response.statusText;
        try { const body = await response.json(); detail = body.detail || body.error || detail; } catch (e) { /* not JSON */ }
        throw new Error(detail);
      }
      return response.json();
    },
    put(path, body) {
      return this.api(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    },
    route() {
      const id = (location.hash || '#dashboard').slice(1);
      this.page = PAGES.some((p) => p.id === id) ? id : 'dashboard';
      if (this.page === 'agents') this.loadEntities();
      if (this.page === 'settings') { this.loadSettings(); this.loadLogs(); }
    },

    // ── data ───────────────────────────────────────────────────────────────────
    async refresh() {
      try {
        this.status = await this.api('api/status');
        this.apiError = '';
      } catch (e) {
        this.apiError = e.message;
      }
    },
    async loadAreas() { try { this.areas = await this.api('api/areas'); } catch (e) { /* banner already shows */ } },
    async loadEntities() {
      const query = this.areaSel ? '?area=' + encodeURIComponent(this.areaSel) : '';
      try { this.entities = await this.api('api/entities' + query); } catch (e) { this.entities = []; }
    },
    async loadSettings() { try { this.settings = await this.api('api/settings'); } catch (e) { /* ignore */ } },
    async loadLogs() {
      try { this.logs = await this.api('api/logs?limit=500&level=' + this.logLevel); } catch (e) { this.logs = []; }
    },
    selectArea(areaId) { this.areaSel = areaId; this.loadEntities(); },

    async setMode(mode) {
      if (!this.status || this.status.mode === mode) return;
      if (mode === 'live' && !confirm(this.t('Switch the global mode to Live? Agents whose own mode is live will be allowed to actuate.'))) return;
      try { this.status = await this.put('api/mode', { mode }); } catch (e) { this.apiError = e.message; }
    },
    async toggleKill() {
      const engaged = !this.status.kill_switch.engaged;
      if (!engaged && !confirm(this.t('Release the kill switch?'))) return;
      try { this.status = await this.put('api/kill-switch', { engaged }); } catch (e) { this.apiError = e.message; }
    },

    connectStream() {
      if (this._stream) this._stream.close();
      const stream = new EventSource('api/events/stream');
      this._stream = stream;
      stream.onopen = () => { this.streamOk = true; };
      stream.onerror = () => { this.streamOk = false; };  // EventSource reconnects by itself
      stream.onmessage = (message) => {
        if (this.feedPaused) return;
        this.feed.unshift(JSON.parse(message.data));
        if (this.feed.length > 500) this.feed.length = 500;
      };
    },

    // ── view helpers ───────────────────────────────────────────────────────────
    get feedKinds() { return [...new Set(this.feed.map((e) => e.kind))].sort(); },
    filteredFeed() {
      const needle = this.feedFilter.toLowerCase();
      return this.feed.filter((e) => (!this.feedKind || e.kind === this.feedKind)
        && (!needle || (e.summary + ' ' + (e.agent_id || '') + ' ' + (e.entity_id || '')).toLowerCase().includes(needle)))
        .slice(0, 300);
    },
    filteredEntities() {
      const needle = this.entityFilter.toLowerCase();
      return this.entities.filter((e) => !needle || (e.entity_id + ' ' + (e.name || '')).toLowerCase().includes(needle));
    },
    areaName(id) { const a = this.areas.find((x) => x.area_id === id); return a ? a.name : (id || '—'); },
    tierHelp(tier) { return this.t(TIER_HELP[tier] || ''); },
    haPill() {
      const ha = this.status && this.status.ha;
      if (!ha) return { cls: '', text: 'HA …' };
      if (ha.connected) return { cls: 'pill-ok', text: 'HA ' + (ha.version || this.t('connected')) };
      return { cls: 'pill-bad', text: 'HA ' + ha.state };
    },
    llmDot() {
      const state = this.status && this.status.llm && this.status.llm.state;
      if (state === 'ok') return 'ok';
      if (state === 'warming' || state === 'busy') return 'warn';
      if (state === 'unconfigured' || !state) return 'off';
      return 'bad';
    },
    clock(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString() : ''; },
    ago(ts) {
      if (!ts) return '—';
      const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
      if (s < 60) return s + ' s';
      if (s < 3600) return Math.round(s / 60) + ' min';
      return Math.round(s / 3600) + ' h';
    },
    mb(value) { return value == null ? '—' : (value / 1024).toFixed(1) + ' GB'; },
    pct(a, b) { return a == null || !b ? 0 : Math.min(100, Math.round((a / b) * 100)); },
    deg(value) { return value == null ? '—' : value.toFixed(0) + ' °C'; },
    fmt(value) { return Array.isArray(value) ? value.join(', ') : String(value); },
  };
}

window.app = app;
