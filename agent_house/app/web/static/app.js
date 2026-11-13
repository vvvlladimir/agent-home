// Agent House GUI. No build step. Ingress rule: every URL is relative (no leading slash).

const PAGES = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'feed', label: 'Live feed' },
  { id: 'runs', label: 'Runs & traces' },
  { id: 'agents', label: 'Agents' },
  { id: 'proposals', label: 'Proposals', milestone: 'M4',
    todo: 'Automation drafts with YAML diff, rationale and shadow-mode evidence. Approve, reject or edit.' },
  { id: 'automations', label: 'Deployed automations', milestone: 'M4',
    todo: 'Everything Agent House deployed into Home Assistant: versions, diff, one-click rollback, enable / disable.' },
  { id: 'migration', label: 'Migration', milestone: 'M5',
    todo: 'Your existing automations and Node-RED flows next to their proposed replacements, with a parity report.' },
  { id: 'chat', label: 'Chat', milestone: 'M5',
    todo: 'Talk to the orchestrator or a room agent; tool calls are shown inline.' },
  { id: 'models', label: 'Models & Benchmarks' },
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
    profiles: [],
    benchmarks: [],
    benchQuick: true,
    benchOpen: '',
    modelsError: '',
    runtimeForm: {},
    runtimeMsg: '',
    runtimeError: '',
    _stream: null,
    _poll: null,
    polling: false,
    // agents
    agentsList: [],
    agentForm: null,
    agentIsNew: false,
    agentMsg: '',
    agentError: '',
    roomEntities: [],
    preview: null,
    previewError: '',
    _previewTimer: null,
    // runs, actions, approvals
    runs: [],
    runAgent: '',
    runStatus: '',
    runSel: null,
    runMsg: '',
    actions: [],
    pending: [],
    inbox: [],

    async init() {
      this.applyTheme(this.stored('theme'));
      await this.setLang(this.stored('lang') || 'en');
      window.addEventListener('hashchange', () => this.route());
      this.route();
      await this.refresh();
      this.loadAreas();
      setInterval(() => this.refresh(), 5000);
      setInterval(() => { if (this.page === 'dashboard') this.loadAreas(); }, 30000);
      setInterval(() => { if (this.page === 'models') this.loadModels(); }, 3000);
      setInterval(() => {
        this.loadPending();
        if (this.page === 'runs') this.loadRuns();
        if (this.page === 'agents' && !this.agentForm) this.loadAgents();
      }, 5000);
      this.loadPending();
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
      if (this.page === 'agents') { this.loadEntities(); this.loadAgents(); }
      if (this.page === 'runs') { this.loadRuns(); this.loadActions(); this.loadAgents(); }
      if (this.page === 'settings') { this.loadSettings(); this.loadLogs(); }
      if (this.page === 'models') this.loadModels();
    },
    post(path, body) {
      return this.api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
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
    async loadSettings() {
      try {
        this.settings = await this.api('api/settings');
        this.runtimeForm = this.formFrom(this.settings.runtime);
      } catch (e) { /* ignore */ }
    },
    formFrom(runtime) {
      const form = {};
      for (const [k, v] of Object.entries(runtime || {})) form[k] = v == null ? '' : String(v);
      return form;
    },
    async saveRuntime() {
      // '' = "use the default" (the field becomes null); everything else is sent as a number
      const body = {};
      const TEXT = ['kill_switch_entity'];
      for (const [k, v] of Object.entries(this.runtimeForm)) body[k] = v === '' ? null : (TEXT.includes(k) ? v.trim() : Number(v));
      this.runtimeMsg = ''; this.runtimeError = '';
      try {
        this.settings = await this.put('api/settings', body);
        this.runtimeForm = this.formFrom(this.settings.runtime);
        this.runtimeMsg = this.t('Saved.');
      } catch (e) { this.runtimeError = e.message; }
    },

    // ── models & benchmarks ────────────────────────────────────────────────────
    async loadModels() {
      try {
        [this.profiles, this.benchmarks] = await Promise.all([this.api('api/models/profiles'), this.api('api/benchmarks')]);
        this.modelsError = '';
      } catch (e) { this.modelsError = e.message; }
    },
    get benchRunning() { return this.benchmarks.some((b) => b.status === 'running'); },
    async activateProfile(name) {
      if (!confirm(this.t('Restart the inference server with this profile? Agents wait while the model loads.'))) return;
      try {
        this.status = { ...this.status, llm: await this.put('api/models/active', { profile: name }) };
        this.modelsError = '';
      } catch (e) { this.modelsError = e.message; }
      this.loadModels();
    },
    async runBenchmark() {
      try {
        await this.post('api/benchmarks', { quick: this.benchQuick });
        this.modelsError = '';
      } catch (e) { this.modelsError = e.message; }
      this.loadModels();
    },
    toolScore(report) {
      const t = report && report.toolcall;
      return t ? t.passed + '/' + t.total : '—';
    },
    num(value, unit, digits) {
      return value == null ? '—' : Number(value).toFixed(digits ?? 1) + (unit ? ' ' + unit : '');
    },
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
      stream.onopen = () => { this.streamOk = true; this.stopPolling(); };
      stream.onerror = () => { this.streamOk = false; };  // EventSource reconnects by itself
      stream.onmessage = (message) => this.addFeed([JSON.parse(message.data)]);
      // Polling fallback: a proxy that buffers or drops SSE never fires onopen, or keeps erroring.
      setTimeout(() => { if (!this.streamOk) this.startPolling(); }, 8000);
      setInterval(() => { if (!this.streamOk) this.startPolling(); }, 15000);
    },
    addFeed(items) {
      if (this.feedPaused) return;
      const seen = new Set(this.feed.map((e) => e.id));
      const fresh = items.filter((e) => !seen.has(e.id)).sort((a, b) => b.id - a.id);
      if (!fresh.length) return;
      this.feed = [...fresh, ...this.feed].sort((a, b) => b.id - a.id).slice(0, 500);
    },
    startPolling() {
      if (this._poll) return;
      this.polling = true;
      const tick = () => this.api('api/events?limit=100').then((items) => this.addFeed(items)).catch(() => {});
      tick();
      this._poll = setInterval(tick, 3000);
    },
    stopPolling() {
      if (this._poll) clearInterval(this._poll);
      this._poll = null;
      this.polling = false;
    },

    // ── agents ─────────────────────────────────────────────────────────────────
    async loadAgents() { try { this.agentsList = await this.api('api/agents'); } catch (e) { this.agentError = e.message; } },
    newAgent() {
      this.agentIsNew = true;
      this.openForm({
        id: '', name: '', room: this.areas[0]?.area_id || '', enabled: true, mode: 'shadow', entities: [],
        max_entities: 20, tier_overrides: {}, system_prompt: '',
        wake_rules: { events: [], cron: [], max_wakes_per_hour: 12 },
        limits: { max_steps: 5, timeout_s: 45, context_budget_tokens: 3000 },
      });
    },
    async editAgent(id) {
      try {
        this.agentIsNew = false;
        this.openForm(await this.api('api/agents/' + encodeURIComponent(id)));
      } catch (e) { this.agentError = e.message; }
    },
    openForm(config) {
      const wr = config.wake_rules || {};
      config.wake_rules = {
        ...wr,
        cron: (wr.cron || []).map((c) => ({ ...c })),
        events: (wr.events || []).map((r) => ({
          ...r, _entities: (r.entities || []).join(', '), _to: (r.to || []).join(', '), _from: (r.from || []).join(', '),
        })),
      };
      config.limits = config.limits || {};
      config.tier_overrides = config.tier_overrides || {};
      config.entities = config.entities || [];
      this.agentForm = config;
      this.agentMsg = ''; this.agentError = ''; this.preview = null;
      this.loadRoomEntities();
    },
    closeForm() { this.agentForm = null; this.loadAgents(); },
    async loadRoomEntities() {
      if (!this.agentForm?.room) { this.roomEntities = []; return; }
      try { this.roomEntities = await this.api('api/entities?area=' + encodeURIComponent(this.agentForm.room)); }
      catch (e) { this.roomEntities = []; }
      this.schedulePreview();
    },
    toggleEntity(id) {
      const list = this.agentForm.entities;
      const i = list.indexOf(id);
      if (i >= 0) list.splice(i, 1); else list.push(id);
      this.schedulePreview();
    },
    setOverride(id, tier) {
      if (tier) this.agentForm.tier_overrides[id] = tier; else delete this.agentForm.tier_overrides[id];
      this.schedulePreview();
    },
    addEventRule() {
      this.agentForm.wake_rules.events.push({ _entities: '', _to: '', _from: '', debounce_s: 5, cooldown_s: 120, priority: 'routine', reason: '' });
    },
    addCron() { this.agentForm.wake_rules.cron.push({ expr: '0 7 * * *', reason: '' }); },
    agentPayload() {
      const f = this.agentForm;
      const list = (text) => (text || '').split(',').map((s) => s.trim()).filter(Boolean);
      const num = (v) => (v === '' || v == null ? undefined : Number(v));
      const events = f.wake_rules.events.map((r) => {
        const { _entities, _to, _from, ...rule } = r;
        const out = { ...rule, entities: list(_entities) };
        if (list(_to).length) out.to = list(_to); else delete out.to;
        if (list(_from).length) out.from = list(_from); else delete out.from;
        for (const k of ['debounce_s', 'cooldown_s', 'min_change', 'above', 'below']) {
          if (num(out[k]) === undefined) delete out[k]; else out[k] = num(out[k]);
        }
        if (!out.reason) delete out.reason;
        return out;
      });
      const cron = f.wake_rules.cron.filter((c) => c.expr).map((c) => (c.reason ? c : { expr: c.expr }));
      const limits = {};
      for (const [k, v] of Object.entries(f.limits)) if (num(v) !== undefined) limits[k] = num(v);
      return {
        ...f,
        max_entities: Number(f.max_entities) || 20,
        wake_rules: { ...f.wake_rules, events, cron, max_wakes_per_hour: Number(f.wake_rules.max_wakes_per_hour ?? 12) },
        limits,
      };
    },
    schedulePreview() {
      clearTimeout(this._previewTimer);
      this._previewTimer = setTimeout(() => this.loadPreview(), 400);
    },
    async loadPreview() {
      if (!this.agentForm) return;
      const id = this.agentForm.id || 'new';
      const body = { ...this.agentPayload(), id: /^[a-z][a-z0-9_]{1,31}$/.test(id) ? id : 'preview' };
      try {
        this.preview = await this.post('api/agents/' + encodeURIComponent(body.id) + '/prompt-preview', body);
        this.previewError = '';
      } catch (e) { this.previewError = e.message; }
    },
    async saveAgent() {
      this.agentMsg = ''; this.agentError = '';
      const body = this.agentPayload();
      try {
        const path = 'api/agents' + (this.agentIsNew ? '' : '/' + encodeURIComponent(body.id));
        const saved = await this.api(path, {
          method: this.agentIsNew ? 'POST' : 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        this.agentIsNew = false;
        this.openForm(saved);
        this.agentMsg = this.t('Saved.');
      } catch (e) { this.agentError = e.message; }
    },
    async deleteAgent(id) {
      if (!confirm(this.t('Delete this agent, its notes and its scheduled wakes? Its runs stay in the trace log.'))) return;
      try {
        const response = await fetch('api/agents/' + encodeURIComponent(id), { method: 'DELETE' });
        if (!response.ok) throw new Error(response.status + ' ' + response.statusText);
        this.agentForm = null;
      } catch (e) { this.agentError = e.message; }
      this.loadAgents();
    },
    async patchAgent(id, changes) {
      try {
        const config = await this.api('api/agents/' + encodeURIComponent(id));
        await this.put('api/agents/' + encodeURIComponent(id), { ...config, ...changes });
      } catch (e) { this.agentError = e.message; }
      this.loadAgents();
    },
    async wakeAgent(id) {
      try {
        const { run_id } = await this.post('api/agents/' + encodeURIComponent(id) + '/wake', {});
        this.agentMsg = this.t('Woken: run') + ' ' + run_id;
        location.hash = '#runs';
        this.openRun(run_id);
      } catch (e) { this.agentError = e.message; }
    },

    // ── runs, actions, approvals ───────────────────────────────────────────────
    async loadRuns() {
      const q = new URLSearchParams({ limit: '100' });
      if (this.runAgent) q.set('agent', this.runAgent);
      if (this.runStatus) q.set('status', this.runStatus);
      try { this.runs = await this.api('api/runs?' + q); } catch (e) { this.runMsg = e.message; }
      if (this.runSel && ['queued', 'running'].includes(this.runSel.status)) this.openRun(this.runSel.id);
    },
    async openRun(id) {
      try { this.runSel = await this.api('api/runs/' + encodeURIComponent(id)); } catch (e) { this.runMsg = e.message; }
    },
    async replayRun(id) {
      try {
        const { run_id } = await this.post('api/runs/' + encodeURIComponent(id) + '/replay', {});
        this.runMsg = this.t('Replaying in shadow mode as') + ' ' + run_id;
        await this.openRun(run_id);
        this.loadRuns();
      } catch (e) { this.runMsg = e.message; }
    },
    async copyTestcase(id) {
      try {
        const response = await fetch('api/runs/' + encodeURIComponent(id) + '/testcase');
        if (!response.ok) throw new Error(response.status + ' ' + response.statusText);
        const text = await response.text();
        try {
          await navigator.clipboard.writeText(text);
          this.runMsg = this.t('Test case copied. Save it under tests/eval/cases/.');
        } catch (e) {
          // clipboard needs a secure context; fall back to a download
          const a = document.createElement('a');
          a.href = URL.createObjectURL(new Blob([text], { type: 'text/yaml' }));
          a.download = 'run-' + id + '.yaml';
          a.click();
          this.runMsg = this.t('Test case downloaded.');
        }
      } catch (e) { this.runMsg = e.message; }
    },
    async loadActions() { try { this.actions = await this.api('api/actions?limit=100'); } catch (e) { /* banner */ } },
    async loadPending() {
      try {
        [this.pending, this.inbox] = await Promise.all([this.api('api/actions?verdict=pending'), this.api('api/inbox')]);
      } catch (e) { /* banner */ }
    },
    async decide(id, approve) {
      try { await this.post('api/approvals/' + encodeURIComponent(id), { approve }); } catch (e) { this.apiError = e.message; }
      this.loadPending();
      if (this.page === 'runs') this.loadActions();
    },
    stepText(step) {
      const d = step.data || {};
      if (step.type === 'tool') return d.name + '(' + JSON.stringify(d.arguments || {}) + ') → ' + JSON.stringify(d.result);
      if (step.type === 'policy') return d.verdict + ': ' + d.domain + '.' + d.service + ' ' + (d.entity_ids || []).join(', ') + ' — ' + d.reason;
      if (step.type === 'llm') {
        const calls = (d.tool_calls || []).map((c) => (c.function?.name || c.name) + '(…)').join(', ');
        return (d.content || '') + (calls ? ' [' + calls + ']' : '');
      }
      if (step.type === 'final') return d.summary || d.text || JSON.stringify(d);
      if (step.type === 'context') return '';
      return JSON.stringify(d);
    },
    json(value) { return JSON.stringify(value, null, 2); },

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
    until(ts) { return ts ? this.ago(2 * Date.now() / 1000 - ts) : '—'; },  // ago() of the mirrored time
    mb(value) { return value == null ? '—' : (value / 1024).toFixed(1) + ' GB'; },
    pct(a, b) { return a == null || !b ? 0 : Math.min(100, Math.round((a / b) * 100)); },
    deg(value) { return value == null ? '—' : value.toFixed(0) + ' °C'; },
    fmt(value) { return Array.isArray(value) ? value.join(', ') : String(value); },
  };
}

window.app = app;
