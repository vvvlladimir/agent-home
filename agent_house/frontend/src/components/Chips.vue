<!-- Filter chips: one choice, optional count and activity marker per chip. -->
<script setup lang="ts" generic="V extends string">
import { t } from '../store';
export interface Chip<V> { value: V; label: string; count?: number; mark?: string }
defineProps<{ options: Chip<V>[]; label: string }>();
const model = defineModel<V>({ required: true });
</script>
<template>
  <div class="chips" role="group" :aria-label="label">
    <button v-for="c in options" :key="c.value" type="button" class="chip" :class="{ on: model === c.value }" :aria-pressed="model === c.value" @click="model = c.value">
      <i v-if="c.mark" class="mk" :class="c.mark" />
      <span>{{ t(c.label) }}</span>
      <span v-if="c.count != null" class="n">{{ c.count }}</span>
    </button>
  </div>
</template>
