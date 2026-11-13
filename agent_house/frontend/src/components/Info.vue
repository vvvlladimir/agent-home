<!-- The ⓘ button: secondary text lives here instead of on the page. Hover, focus or tap shows it. -->
<script setup lang="ts">
import Icon from './Icon.vue';
import { t } from '../store';
defineProps<{ tip: string; label?: string }>();
</script>
<template>
  <button class="info" type="button" :data-tip="tip" :aria-label="label || t('Details')"><Icon name="info" /></button>
</template>
