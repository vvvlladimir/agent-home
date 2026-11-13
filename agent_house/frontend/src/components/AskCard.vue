<!-- A decision waiting for the owner: who/why line, title, actions; the right end holds quiet controls. -->
<script setup lang="ts">
defineProps<{ title: string; first?: boolean; leaving?: boolean }>();
</script>
<template>
  <article class="card ask" :class="{ first, leaving }">
    <div class="meta"><slot name="meta" /></div>
    <h3>{{ title }}</h3>
    <div class="act">
      <slot />
      <span class="end"><slot name="end" /></span>
    </div>
  </article>
</template>
