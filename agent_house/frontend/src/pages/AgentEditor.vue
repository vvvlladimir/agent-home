<!-- Agent editor: room, mode, entities and tier overrides, wakes, limits, prompt; live prompt preview. -->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { api, enc, type AgentConfig, type Entity } from '../api';
import DetailHead from '../components/DetailHead.vue';
import Icon from '../components/Icon.vue';
import IconButton from '../components/IconButton.vue';
import Info from '../components/Info.vue';
import SearchField from '../components/SearchField.vue';
import Seg from '../components/Seg.vue';
import TierBadge from '../components/TierBadge.vue';
import { pct } from '../format';
import { tiersTip } from '../labels';
import { fail, state, t, toast } from '../store';

const props = defineProps<{ id: string }>(); // '' = new agent
const emit = defineEmits<{ close: []; wake: [id: string] }>();

// Wake rules are edited as comma-separated text and turned back into lists on save.
interface EventRule {
  _entities: string; _to: string; _from: string; debounce_s?: number | string; cooldown_s?: number | string;
  priority?: string; reason?: string; [k: string]: unknown;
}
type Form = Omit<AgentConfig, 'wake_rules' | 'limits' | 'entities' | 'tier_overrides' | 'mode'> & {
  entities: string[];
  tier_overrides: Record<string, string>;
  mode: 'shadow' | 'live';
  wake_rules: { events: EventRule[]; cron: { expr: string; reason?: string }[]; max_wakes_per_hour?: number | string };
  limits: Record<string, number | string | undefined>;
};

const isNew = ref(!props.id);
const form = ref<Form | null>(null);
const roomEntities = ref<Entity[]>([]);
const preview = ref<{ tokens_estimate: number; budget: number; entities_included: number; entities_dropped: string[]; system: string; user: string } | null>(null);
const previewError = ref('');
const error = ref('');

const join = (v: unknown) => (Array.isArray(v) ? v.join(', ') : '');
function load(config: AgentConfig) {
  const wr = config.wake_rules || {};
  form.value = {
    ...config,
    entities: config.entities || [],
    mode: config.mode || 'shadow',
    entities_mode: config.entities_mode || 'only',
    tier_overrides: config.tier_overrides || {},
    limits: { ...(config.limits || {}) },
    wake_rules: {
      ...wr,
      cron: (wr.cron || []).map((c) => ({ ...c })),
      events: (wr.events || []).map((r) => ({
        ...r, _entities: join(r.entities), _to: join(r.to), _from: join(r.from),
      })),
    },
  };
  error.value = '';
  preview.value = null;
  loadRoomEntities();
}

onMounted(async () => {
  if (isNew.value) {
    load({
      id: '', name: '', room: state.areas[0]?.area_id || '', enabled: true, mode: 'shadow', entities: [], entities_mode: 'only',
      max_entities: 20, tier_overrides: {}, system_prompt: '',
      wake_rules: { events: [], cron: [], max_wakes_per_hour: 12 },
      limits: { max_steps: 5, timeout_s: 45, context_budget_tokens: 3000 },
    });
  } else {
    try { load(await api.get<AgentConfig>(`api/agents/${enc(props.id)}`)); } catch (e) { fail(e); emit('close'); }
  }
});

async function loadRoomEntities() {
  if (!form.value?.room) { roomEntities.value = []; return; }
  try { roomEntities.value = await api.get<Entity[]>(`api/entities?area=${enc(form.value.room)}`); } catch { roomEntities.value = []; }
  schedulePreview();
}
// New agents get their id from the name (or the room): lowercase ascii, unique among agents.
const ID_RE = /^[a-z][a-z0-9_]{1,31}$/;
const slug = (text: string) => text.normalize('NFKD').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^[^a-z]+|_+$/g, '').slice(0, 28);
const autoId = computed(() => {
  const f = form.value;
  if (!f) return '';
  let base = slug(f.name) || slug(f.room) || 'agent';
  if (base.length < 2) base = `${base}_agent`;
  const taken = new Set(state.agents.map((a) => a.id));
  let id = base;
  for (let n = 2; taken.has(id); n++) id = `${base}_${n}`;
  return id;
});

// Entity list: search, domain filter, sort.
const entQuery = ref('');
const entDomain = ref('');
const entSort = ref<'ticked' | 'name' | 'domain' | 'tier'>('ticked');
const domainOf = (id: string) => id.split('.')[0];
const domains = computed(() => [...new Set(roomEntities.value.map((e) => domainOf(e.entity_id)))].sort());
const tierRank = (e: Entity) => (e.read_only ? 9 : Number(String(form.value?.tier_overrides[e.entity_id] || e.tier || 'T9').slice(1)));
const shownEntities = computed(() => {
  const needle = entQuery.value.trim().toLowerCase();
  const ticked = new Set(form.value?.entities || []);
  const label = (e: Entity) => (e.name || e.entity_id).toLowerCase();
  const list = roomEntities.value.filter((e) => (!entDomain.value || domainOf(e.entity_id) === entDomain.value)
    && (!needle || `${e.entity_id} ${e.name || ''}`.toLowerCase().includes(needle)));
  const by = {
    name: (a: Entity, b: Entity) => label(a).localeCompare(label(b)),
    domain: (a: Entity, b: Entity) => a.entity_id.localeCompare(b.entity_id),
    tier: (a: Entity, b: Entity) => tierRank(a) - tierRank(b) || label(a).localeCompare(label(b)),
    ticked: (a: Entity, b: Entity) => Number(ticked.has(b.entity_id)) - Number(ticked.has(a.entity_id)) || label(a).localeCompare(label(b)),
  }[entSort.value];
  return list.sort(by);
});

function changeRoom() { form.value!.entities = []; loadRoomEntities(); }
function toggleEntity(id: string) {
  const list = form.value!.entities;
  const i = list.indexOf(id);
  if (i >= 0) list.splice(i, 1); else list.push(id);
  schedulePreview();
}
function setOverride(id: string, tier: string) {
  const o = form.value!.tier_overrides;
  if (tier) o[id] = tier; else delete o[id];
  schedulePreview();
}
const addEventRule = () => form.value!.wake_rules.events.push({ _entities: '', _to: '', _from: '', debounce_s: 5, cooldown_s: 120, priority: 'routine', reason: '' });
const addCron = () => form.value!.wake_rules.cron.push({ expr: '0 7 * * *', reason: '' });

function payload() {
  const f = form.value!;
  if (isNew.value) f.id = autoId.value;
  const list = (text: string) => (text || '').split(',').map((s) => s.trim()).filter(Boolean);
  const num = (v: unknown) => (v === '' || v == null ? undefined : Number(v));
  const events = f.wake_rules.events.map(({ _entities, _to, _from, ...rule }) => {
    const out: Record<string, unknown> = { ...rule, entities: list(_entities) };
    if (list(_to).length) out.to = list(_to); else delete out.to;
    if (list(_from).length) out.from = list(_from); else delete out.from;
    for (const k of ['debounce_s', 'cooldown_s', 'min_change', 'above', 'below']) {
      if (num(out[k]) === undefined) delete out[k]; else out[k] = num(out[k]);
    }
    if (!out.reason) delete out.reason;
    return out;
  });
  const cron = f.wake_rules.cron.filter((c) => c.expr).map((c) => (c.reason ? c : { expr: c.expr }));
  const limits: Record<string, number> = {};
  for (const [k, v] of Object.entries(f.limits)) if (num(v) !== undefined) limits[k] = num(v)!;
  return {
    ...f,
    max_entities: Number(f.max_entities) || 20,
    wake_rules: { ...f.wake_rules, events, cron, max_wakes_per_hour: Number(f.wake_rules.max_wakes_per_hour ?? 12) },
    limits,
  };
}

let previewTimer: number | undefined;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(loadPreview, 400);
}
async function loadPreview() {
  if (!form.value) return;
  const id = ID_RE.test(form.value.id) ? form.value.id : 'preview';
  try {
    preview.value = await api.post(`api/agents/${enc(id)}/prompt-preview`, { ...payload(), id });
    previewError.value = '';
  } catch (e) { previewError.value = (e as Error).message; }
}

async function save() {
  error.value = '';
  const body = payload();
  try {
    const saved = isNew.value
      ? await api.post<AgentConfig>('api/agents', body)
      : await api.put<AgentConfig>(`api/agents/${enc(body.id)}`, body);
    isNew.value = false;
    load(saved);
    toast(t('Saved.'));
  } catch (e) { error.value = (e as Error).message; }
}
async function remove() {
  if (!confirm(t('Delete this agent, its notes and its scheduled wakes? Its runs stay in the trace log.'))) return;
  try {
    await api.del(`api/agents/${enc(form.value!.id)}`);
    toast(t('Deleted.'));
    emit('close');
  } catch (e) { error.value = (e as Error).message; }
}
const MODES = [['shadow', 'Shadow'], ['live', 'Live']] as const;
</script>

<template>
  <div v-if="!form" class="stack"><div class="sk" style="width: 40%" /><div class="sk block" /><div class="sk block" /></div>
  <div v-else class="sec">
    <DetailHead :title="isNew ? t('New room agent') : form.name" @back="emit('close')">
      <button v-if="!isNew" class="btn quiet danger" @click="remove"><Icon name="trash" /><span>{{ t('Delete') }}</span></button>
      <button v-if="!isNew" class="btn" @click="emit('wake', form.id)"><Icon name="play" /><span>{{ t('Wake now') }}</span></button>
      <button class="btn primary" @click="save">{{ t('Save') }}</button>
    </DetailHead>
    <p v-if="error" class="error">{{ error }}</p>

    <div class="cols side" @input="schedulePreview" @change="schedulePreview">
      <div class="stack">
        <article class="card stack">
          <div class="fields">
            <label class="field"><span>{{ t('Name') }}</span><input v-model="form.name" /></label>
            <label class="field"><span>{{ t('Room') }}</span>
              <select v-model="form.room" @change="changeRoom"><option v-for="a in state.areas" :key="a.area_id" :value="a.area_id">{{ a.name }}</option></select></label>
          </div>
          <p class="faint small">{{ t('Id') }}: <span class="mono">{{ isNew ? autoId : form.id }}</span></p>
          <div class="row">
            <Seg v-model="form.mode" :options="MODES" :label="t('Mode')" />
            <Info :tip="t('Shadow: only logs what it would do. Live: may act within its tiers when the global mode is live too.')" />
            <label class="field check" style="margin-left: auto"><input v-model="form.enabled" type="checkbox" /> <span>{{ t('Enabled') }}</span></label>
          </div>
        </article>

        <article class="card stack">
          <div class="sec-h"><h2>{{ t('Entities and tiers') }}
            <Info :tip="t('None ticked = the whole room except device noise (battery, signal, firmware). The first ones up to the cap go into the prompt; the agent finds the rest with a search.') + '\n\n' + tiersTip()" /></h2></div>
          <div class="fields">
            <label class="field"><span>{{ t('Ticked entities') }}</span>
              <select v-model="form.entities_mode"><option value="only">{{ t('Only these') }}</option><option value="pinned">{{ t('These first, then the whole room') }}</option></select></label>
            <label class="field"><span>{{ t('In the prompt (max)') }}</span><input v-model="form.max_entities" type="number" min="1" max="25" /></label>
          </div>
          <div class="toolbar">
            <SearchField v-model="entQuery" :placeholder="t('Search id or name')" />
            <label class="field auto"><select v-model="entDomain" :aria-label="t('Type')">
              <option value="">{{ t('All types') }}</option>
              <option v-for="d in domains" :key="d" :value="d">{{ d }}</option></select></label>
            <label class="field auto"><select v-model="entSort" :aria-label="t('Sort')">
              <option value="ticked">{{ t('Ticked first') }}</option>
              <option value="name">{{ t('By name') }}</option>
              <option value="domain">{{ t('By type') }}</option>
              <option value="tier">{{ t('By tier') }}</option></select></label>
            <span class="muted small count">{{ shownEntities.length }} / {{ roomEntities.length }} · {{ form.entities.length }} {{ t('ticked') }}</span>
          </div>
          <div class="table-wrap">
            <table><tbody>
              <tr v-if="!shownEntities.length"><td class="empty">{{ t('Nothing matches.') }}</td></tr>
              <tr v-for="e in shownEntities" :key="e.entity_id" :class="{ dim: e.hidden }">
                <td style="width: 1%"><input type="checkbox" :checked="form.entities.includes(e.entity_id)" :aria-label="e.entity_id" @change="toggleEntity(e.entity_id)" /></td>
                <td><div class="cell-2"><span>{{ e.name || e.entity_id }}</span><small class="mono">{{ e.entity_id }}</small></div></td>
                <td class="small muted">{{ (e.state ?? '') + (e.unit ? ' ' + e.unit : '') }}</td>
                <td><TierBadge :tier="e.tier" :read-only="e.read_only" /></td>
                <td style="width: 7rem">
                  <select v-if="!e.read_only" class="sm" :aria-label="t('Tier override')" @change="setOverride(e.entity_id, ($event.target as HTMLSelectElement).value)">
                    <option value="">{{ t('Policy') }}</option>
                    <option v-for="tier in ['T0', 'T1', 'T2', 'T3']" :key="tier" :value="tier" :selected="form.tier_overrides[e.entity_id] === tier">{{ tier }}</option>
                  </select></td>
              </tr>
            </tbody></table>
          </div>
        </article>

        <article class="card stack">
          <div class="sec-h"><h2>{{ t('Wakes') }}</h2>
            <div class="row">
              <button class="btn sm" type="button" @click="addEventRule"><Icon name="plus" /><span>{{ t('State rule') }}</span></button>
              <button class="btn sm" type="button" @click="addCron"><Icon name="plus" /><span>{{ t('Schedule') }}</span></button>
            </div></div>
          <div v-for="(r, i) in form.wake_rules.events" :key="i" class="card stack" style="background: var(--surface-2)">
            <div class="fields">
              <label class="field" style="grid-column: 1 / -1"><span>{{ t('Entities (ids or globs, comma separated)') }}</span><input v-model="r._entities" placeholder="binary_sensor.living_room_motion" /></label>
              <label class="field"><span>{{ t('From') }}</span><input v-model="r._from" placeholder="off" /></label>
              <label class="field"><span>{{ t('To') }}</span><input v-model="r._to" placeholder="on" /></label>
              <label class="field"><span>{{ t('Debounce s') }}</span><input v-model="r.debounce_s" type="number" min="0" /></label>
              <label class="field"><span>{{ t('Cooldown s') }}</span><input v-model="r.cooldown_s" type="number" min="0" /></label>
              <label class="field"><span>{{ t('Priority') }}</span><select v-model="r.priority"><option value="routine">routine</option><option value="alarm">alarm</option></select></label>
              <label class="field"><span>{{ t('Reason') }}</span><input v-model="r.reason" maxlength="120" /></label>
            </div>
            <div><button class="btn quiet sm danger" type="button" @click="form.wake_rules.events.splice(i, 1)">{{ t('Remove') }}</button></div>
          </div>
          <div v-for="(c, i) in form.wake_rules.cron" :key="'c' + i" class="toolbar">
            <label class="field"><span>{{ t('Cron') }}</span><input v-model="c.expr" class="mono" /></label>
            <label class="field grow"><span>{{ t('Reason') }}</span><input v-model="c.reason" maxlength="120" /></label>
            <IconButton icon="x" :tip="t('Remove')" @click="form.wake_rules.cron.splice(i, 1)" />
          </div>
          <div class="fields">
            <label class="field"><span>{{ t('Max wakes per hour') }}</span><input v-model="form.wake_rules.max_wakes_per_hour" type="number" min="0" max="120" /></label>
            <label class="field"><span>{{ t('Max steps') }}</span><input v-model="form.limits.max_steps" type="number" min="1" max="12" /></label>
            <label class="field"><span>{{ t('Timeout s') }}</span><input v-model="form.limits.timeout_s" type="number" min="5" max="300" /></label>
            <label class="field"><span>{{ t('Context budget (tokens)') }}</span><input v-model="form.limits.context_budget_tokens" type="number" min="512" max="16384" /></label>
          </div>
        </article>

        <article class="card stack">
          <div class="sec-h"><h2>{{ t('System prompt') }} <Info :tip="t('Empty = built-in default. Variables: {{agent_name}} {{room_name}} {{entity_table}} {{tiers}}')" /></h2></div>
          <textarea v-model="form.system_prompt" rows="8" class="mono" maxlength="4000" :aria-label="t('System prompt')" />
        </article>
      </div>

      <article class="card stack">
        <div class="sec-h"><h2>{{ t('Prompt preview') }} <Info :tip="t('Estimate ≈ characters / 3.5, not the model tokenizer. No model is called.')" /></h2></div>
        <p v-if="previewError" class="error">{{ previewError }}</p>
        <div v-else-if="!preview" class="stack"><div class="sk" /><div class="sk block" /></div>
        <div v-if="preview" class="stack">
          <div class="row"><span class="big">{{ preview.tokens_estimate }}</span>
            <span class="muted small">/ {{ preview.budget }} {{ t('tokens') }} · {{ preview.entities_included }} {{ t('entities') }}</span></div>
          <span class="meter" style="width: 100%"><span :style="{ width: pct(preview.tokens_estimate, preview.budget) + '%' }" /></span>
          <p v-if="preview.entities_dropped.length" class="muted small">{{ t('Not in the prompt, findable by the agent:') }} {{ preview.entities_dropped.join(', ') }}</p>
          <details><summary>{{ t('System') }}</summary><pre class="log">{{ preview.system }}</pre></details>
          <details><summary>{{ t('User (example wake)') }}</summary><pre class="log">{{ preview.user }}</pre></details>
        </div>
      </article>
    </div>
  </div>
</template>
