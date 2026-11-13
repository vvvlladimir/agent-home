<!-- Rooms: the room agents (list → editor) and every entity with its tier. -->
<script setup lang="ts">
import { computed, onActivated, ref } from 'vue';
import { api, enc, type Entity } from '../api';
import Icon from '../components/Icon.vue';
import IconButton from '../components/IconButton.vue';
import Info from '../components/Info.vue';
import SearchField from '../components/SearchField.vue';
import TierBadge from '../components/TierBadge.vue';
import { ago } from '../format';
import { areaName, tiersTip } from '../labels';
import { go } from '../router';
import { fail, loadAgents, nav, state, t, toast } from '../store';
import AgentEditor from './AgentEditor.vue';

const editing = ref<string | null>(null); // agent id, '' = new agent, null = list
const entities = ref<Entity[]>([]);
const area = ref('');
const query = ref('');

async function loadEntities() {
  const q = area.value ? `?area=${enc(area.value)}` : '';
  try { entities.value = await api.get<Entity[]>(`api/entities${q}`); } catch { entities.value = []; }
}
const shown = computed(() => {
  const needle = query.value.toLowerCase();
  return entities.value.filter((e) => !needle || `${e.entity_id} ${e.name || ''}`.toLowerCase().includes(needle));
});

async function wake(id: string) {
  try {
    const { run_id } = await api.post<{ run_id: string }>(`api/agents/${enc(id)}/wake`);
    toast(`${t('Woken: run')} ${run_id}`);
    nav.runId = run_id;
    go('runs');
  } catch (e) { fail(e); }
}
async function setEnabled(id: string, enabled: boolean) {
  try {
    const config = await api.get<object>(`api/agents/${enc(id)}`);
    await api.put(`api/agents/${enc(id)}`, { ...config, enabled });
  } catch (e) { fail(e); }
  loadAgents();
}
function closeEditor() { editing.value = null; loadAgents(); }

onActivated(() => {
  if (nav.agentId) { editing.value = nav.agentId; nav.agentId = ''; }
  if (nav.areaSel) { area.value = nav.areaSel; nav.areaSel = ''; }
  loadAgents();
  loadEntities();
});
</script>

<template>
  <section class="page">
    <AgentEditor v-if="editing !== null" :id="editing" @close="closeEditor" @wake="wake" />

    <template v-else>
      <div class="sec">
        <div class="sec-h">
          <span class="muted small">{{ state.agents.length }} {{ t('agents') }}</span>
          <div class="row">
            <Info :tip="t('A room agent watches one room. It starts in shadow mode: it only logs what it would do until you set it live.')" />
            <button class="btn primary" @click="editing = ''"><Icon name="plus" /><span>{{ t('New room agent') }}</span></button>
          </div>
        </div>
        <div v-if="state.agents.length" class="table-wrap">
          <table>
            <thead><tr><th>{{ t('Room') }}</th><th>{{ t('Mode') }}</th><th>{{ t('Entities') }}</th><th>{{ t('Last run') }}</th><th /></tr></thead>
            <tbody>
              <tr v-for="a in state.agents" :key="a.id" class="clickable" :class="{ dim: !a.enabled }" @click="editing = a.id">
                <td><div class="cell-2"><b>{{ areaName(a.room) }}</b><small>{{ a.name }}</small></div></td>
                <td :data-tip="a.mode !== a.effective_mode ? `${t('Own mode')}: ${a.mode}. ${t('The global mode is shadow.')}` : ''">
                  <span v-if="a.effective_mode === 'live'" class="live-dot">{{ t('Live') }}</span>
                  <span v-else class="status shadow">{{ t('Shadow') }}</span></td>
                <td>{{ a.entity_count }}</td>
                <td class="small">{{ a.last_run ? a.last_run.summary || a.last_run.status : '—' }}
                  <span v-if="a.last_run" class="faint">· {{ ago(a.last_run.started) }}</span></td>
                <td class="act" @click.stop>
                  <IconButton icon="play" :tip="t('Wake now')" :disabled="!a.enabled" @click="wake(a.id)" />
                  <button class="btn sm quiet" @click="setEnabled(a.id, !a.enabled)">{{ a.enabled ? t('Disable') : t('Enable') }}</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="card empty">{{ t('No room agents yet.') }}</p>
      </div>

      <div class="sec">
        <div class="sec-h"><h2>{{ t('Entities') }} <Info :tip="tiersTip()" :label="t('Tiers')" /></h2></div>
        <div class="toolbar">
          <SearchField v-model="query" :placeholder="t('Search id or name')" />
          <label class="field">
            <select v-model="area" :aria-label="t('Room')" @change="loadEntities">
              <option value="">{{ t('All rooms') }}</option>
              <option v-for="a in state.areas" :key="a.area_id" :value="a.area_id">{{ a.name }}</option>
              <option value="none">{{ t('Without a room') }}</option>
            </select></label>
          <span class="muted small">{{ shown.length }} {{ t('entities') }}</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>{{ t('Name') }}</th><th>{{ t('Room') }}</th><th>{{ t('State') }}</th><th>{{ t('Tier') }}</th></tr></thead>
            <tbody>
              <tr v-for="e in shown" :key="e.entity_id" :class="{ dim: e.hidden }">
                <td><div class="cell-2"><span>{{ e.name || e.entity_id }}</span><small class="mono">{{ e.entity_id }}</small></div></td>
                <td>{{ areaName(e.area_id) }}</td>
                <td>{{ e.state }} <span class="muted">{{ e.unit || '' }}</span></td>
                <td><TierBadge :tier="e.tier" :read-only="e.read_only" /></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </section>
</template>
