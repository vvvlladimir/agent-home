<!-- One icon from the inline sprite (IconSprite.vue). Decorative: the control around it carries the label. -->
<script setup lang="ts">
defineProps<{ name: string }>();
</script>
<template><svg class="i" aria-hidden="true"><use :href="'#i-' + name" /></svg></template>
