<!-- One proposal: YAML (editable while a draft), checks, simulation against history, approve / reject. -->
<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { api, type Proposal } from '../api';
import DetailHead from '../components/DetailHead.vue';
import Icon from '../components/Icon.vue';
import Info from '../components/Info.vue';
import { dateTime } from '../format';
import { go } from '../router';
import { killed, loadAsks, nav, t, toast } from '../store';

const props = defineProps<{ id: string }>();
const emit = defineEmits<{ close: [] }>();

const p = ref<Proposal | null>(null);
const yaml = ref('');
const busy = ref(false);
const error = ref('');
const days = ref(7);

function show(next: Proposal) { p.value = next; yaml.value = next.yaml; }
onMounted(async () => {
  try { show(await api.get<Proposal>(`api/proposals/${props.id}`)); } catch (e) { error.value = (e as Error).message; }
});

async function act(fn: () => Promise<Proposal | unknown>, done: string) {
  busy.value = true; error.value = '';
  try {
    const result = await fn();
    if (result && typeof result === 'object' && 'id' in result) show(result as Proposal);
    toast(done);
    loadAsks();
  } catch (e) { error.value = (e as Error).message; }
  busy.value = false;
}
const editable = () => p.value?.status === 'draft' || p.value?.status === 'blocked';
const save = () => act(() => api.put(`api/proposals/${props.id}`, { yaml: yaml.value }), t('Saved; the checks ran again.'));
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
function openRun() { nav.runId = p.value!.run_id!; go('runs'); }
const ev = () => p.value!.evidence as { would_fire: number; days: number; blocked?: number; conditions_evaluated?: boolean; unsupported?: string[]; samples?: { ts: number; trigger: string }[] };
</script>

<template>
  <div v-if="!p" class="stack"><p v-if="error" class="error">{{ error }}</p><div v-else class="sk block" /></div>
  <div v-else class="sec">
    <DetailHead :title="p.title" @back="emit('close')">
      <template #badge><span class="tag" :class="({ deployed: 'ok', blocked: 'bad' } as Record<string, string>)[p.status] ?? ''">{{ p.status }}</span></template>
      <template v-if="editable()">
        <button class="btn quiet" :disabled="busy" @click="reject">{{ t('Reject') }}</button>
        <button class="btn" :disabled="busy || yaml === p.yaml" @click="save">{{ t('Save and re-check') }}</button>
        <button class="btn primary" :aria-busy="busy || undefined" :disabled="busy || p.status !== 'draft' || yaml !== p.yaml || killed()"
                :data-tip="yaml !== p.yaml ? t('Save the edit first.') : ''" @click="approve">{{ t('Approve and deploy') }}</button>
      </template>
    </DetailHead>
    <p v-if="error" class="error">{{ error }}</p>
    <div class="cols side">
      <div class="stack">
        <p v-if="p.rationale">{{ p.rationale }}</p>
        <article class="card stack">
          <div class="sec-h"><h2>YAML</h2><span class="mono faint">{{ p.ha_id }}</span></div>
          <textarea v-model="yaml" rows="14" class="mono" :readonly="!editable()" aria-label="YAML" />
          <details v-if="p.diff"><summary>{{ t('Diff against what is deployed') }}</summary>
            <pre class="log diff"><span v-for="(line, i) in p.diff.split('\n')" :key="i" :class="diffClass(line)">{{ line + '\n' }}</span></pre></details>
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
              <span v-if="!c.ok" class="error small">{{ c.detail || '' }}</span></li>
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
        <button v-if="p.run_id" class="btn quiet" @click="openRun"><Icon name="trace" /><span>{{ t('Drafting trace') }}</span></button>
      </div>
    </div>
  </div>
</template>
