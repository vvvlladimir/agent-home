<!-- Your automations: read-only copies of the owner's automations, scripts and Node-RED flows;
     propose a replacement, compare both against history, then switch the original off. -->
<script setup lang="ts">
import { onActivated, ref } from 'vue';
import { api, enc, type Original, type Parity } from '../api';
import Icon from '../components/Icon.vue';
import Info from '../components/Info.vue';
import { dateTime } from '../format';
import { sourceLabel } from '../labels';
import { killed, t, toast } from '../store';

const originals = ref<Original[]>([]);
const sel = ref<Original | null>(null);
const parity = ref<Parity | null>(null);
const busy = ref(false);
const error = ref('');

async function load() {
  try { originals.value = await api.get<Original[]>('api/migration/originals'); } catch (e) { error.value = (e as Error).message; }
}
async function act<T>(fn: () => Promise<T>, done?: (out: T) => string) {
  error.value = ''; busy.value = true;
  try { const out = await fn(); if (done) toast(done(out)); await load(); } catch (e) { error.value = (e as Error).message; }
  busy.value = false;
}
const importAll = () => act(() => api.post<{ imported: number; updated: number; skipped?: string[] }>('api/migration/import'),
  (r) => `${r.imported} ${t('new')}, ${r.updated} ${t('changed')}${r.skipped?.length ? ' · ' + r.skipped.join(' · ') : ''}`);
const propose = (o: Original) => act(() => api.post(`api/migration/originals/${enc(o.id)}/propose`),
  () => t('Drafting a replacement: it appears here and under Drafts in a moment.'));
function setEnabled(o: Original, enabled: boolean) {
  if (!enabled && !confirm(t('Switch your own automation off in Home Assistant? You can switch it on again here.'))) { load(); return; }
  act(() => api.put(`api/migration/originals/${enc(o.id)}/enabled`, { enabled }));
}
async function open(o: Original) {
  sel.value = o; parity.value = null; error.value = '';
  if (!o.replacement_proposal_id) return;
  try { parity.value = await api.get<Parity>(`api/migration/originals/${enc(o.id)}/parity?days=7`); } catch (e) { error.value = (e as Error).message; }
}
onActivated(load);
</script>

<template>
  <section class="page">
    <div class="row">
      <button class="btn" :aria-busy="busy || undefined" :disabled="busy" @click="importAll"><Icon name="refresh" /><span>{{ t('Import') }}</span></button>
      <Info :tip="t('Reads your automations, scripts and Node-RED flows (exports in /share/agent_house/nodered). Ask for a replacement, compare both against history, then switch the original off. Nothing of yours is edited or deleted.')" />
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="originals.length" class="table-wrap">
      <table>
        <thead><tr><th>{{ t('Original') }}</th><th>{{ t('On') }}</th><th>{{ t('Replacement') }}</th><th /></tr></thead>
        <tbody>
          <tr v-for="o in originals" :key="o.id" class="clickable" :class="{ sel: sel?.id === o.id }" @click="open(o)">
            <td><div class="cell-2"><span>{{ o.title }}</span><small>{{ sourceLabel(o.source) }} · {{ o.summary || t('no readable config') }}</small></div></td>
            <td @click.stop>
              <input v-if="o.source === 'ha_automation'" type="checkbox" :checked="!!o.enabled" :disabled="busy" :aria-label="t('Enabled')"
                     @change="setEnabled(o, ($event.target as HTMLInputElement).checked)" />
              <span v-else class="muted small">{{ o.enabled === false ? t('off') : '—' }}</span>
              <i v-if="o.disabled_by_us" class="mk blocked" :data-tip="t('Switched off by Agent House')" />
            </td>
            <td class="small">
              <a v-if="o.replacement_proposal_id" href="#proposals" @click.stop>{{ o.replacement_ha_id ? t('deployed') : t('draft') }}</a>
              <span v-else class="faint">—</span></td>
            <td class="act" @click.stop><button class="btn sm" :disabled="busy || !o.config || killed()" @click="propose(o)">{{ t('Propose replacement') }}</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="card empty">{{ t('Nothing imported yet.') }}</p>

    <article v-if="sel" class="card stack">
      <h2>{{ sel.title }}</h2>
      <div v-if="parity" class="stack">
        <div class="row" style="gap: 28px">
          <div><span class="big">{{ parity.original_fired }}</span><div class="muted small">{{ t('original') }}</div></div>
          <div><span class="big">{{ parity.replacement_would_fire }}</span><div class="muted small">{{ t('replacement') }}</div></div>
          <div><span class="big">{{ parity.matched }}</span><div class="muted small">{{ t('matched') }}</div></div>
          <Info :tip="`${t('Trigger firings replayed from the last')} ${parity.days} ${t('days; conditions are not evaluated.')}` + (parity.supported ? '' : '\n' + t('Some triggers could not be replayed.'))" />
        </div>
        <p v-for="u in parity.unsupported || []" :key="u" class="muted small">{{ u }}</p>
        <ul class="feed small">
          <li v-for="d in parity.diffs" :key="`${d.ts}${d.side}`" style="grid-template-columns: auto auto minmax(0, 1fr)">
            <time>{{ dateTime(d.ts) }}</time>
            <span class="tag">{{ t(d.side === 'original_only' ? 'only the original' : 'only the replacement') }}</span><span>{{ d.detail }}</span></li>
        </ul>
      </div>
      <p v-else-if="!sel.replacement_proposal_id" class="muted">{{ t('No replacement yet: propose one to compare.') }}</p>
      <details><summary>{{ t('Config') }}</summary><pre class="log">{{ JSON.stringify(sel.config, null, 2) }}</pre></details>
    </article>
  </section>
</template>
