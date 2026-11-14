<!-- Overview: decisions waiting (T2 approvals, drafts, findings), rooms, and what the agents did today. -->
<script setup lang="ts">
import { computed, onActivated, ref } from 'vue';
import { api, type Area, type FeedItem, type Finding, type ManagerStatus, type Metrics, type Proposal } from '../api';
import AskCard from '../components/AskCard.vue';
import Chips from '../components/Chips.vue';
import Icon from '../components/Icon.vue';
import IconButton from '../components/IconButton.vue';
import Info from '../components/Info.vue';
import TierBadge from '../components/TierBadge.vue';
import { ago, hm, until } from '../format';
import { actionTip, actionTitle, agentName, areaName, callTitle, findingKind, VERDICT_MARK, VERDICT_TEXT } from '../labels';
import { go } from '../router';
import { decide, killed, loadAgents, loadAsks, nav, state, t, toast, withBusy } from '../store';

onActivated(() => { loadAsks(); loadAgents(); loadHealth(); });

// ── accuracy (D28) and the house manager (D27) ──
const metrics = ref<Metrics | null>(null);
const manager = ref<ManagerStatus | null>(null);
async function loadHealth() {
  try { manager.value = await api.get<ManagerStatus>('api/manager'); } catch { manager.value = null; }
  try { metrics.value = await api.get<Metrics>('api/metrics'); } catch { metrics.value = null; }
}
const pct = (v?: number | null) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`);
// same rule as core/metrics.is_weak: a point estimate (with enough data) under 0.6
const low = (v?: number | null) => v !== null && v !== undefined && v < 0.6;
const weakAutos = computed(() => (metrics.value?.automations || []).filter((a) => low(a.precision.value) || low(a.coverage.value)).slice(0, 5));
async function wakeManager() {
  const done = await withBusy('manager', () => api.post('api/manager/wake', { reason: 'from the overview' }), true);
  if (done !== undefined) toast(t('The house manager is looking.'));
  loadHealth();
}

const newFindings = computed(() => state.findings.filter((f) => f.status === 'new' || f.status === 'failed'));
const askCount = computed(() => state.pending.length + state.drafts.length + newFindings.value.length);

function openRun(id?: string | null) { if (id) { nav.runId = id; go('runs'); } }
function reviewDraft(p: Proposal) { nav.proposalId = p.id; go('proposals'); }
function openRoom(area: Area) {
  if (area.agent_id) nav.agentId = area.agent_id; else nav.areaSel = area.area_id;
  go('agents');
}
async function dismissDraft(p: Proposal) {
  const done = await withBusy(p.id, () => api.post(`api/proposals/${p.id}/reject`, { note: 'dismissed on the overview' }), true);
  if (done !== undefined) toast(t('Dismissed'));
  loadAsks();
}
async function finding(f: Finding, action: 'draft' | 'dismiss') {
  const done = await withBusy(f.id, () => api.post(`api/findings/${encodeURIComponent(f.id)}/${action}`), true);
  if (done === undefined) return;
  toast(action === 'draft' ? t('Drafting a fix; it appears under Drafts in a moment.') : t('Dismissed'));
  loadAsks();
}

function roomLine(area: Area) {
  const agent = state.agents.find((a) => a.id === area.agent_id);
  if (!agent) return { text: t('no agent yet'), live: false, none: true };
  const run = agent.last_run;
  const text = !agent.enabled ? t('disabled') : run ? `${run.summary || run.error || run.status} · ${ago(run.started)}` : t('no run yet');
  return { text, live: agent.effective_mode === 'live', none: false };
}

// ── today: what the agents did, one marker per outcome ──
// Feed kinds that tell the owner what the agents did; state changes and system lines stay in the Live feed.
const AGENT_KINDS = ['action', 'approval', 'report', 'proposal', 'deploy', 'council', 'run'];
interface TodayItem { e: FeedItem; mark: string; tip: string; text: string }
function view(e: FeedItem): TodayItem {
  const d = (e.data || {}) as Record<string, string>;
  const m = /^(\w+): (\w+)\.(\w+) (\S+)/.exec(e.summary);
  if (m && (e.kind === 'action' || e.kind === 'approval')) {
    return { e, mark: VERDICT_MARK[m[1]] || '', tip: `${t(VERDICT_TEXT[m[1]] || m[1])}\n${e.summary}`, text: callTitle(m[2], m[3], [m[4]]) };
  }
  const text = e.agent_id && e.summary.startsWith(e.agent_id + ': ') ? e.summary.slice(e.agent_id.length + 2) : e.summary;
  if (e.kind === 'report') return { e, mark: d.severity === 'info' ? 'note' : 'warn', tip: t('Report'), text };
  if (e.kind === 'run') return { e, mark: 'blocked', tip: `${t('Run')} ${d.status || ''}`, text };
  return { e, mark: 'note', tip: e.kind, text };
}
const today = computed(() => {
  const since = new Date().setHours(0, 0, 0, 0) / 1000;
  // successful runs are bookkeeping: a run shows up only when it failed
  return state.feed.filter((e) => e.ts >= since && AGENT_KINDS.includes(e.kind) && !(e.kind === 'run' && e.level !== 'warning')).map(view);
});
type Filter = '' | 'would' | 'done' | 'blocked' | 'note';
const filter = ref<Filter>('');
const group = (mark: string) => (mark === 'warn' ? 'note' : mark);
const count = (f: Filter) => today.value.filter((x) => group(x.mark) === f).length;
const chips = computed(() => [
  { value: '' as Filter, label: 'All', count: today.value.length },
  { value: 'would' as Filter, label: 'Would have', count: count('would'), mark: 'would' },
  { value: 'done' as Filter, label: 'Done', count: count('done'), mark: 'done' },
  { value: 'blocked' as Filter, label: 'Blocked', count: count('blocked'), mark: 'blocked' },
  { value: 'note' as Filter, label: 'Reports', count: count('note'), mark: 'note' },
]);
const shown = computed(() => today.value.filter((x) => !filter.value || group(x.mark) === filter.value).slice(0, 40));
</script>

<template>
  <section class="page">
    <div class="cols">
      <div class="sec">
        <div class="sec-h">
          <h2>{{ !state.asksLoaded ? t('Decisions') : askCount ? `${askCount} ${t(askCount === 1 ? 'decision waiting' : 'decisions waiting')}` : t('Nothing waiting') }}</h2>
        </div>
        <div v-if="!state.asksLoaded" class="stack"><div class="sk block" /><div class="sk block" /></div>
        <p v-else-if="!askCount" class="card empty">{{ t('Approvals, new automations and findings show up here.') }}</p>

        <AskCard v-for="(a, i) in state.pending" :key="a.id" :title="actionTitle(a)" :first="i === 0" :leaving="state.leaving[a.id]">
          <template #meta>
            <span>{{ agentName(a.agent_id) }}</span><TierBadge :tier="a.tier" />
            <span v-if="a.expires" class="exp">{{ t('expires in') }} {{ until(a.expires) }}</span>
          </template>
          <button class="btn primary" :class="{ lg: i === 0 }" :aria-busy="state.busy[a.id] || undefined" :disabled="state.busy[a.id] || killed()" @click="decide(a.id, true)">
            <Icon name="check" /><span>{{ t('Approve') }}</span></button>
          <button class="btn" :class="{ lg: i === 0 }" :disabled="state.busy[a.id]" @click="decide(a.id, false)">{{ t('Deny') }}</button>
          <Info :tip="actionTip(a)" />
          <template #end><IconButton v-if="a.run_id" icon="trace" :tip="t('Open trace')" @click="openRun(a.run_id)" /></template>
        </AskCard>

        <AskCard v-for="(p, i) in state.drafts" :key="p.id" :title="p.title" :first="!state.pending.length && i === 0" :leaving="state.leaving[p.id]">
          <template #meta><span>{{ p.origin_agent ? agentName(p.origin_agent) : t('You') }}</span><span>· {{ t('new automation') }}</span></template>
          <button class="btn primary" @click="reviewDraft(p)">{{ t('Review') }}</button>
          <Info v-if="p.rationale" :tip="p.rationale" :label="t('Why')" />
          <template #end><IconButton icon="x" :tip="t('Dismiss')" :busy="state.busy[p.id]" @click="dismissDraft(p)" /></template>
        </AskCard>

        <AskCard v-for="f in newFindings" :key="f.id" :title="f.title" :leaving="state.leaving[f.id]">
          <template #meta><span>{{ f.room ? areaName(f.room) : t('Whole house') }}</span><span>· {{ findingKind(f.kind) }}</span></template>
          <button class="btn" :aria-busy="state.busy[f.id] || undefined" :disabled="state.busy[f.id]" @click="finding(f, 'draft')">{{ t('Draft automation') }}</button>
          <Info :tip="f.request" />
          <template #end><IconButton icon="x" :tip="t('Dismiss')" @click="finding(f, 'dismiss')" /></template>
        </AskCard>
      </div>

      <div class="sec">
        <div class="sec-h"><h2>{{ t('Rooms') }}</h2><a class="btn quiet sm" href="#agents">{{ t('Manage') }}</a></div>
        <div class="card list">
          <div class="rooms">
            <a v-for="area in state.areas" :key="area.area_id" class="rm" href="#agents" @click.prevent="openRoom(area)">
              <b>{{ area.name }}</b>
              <span class="last" :class="{ faint: roomLine(area).none }">{{ roomLine(area).text }}</span>
              <span v-if="roomLine(area).live" class="live-dot" :data-tip="t('Acts by itself on T0–T1, asks you for T2.')">{{ t('Live') }}</span>
            </a>
          </div>
          <p v-if="!state.areas.length" class="empty">{{ state.status?.ha?.connected ? t('Home Assistant has no areas yet.') : t('Waiting for Home Assistant…') }}</p>
        </div>
      </div>
    </div>

    <div class="sec">
      <div class="sec-h"><h2>{{ t('Accuracy') }} <Info :tip="t('Last 7 days. Kept = not undone by hand within 10 minutes; missed = you did by hand what one of our automations does. Score = F1 of precision and coverage; the lower bound is the cautious number. Empty until there are 5 outcomes.')" /></h2>
        <button v-if="manager" class="btn quiet sm" :disabled="killed()" @click="wakeManager">{{ t('Ask the house manager') }}</button></div>
      <div class="card stack">
        <dl class="kv">
          <dt>{{ t('House score') }}</dt><dd>{{ pct(metrics?.program.score) }} <span class="faint">({{ t('at least') }} {{ pct(metrics?.program.score_low) }})</span></dd>
          <dt>{{ t('Precision') }}</dt><dd>{{ pct(metrics?.program.precision.value) }} <span class="faint">n={{ metrics?.program.precision.n ?? 0 }}</span></dd>
          <dt>{{ t('Coverage') }}</dt><dd>{{ pct(metrics?.program.coverage.value) }} <span class="faint">n={{ metrics?.program.coverage.n ?? 0 }}</span></dd>
          <dt>{{ t('Automated share') }}</dt><dd>{{ pct(metrics?.program.house_share.value) }}</dd>
          <dt>{{ t('House manager') }}</dt><dd>{{ manager ? (manager.enabled ? `${t('on')} · ${manager.mode}` : t('off')) : '—' }}<Info v-if="manager?.hint" :tip="manager.hint" /><span v-if="manager?.anomalies.length" class="faint"> · {{ manager.anomalies.length }} {{ t('things to look at') }}</span><span v-if="manager" class="faint"> · {{ manager.owned.length }} {{ t('without a room agent') }} <Info :tip="manager.owned_note || ''" /></span></dd>
        </dl>
        <ul v-if="weakAutos.length" class="feed">
          <li v-for="a in weakAutos" :key="a.ha_id"><i class="mk warn" /><span class="msg">{{ a.title || a.ha_id }} · F1 {{ pct(a.f1) }} <span class="where">{{ a.examples?.[0] }}</span></span></li>
        </ul>
      </div>
    </div>

    <div class="sec">
      <div class="sec-h"><h2>{{ t('Today') }}</h2><Chips v-model="filter" :options="chips" :label="t('Filter')" /></div>
      <div class="card list">
        <ul class="feed">
          <li v-for="x in shown" :key="x.e.id">
            <time>{{ hm(x.e.ts) }}</time>
            <i class="mk" :class="x.mark" :data-tip="x.tip" />
            <span class="msg">{{ x.text }} <span v-if="x.e.agent_id" class="where">· {{ agentName(x.e.agent_id) }}</span></span>
            <IconButton v-if="x.e.run_id" icon="trace" :tip="t('Open trace')" @click="openRun(x.e.run_id)" />
          </li>
        </ul>
        <p v-if="!shown.length" class="empty">{{ t('Nothing yet today.') }}</p>
      </div>
    </div>
  </section>
</template>
