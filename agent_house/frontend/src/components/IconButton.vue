<!-- Icon-only control. `tip` is both the tooltip and the accessible name. With `href` it is a link. -->
<script setup lang="ts">
import Icon from './Icon.vue';
defineProps<{ icon: string; tip: string; href?: string; busy?: boolean; disabled?: boolean }>();
</script>
<template>
  <a v-if="href" class="icon-btn" :href="href" :data-tip="tip" :aria-label="tip"><Icon :name="icon" /></a>
  <button v-else class="icon-btn" type="button" :data-tip="tip" :aria-label="tip" :aria-busy="busy || undefined" :disabled="disabled">
    <Icon :name="icon" />
  </button>
</template>
