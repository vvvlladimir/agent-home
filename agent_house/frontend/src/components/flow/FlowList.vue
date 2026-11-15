<!-- One list of the automation (triggers, conditions, a branch's steps): nodes joined by connector
     slots, top to bottom; `row` lays them side by side (any of the triggers, what a wait waits for)
     with a drop zone on each branch's left edge and a last branch that only adds. -->
<script setup lang="ts">
import { computed } from 'vue';
import { items, type Kind, type Node } from '../../flow';
import { t } from '../../store';
import { useFlow } from './context';
import FlowNode from './FlowNode.vue';
import FlowSlot from './FlowSlot.vue';

const props = defineProps<{ holder: Node; lkey: string; kind: Kind; row?: boolean; empty?: string }>();
const list = computed(() => items(props.holder, props.lkey));
const flow = useFlow();
</script>

<template>
  <div v-if="row" class="fork">
    <div v-for="(node, i) in list" :key="i" class="fbranch">
      <FlowSlot :holder="holder" :lkey="lkey" :index="i" :kind="kind" across />
      <FlowNode :node="node" :loc="{ holder, key: lkey, index: i, kind }" />
    </div>
    <div v-if="!flow.readonly.value || !list.length" class="fbranch fghost">
      <p v-if="!list.length" class="fempty">{{ t(empty || 'Nothing yet') }}</p>
      <FlowSlot :holder="holder" :lkey="lkey" :index="list.length" :kind="kind" />
    </div>
  </div>
  <div v-else class="fcol">
    <template v-for="(node, i) in list" :key="i">
      <FlowSlot :holder="holder" :lkey="lkey" :index="i" :kind="kind" />
      <FlowNode :node="node" :loc="{ holder, key: lkey, index: i, kind }" />
    </template>
    <p v-if="!list.length && empty" class="fempty">{{ t(empty) }}</p>
    <FlowSlot :holder="holder" :lkey="lkey" :index="list.length" :kind="kind" />
  </div>
</template>
