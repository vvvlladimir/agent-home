<!-- Which model writes this one request: the add-on setting, the Jetson, or a cloud. -->
<script setup lang="ts">
import { computed } from 'vue';
import { state, t } from '../store';

defineProps<{ label?: string }>();
const model = defineModel<string>({ required: true });
const orch = computed(() => state.status?.orchestrator);
const can = computed(() => (orch.value?.choices || {}) as Record<string, boolean | undefined>);
const options = computed(() => [
  { value: '', label: `${t('As in Settings')} (${orch.value?.provider ?? '…'} → ${orch.value?.effective ?? '…'})`, ok: true },
  { value: 'local', label: t('Local model (Jetson)'), ok: can.value.local !== false, why: t('not reachable now') },
  { value: 'anthropic', label: t('Claude (cloud, redacted)'), ok: !!can.value.anthropic, why: t('no API key') },
  { value: 'openai', label: t('OpenAI (cloud, redacted)'), ok: !!can.value.openai, why: t('no API key') },
]);
</script>

<template>
  <label class="field"><span>{{ t(label || 'Written by') }}</span>
    <select v-model="model">
      <option v-for="o in options" :key="o.value" :value="o.value" :disabled="!o.ok">{{ o.label }}{{ o.ok ? '' : ` — ${o.why}` }}</option>
    </select>
  </label>
</template>
