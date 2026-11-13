<script setup lang="ts">
import Icon from './Icon.vue';
defineProps<{ placeholder: string }>();
const model = defineModel<string>({ required: true });
</script>
<template>
  <label class="field search grow"><Icon name="search" /><input v-model="model" type="search" :placeholder="placeholder" :aria-label="placeholder" /></label>
</template>
