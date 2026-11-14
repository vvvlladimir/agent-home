<!-- Drafts: automation proposals (list → detail), a new draft from text or YAML, and history findings. -->
<script setup lang="ts">
import { computed, onActivated, ref } from 'vue';
import { api, type Finding, type Proposal } from '../api';
import Chips from '../components/Chips.vue';
import Icon from '../components/Icon.vue';
import IconButton from '../components/IconButton.vue';
import Info from '../components/Info.vue';
import Seg from '../components/Seg.vue';
import { ago } from '../format';
import { agentName, areaName, findingKind } from '../labels';
import { loadAsks, nav, state, t, toast, withBusy } from '../store';
import DraftDetail from './DraftDetail.vue';

type StatusFilter = '' | 'draft' | 'blocked' | 'deployed' | 'rejected';
const STATUS_CHIPS = [
  { value: 'draft' as StatusFilter, label: 'Drafts' }, { value: 'blocked' as StatusFilter, label: 'Blocked' },
  { value: 'deployed' as StatusFilter, label: 'Deployed' }, { value: 'rejected' as StatusFilter, label: 'Rejected' },
  { value: '' as StatusFilter, label: 'All' },
];
const INPUT_MODES = [['request', 'Describe'], ['yaml', 'YAML']] as const;

const status = ref<StatusFilter>('draft');
const proposals = ref<Proposal[]>([]);
const selId = ref('');
const draft = ref({ mode: 'request' as 'request' | 'yaml', request: '', room: '', yaml: '' });
const drafting = ref(false);
const error = ref('');
const scanning = ref(false);

async function load() {
  const q = status.value ? `?status=${encodeURIComponent(status.value)}` : '';
  try { proposals.value = await api.get<Proposal[]>(`api/proposals${q}`); } catch (e) { error.value = (e as Error).message; }
}
async function create() {
  const d = draft.value;
  const body = d.mode === 'yaml' ? { yaml: d.yaml } : { request: d.request, room: d.room || undefined };
  drafting.value = true; error.value = '';
  try {
    const created = await api.post<Proposal>('api/proposals', body);
    toast(t('Drafted. Review the checks, then approve or reject.'));
    d.request = ''; d.yaml = '';
    selId.value = created.id;
    loadAsks();
  } catch (e) { error.value = (e as Error).message; }
  drafting.value = false;
}
type Funnel = { days: number; min_days: number; entities: number; relevant: number; dossiers: number; left_out?: Record<string, number>; near?: { entity_id: string; days: number; needed: number }[] };
const funnel = ref<Funnel | null>(null);
async function loadFunnel() {
  try { funnel.value = ((await api.get<{ last_scan?: { habits?: Funnel } | null }>('api/council')).last_scan?.habits) || null; } catch { funnel.value = null; }
}
const funnelText = computed(() => {
  const f = funnel.value;
  if (!f) return '';
  const near = (f.near || []).slice(0, 3).map((n) => `${n.entity_id} (${n.days}/${n.needed} ${t('days')})`).join(', ');
  const left = Object.entries(f.left_out || {}).slice(0, 3).map(([why, n]) => `${n} ${t(why)}`).join('; ');
  return `${t('Last')} ${f.days} ${t('days')}: ${f.relevant} ${t('of')} ${f.entities} ${t('entities are devices you switch by hand on')} ≥ ${f.min_days} ${t('days')}, ${f.dossiers} ${t('regularly')}.`
    + (left ? ` ${t('Left out')}: ${left}.` : '') + (near ? ` ${t('Almost')}: ${near}.` : '');
});
async function scan() {
  scanning.value = true;
  const r = await withBusy('scan', () => api.post<{ changes: number; manual: number; findings: unknown[]; habits?: Funnel }>('api/council/scan'));
  if (r) { toast(`${r.changes} ${t('changes scanned')}, ${r.manual} ${t('by hand')}, ${r.findings.length} ${t('findings')}`); funnel.value = r.habits || null; }
  await loadAsks();
  scanning.value = false;
}
async function finding(f: Finding, action: 'draft' | 'dismiss') {
  const done = await withBusy(f.id, () => api.post(`api/findings/${encodeURIComponent(f.id)}/${action}`), true);
  if (done !== undefined) toast(action === 'draft' ? t('Drafting a fix; it appears here in a moment.') : t('Dismissed'));
  loadAsks();
}
function closeDetail() { selId.value = ''; load(); }
const draftedBy = () => {
  const p = state.status?.orchestrator?.effective || '…';
  return `${t('Drafted by')} ${p}${p === 'anthropic' || p === 'openai' ? ' ' + t('(cloud, redacted)') : ''}`;
};

onActivated(() => {
  if (nav.proposalId) { selId.value = nav.proposalId; nav.proposalId = ''; }
  load();
  loadAsks();
  loadFunnel();
});
</script>

<template>
  <section class="page">
    <DraftDetail v-if="selId" :id="selId" @close="closeDetail" />

    <div v-else class="cols side">
      <div class="sec">
        <div class="sec-h">
          <Chips v-model="status" :options="STATUS_CHIPS" :label="t('Status')" @update:model-value="load" />
          <Info :tip="t('Nothing reaches Home Assistant until you approve it. Deployed ids start with agent_house_.')" />
        </div>
        <div v-if="proposals.length" class="table-wrap">
          <table><tbody>
            <tr v-for="p in proposals" :key="p.id" class="clickable" @click="selId = p.id">
              <td><div class="cell-2"><span style="font-weight: 500">{{ p.title }}</span>
                <small>{{ p.origin_agent ? agentName(p.origin_agent) : t('You') }} · {{ ago(p.created) }}</small></div></td>
              <td class="act"><span class="tag" :class="({ deployed: 'ok', blocked: 'bad', draft: '' } as Record<string, string>)[p.status] ?? 'warn'">{{ p.status }}</span></td>
            </tr>
          </tbody></table>
        </div>
        <p v-else class="card empty">{{ t('No automations with this status.') }}</p>
      </div>

      <div class="sec">
        <article class="card stack">
          <h2>{{ t('New automation') }}</h2>
          <Seg v-model="draft.mode" :options="INPUT_MODES" :label="t('Input')" />
          <div v-if="draft.mode === 'request'" class="stack">
            <textarea v-model="draft.request" rows="3" :aria-label="t('What should happen, and when?')" :placeholder="t('Turn the bedroom lamp on when the radar sees someone after sunset')" />
            <label class="field"><span>{{ t('Room') }}</span>
              <select v-model="draft.room"><option value="">{{ t('Whole house') }}</option>
                <option v-for="a in state.areas" :key="a.area_id" :value="a.area_id">{{ a.name }}</option></select></label>
          </div>
          <textarea v-else v-model="draft.yaml" rows="8" class="mono" :aria-label="t('Automation YAML')" :placeholder="'alias: …\ntriggers: …\nactions: …'" />
          <div class="row">
            <button class="btn primary" :aria-busy="drafting || undefined"
                    :disabled="drafting || !(draft.mode === 'yaml' ? draft.yaml.trim() : draft.request.trim())" @click="create">{{ t('Draft') }}</button>
            <Info v-if="draft.mode === 'request'" :tip="draftedBy()" />
          </div>
          <p v-if="error" class="error">{{ error }}</p>
        </article>

        <article class="card stack">
          <div class="sec-h">
            <h2>{{ t('Found in the history') }} <Info :tip="t('Counted without a model: devices you switch by hand regularly (time of day, after a sensor, with someone home), automations you undo, automations that fight. The first look after install and the daily review draft the new ones.')" /></h2>
            <button class="btn sm" :aria-busy="scanning || undefined" :disabled="scanning" @click="scan"><Icon name="refresh" /><span>{{ t('Scan') }}</span></button>
          </div>
          <div v-for="f in state.findings" :key="f.id" class="row" :class="{ leaving: state.leaving[f.id] }" style="border-top: 1px solid var(--line); padding-top: 10px">
            <div class="grow cell-2"><span>{{ f.title }}</span>
              <small>{{ findingKind(f.kind) }}{{ f.room ? ' · ' + areaName(f.room) : '' }}{{ f.status !== 'new' ? ' · ' + f.status : '' }}</small></div>
            <button v-if="f.status === 'new' || f.status === 'failed'" class="btn sm" :disabled="state.busy[f.id]" @click="finding(f, 'draft')">{{ t('Draft') }}</button>
            <IconButton v-if="f.status !== 'drafting'" icon="x" :tip="t('Dismiss')" @click="finding(f, 'dismiss')" />
          </div>
          <p v-if="!state.findings.length" class="muted small">{{ t('Nothing found yet.') }}</p>
          <p v-if="funnelText" class="muted small">{{ funnelText }}</p>
        </article>
      </div>
    </div>
  </section>
</template>
