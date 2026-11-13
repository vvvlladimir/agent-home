<!-- Header of a detail view (agent editor, draft): back, title, actions on the right. -->
<script setup lang="ts">
import IconButton from './IconButton.vue';
import { t } from '../store';
defineProps<{ title: string }>();
defineEmits<{ back: [] }>();
</script>
<template>
  <div class="head">
    <IconButton icon="back" :tip="t('Back')" @click="$emit('back')" />
    <h1>{{ title }}</h1>
    <slot name="badge" />
    <div class="actions"><slot /></div>
  </div>
</template>
