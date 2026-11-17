<!-- Developer tools: GET api/dev as a readable page. One collapsible section per top-level key; the learner
     first, with what the Jetson came up with as tables. Anything not special-cased goes through the generic
     renderer (DevNode), so new backend fields appear by themselves. -->
<script setup lang="ts">
import { computed, onActivated, reactive, ref } from 'vue';
import { api, type DevSnapshot } from '../api';
import DevNode from '../components/DevNode.vue';
import DevTable from '../components/DevTable.vue';
import Icon from '../components/Icon.vue';
import SearchField from '../components/SearchField.vue';
import { copyText, count, isEmpty, isObj, prune, type Obj } from '../devfmt';
import { json } from '../format';
import { toast, t } from '../store';

const snap = ref<Obj | null>(null);
const error = ref('');
const loading = ref(false);
const loadedAt = ref('');
const query = ref('');
const closed = reactive<Record<string, boolean>>({});

async function load() {
  loading.value = true;
  try {
    snap.value = (await api.get<DevSnapshot>('api/dev')) as unknown as Obj;
    loadedAt.value = new Date().toLocaleTimeString();
    error.value = '';
  } catch (e) { error.value = (e as Error).message; }
  loading.value = false;
}
onActivated(load);

async function copy(label: string, value: unknown) {
  toast((await copyText(json(value))) ? `${t('Copied')}: ${label}` : t('Copy failed'), '');
}

// ── the snapshot, cut down to what matches the search ──
const needle = computed(() => query.value.trim().toLowerCase());
const searching = computed(() => !!needle.value);
const view = computed(() => (snap.value ? (searching.value ? (prune(snap.value, needle.value) as Obj) : snap.value) : null));
// the most important first; anything else the backend adds follows in its own order
const ORDER = ['learner', 'manager', 'orchestrator', 'policy', 'council', 'store', 'ha', 'config'];
const keys = computed(() => {
  const all = Object.keys(snap.value || {}).filter((k) => !isScalarTop(k));
  return [...ORDER.filter((k) => all.includes(k)), ...all.filter((k) => !ORDER.includes(k))];
});
const isScalarTop = (k: string) => snap.value !== null && (snap.value[k] === null || typeof snap.value[k] !== 'object');
const meta = computed(() => Object.entries(snap.value || {}).filter(([k]) => isScalarTop(k)));
const sections = computed(() => keys.value.filter((k) => !searching.value || !isEmpty(view.value?.[k])));
const open = (k: string) => searching.value || !closed[k];
const toggle = (k: string) => { closed[k] = !closed[k]; };

const TITLE: Record<string, string> = {
  learner: 'Learner', manager: 'House manager', orchestrator: 'Orchestrator', policy: 'Policy', council: 'Council',
  store: 'Store', ha: 'Home Assistant', config: 'Config',
};
const title = (k: string) => t(TITLE[k] || k);
// small count badges in a section header: the lists and tables it holds
function badges(k: string): { n: number; label: string }[] {
  const v = view.value?.[k];
  if (k === 'learner') return learnerParts.value.filter((p) => p.n).slice(0, 6).map((p) => ({ n: p.n, label: p.label }));
  if (!isObj(v)) return [];
  return Object.entries(v).filter(([, x]) => Array.isArray(x)).map(([key, x]) => ({ n: (x as unknown[]).length, label: key })).slice(0, 6);
}

// ── the learner: what the Jetson came up with ──
const learner = computed(() => (view.value?.learner || {}) as Obj);
const learnerFull = computed(() => (snap.value?.learner || {}) as Obj);
const SPECIAL = ['house_model', 'live_suggestions', 'targets', 'facts'];
const learnerRest = computed(() => Object.fromEntries(Object.entries(learner.value).filter(([k]) => !SPECIAL.includes(k))));
const hm = computed(() => (isObj(learner.value.house_model) ? (learner.value.house_model as Obj) : null));
const hmMissing = computed(() => !isObj(learnerFull.value.house_model));
const PARTS: { key: string; label: string; cols?: string[]; pick?: string }[] = [
  { key: 'habits', label: 'Habits', pick: 'predictions',
    cols: ['entity_id', 'state', 'room', 'mode', 'p', 'fires', 'right', 'right_days', 'per_day', 'value', 'cost', 'rule'] },
  { key: 'live', label: 'Live targets' },
  { key: 'routines', label: 'Routines' },
  { key: 'dependencies', label: 'Dependencies' },
  { key: 'corrections', label: 'Corrections' },
  { key: 'timeouts', label: 'Timeouts' },
  { key: 'usual_states', label: 'Usual states' },
  { key: 'event_relevance', label: 'Event relevance' },
  { key: 'health', label: 'Health' },
  { key: 'modes', label: 'Modes' },
  { key: 'asks', label: 'Asks' },
];
const META = ['schema_version', 'trained_at', 'duration_s', 'history'];
interface Part { key: string; label: string; n: number; value: unknown; cols?: string[]; extra?: Obj }
const learnerParts = computed<Part[]>(() => {
  const m = hm.value;
  if (!m) return [];
  const known = PARTS.map((p): Part => {
    const raw = m[p.key];
    const value = p.pick && isObj(raw) ? raw[p.pick] : raw;
    const extra = p.pick && isObj(raw) ? Object.fromEntries(Object.entries(raw).filter(([k]) => k !== p.pick)) : undefined;
    return { key: p.key, label: p.label, n: count(value), value, cols: p.cols, extra };
  });
  const other = Object.keys(m).filter((k) => !META.includes(k) && !PARTS.some((p) => p.key === k))
    .map((k): Part => ({ key: k, label: k, n: count(m[k]), value: m[k] }));
  return [...known, ...other].filter((p) => !searching.value || !isEmpty(p.value));
});
const hmMeta = computed(() => Object.fromEntries(Object.entries(hm.value || {}).filter(([k]) => META.includes(k))));
const suggestions = computed(() => (Array.isArray(learner.value.live_suggestions) ? (learner.value.live_suggestions as unknown[]) : []));
const targets = computed(() => (Array.isArray(learner.value.targets) ? (learner.value.targets as unknown[]) : []));
const facts = computed(() => (Array.isArray(learner.value.facts) ? (learner.value.facts as string[]) : []));
const notConnected = computed(() => !learnerFull.value.configured);
</script>

<template>
  <section class="page dev">
    <div class="toolbar">
      <SearchField v-model="query" :placeholder="t('Filter the whole snapshot')" />
      <button class="btn" :aria-busy="loading || undefined" :disabled="loading" @click="load"><Icon name="refresh" /><span>{{ t('Refresh') }}</span></button>
      <button class="btn quiet" :disabled="!snap" @click="copy(t('everything'), snap)"><Icon name="copy" /><span>{{ t('Copy all JSON') }}</span></button>
      <span class="faint small count">{{ loadedAt ? `${t('Loaded')} ${loadedAt}` : '' }}</span>
    </div>
    <p v-if="error" class="card error">{{ t('Could not load the snapshot') }}: {{ error }}</p>
    <div v-if="!snap && !error" class="stack"><div class="sk block" /><div class="sk block" /></div>

    <template v-if="snap">
      <dl v-if="meta.length" class="kv card">
        <template v-for="[k, v] in meta" :key="k"><dt class="mono">{{ k }}</dt><dd><DevNode :value="v" :name="k" /></dd></template>
      </dl>
      <p v-if="searching && !sections.length" class="card empty">{{ t('Nothing in the snapshot matches') }} “{{ query }}”.</p>

      <article v-for="k in sections" :key="k" class="card dev-sec" :class="{ shut: !open(k) }">
        <header class="dev-head">
          <button class="dev-toggle" type="button" :aria-expanded="open(k)" @click="toggle(k)">
            <span class="chev" aria-hidden="true" />
            <h2>{{ title(k) }}</h2>
          </button>
          <span class="chips"><span v-for="b in badges(k)" :key="b.label" class="tag">{{ b.n }} {{ b.label }}</span></span>
          <button class="btn quiet sm" type="button" @click="copy(title(k), snap[k])"><Icon name="copy" /><span>{{ t('Copy JSON') }}</span></button>
        </header>

        <div v-if="open(k)" class="dev-body">
          <!-- the learner: link and Jetson first, then what it learnt -->
          <div v-if="k === 'learner'" class="stack">
            <p v-if="notConnected" class="muted">{{ t('The learner is not set up: set the add-on option Jetson learner URL.') }}</p>
            <DevNode :value="learnerRest" />

            <h3 class="dev-h big-h">{{ t('What the Jetson came up with') }}
              <span v-if="hm?.trained_at" class="faint small">· <DevNode :value="hm.trained_at" name="trained_at" /></span></h3>
            <p v-if="hmMissing" class="muted">{{ notConnected ? t('Nothing: no learner is connected.') : t('No house model yet: the Jetson has not trained, or the add-on has not fetched the result.') }}</p>
            <template v-else-if="hm">
              <DevNode v-if="!searching" :value="hmMeta" />
              <div v-if="facts.length" class="dev-sub">
                <h3 class="dev-h"><span>{{ t('Facts the orchestrator reads') }}</span><span class="tag">{{ facts.length }}</span></h3>
                <ul class="feed marks small"><li v-for="(f, i) in facts" :key="i"><i class="mk note" /><span class="msg">{{ f }}</span></li></ul>
              </div>
              <div v-if="targets.length" class="dev-sub">
                <h3 class="dev-h"><span>{{ t('Devices it may act on now') }}</span><span class="tag">{{ targets.length }}</span></h3>
                <DevNode :value="targets" />
              </div>
              <details v-for="p in learnerParts" :key="p.key" class="dev-part" open>
                <summary><h3 class="dev-h"><span>{{ t(p.label) }}</span><span class="tag">{{ p.n }}</span></h3></summary>
                <DevNode v-if="p.extra && count(p.extra)" :value="p.extra" />
                <p v-if="!p.n" class="faint small">{{ t('none') }}</p>
                <DevTable v-else-if="Array.isArray(p.value) && p.value.length && p.cols" :rows="p.value" :cols="p.cols" />
                <DevNode v-else :value="p.value" :name="p.key" />
              </details>
            </template>

            <h3 class="dev-h big-h">{{ t('Live suggestions and their verdicts') }}<span class="tag">{{ suggestions.length }}</span></h3>
            <DevTable v-if="suggestions.length" :rows="suggestions" />
            <p v-else class="muted">{{ t('None yet: the learner has not suggested anything live.') }}</p>
          </div>

          <DevNode v-else :value="view![k]" :name="k" />
        </div>
      </article>
    </template>
  </section>
</template>
