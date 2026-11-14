<!-- General settings: global mode, language, local model requests, backup, diagnostics, add-on options. -->
<script setup lang="ts">
import { computed, onActivated, ref } from 'vue';
import { api, type Settings } from '../api';
import Info from '../components/Info.vue';
import Seg from '../components/Seg.vue';
import { fmt } from '../format';
import { loadAgents, setLang, setMode, state, t, toast } from '../store';

const settings = ref<Settings | null>(null);
const form = ref<Record<string, string>>({});
const error = ref('');
const configError = ref('');

const fromRuntime = (runtime: Record<string, unknown> = {}) =>
  Object.fromEntries(Object.entries(runtime).map(([k, v]) => [k, v == null ? '' : String(v)]));

async function load() {
  try {
    settings.value = await api.get<Settings>('api/settings');
    form.value = fromRuntime(settings.value.runtime as Record<string, unknown>);
  } catch { /* banner */ }
}
async function save() {
  // '' = "use the default" (null); text fields stay text; everything else is a number
  const TEXT = ['kill_switch_entity', 'orchestrator_mode', 'night_window'];
  const body: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(form.value)) {
    if (k === 'daily_review_at') body[k] = v.trim(); // '' = no daily review
    else if (k === 'auto_house_mode' || k === 'manager_enabled') body[k] = v === 'true';
    else body[k] = v === '' ? null : TEXT.includes(k) ? v.trim() : Number(v);
  }
  error.value = '';
  try {
    settings.value = await api.put<Settings>('api/settings', body);
    form.value = fromRuntime(settings.value.runtime as Record<string, unknown>);
    toast(t('Saved.'));
  } catch (e) { error.value = (e as Error).message; }
}
async function importConfig(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  configError.value = '';
  try {
    const out = await api.postText<{ agents: string[]; set_to_shadow: string[] }>('api/config/import', await file.text(), 'text/yaml');
    toast(`${t('Imported agents:')} ${out.agents.join(', ') || '—'}${out.set_to_shadow.length ? ` · ${t('set to shadow:')} ${out.set_to_shadow.join(', ')}` : ''}`);
    load();
    loadAgents();
  } catch (e) { configError.value = (e as Error).message; }
}

const MODES = [['shadow', 'Shadow'], ['live', 'Live']] as const;
const mode = computed({ get: () => state.status?.mode ?? 'shadow', set: (m) => setMode(m) });
const lang = computed({ get: () => state.lang, set: (l) => setLang(l) });
const FIELDS: { key: string; label: string; type?: string; min?: number; max?: number; step?: number; placeholder?: string }[] = [
  { key: 'llm_timeout_s', label: 'Timeout per request (s)', min: 5, max: 900 },
  { key: 'llm_max_retries', label: 'Retries', min: 0, max: 5 },
  { key: 'llm_stale_after_s', label: 'Drop stale wakes (s)', min: 1, max: 3600 },
  { key: 'llm_max_queue', label: 'Queue length', min: 1, max: 100 },
  { key: 'llm_presence_penalty', label: 'Presence penalty', min: 0, max: 2, step: 0.1, placeholder: 'profile' },
  { key: 'daily_review_at', label: 'Daily review at', type: 'text', placeholder: '04:30' },
  { key: 'habit_days', label: 'Habits: days of history', min: 3, max: 30 },
  { key: 'habit_min_repeats', label: 'Habits: min. days', min: 3, max: 50 },
];
const options = computed(() => Object.entries((settings.value?.options || {}) as Record<string, unknown>));
onActivated(load);
</script>

<template>
  <section class="page">
    <div class="cols" style="grid-template-columns: repeat(auto-fit, minmax(320px, 1fr))">
      <div class="stack">
        <article class="card stack">
          <h2>{{ t('Mode') }}</h2>
          <div class="row">
            <Seg v-model="mode" :options="MODES" :label="t('Mode')" />
            <Info :tip="t('Shadow: agents observe and log what they would do. Live: agents whose own mode is live may act, within their tiers.')" />
          </div>
          <div class="fields">
            <label class="field"><span>{{ t('Language') }}</span><select v-model="lang"><option value="en">English</option></select></label>
          </div>
        </article>

        <article class="card stack">
          <h2>{{ t('Backup') }} <Info :tip="t('Agents, policy and these settings as one YAML file. No add-on options, so no secrets. Imported agents always start in shadow mode.')" /></h2>
          <div class="row">
            <a class="btn" href="api/config/export" download="agent-house-config.yaml">{{ t('Export') }}</a>
            <label class="btn"><span>{{ t('Import…') }}</span><input type="file" accept=".yaml,.yml,text/yaml" hidden @change="importConfig" /></label>
          </div>
          <p v-if="configError" class="error">{{ configError }}</p>
          <h2>{{ t('Diagnostics') }} <Info :tip="t('A zip with status, redacted settings, registry summary, recent feed and log. No tokens or API keys. Commit it to owner-reports/diagnostics/ when reporting a problem.')" /></h2>
          <div><a class="btn" href="api/diagnostics" download>{{ t('Download bundle') }}</a></div>
          <dl class="kv">
            <dt>{{ t('Version') }}</dt><dd>{{ state.status?.version || '—' }}</dd>
            <dt>{{ t('Database') }}</dt><dd class="mono">{{ (settings?.paths as any)?.database || '—' }}</dd>
          </dl>
        </article>

        <article class="card stack">
          <h2>{{ t('Add-on options') }} <Info :tip="t('Edit in Home Assistant → Settings → Add-ons → Agent House → Configuration. Secrets are masked.')" /></h2>
          <dl class="kv"><template v-for="[k, v] in options" :key="k"><dt class="mono">{{ k }}</dt><dd class="mono">{{ fmt(v) }}</dd></template></dl>
        </article>
      </div>

      <div class="stack">
        <article class="card stack">
          <h2>{{ t('Orchestrator') }} <Info :tip="t('The orchestrator owns the house mode helper. In shadow it only logs what it would switch.')" /></h2>
          <form class="stack" @submit.prevent="save">
            <div class="row">
              <Seg v-model="form.orchestrator_mode" :options="MODES" :label="t('Orchestrator mode')" />
              <label class="field check"><input type="checkbox" :checked="form.auto_house_mode === 'true'"
                @change="form.auto_house_mode = ($event.target as HTMLInputElement).checked ? 'true' : 'false'" /> <span>{{ t('Switches the house mode itself') }}</span></label>
            </div>
            <div class="fields">
              <label class="field"><span>{{ t('Night window') }}</span><input v-model="form.night_window" type="text" class="mono" placeholder="23:00-06:00" /></label>
            </div>
            <div class="row">
              <label class="field check"><input type="checkbox" :checked="form.manager_enabled === 'true'"
                @change="form.manager_enabled = ($event.target as HTMLInputElement).checked ? 'true' : 'false'" /> <span>{{ t('House manager') }}</span></label>
              <Info :tip="t('Between reviews the orchestrator checks the house (lights on with nobody home, failed room runs, weak automations) and acts on entities no room agent owns. The model is called only when the check finds something. Its actions follow the orchestrator mode.')" />
            </div>
            <div class="fields">
              <label class="field"><span>{{ t('Check every (min, 0 = off)') }}</span><input v-model="form.manager_sweep_min" type="number" min="0" max="1440" /></label>
              <label class="field"><span>{{ t('Wakes per hour') }}</span><input v-model="form.manager_max_wakes_per_hour" type="number" min="0" max="30" /></label>
            </div>
            <div class="row"><button class="btn primary" type="submit">{{ t('Save') }}</button><p v-if="error" class="error">{{ error }}</p></div>
          </form>
        </article>
        <article class="card stack">
          <h2>{{ t('Local model requests') }} <Info :tip="t('Stored in /data/settings.yaml. Empty = default.')" /></h2>
          <form class="stack" @submit.prevent="save">
            <div class="fields">
              <label v-for="f in FIELDS" :key="f.key" class="field"><span>{{ t(f.label) }}</span>
                <input v-model="form[f.key]" :type="f.type || 'number'" :class="{ mono: f.type === 'text' }" :min="f.min" :max="f.max" :step="f.step ?? 1" :placeholder="f.placeholder && t(f.placeholder)" /></label>
              <label class="field" style="grid-column: 1 / -1"><span>{{ t('Kill switch helper') }}
                <Info :tip="t('Optional input_boolean in Home Assistant that stops all agents when on.')" /></span>
                <input v-model="form.kill_switch_entity" type="text" class="mono" placeholder="input_boolean.agent_house_kill" /></label>
            </div>
            <div class="row"><button class="btn primary" type="submit">{{ t('Save') }}</button><p v-if="error" class="error">{{ error }}</p></div>
          </form>
        </article>
      </div>
    </div>
  </section>
</template>
