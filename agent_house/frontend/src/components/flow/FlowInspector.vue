<!-- The selected node's settings: a form for the usual kinds, YAML for anything else, and moving,
     copying, switching off or deleting it. -->
<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../api';
import { actionType, at, clone, describe, duration, form, get, items, set, typeOf, type Field, type Kind, type Loc } from '../../flow';
import { state, t } from '../../store';
import { useFlow } from './context';

const props = defineProps<{ loc: Loc }>();
const flow = useFlow();
const node = computed(() => at(props.loc));
const look = computed<Kind>(() => (props.loc.kind === 'action' && node.value && actionType(node.value) === 'condition' ? 'condition' : props.loc.kind));
const fields = computed(() => (node.value ? form(node.value, look.value) : []));
const count = computed(() => items(props.loc.holder, props.loc.key).length);
const ro = computed(() => flow.readonly.value);
const WEEK = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const KIND_NAME: Record<Kind, string> = { trigger: 'Trigger', condition: 'Condition', action: 'Step' };

// --- field values ---------------------------------------------------------------------------
function shown(f: Field): string {
  const v = get(node.value!, f.path);
  if (v == null) return '';
  if (f.kind === 'entities') return (Array.isArray(v) ? v : [v]).join(', ');
  if (f.kind === 'duration' && typeof v === 'object') {
    const o = v as Record<string, number>;
    const s = (o.hours || 0) * 3600 + (o.minutes || 0) * 60 + (o.seconds || 0);
    return `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}
function write(f: Field, raw: string | boolean | string[]) {
  let value: unknown = raw;
  if (f.kind === 'entities' && typeof raw === 'string') {
    const ids = raw.split(',').map((s) => s.trim()).filter(Boolean);
    value = ids.length > 1 ? ids : ids[0] ?? '';
  } else if (f.kind === 'number' && typeof raw === 'string') {
    value = raw.trim() === '' ? '' : Number.isFinite(Number(raw)) ? Number(raw) : raw.trim(); // an entity id works too
  }
  set(node.value!, f.path, value);
  flow.changed();
}
const weekdays = (f: Field) => { const v = get(node.value!, f.path); return Array.isArray(v) ? v : v ? [v] : []; };
function toggleDay(f: Field, day: string) {
  const days = weekdays(f).includes(day) ? weekdays(f).filter((d) => d !== day) : WEEK.filter((d) => [...weekdays(f), day].includes(d));
  write(f, days);
}
const entityIds = computed(() => Object.keys(state.names).sort());

// --- YAML of this node ------------------------------------------------------------------------
const yaml = ref('');
const yamlOpen = ref(false);
const yamlError = ref('');
watch(() => props.loc, () => { yamlOpen.value = false; yamlError.value = ''; });
async function openYaml() {
  yamlError.value = '';
  try { yaml.value = (await api.post<{ yaml: string }>('api/automation-yaml', { config: clone(node.value) })).yaml; yamlOpen.value = true; }
  catch (e) { yamlError.value = (e as Error).message; }
}
async function applyYaml() {
  yamlError.value = '';
  try {
    const { config } = await api.post<{ config: Record<string, unknown> }>('api/automation-yaml', { yaml: yaml.value, part: true });
    items(props.loc.holder, props.loc.key).splice(props.loc.index, 1, config);
    yamlOpen.value = false;
    flow.changed();
  } catch (e) { yamlError.value = (e as Error).message; }
}

// --- the node as a whole ----------------------------------------------------------------------
function shift(by: number) { flow.drop({ ...props.loc, index: props.loc.index + (by > 0 ? 2 : -1) }, props.loc); }
function duplicate() { flow.insert({ ...props.loc, index: props.loc.index + 1 }, clone(node.value!)); }
function toggleOff() {
  if (node.value!.enabled === false) delete node.value!.enabled; else node.value!.enabled = false;
  flow.changed();
}
const title = computed(() => `${t(KIND_NAME[look.value])}: ${typeOf(node.value!, look.value)}`);
</script>

<template>
  <aside v-if="node" class="finspect stack" @click.stop>
    <div class="sec-h"><h3>{{ title }}</h3>
      <button type="button" class="btn quiet sm" :aria-label="t('Close')" @click="flow.select(null)">×</button></div>
    <p class="small muted">{{ describe(node, look) }}<template v-if="node.enabled === false"> · {{ t('switched off: Home Assistant skips it') }}</template></p>

    <datalist id="flow-entities"><option v-for="e in entityIds" :key="e" :value="e">{{ state.names[e] }}</option></datalist>
    <template v-for="f in fields" :key="f.path">
      <label v-if="f.kind === 'bool'" class="row small"><input type="checkbox" :checked="!!get(node, f.path)" :disabled="ro"
             @change="write(f, ($event.target as HTMLInputElement).checked)" /><span>{{ t(f.label) }}</span></label>
      <div v-else-if="f.kind === 'weekday'" class="field"><span>{{ t(f.label) }}</span>
        <div class="row fweek"><button v-for="d in WEEK" :key="d" type="button" class="btn sm" :class="{ primary: weekdays(f).includes(d) }"
                :disabled="ro" @click="toggleDay(f, d)">{{ t(d) }}</button></div></div>
      <label v-else-if="f.kind === 'select'" class="field"><span>{{ t(f.label) }}</span>
        <select :value="shown(f)" :disabled="ro" @change="write(f, ($event.target as HTMLSelectElement).value)">
          <option v-for="o in f.options" :key="o" :value="o">{{ o || '—' }}</option></select></label>
      <label v-else-if="f.kind === 'longtext'" class="field"><span>{{ t(f.label) }}</span>
        <textarea class="mono" rows="3" :value="shown(f)" :readonly="ro" @change="write(f, ($event.target as HTMLTextAreaElement).value)" /></label>
      <label v-else class="field"><span>{{ t(f.label) }}<template v-if="f.kind === 'duration' && shown(f)"> · {{ duration(get(node, f.path)) }}</template></span>
        <input :value="shown(f)" :readonly="ro" :list="f.kind === 'entity' || f.kind === 'entities' ? 'flow-entities' : undefined"
               :placeholder="f.kind === 'duration' ? '00:05:00' : f.kind === 'entities' ? 'light.x, light.y' : ''"
               :class="{ mono: f.kind === 'entity' || f.kind === 'entities' || f.kind === 'duration' }"
               @change="write(f, ($event.target as HTMLInputElement).value)" />
        <small v-if="f.hint" class="faint">{{ t(f.hint) }}</small></label>
    </template>
    <p v-if="!fields.length" class="small faint">{{ t('No form for this kind: edit it as YAML.') }}</p>

    <details :open="yamlOpen" @toggle="($event.target as HTMLDetailsElement).open !== yamlOpen && (($event.target as HTMLDetailsElement).open ? openYaml() : (yamlOpen = false))">
      <summary class="small">{{ t('As YAML') }}</summary>
      <div v-if="yamlOpen" class="stack" style="margin-top: 8px">
        <textarea v-model="yaml" class="mono" rows="7" :readonly="ro" :aria-label="t('This part as YAML')" />
        <button v-if="!ro" type="button" class="btn sm" @click="applyYaml">{{ t('Apply') }}</button>
      </div>
    </details>
    <p v-if="yamlError" class="error small">{{ yamlError }}</p>

    <div v-if="!ro" class="row">
      <button type="button" class="btn sm" :disabled="loc.index === 0" :aria-label="t('Move up')" @click="shift(-1)">↑</button>
      <button type="button" class="btn sm" :disabled="loc.index >= count - 1" :aria-label="t('Move down')" @click="shift(1)">↓</button>
      <button type="button" class="btn sm" @click="duplicate">{{ t('Copy') }}</button>
      <button type="button" class="btn sm" @click="toggleOff">{{ node.enabled === false ? t('Switch on') : t('Switch off') }}</button>
      <button type="button" class="btn sm danger" @click="flow.remove(loc)">{{ t('Delete') }}</button>
    </div>
  </aside>
</template>
