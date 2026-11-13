<!-- Live feed: every event (state changes, agents, system), searchable. -->
<script setup lang="ts">
import { computed, ref } from 'vue';
import SearchField from '../components/SearchField.vue';
import { clock } from '../format';
import { state, t } from '../store';

const kind = ref('');
const query = ref('');
const kinds = computed(() => [...new Set(state.feed.map((e) => e.kind))].sort());
const shown = computed(() => {
  const needle = query.value.toLowerCase();
  return state.feed
    .filter((e) => (!kind.value || e.kind === kind.value)
      && (!needle || `${e.summary} ${e.agent_id || ''} ${e.entity_id || ''}`.toLowerCase().includes(needle)))
    .slice(0, 300);
});
</script>

<template>
  <section class="page">
    <div class="toolbar">
      <SearchField v-model="query" :placeholder="t('Search entity, agent, text')" />
      <label class="field"><span>{{ t('Kind') }}</span>
        <select v-model="kind"><option value="">{{ t('All') }}</option><option v-for="k in kinds" :key="k" :value="k">{{ k }}</option></select></label>
      <label class="field check"><input v-model="state.feedPaused" type="checkbox" /> <span>{{ t('Pause') }}</span></label>
      <span class="status" :class="state.streamOk ? '' : state.polling ? 'warn' : 'bad'">
        {{ state.streamOk ? t('Live') : state.polling ? t('Polling every 3 s') : t('Reconnecting…') }}</span>
    </div>
    <div class="card list">
      <ul class="feed">
        <li v-for="e in shown" :key="e.id" :class="'lvl-' + e.level">
          <time>{{ clock(e.ts) }}</time><span /><span class="msg">{{ e.summary }}</span><span class="tag">{{ e.kind }}</span>
        </li>
      </ul>
      <p v-if="!shown.length" class="empty">{{ t('State changes and agent activity show up here as they happen.') }}</p>
    </div>
  </section>
</template>
