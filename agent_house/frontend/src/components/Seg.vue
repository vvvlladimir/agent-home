<!-- Segmented control: a small set of exclusive choices (mode, input type). -->
<script setup lang="ts" generic="V extends string">
import { t } from '../store';
defineProps<{ options: readonly (readonly [V, string])[]; label: string }>();
const model = defineModel<V>({ required: true });
</script>
<template>
  <div class="seg" role="group" :aria-label="label">
    <button v-for="[value, text] in options" :key="value" type="button" :class="{ on: model === value }" :aria-pressed="model === value" @click="model = value">{{ t(text) }}</button>
  </div>
</template>
