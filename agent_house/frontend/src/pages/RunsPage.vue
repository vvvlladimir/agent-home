<!-- Runs: every agent run with its trace (replay, copy as a test case) and the action log. -->
<script setup lang="ts">
import { onActivated, onDeactivated, ref } from 'vue';
import { api, enc, type Action, type Run, type RunSummary, type Step } from '../api';
import Icon from '../components/Icon.vue';
import IconButton from '../components/IconButton.vue';
import TierBadge from '../components/TierBadge.vue';
import { clock, json, num } from '../format';
import { actionTip, actionTitle, agentName, VERDICT_MARK } from '../labels';
import { decide, fail, killed, nav, state, t, toast } from '../store';

const runs = ref<RunSummary[]>([]);
const loaded = ref(false);
const actions = ref<Action[]>([]);
const sel = ref<Run | null>(null);
const agent = ref('');
const status = ref('');
const STATUSES = ['queued', 'running', 'ok', 'error', 'timeout', 'dropped', 'escalated'];

async function loadRuns() {
  const q = new URLSearchParams({ limit: '100' });
  if (agent.value) q.set('agent', agent.value);
  if (status.value) q.set('status', status.value);
  try { runs.value = await api.get<RunSummary[]>(`api/runs?${q}`); loaded.value = true; } catch (e) { fail(e); }
  if (sel.value && ['queued', 'running'].includes(sel.value.status)) open(sel.value.id);
}
async function loadActions() {
  try { actions.value = await api.get<Action[]>('api/actions?limit=100'); } catch { /* banner */ }
}
async function open(id: string) {
  try { sel.value = await api.get<Run>(`api/runs/${enc(id)}`); scrollTo(0, 0); } catch (e) { fail(e); }
}
async function replay(id: string) {
  try {
    const { run_id } = await api.post<{ run_id: string }>(`api/runs/${enc(id)}/replay`);
    toast(`${t('Replaying in shadow mode as')} ${run_id}`);
    await open(run_id);
    loadRuns();
  } catch (e) { fail(e); }
}
async function copyTestcase(id: string) {
  try {
    const text = await api.get<string>(`api/runs/${enc(id)}/testcase`);
    try {
      await navigator.clipboard.writeText(text);
      toast(t('Test case copied. Save it under tests/eval/cases/.'));
    } catch {
      // the clipboard needs a secure context; fall back to a download
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([text], { type: 'text/yaml' }));
      a.download = `run-${id}.yaml`;
      a.click();
      toast(t('Test case downloaded.'));
    }
  } catch (e) { fail(e); }
}
async function decideAndReload(id: string, approve: boolean) {
  await decide(id, approve);
  loadActions();
}

function stepText(step: Step) {
  const d = (step.data || {}) as Record<string, any>;
  if (step.type === 'tool') return `${d.name}(${JSON.stringify(d.arguments || {})}) → ${JSON.stringify(d.result)}`;
  if (step.type === 'policy') return `${d.verdict}: ${d.domain}.${d.service} ${(d.entity_ids || []).join(', ')} — ${d.reason}`;
  if (step.type === 'llm') {
    const calls = (d.tool_calls || []).map((c: any) => `${c.function?.name || c.name}(…)`).join(', ');
    return (d.content || '') + (calls ? ` [${calls}]` : '');
  }
  if (step.type === 'final') return d.summary || d.text || JSON.stringify(d);
  if (step.type === 'context') return '';
  return JSON.stringify(d);
}
const stepMeta = (s: Step) => [s.latency_ms != null ? `${s.latency_ms} ms` : '', s.tokens_in != null ? `${s.tokens_in}→${s.tokens_out} tok` : ''].filter(Boolean).join(' · ');
const statusClass = (s: string) => (({ ok: 'ok', escalated: 'warn', running: 'warn', queued: 'warn', dropped: '' }) as Record<string, string>)[s] ?? 'bad';

let timer: number | undefined;
onActivated(() => {
  if (nav.runId) { open(nav.runId); nav.runId = ''; }
  loadRuns(); loadActions();
  timer = setInterval(loadRuns, 5000);
});
onDeactivated(() => clearInterval(timer));
</script>

<template>
  <section class="page">
    <article v-if="sel" class="card stack">
      <div class="sec-h">
        <h2>{{ t('Trace') }} <span class="mono faint">{{ sel.id }}</span>
          <span v-if="sel.replay_of" class="tag">{{ t('replay of') }} {{ sel.replay_of }}</span></h2>
        <div class="row">
          <button class="btn" :data-tip="t('Runs the same wake again in shadow mode.')" @click="replay(sel.id)"><Icon name="refresh" /><span>{{ t('Replay') }}</span></button>
          <button class="btn quiet" @click="copyTestcase(sel.id)">{{ t('Copy as test case') }}</button>
          <IconButton icon="x" :tip="t('Close')" @click="sel = null" />
        </div>
      </div>
      <p class="small muted">{{ [sel.provider, sel.profile].filter(Boolean).join(' · ') }}</p>
      <div>
        <details v-for="s in sel.trace" :key="s.idx" :open="s.type !== 'context'">
          <summary><span class="tag">{{ s.type }}</span><span class="grow">{{ stepText(s) }}</span><span class="faint small">{{ stepMeta(s) }}</span></summary>
          <div v-if="s.type === 'context'" class="stack">
            <h3>{{ t('System') }}</h3><pre class="log">{{ (s.data as any).system }}</pre>
            <h3>{{ t('User') }}</h3><pre class="log">{{ (s.data as any).user }}</pre>
          </div>
          <pre v-else class="log">{{ json(s.data) }}</pre>
        </details>
      </div>
    </article>

    <div class="sec">
      <div class="toolbar">
        <label class="field"><span>{{ t('Agent') }}</span>
          <select v-model="agent" @change="loadRuns"><option value="">{{ t('All') }}</option>
            <option v-for="a in state.agents" :key="a.id" :value="a.id">{{ agentName(a.id) }}</option></select></label>
        <label class="field"><span>{{ t('Status') }}</span>
          <select v-model="status" @change="loadRuns"><option value="">{{ t('All') }}</option>
            <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option></select></label>
      </div>
      <div v-if="!loaded" class="sk block" />
      <div v-else-if="runs.length" class="table-wrap">
        <table>
          <thead><tr><th>{{ t('Time') }}</th><th>{{ t('Agent') }}</th><th>{{ t('Status') }}</th><th>{{ t('Wake') }}</th><th>{{ t('Summary') }}</th><th>{{ t('Latency') }}</th></tr></thead>
          <tbody>
            <tr v-for="r in runs" :key="r.id" class="clickable" :class="{ sel: sel?.id === r.id }" @click="open(r.id)">
              <td class="nowrap">{{ clock(r.started) }}</td>
              <td>{{ agentName(r.agent_id) }}</td>
              <td class="nowrap" :data-tip="`${r.steps} ${t('steps')} · ${r.tokens_in} / ${r.tokens_out} ${t('tokens in / out')}`">
                <span class="tag" :class="statusClass(r.status)">{{ r.status }}</span>
                <i v-if="r.shadow" class="mk would" :data-tip="t('Shadow: nothing was switched')" /></td>
              <td class="small" :data-tip="r.wake_reason || ''">{{ r.wake_source }}</td>
              <td class="small">{{ r.error || r.summary || '' }}</td>
              <td class="nowrap faint">{{ num(r.latency_ms, 'ms', 0) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="card empty">{{ t('No runs yet. Agents run when a wake rule fires, on a schedule or when you wake them.') }}</p>
    </div>

    <div v-if="actions.length" class="sec">
      <div class="sec-h"><h2>{{ t('Actions') }}</h2></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>{{ t('Time') }}</th><th>{{ t('Agent') }}</th><th>{{ t('Action') }}</th><th>{{ t('Tier') }}</th><th>{{ t('Result') }}</th><th /></tr></thead>
          <tbody>
            <tr v-for="a in actions" :key="a.id">
              <td class="nowrap">{{ clock(a.ts) }}</td>
              <td>{{ agentName(a.agent_id) }}</td>
              <td :data-tip="actionTip(a)">{{ actionTitle(a) }}</td>
              <td><TierBadge :tier="a.tier" /></td>
              <td class="nowrap"><span class="row"><i class="mk" :class="VERDICT_MARK[a.verdict] || 'blocked'" />
                <span :data-tip="a.reason + (a.decided_by ? ` (${a.decided_by})` : '')">{{ a.verdict }}</span></span></td>
              <td class="act">
                <span v-if="a.verdict === 'pending'" class="row">
                  <button class="btn primary sm" :disabled="state.busy[a.id] || killed()" @click="decideAndReload(a.id, true)">{{ t('Approve') }}</button>
                  <button class="btn sm" :disabled="state.busy[a.id]" @click="decideAndReload(a.id, false)">{{ t('Deny') }}</button></span>
                <IconButton v-if="a.run_id" icon="trace" :tip="t('Open trace')" @click="open(a.run_id)" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
