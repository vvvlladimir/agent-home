<!-- T0–T3 badge (or "read" for sensors) with the meaning in a tooltip. -->
<script setup lang="ts">
import { tierHelp } from '../labels';
import { t } from '../store';
defineProps<{ tier: string; readOnly?: boolean }>();
</script>
<template>
  <span class="tier" :class="readOnly ? '' : 'tier-' + tier" :data-tip="readOnly ? t('Read only: nothing to switch') : tierHelp(tier)">{{ readOnly ? t('read') : tier }}</span>
</template>
