<!-- One proposal: what it does in plain words (a model, on request), the automation as a flow chart
     or YAML (editable while a draft), a change in words by the model the owner picks, checks,
     simulation against history, approve / reject. -->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { api, type Proposal } from '../api';
import DetailHead from '../components/DetailHead.vue';
import FlowEditor from '../components/flow/FlowEditor.vue';
import Icon from '../components/Icon.vue';
import Info from '../components/Info.vue';
import ProviderPick from '../components/ProviderPick.vue';
import Seg from '../components/Seg.vue';
import type { Config } from '../flow';
import { dateTime } from '../format';
import { go } from '../router';
import { killed, loadAsks, nav, state, t, toast } from '../store';

const props = defineProps<{ id: string }>();
const emit = defineEmits<{ close: []; open: [id: string] }>();

const p = ref<Proposal | null>(null);
const yaml = ref('');
const busy = ref(false);
const error = ref('');
const days = ref(7);

// --- the automation: flow (an object) or YAML (text); the YAML is what is saved ---------------
const VIEWS = [['flow', 'Flow'], ['yaml', 'YAML']] as const;
const view = ref<'flow' | 'yaml'>('flow');
const config = ref<Config | null>(null);
const flowBase = ref('');     // the config as loaded or last converted, to tell an edit
const flowYaml = ref('');     // the YAML the config was made from
const flowError = ref('');
const flowDirty = computed(() => !!config.value && JSON.stringify(config.value) !== flowBase.value);
const dirty = computed(() => (view.value === 'flow' ? flowDirty.value : yaml.value !== p.value?.yaml));

async function toFlow(text: string): Promise<boolean> {
  try {
    const out = await api.post<{ config: Config }>('api/automation-yaml', { yaml: text });
    config.value = out.config; flowBase.value = JSON.stringify(out.config); flowYaml.value = text; flowError.value = '';
    return true;
  } catch (e) { flowError.value = (e as Error).message; return false; }
}
async function fromFlow(): Promise<string> {
  return (await api.post<{ yaml: string }>('api/automation-yaml', { config: config.value })).yaml;
}
async function switchView(next: 'flow' | 'yaml') {
  if (next === view.value) return;
  error.value = '';
  try {
    if (next === 'yaml' && flowDirty.value) yaml.value = await fromFlow();
    if (next === 'flow' && yaml.value !== flowYaml.value && !(await toFlow(yaml.value))) return;
    view.value = next;
  } catch (e) { error.value = (e as Error).message; }
}

async function show(next: Proposal) {
  p.value = next; yaml.value = next.yaml;
  if (!(await toFlow(next.yaml))) view.value = 'yaml';
}
onMounted(async () => {
  try { await show(await api.get<Proposal>(`api/proposals/${props.id}`)); } catch (e) { error.value = (e as Error).message; }
  explain(false, true);
});

async function act(fn: () => Promise<Proposal | unknown>, done: string) {
  busy.value = true; error.value = '';
  try {
    const result = await fn();
    if (result && typeof result === 'object' && 'id' in result) await show(result as Proposal);
    toast(done);
    loadAsks();
  } catch (e) { error.value = (e as Error).message; }
  busy.value = false;
}
const editable = () => p.value?.status === 'draft' || p.value?.status === 'blocked';
async function save() {
  await act(async () => {
    const text = view.value === 'flow' && flowDirty.value ? await fromFlow() : yaml.value;
    return api.put(`api/proposals/${props.id}`, { yaml: text });
  }, t('Saved; the checks ran again.'));
  explain(false, true);
}
function discard() { if (p.value) show(p.value); }
function approve() {
  if (!confirm(t('Deploy this automation to Home Assistant? It runs on its own from then on.'))) return;
  act(() => api.post(`api/proposals/${props.id}/approve`), t('Deployed. See Automations → Deployed.'));
}
function reject() {
  const note = prompt(t('Why? (optional)'));
  if (note === null) return;
  act(() => api.post(`api/proposals/${props.id}/reject`, { note }), t('Rejected.'));
}
async function simulate() {
  busy.value = true; error.value = '';
  try {
    const evidence = await api.post<Proposal['evidence']>(`api/proposals/${props.id}/evidence`, { days: Number(days.value) });
    p.value = { ...p.value!, evidence };
  } catch (e) { error.value = (e as Error).message; }
  busy.value = false;
}
const diffClass = (line: string) =>
  line.startsWith('+') && !line.startsWith('+++') ? 'diff-add' : line.startsWith('-') && !line.startsWith('---') ? 'diff-del' : '';
function openRun(id?: string | null) { nav.runId = id || p.value!.run_id!; go('runs'); }
const ev = () => p.value!.evidence as { would_fire: number; days: number; blocked?: number; conditions_evaluated?: boolean; unsupported?: string[]; samples?: { ts: number; trigger: string }[] };
const stats = computed(() => (p.value?.evidence ? { fired: ev().would_fire, blocked: ev().blocked, days: ev().days } : null));

// --- in plain words: a model explains it (cached per YAML; on open only the cached text) -----
const words = ref<{ text: string; provider: string; run_id?: string | null } | null>(null);
const wordsBy = ref('');
const wordsBusy = ref(false);
const wordsError = ref('');
async function explain(fresh = false, cachedOnly = false) {
  wordsBusy.value = !cachedOnly; wordsError.value = '';
  try {
    const out = await api.post<{ text: string; provider: string; run_id?: string | null }>(`api/proposals/${props.id}/explain`,
      { provider: wordsBy.value || undefined, language: state.lang, fresh, cached_only: cachedOnly });
    words.value = out.text ? out : cachedOnly ? null : out;
  } catch (e) { if (!cachedOnly) wordsError.value = (e as Error).message; }
  wordsBusy.value = false;
}
const PROVIDER: Record<string, string> = { anthropic: 'Claude', openai: 'OpenAI' };

// --- change it in words, on the model the owner picks ----------------------------------------
const ask = ref('');
const askBy = ref('');
const asking = ref(false);
async function revise() {
  asking.value = true; error.value = '';
  try {
    const out = await api.post<Proposal>(`api/proposals/${props.id}/revise`, { instruction: ask.value, provider: askBy.value || undefined });
    ask.value = '';
    if (out.id !== props.id) { toast(t('Drafted as a new proposal.')); emit('open', out.id); }
    else { await show(out); toast(t('Changed; the checks ran again.')); explain(false, true); }
    loadAsks();
  } catch (e) { error.value = (e as Error).message; }
  asking.value = false;
}
</script>

<template>
  <div v-if="!p" class="stack"><p v-if="error" class="error">{{ error }}</p><div v-else class="sk block" /></div>
  <div v-else class="sec">
    <DetailHead :title="p.title" @back="emit('close')">
      <template #badge><span class="tag" :class="({ deployed: 'ok', blocked: 'bad' } as Record<string, string>)[p.status] ?? ''">{{ p.status }}</span></template>
      <template v-if="editable()">
        <button class="btn quiet" :disabled="busy" @click="reject">{{ t('Reject') }}</button>
        <button v-if="dirty" class="btn quiet" :disabled="busy" @click="discard">{{ t('Discard edits') }}</button>
        <button class="btn" :disabled="busy || !dirty" @click="save">{{ t('Save and re-check') }}</button>
        <button class="btn primary" :aria-busy="busy || undefined" :disabled="busy || p.status !== 'draft' || dirty || killed()"
                :data-tip="dirty ? t('Save the edit first.') : ''" @click="approve">{{ t('Approve and deploy') }}</button>
      </template>
    </DetailHead>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="p.rationale" class="muted">{{ p.rationale }}</p>

    <article class="card stack">
      <div class="sec-h">
        <h2>{{ t('In plain words') }} <Info :tip="t('A model reads the automation and says what it does. Cached: asking again for the same YAML costs nothing.')" /></h2>
        <div class="row" style="align-items: end">
          <ProviderPick v-model="wordsBy" label="Explained by" />
          <button class="btn" :aria-busy="wordsBusy || undefined" :disabled="wordsBusy" @click="explain(!!words)">{{ words ? t('Ask again') : t('Explain') }}</button>
        </div>
      </div>
      <p v-if="words" class="explain">{{ words.text }}</p>
      <p v-if="words" class="small faint">{{ t('By') }} {{ t(PROVIDER[words.provider] || words.provider) }}<template v-if="words.run_id"> ·
        <a href="#" @click.prevent="openRun(words.run_id)">{{ t('trace') }}</a></template></p>
      <p v-else-if="!wordsBusy" class="small muted">{{ t('Not explained yet. The flow below shows it step by step.') }}</p>
      <p v-if="wordsError" class="error small">{{ wordsError }}</p>
    </article>

    <article class="card stack">
      <div class="sec-h">
        <Seg :model-value="view" :options="VIEWS" :label="t('View')" @update:model-value="switchView" />
        <span class="mono faint">{{ p.ha_id }}</span>
      </div>
      <template v-if="view === 'flow'">
        <FlowEditor v-if="config" v-model="config" :readonly="!editable()" :stats="stats" />
        <p v-if="flowError" class="error small">{{ t('This YAML cannot be drawn:') }} {{ flowError }}</p>
      </template>
      <textarea v-else v-model="yaml" rows="16" class="mono" :readonly="!editable()" aria-label="YAML" />
      <p v-if="dirty" class="small warn-text">{{ t('Edited, not saved. Save and re-check runs the same checks as before; nothing reaches Home Assistant until you approve.') }}</p>
      <details v-if="p.diff"><summary>{{ t('Diff against what is deployed') }}</summary>
        <pre class="log diff"><span v-for="(line, i) in p.diff.split('\n')" :key="i" :class="diffClass(line)">{{ line + '\n' }}</span></pre></details>
    </article>

    <div class="cols side">
      <div class="stack">
        <article class="card stack">
          <h2>{{ t('Change it in words') }} <Info :tip="editable() ? t('The model edits this draft; the checks run again. Nothing is deployed.') : t('This one is not a draft any more: the model writes a new draft from it.')" /></h2>
          <textarea v-model="ask" rows="3" :aria-label="t('What should change?')" :placeholder="t('Only after sunset, and switch it off again after 10 minutes')" />
          <div class="row" style="align-items: end">
            <ProviderPick v-model="askBy" />
            <button class="btn primary" :aria-busy="asking || undefined" :disabled="asking || !ask.trim() || killed()" @click="revise">
              {{ editable() ? t('Change') : t('Draft a changed copy') }}</button>
          </div>
        </article>
        <p v-if="p.note" class="muted small">{{ t('Note:') }} {{ p.note }}</p>
      </div>
      <div class="stack">
        <article v-if="p.supersedes?.length" class="card stack" style="border-color: var(--warn)">
          <h2 class="warn-text">{{ t('Approving switches off') }}
            <Info :tip="t('These automations of ours are replaced by this one. You can switch them on again under Deployed.')" /></h2>
          <ul class="checks"><li v-for="id in p.supersedes" :key="id" class="mono">{{ id }}</li></ul>
        </article>
        <article class="card stack">
          <h2>{{ t('Checks') }}</h2>
          <ul class="checks">
            <li v-for="c in (p.checks as { name: string; ok: boolean; detail?: string }[])" :key="c.name">
              <i class="mk" :class="c.ok ? 'done' : 'blocked'" /><span :data-tip="c.detail || ''">{{ c.name }}</span>
              <span v-if="!c.ok" class="error small">{{ c.detail || '' }}</span>
              <span v-else-if="c.detail" class="muted small">{{ c.detail }}</span></li>
          </ul>
        </article>
        <article class="card stack">
          <div class="sec-h"><h2>{{ t('Against history') }}
            <Info :tip="t('Replays the triggers over recorded history and checks the conditions (state, numeric, time) at each firing.') + (p.expected_triggers ? '\n' + t('Expected to fire:') + ' ' + p.expected_triggers : '')" /></h2></div>
          <div class="row" style="align-items: end">
            <label class="field" style="width: 6rem"><span>{{ t('Days') }}</span><input v-model="days" type="number" min="1" max="30" /></label>
            <button class="btn" :disabled="busy" @click="simulate">{{ t('Simulate') }}</button>
          </div>
          <div v-if="p.evidence" class="stack">
            <div class="row"><span class="big">{{ ev().would_fire }}</span>
              <span class="muted small">{{ t('times it would have fired in') }} {{ ev().days }} {{ t('days') }}<template v-if="ev().blocked"> · {{ ev().blocked }} {{ t('stopped by its conditions') }}</template><template v-if="ev().conditions_evaluated === false"> · {{ t('upper bound: some conditions were not checked') }}</template></span></div>
            <p v-if="ev().unsupported?.length" class="error small">{{ t('Not simulated:') }} {{ ev().unsupported!.join('; ') }}</p>
            <ul class="feed small">
              <li v-for="(x, i) in (ev().samples || []).slice(0, 8)" :key="i" style="grid-template-columns: auto minmax(0, 1fr)">
                <time>{{ dateTime(x.ts) }}</time><span class="mono">{{ x.trigger }}</span></li>
            </ul>
          </div>
        </article>
        <button v-if="p.run_id" class="btn quiet" @click="openRun()"><Icon name="trace" /><span>{{ t('Drafting trace') }}</span></button>
      </div>
    </div>
  </div>
</template>
