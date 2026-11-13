<!-- Deployed: automations Agent House put into HA (agent_house_*): on/off, versions, roll back, remove. -->
<script setup lang="ts">
import { onActivated, ref } from 'vue';
import { api, type DeployedAutomation } from '../api';
import IconButton from '../components/IconButton.vue';
import Info from '../components/Info.vue';
import { dateTime } from '../format';
import { agentName } from '../labels';
import { killed, refresh, t, toast } from '../store';

const list = ref<DeployedAutomation[]>([]);
const selId = ref('');
const error = ref('');
const sel = () => list.value.find((d) => d.ha_id === selId.value);

async function load() {
  try { list.value = await api.get<DeployedAutomation[]>('api/automations'); } catch (e) { error.value = (e as Error).message; }
}
async function act(fn: () => Promise<unknown>, done: string) {
  error.value = '';
  try { await fn(); toast(done); } catch (e) { error.value = (e as Error).message; }
  load();
  refresh();
}
const setEnabled = (d: DeployedAutomation, enabled: boolean) =>
  act(() => api.put(`api/automations/${d.ha_id}/enabled`, { enabled }), enabled ? t('Enabled.') : t('Disabled.'));
function rollback(d: DeployedAutomation, version: number) {
  if (!confirm(`${t('Deploy version')} ${version}?`)) return;
  act(() => api.post(`api/automations/${d.ha_id}/rollback`, { version }), t('Rolled back.'));
}
function remove(d: DeployedAutomation) {
  if (!confirm(t('Remove this automation from Home Assistant? Its versions stay here.'))) return;
  if (selId.value === d.ha_id) selId.value = '';
  act(() => api.del(`api/automations/${d.ha_id}`), t('Removed.'));
}
onActivated(load);
</script>

<template>
  <section class="page">
    <div class="sec-h">
      <span class="muted small">{{ list.length }} {{ t('deployed by Agent House') }}</span>
      <Info :tip="t('Your own automations are never listed or changed here.')" />
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="list.length" class="table-wrap">
      <table>
        <thead><tr><th>{{ t('Automation') }}</th><th>{{ t('Version') }}</th><th>{{ t('On') }}</th><th /></tr></thead>
        <tbody>
          <tr v-for="d in list" :key="d.ha_id" class="clickable" :class="{ sel: selId === d.ha_id }" @click="selId = d.ha_id">
            <td><div class="cell-2"><span>{{ d.title }}</span>
              <small>{{ d.origin_agent ? agentName(d.origin_agent) : t('You') }}
                <span v-if="!d.present_in_ha" class="error">· {{ t('missing in Home Assistant') }}</span></small></div></td>
            <td>v{{ d.current_version }}</td>
            <td @click.stop><input type="checkbox" :checked="d.enabled" :disabled="!d.present_in_ha" :aria-label="t('Enabled')"
                                   @change="setEnabled(d, ($event.target as HTMLInputElement).checked)" /></td>
            <td class="act" @click.stop><IconButton icon="trash" :tip="t('Remove from Home Assistant')" @click="remove(d)" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="card empty">{{ t('Nothing deployed yet. Approve a draft first.') }}</p>

    <article v-if="sel()" class="card stack">
      <div class="sec-h"><h2>{{ sel()!.title }}</h2><span class="mono faint">{{ sel()!.ha_id }}</span></div>
      <div>
        <details v-for="v in sel()!.versions" :key="v.version" :open="v.version === sel()!.current_version">
          <summary><b>v{{ v.version }}</b>
            <span v-if="v.version === sel()!.current_version" class="tag ok">{{ t('current') }}</span>
            <span class="muted small">{{ dateTime(v.created) }}{{ v.note ? ' · ' + v.note : '' }}</span></summary>
          <pre class="log">{{ v.yaml }}</pre>
          <button v-if="v.version !== sel()!.current_version" class="btn sm" :disabled="killed()" @click="rollback(sel()!, v.version)">{{ t('Roll back to this version') }}</button>
        </details>
      </div>
    </article>
  </section>
</template>
