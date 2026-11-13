// Agent House GUI. No build step. Ingress rule: every URL is relative (no leading slash).

// Six tabs; a tab with several pages shows them as a sub-navigation. The hash is the page id.
const TABS = [
  { id: 'overview', label: 'Overview', pages: ['overview'] },
  { id: 'activity', label: 'Activity', pages: ['feed', 'runs'] },
  { id: 'rooms', label: 'Rooms', pages: ['agents'] },
  { id: 'automations', label: 'Automations', pages: ['proposals', 'automations', 'migration'] },
  { id: 'chat', label: 'Chat', pages: ['chat'] },
  { id: 'settings', label: 'Settings', pages: ['settings', 'models', 'logs'] },
];
const PAGE_LABELS = {
  feed: 'Live feed', runs: 'Runs', proposals: 'Drafts', automations: 'Deployed', migration: 'Your automations',
  settings: 'General', models: 'Models', logs: 'Logs',
};
// old hashes keep working (bookmarks, links in owner reports)
const PAGE_ALIASES = { dashboard: 'overview', activity: 'feed', rooms: 'agents' };
const PAGES = TABS.flatMap((tab) => tab.pages);

const TIER_HELP = {
  T0: 'free: the agent may act',
  T1: 'the agent may act and you are notified',
  T2: 'the agent may act only after your approval',
  T3: 'never: agents cannot actuate this entity',
};
const TIERS_TIP = 'T0 the agent acts\nT1 acts and tells you\nT2 asks you first\nT3 never acts\nAgent overrides can only make a tier stricter.';

// What an action verdict means for the owner: marker class + filter group.
const VERDICT_MARK = {
  executed: 'done', approved: 'done', shadow: 'would', pending: 'ask', denied: 'blocked', expired: 'blocked', failed: 'blocked',
};
const VERDICT_TEXT = {
  executed: 'Done', approved: 'Approved and done', shadow: 'Would have done it (shadow mode)', pending: 'Waiting for you',
  denied: 'Blocked by policy', expired: 'Approval expired or denied', failed: 'Home Assistant returned an error',
};
// Feed kinds that tell the owner what the agents did; the rest (state changes, system) stay in the Live feed.
const AGENT_KINDS = ['action', 'approval', 'report', 'proposal', 'deploy', 'council', 'run'];

function app() {
  return {
    tabs: TABS,
    pageLabels: PAGE_LABELS,
    page: 'overview',
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
    runtimeError: '',
    _stream: null,
    _poll: null,
    polling: false,
    // chat
    chatTarget: 'orchestrator',
    chatSession: '',
    chatLog: [],
    chatInput: '',
    chatBusy: false,
    chatError: '',
    configError: '',
    // migration
    originals: [],
    originalSel: null,
    parity: null,
    migrationError: '',
    migrationBusy: false,
    // agents
    agentsList: [],
    agentForm: null,
    agentIsNew: false,
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
    runsLoaded: false,
    actions: [],
    pending: [],
    // proposals and deployed automations (M4)
    proposals: [],
    proposalStatus: 'draft',
    proposalSel: null,
    proposalYaml: '',
    proposalError: '',
    proposalBusy: false,
    draftForm: { request: '', room: '', yaml: '', mode: 'request' },
    evidenceDays: 7,
    // history findings: habits, undone automations, conflicts (M5)
    findings: [],
    findingBusy: false,
    deployedList: [],
    deployedSel: null,
    deployedError: '',
    // overview
    drafts: [],
    asksLoaded: false,
    todayFilter: '',
    names: {},        // entity_id → friendly name, for readable action titles
    busy: {},         // id → true while a button's request runs
    leaving: {},      // id → true while a resolved card animates out
    toasts: [],

    async init() {
      this.applyTheme(this.stored('theme'));
      await this.setLang(this.stored('lang') || 'en');
      window.addEventListener('hashchange', () => this.route());
      this.route();
      await this.refresh();
      this.loadAreas();
      this.loadAgents();
      this.loadNames();
      setInterval(() => this.refresh(), 5000);
      setInterval(() => { this.loadAreas(); this.loadNames(); }, 60000);
      setInterval(() => { if (this.page === 'models') this.loadModels(); }, 3000);
      setInterval(() => {
        this.loadPending();
        if (this.page === 'runs') this.loadRuns();
        if (['overview', 'agents'].includes(this.page) && !this.agentForm) this.loadAgents();
      }, 5000);
      setInterval(() => this.loadAsks(), 20000);
      this.loadPending();
      this.loadAsks();
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
      const raw = (location.hash || '#overview').slice(1);
      const id = PAGE_ALIASES[raw] || raw;
      this.page = PAGES.includes(id) ? id : 'overview';
      if (this.page === 'overview') { this.loadAsks(); this.loadAgents(); }
      if (this.page === 'agents') { this.loadEntities(); this.loadAgents(); }
      if (this.page === 'runs') { this.loadRuns(); this.loadActions(); this.loadAgents(); }
      if (this.page === 'settings') this.loadSettings();
      if (this.page === 'logs') this.loadLogs();
      if (this.page === 'models') this.loadModels();
      if (this.page === 'proposals') { this.loadProposals(); this.loadAreas(); this.loadFindings(); }
      if (this.page === 'automations') this.loadDeployed();
      if (this.page === 'migration') this.loadOriginals();
      if (this.page === 'chat') this.loadAgents();
      window.scrollTo(0, 0);
    },
    get tab() { return TABS.find((tab) => tab.pages.includes(this.page)) || TABS[0]; },
    toast(text, kind) {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, text, kind: kind || '' });
      setTimeout(() => { this.toasts = this.toasts.filter((x) => x.id !== id); }, kind === 'bad' ? 6000 : 3500);
    },
    // Runs fn with a spinner on the button keyed by id; on success the card keyed by id slides out first.
    async withBusy(id, fn, { leave = false } = {}) {
      this.busy = { ...this.busy, [id]: true };
      try {
        const out = await fn();
        if (leave) { this.leaving = { ...this.leaving, [id]: true }; await new Promise((r) => setTimeout(r, 300)); }
        return out;
      } catch (e) {
        this.toast(e.message, 'bad');
        return undefined;
      } finally {
        const { [id]: _b, ...busy } = this.busy; this.busy = busy;
        const { [id]: _l, ...leaving } = this.leaving; this.leaving = leaving;
      }
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
      const TEXT = ['kill_switch_entity', 'orchestrator_mode', 'night_window'];
      for (const [k, v] of Object.entries(this.runtimeForm)) {
        if (k === 'daily_review_at') body[k] = v.trim();  // '' = no daily review
        else if (k === 'auto_house_mode') body[k] = v === true || v === 'true';
        else body[k] = v === '' ? null : (TEXT.includes(k) ? v.trim() : Number(v));
      }
      this.runtimeError = '';
      try {
        this.settings = await this.put('api/settings', body);
        this.runtimeForm = this.formFrom(this.settings.runtime);
        this.toast(this.t('Saved.'));
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
      try {
        this.status = await this.put('api/mode', { mode });
        this.toast(mode === 'live' ? this.t('Live mode on') : this.t('Shadow mode on'));
      } catch (e) { this.toast(e.message, 'bad'); }
    },
    // Stopping is instant; resuming asks first.
    async stopAll() {
      try {
        this.status = await this.put('api/kill-switch', { engaged: true, reason: 'stopped from the panel' });
        this.toast(this.t('All agents stopped'));
      } catch (e) { this.toast(e.message, 'bad'); }
    },
    async resumeAll() {
      if (!confirm(this.t('Resume the agents? They act again within their tiers and modes.'))) return;
      try {
        this.status = await this.put('api/kill-switch', { engaged: false });
        this.toast(this.t('Agents resumed'));
      } catch (e) { this.toast(e.message, 'bad'); }
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
        id: '', name: '', room: this.areas[0]?.area_id || '', enabled: true, mode: 'shadow', entities: [], entities_mode: 'only',
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
      config.entities_mode = config.entities_mode || 'only';
      this.agentForm = config;
      this.agentError = ''; this.preview = null;
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
      this.agentError = '';
      const body = this.agentPayload();
      try {
        const path = 'api/agents' + (this.agentIsNew ? '' : '/' + encodeURIComponent(body.id));
        const saved = await this.api(path, {
          method: this.agentIsNew ? 'POST' : 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        this.agentIsNew = false;
        this.openForm(saved);
        this.toast(this.t('Saved.'));
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
        this.toast(this.t('Woken: run') + ' ' + run_id);
        location.hash = '#runs';
        this.openRun(run_id);
      } catch (e) { this.agentError = e.message; }
    },

    // ── runs, actions, approvals ───────────────────────────────────────────────
    async loadRuns() {
      const q = new URLSearchParams({ limit: '100' });
      if (this.runAgent) q.set('agent', this.runAgent);
      if (this.runStatus) q.set('status', this.runStatus);
      try { this.runs = await this.api('api/runs?' + q); this.runsLoaded = true; } catch (e) { this.runMsg = e.message; }
      if (this.runSel && ['queued', 'running'].includes(this.runSel.status)) this.openRun(this.runSel.id);
    },
    async openRun(id) {
      try { this.runSel = await this.api('api/runs/' + encodeURIComponent(id)); } catch (e) { this.runMsg = e.message; }
    },
    async replayRun(id) {
      try {
        const { run_id } = await this.post('api/runs/' + encodeURIComponent(id) + '/replay', {});
        this.toast(this.t('Replaying in shadow mode as') + ' ' + run_id);
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
          this.toast(this.t('Test case copied. Save it under tests/eval/cases/.'));
        } catch (e) {
          // clipboard needs a secure context; fall back to a download
          const a = document.createElement('a');
          a.href = URL.createObjectURL(new Blob([text], { type: 'text/yaml' }));
          a.download = 'run-' + id + '.yaml';
          a.click();
          this.toast(this.t('Test case downloaded.'));
        }
      } catch (e) { this.runMsg = e.message; }
    },
    // ── proposals (M4) ─────────────────────────────────────────────────────────
    async loadProposals() {
      const query = this.proposalStatus ? '?status=' + encodeURIComponent(this.proposalStatus) : '';
      try { this.proposals = await this.api('api/proposals' + query); } catch (e) { this.proposalError = e.message; }
    },
    async loadFindings() {
      try { this.findings = (await this.api('api/findings')).filter((f) => f.status !== 'dismissed'); }
      catch (e) { this.toast(e.message, 'bad'); }
    },
    async scanHistory() {
      this.findingBusy = true;
      try {
        const r = await this.post('api/council/scan');
        this.toast(r.changes + ' ' + this.t('changes scanned') + ', ' + r.manual + ' ' + this.t('by hand') + ', '
          + r.findings.length + ' ' + this.t('findings'));
        await this.loadFindings();
      } catch (e) { this.toast(e.message, 'bad'); }
      this.findingBusy = false;
    },
    async findingAction(f, action) {
      const done = await this.withBusy(f.id, () => this.post('api/findings/' + encodeURIComponent(f.id) + '/' + action), { leave: true });
      if (done === undefined) return;
      this.toast(action === 'draft' ? this.t('Drafting a fix; it appears under Drafts in a moment.') : this.t('Dismissed'));
      this.loadFindings();
    },
    openProposal(p) {
      this.proposalSel = p;
      this.proposalYaml = p.yaml;
      this.proposalError = '';
    },
    async proposalAction(fn, done) {
      this.proposalBusy = true; this.proposalError = '';
      try {
        const result = await fn();
        if (result && result.id) this.openProposal(result);
        this.toast(done);
        this.loadAsks();
        this.loadProposals();
        this.refresh();
      } catch (e) { this.proposalError = e.message; }
      this.proposalBusy = false;
    },
    createProposal() {
      const f = this.draftForm;
      const body = f.mode === 'yaml' ? { yaml: f.yaml } : { request: f.request, room: f.room || undefined };
      return this.proposalAction(async () => {
        const created = await this.post('api/proposals', body);
        this.proposalStatus = '';
        return created;
      }, this.t('Drafted. Review the checks, then approve or reject.'));
    },
    saveProposal() {
      return this.proposalAction(() => this.put('api/proposals/' + this.proposalSel.id, { yaml: this.proposalYaml }),
        this.t('Saved; the checks ran again.'));
    },
    approveProposal() {
      if (!confirm(this.t('Deploy this automation to Home Assistant? It runs on its own from then on.'))) return;
      return this.proposalAction(() => this.post('api/proposals/' + this.proposalSel.id + '/approve'),
        this.t('Deployed. See Automations → Deployed.'));
    },
    rejectProposal() {
      const note = prompt(this.t('Why? (optional)')) ?? null;
      if (note === null) return;
      return this.proposalAction(() => this.post('api/proposals/' + this.proposalSel.id + '/reject', { note }),
        this.t('Rejected.'));
    },
    async computeEvidence() {
      this.proposalBusy = true; this.proposalError = '';
      try {
        const evidence = await this.post('api/proposals/' + this.proposalSel.id + '/evidence', { days: Number(this.evidenceDays) });
        this.proposalSel = { ...this.proposalSel, evidence };
      } catch (e) { this.proposalError = e.message; }
      this.proposalBusy = false;
    },
    diffClass(line) {
      if (line.startsWith('+') && !line.startsWith('+++')) return 'diff-add';
      if (line.startsWith('-') && !line.startsWith('---')) return 'diff-del';
      return '';
    },

    // ── deployed automations (M4) ──────────────────────────────────────────────
    async loadDeployed() {
      try {
        this.deployedList = await this.api('api/automations');
        if (this.deployedSel) this.deployedSel = this.deployedList.find((d) => d.ha_id === this.deployedSel.ha_id) || null;
      } catch (e) { this.deployedError = e.message; }
    },
    async deployedAction(fn, done) {
      this.deployedError = '';
      try { await fn(); this.toast(done); } catch (e) { this.deployedError = e.message; }
      this.loadDeployed();
      this.refresh();
    },
    setDeployedEnabled(d, enabled) {
      return this.deployedAction(() => this.put('api/automations/' + d.ha_id + '/enabled', { enabled }),
        enabled ? this.t('Enabled.') : this.t('Disabled.'));
    },
    rollbackDeployed(d, version) {
      if (!confirm(this.t('Deploy version') + ' ' + version + '?')) return;
      return this.deployedAction(() => this.post('api/automations/' + d.ha_id + '/rollback', { version }),
        this.t('Rolled back.'));
    },
    removeDeployed(d) {
      if (!confirm(this.t('Remove this automation from Home Assistant? Its versions stay here.'))) return;
      return this.deployedAction(async () => {
        const response = await fetch('api/automations/' + d.ha_id, { method: 'DELETE' });
        if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
        this.deployedSel = null;
      }, this.t('Removed.'));
    },

    // ── chat, config export / import (M5) ──────────────────────────────────────
    chatReset() { this.chatSession = ''; this.chatLog = []; this.chatError = ''; },
    async chatSend() {
      const message = this.chatInput.trim();
      if (!message || this.chatBusy) return;
      this.chatBusy = true; this.chatError = ''; this.chatInput = '';
      this.chatLog.push({ role: 'user', text: message });
      try {
        const body = { target: this.chatTarget, message };
        if (this.chatSession) body.session_id = this.chatSession;
        const out = await this.post('api/chat', body);
        this.chatSession = out.session_id;
        const tools = (out.run.trace || []).filter((s) => s.type === 'tool' || s.type === 'policy')
          .map((s) => (s.type === 'tool'
            ? s.data.name + ' ' + JSON.stringify(s.data.arguments || { proposal: s.data.proposal_id, status: s.data.status })
            : s.data.verdict + ': ' + s.data.domain + '.' + s.data.service + ' ' + (s.data.entity_ids || []).join(', ')));
        this.chatLog.push({ role: 'assistant', text: out.reply, tools, run: out.run.id, shadow: out.run.shadow });
      } catch (e) { this.chatError = e.message; }
      this.chatBusy = false;
    },
    async importConfig(event) {
      const file = event.target.files[0];
      event.target.value = '';
      if (!file) return;
      this.configError = '';
      try {
        const out = await this.api('api/config/import', { method: 'POST', headers: { 'Content-Type': 'text/yaml' }, body: await file.text() });
        this.toast(this.t('Imported agents:') + ' ' + (out.agents.join(', ') || '—')
          + (out.set_to_shadow.length ? ' · ' + this.t('set to shadow:') + ' ' + out.set_to_shadow.join(', ') : ''));
        this.loadSettings();
      } catch (e) { this.configError = e.message; }
    },

    // ── migration (M5) ─────────────────────────────────────────────────────────
    async loadOriginals() {
      try { this.originals = await this.api('api/migration/originals'); } catch (e) { this.migrationError = e.message; }
    },
    async migrationAction(fn, done) {
      this.migrationError = ''; this.migrationBusy = true;
      try { const out = await fn(); if (done) this.toast(done(out)); await this.loadOriginals(); }
      catch (e) { this.migrationError = e.message; }
      this.migrationBusy = false;
    },
    importOriginals() {
      return this.migrationAction(() => this.post('api/migration/import'),
        (r) => r.imported + ' ' + this.t('new') + ', ' + r.updated + ' ' + this.t('changed')
          + ((r.skipped || []).length ? ' · ' + r.skipped.join(' · ') : ''));
    },
    proposeReplacement(o) {
      return this.migrationAction(() => this.post('api/migration/originals/' + encodeURIComponent(o.id) + '/propose'),
        () => this.t('Drafting a replacement: it appears here and under Drafts in a moment.'));
    },
    setOriginalEnabled(o, enabled) {
      if (!enabled && !confirm(this.t('Switch your own automation off in Home Assistant? You can switch it on again here.'))) {
        return this.loadOriginals();
      }
      return this.migrationAction(() => this.put('api/migration/originals/' + encodeURIComponent(o.id) + '/enabled', { enabled }));
    },
    async loadParity(o) {
      this.originalSel = o; this.parity = null; this.migrationError = '';
      if (!o.replacement_proposal_id) return;
      try { this.parity = await this.api('api/migration/originals/' + encodeURIComponent(o.id) + '/parity?days=7'); }
      catch (e) { this.migrationError = e.message; }
    },
    sourceLabel(source) {
      return { ha_automation: 'HA automation', ha_script: 'HA script', node_red: 'Node-RED' }[source] || source;
    },

    async loadActions() { try { this.actions = await this.api('api/actions?limit=100'); } catch (e) { /* banner */ } },
    async loadPending() {
      try {
        this.pending = await this.api('api/actions?verdict=pending');
      } catch (e) { /* banner */ }
    },
    // Drafts and history findings: the other two kinds of "decisions waiting" on the overview.
    async loadAsks() {
      try {
        const [drafts, findings] = await Promise.all([this.api('api/proposals?status=draft'), this.api('api/findings')]);
        this.drafts = drafts;
        this.findings = findings.filter((f) => f.status !== 'dismissed');
        this.asksLoaded = true;
      } catch (e) { /* banner */ }
    },
    async loadNames() {
      try {
        const names = {};
        for (const e of await this.api('api/entities')) if (e.name) names[e.entity_id] = e.name;
        this.names = names;
      } catch (e) { /* titles fall back to entity ids */ }
    },
    async decide(id, approve) {
      const done = await this.withBusy(id, () => this.post('api/approvals/' + encodeURIComponent(id), { approve }), { leave: true });
      if (done !== undefined) this.toast(approve ? this.t('Approved') : this.t('Denied'));
      this.loadPending();
      if (this.page === 'runs') this.loadActions();
    },
    async rejectDraft(p) {
      const done = await this.withBusy(p.id, () => this.post('api/proposals/' + p.id + '/reject', { note: 'dismissed on the overview' }), { leave: true });
      if (done !== undefined) this.toast(this.t('Dismissed'));
      this.loadAsks();
    },
    reviewDraft(p) { location.hash = '#proposals'; this.openProposal(p); },
    get newFindings() { return this.findings.filter((f) => ['new', 'failed'].includes(f.status)); },
    get askCount() { return this.pending.length + this.drafts.length + this.newFindings.length; },
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
    tiersTip() { return this.t(TIERS_TIP); },

    // ── readable titles: "Office radiator: set temperature 21.5" instead of climate.set_temperature ──
    entityName(id) { return this.names[id] || (id || '').split('.').pop().replace(/_/g, ' '); },
    callTitle(domain, service, entityIds, data) {
      const who = (entityIds || []).map((id) => this.entityName(id)).join(', ') || domain;
      const d = data || {};
      const value = [['temperature', ''], ['brightness_pct', ' %'], ['position', ' %'], ['percentage', ' %'],
        ['hvac_mode', ''], ['preset_mode', ''], ['option', ''], ['value', '']]
        .filter(([k]) => d[k] != null).map(([k, unit]) => d[k] + unit)[0];
      return who + ': ' + service.replace(/_/g, ' ') + (value != null ? ' ' + value : '');
    },
    agentName(agentId) {
      const agent = this.agentsList.find((a) => a.id === agentId);
      if (!agent) return agentId === 'orchestrator' ? this.t('Orchestrator') : (agentId || '');
      return this.areaName(agent.room) !== agent.room ? this.areaName(agent.room) : agent.name;
    },
    actionTip(a) {
      return a.domain + '.' + a.service + ' ' + a.entity_ids.join(', ')
        + (Object.keys(a.data || {}).length ? '\n' + JSON.stringify(a.data) : '') + '\n' + a.tier + ' · ' + a.reason;
    },
    // Overview "Today": what the agents did, with a marker per outcome.
    todayItem(e) {
      const d = e.data || {};
      const m = /^(\w+): (\w+)\.(\w+) (\S+)/.exec(e.summary);
      if (m && ['action', 'approval'].includes(e.kind)) {
        return { mark: VERDICT_MARK[m[1]] || '', tip: this.t(VERDICT_TEXT[m[1]] || m[1]) + '\n' + e.summary, text: this.callTitle(m[2], m[3], [m[4]], {}) };
      }
      const text = e.agent_id && e.summary.startsWith(e.agent_id + ': ') ? e.summary.slice(e.agent_id.length + 2) : e.summary;
      if (e.kind === 'report') return { mark: d.severity === 'info' ? 'note' : 'warn', tip: this.t('Report'), text };
      if (e.kind === 'run') return { mark: 'blocked', tip: this.t('Run') + ' ' + (d.status || ''), text };
      return { mark: 'note', tip: e.kind, text };
    },
    get today() {
      const since = new Date().setHours(0, 0, 0, 0) / 1000;
      // successful runs are bookkeeping; a run shows up here only when it failed
      return this.feed.filter((e) => e.ts >= since && AGENT_KINDS.includes(e.kind) && !(e.kind === 'run' && e.level !== 'warning'))
        .map((e) => ({ ...e, view: this.todayItem(e) }));
    },
    todayCount(mark) { return this.today.filter((e) => (mark === 'note' ? ['note', 'warn'] : [mark]).includes(e.view.mark)).length; },
    filteredToday() {
      const f = this.todayFilter;
      return this.today.filter((e) => !f || e.view.mark === f || (f === 'note' && e.view.mark === 'warn')).slice(0, 40);
    },
    roomLine(area) {
      const agent = this.agentsList.find((a) => a.id === area.agent_id);
      if (!agent) return { text: this.t('no agent yet'), live: false, agent: null };
      const run = agent.last_run;
      const text = !agent.enabled ? this.t('disabled') : run ? (run.summary || run.error || run.status) + ' · ' + this.ago(run.started) : this.t('no run yet');
      return { text, live: agent.effective_mode === 'live', agent };
    },

    // ── header status ──
    get connection() {
      const s = this.status;
      if (!s) return { cls: 'off', text: '…', tip: '' };
      const ha = s.ha; const llm = s.llm || {}; const j = s.jetson || {};
      const lines = [
        'Home Assistant ' + (ha.version || '') + ' · ' + ha.state + ' · ' + (ha.entities ?? '—') + ' ' + this.t('entities'),
        this.t('Local model') + ': ' + (llm.state || '—') + (llm.active_profile ? ' · ' + llm.active_profile : '')
          + (llm.decode_tps ? ' · ' + this.num(llm.decode_tps) + ' tok/s' : ''),
      ];
      if (j.metrics) lines.push('Jetson ' + this.deg(j.metrics.temp_tj_c ?? j.metrics.temp_cpu_c) + ' · ' + this.mb(j.metrics.ram_used_mb) + ' / ' + this.mb(j.metrics.ram_total_mb));
      if (s.orchestrator) lines.push(this.t('Orchestrator') + ': ' + s.orchestrator.effective);
      const err = ha.last_error || llm.last_error;
      if (err) lines.push(err);
      const tip = lines.join('\n');
      if (!ha.connected) return { cls: 'bad', text: 'Home Assistant ' + ha.state, tip };
      if (['unreachable', 'degraded'].includes(llm.state)) return { cls: 'bad', text: this.t('Model') + ' ' + llm.state, tip };
      if (llm.state === 'warming') return { cls: 'warn', text: this.t('Model warming up'), tip };
      return { cls: '', text: this.t('Connected'), tip };
    },
    get modeTip() {
      const live = this.agentsList.filter((a) => a.effective_mode === 'live').length;
      return this.status?.mode === 'live'
        ? this.t('Live: agents whose own mode is live may act within their tiers.') + '\n' + live + ' ' + this.t('of') + ' ' + this.agentsList.length + ' ' + this.t('agents live')
        : this.t('Shadow: agents only observe and log what they would do. Nothing is switched.');
    },
    llmDot() {
      const state = this.status && this.status.llm && this.status.llm.state;
      if (state === 'ok') return '';
      if (state === 'warming' || state === 'busy') return 'warn';
      if (state === 'unconfigured' || !state) return 'off';
      return 'bad';
    },
    clock(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString() : ''; },
    hm(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''; },
    findingKind(kind) { return this.t({ habit: 'Habit', undo: 'Undone automation', conflict: 'Conflict' }[kind] || kind); },
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

// ── tooltips: any element with data-tip. Hover, keyboard focus, or a tap on an .info button.
// Positioned with fixed coordinates so cards and tables never clip it.
(() => {
  let tip = null;
  let owner = null;
  const show = (el) => {
    tip = tip || document.getElementById('tip');
    if (!tip || !el.dataset.tip) return;
    owner = el;
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    const x = Math.max(8, Math.min(r.left + r.width / 2 - tip.offsetWidth / 2, innerWidth - tip.offsetWidth - 8));
    const above = r.top - tip.offsetHeight - 8;
    tip.style.left = x + 'px';
    tip.style.top = (above < 8 ? r.bottom + 8 : above) + 'px';
    requestAnimationFrame(() => tip.classList.add('on'));
  };
  const hide = () => { owner = null; if (tip) { tip.classList.remove('on'); tip.hidden = true; } };
  document.addEventListener('pointerover', (e) => { const el = e.target.closest('[data-tip]'); if (el && el !== owner) show(el); });
  document.addEventListener('pointerout', (e) => { if (owner && !owner.contains(e.relatedTarget)) hide(); });
  document.addEventListener('focusin', (e) => { const el = e.target.closest('[data-tip]'); if (el) show(el); });
  document.addEventListener('focusout', hide);
  document.addEventListener('click', (e) => {
    const el = e.target.closest('.info[data-tip]');
    if (el) { e.preventDefault(); if (owner === el) hide(); else show(el); }
  });
  addEventListener('scroll', hide, true);
})();
