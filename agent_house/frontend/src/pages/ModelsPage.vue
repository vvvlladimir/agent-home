<!-- Models: the local inference server, benchmarks, profiles. Polls while open. -->
<script setup lang="ts">
import { computed, onActivated, onDeactivated, ref } from 'vue';
import { api, type ModelProfile } from '../api';
import Info from '../components/Info.vue';
import { deg, mb, num, pct } from '../format';
import { llmClass } from '../labels';
import { state, t, toast } from '../store';

interface Bench { id: string; created: string; profile: string; status: string; progress?: { done: number; total: number; label: string } | null; report?: any }
const profiles = ref<ModelProfile[]>([]);
const benchmarks = ref<Bench[]>([]);
const quick = ref(true);
const open = ref('');
const error = ref('');
const running = computed(() => benchmarks.value.some((b) => b.status === 'running'));
const llm = computed(() => state.status?.llm);
const metrics = computed(() => state.status?.jetson?.metrics as Record<string, any> | null | undefined);

async function load() {
  try {
    [profiles.value, benchmarks.value] = await Promise.all([api.get<ModelProfile[]>('api/models/profiles'), api.get<Bench[]>('api/benchmarks')]);
    error.value = '';
  } catch (e) { error.value = (e as Error).message; }
}
async function activate(name: string) {
  if (!confirm(t('Restart the inference server with this profile? Agents wait while the model loads.'))) return;
  try {
    const next = await api.put<NonNullable<typeof llm.value>>('api/models/active', { profile: name });
    if (state.status) state.status.llm = next;
    toast(`${t('Switching to')} ${name}`);
  } catch (e) { error.value = (e as Error).message; }
  load();
}
async function bench() {
  try { await api.post('api/benchmarks', { quick: quick.value }); } catch (e) { error.value = (e as Error).message; }
  load();
}
const toolScore = (r: any) => (r?.toolcall ? `${r.toolcall.passed}/${r.toolcall.total}` : '—');
let timer: number | undefined;
onActivated(() => { load(); timer = setInterval(load, 3000); });
onDeactivated(() => clearInterval(timer));
</script>

<template>
  <section class="page">
    <p v-if="error" class="error">{{ error }}</p>
    <div class="cols even">
      <article class="card stack">
        <div class="sec-h">
          <h2>{{ t('Inference server') }} <Info :tip="t('Rolling medians of the last 20 requests.')" /></h2>
          <span class="status" :class="llmClass()">{{ llm?.state || '…' }}</span>
        </div>
        <div class="row" style="gap: 28px">
          <div><span class="big">{{ num(llm?.decode_tps) }}</span><div class="muted small">decode tok/s</div></div>
          <div><span class="big">{{ num(llm?.prefill_tps) }}</span><div class="muted small">prefill tok/s</div></div>
          <div><span class="big">{{ num(llm?.ttft_ms, '', 0) }}</span><div class="muted small">{{ t('ms to first token') }}</div></div>
        </div>
        <dl class="kv">
          <dt>{{ t('Profile') }}</dt><dd class="mono">{{ llm?.active_profile || '—' }}</dd>
          <dt>{{ t('Model') }}</dt><dd class="mono">{{ llm?.model || '—' }}</dd>
          <dt>{{ t('Queue') }}</dt><dd>{{ llm?.queue_depth ?? 0 }}{{ llm?.in_flight ? ` · ${t('serving')} ${llm.in_flight}` : '' }}</dd>
          <dt>{{ t('Requests / dropped / errors') }}</dt><dd>{{ llm?.requests_total ?? 0 }} / {{ llm?.dropped_total ?? 0 }} / {{ llm?.errors_total ?? 0 }}</dd>
          <template v-if="metrics">
            <dt>Jetson</dt>
            <dd>{{ mb(metrics.ram_used_mb) }} / {{ mb(metrics.ram_total_mb) }}
              <span class="meter"><span :style="{ width: pct(metrics.ram_used_mb, metrics.ram_total_mb) + '%' }" /></span>
              · {{ deg(metrics.temp_tj_c ?? metrics.temp_cpu_c) }}
              <span v-if="metrics.throttled" class="tag warn">{{ t('throttling') }}</span></dd>
          </template>
        </dl>
        <p v-if="llm?.last_error" class="error">{{ llm.last_error }}</p>
      </article>

      <article class="card stack">
        <h2>{{ t('Benchmark') }} <Info :tip="t('Speed and the tool-call eval against the loaded model, at background priority: room agents go first. Quick is a smoke test. The full comparison of all profiles runs on the Jetson: jetson/bench/run_all.sh.')" /></h2>
        <div class="row">
          <button class="btn primary" :aria-busy="running || undefined" :disabled="running || !['ok', 'busy'].includes(llm?.state || '')" @click="bench">
            {{ running ? t('Running') : t('Run benchmark') }}</button>
          <label class="field check"><input v-model="quick" type="checkbox" /> <span>{{ t('Quick') }}</span></label>
        </div>
        <div v-for="b in benchmarks.filter((b) => b.progress)" :key="b.id" class="row">
          <span class="meter" style="width: 160px"><span :style="{ width: pct(b.progress!.done, b.progress!.total) + '%' }" /></span>
          <span class="muted small">{{ b.progress!.done }}/{{ b.progress!.total }} · {{ b.progress!.label }}</span>
        </div>
      </article>
    </div>

    <div class="sec">
      <div class="sec-h"><h2>{{ t('Profiles') }}</h2>
        <span v-if="!state.status?.jetson?.configured" class="muted small">{{ t('Switching needs jetson_metrics_url and jetson_token in the add-on options.') }}</span></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>{{ t('Profile') }}</th><th>{{ t('Model') }}</th><th>{{ t('Context') }}</th><th /></tr></thead>
          <tbody>
            <tr v-for="p in profiles" :key="p.name">
              <td><div class="cell-2"><span class="row"><span class="mono">{{ p.name }}</span>
                <span v-if="p.active" class="tag ok">{{ t('active') }}</span>
                <span v-if="p.provisional" class="tag" :data-tip="t('Not confirmed by a benchmark on your Jetson yet')">{{ t('provisional') }}</span>
                <span v-if="p.experimental" class="tag warn">{{ t('experimental') }}</span></span>
                <small v-if="p.notes">{{ p.notes }}</small></div></td>
              <td class="mono small" :data-tip="`T ${p.temperature} · top_p ${p.top_p} · top_k ${p.top_k} · pp ${p.presence_penalty}`">{{ p.model }}</td>
              <td>{{ p.ctx_size }}</td>
              <td class="act"><button class="btn sm" :disabled="p.active || running || llm?.state === 'warming' || !state.status?.jetson?.configured" @click="activate(p.name)">{{ t('Activate') }}</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="benchmarks.length" class="sec">
      <div class="sec-h"><h2>{{ t('Benchmark history') }}</h2></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>{{ t('When') }}</th><th>{{ t('Profile') }}</th><th>{{ t('Status') }}</th><th>{{ t('Decode') }}</th><th>{{ t('Prefill') }}</th><th>{{ t('Wake') }}</th><th>{{ t('Tool calls') }}</th></tr></thead>
          <tbody>
            <tr v-for="b in benchmarks" :key="b.id" class="clickable" :class="{ sel: open === b.id }" @click="open = open === b.id ? '' : b.id">
              <td class="nowrap">{{ new Date(b.created).toLocaleString() }}</td>
              <td class="mono small">{{ b.profile }}</td>
              <td><span class="tag" :class="b.status === 'done' ? 'ok' : b.status === 'error' ? 'bad' : 'warn'">{{ b.status }}</span></td>
              <td>{{ num(b.report?.speed?.decode_tps, 'tok/s') }}</td>
              <td>{{ num(b.report?.speed?.prefill_tps_warm ?? b.report?.speed?.prefill_tps, 'tok/s') }}</td>
              <td :data-tip="`TTFT ${num(b.report?.speed?.ttft_ms_warm, 'ms', 0)} · RAM ${mb(b.report?.resources?.ram_peak_mb)} · ${deg(b.report?.resources?.temp_max_c)}`">{{ num(b.report?.speed?.e2e_wake_ms_warm, 'ms', 0) }}</td>
              <td>{{ toolScore(b.report) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <article v-for="b in benchmarks.filter((b) => b.id === open && b.report)" :key="b.id" class="card stack">
        <h2>{{ b.profile }} · {{ toolScore(b.report) }} {{ t('tool calls') }} · {{ b.report.toolcall.json_valid }} {{ t('with clean JSON') }}</h2>
        <dl class="kv"><template v-for="[cat, c] in Object.entries(b.report.toolcall.by_category || {}) as [string, any][]" :key="cat"><dt>{{ cat }}</dt><dd>{{ c.passed }}/{{ c.total }}</dd></template></dl>
        <ul v-if="b.report.toolcall.failures.length" class="feed">
          <li v-for="f in b.report.toolcall.failures" :key="f.id" style="grid-template-columns: auto minmax(0, 1fr)"><span class="mono small">{{ f.id }}</span><span class="msg">{{ f.reason }}</span></li>
        </ul>
        <p v-if="b.report.errors?.length" class="error">{{ b.report.errors.length }} {{ t('request errors; see the diagnostics bundle') }}</p>
      </article>
    </div>
  </section>
</template>
