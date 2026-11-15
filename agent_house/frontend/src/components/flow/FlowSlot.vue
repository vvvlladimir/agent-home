<!-- A place between two nodes: the connector line, a "+" to add a node here, and a drop zone
     while a node is being dragged (highlighted only where that node fits). -->
<script setup lang="ts">
import { computed, ref } from 'vue';
import { fits, holds, items, PALETTE, type Kind, type Node } from '../../flow';
import { t } from '../../store';
import { useFlow } from './context';

const props = defineProps<{ holder: Node; lkey: string; index: number; kind: Kind; across?: boolean; last?: boolean }>();
const flow = useFlow();
const over = ref(false);
const here = computed(() => ({ holder: props.holder, key: props.lkey, index: props.index, kind: props.kind }));
const open = computed(() => {
  const a = flow.adding.value;
  return !!a && a.holder === props.holder && a.key === props.lkey && a.index === props.index;
});
const accepts = computed(() => {
  const d = flow.dragging.value;
  if (!d || flow.readonly.value || !fits(d.kind, props.kind)) return false;
  const node = items(d.holder, d.key)[d.index];
  if (holds(node, props.holder)) return false;
  // dropping right before or after itself changes nothing
  return !(d.holder === props.holder && d.key === props.lkey && (d.index === props.index || d.index + 1 === props.index));
});
function onDrop(e: DragEvent) {
  e.preventDefault(); over.value = false;
  if (accepts.value) flow.drop(here.value);
}
function toggle() { flow.adding.value = open.value ? null : here.value; }
function pick(make: () => Node) { flow.insert(here.value, make()); flow.adding.value = null; }
const palette = computed(() => PALETTE[props.kind]);
</script>

<template>
  <div class="fslot" :class="{ across, last, accepts, over }"
       @dragover="accepts && ($event.preventDefault(), over = true)" @dragleave="over = false" @drop="onDrop">
    <button v-if="!flow.readonly.value" type="button" class="fplus" :class="{ on: open }"
            :aria-label="t('Add here')" :aria-expanded="open" @click.stop="toggle">+</button>
    <div v-if="open" class="fmenu" role="menu" @click.stop>
      <div class="fmenu-h">{{ t(kind === 'trigger' ? 'Start when…' : kind === 'condition' ? 'Only if…' : 'Do…') }}</div>
      <button v-for="p in palette" :key="p.label" type="button" role="menuitem" @click="pick(p.make)">
        <span>{{ t(p.label) }}</span><small v-if="p.hint" class="faint">{{ t(p.hint) }}</small></button>
    </div>
  </div>
</template>
