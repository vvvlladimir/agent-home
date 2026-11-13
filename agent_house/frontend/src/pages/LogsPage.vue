<!-- The add-on log, newest last. -->
<script setup lang="ts">
import { onActivated, ref } from 'vue';
import { api } from '../api';
import Icon from '../components/Icon.vue';
import { clock } from '../format';
import { t } from '../store';

const level = ref('info');
const logs = ref<{ ts: number; level: string; logger: string; message: string }[]>([]);
async function load() {
  try { logs.value = await api.get(`api/logs?limit=500&level=${level.value}`); } catch { logs.value = []; }
}
onActivated(load);
</script>

<template>
  <section class="page">
    <div class="toolbar">
      <label class="field"><span>{{ t('Level') }}</span>
        <select v-model="level" @change="load"><option v-for="l in ['debug', 'info', 'warning', 'error']" :key="l">{{ l }}</option></select></label>
      <button class="btn" @click="load"><Icon name="refresh" /><span>{{ t('Refresh') }}</span></button>
    </div>
    <pre class="log"><span v-for="(l, i) in logs" :key="i" :class="'lvl-' + l.level">{{ `${clock(l.ts)} ${l.level.toUpperCase().padEnd(7)} ${l.logger}: ${l.message}\n` }}</span></pre>
  </section>
</template>
