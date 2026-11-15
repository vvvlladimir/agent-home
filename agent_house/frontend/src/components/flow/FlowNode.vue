<!-- One node: a card (click to edit, drag to move), and for branching steps the branches below it,
     side by side, joining again underneath. -->
<script setup lang="ts">
import { computed } from 'vue';
import { actionType, conditionType, describe, duration, items, structure, type Kind, type Loc, type Node } from '../../flow';
import { t } from '../../store';
import { same, useFlow } from './context';
import FlowList from './FlowList.vue';

const props = defineProps<{ node: Node; loc: Loc }>();
const flow = useFlow();
const shape = computed(() => structure(props.node));
// a condition used as a step reads and looks like a condition
const look = computed<Kind>(() => (props.loc.kind === 'action' && actionType(props.node) === 'condition' ? 'condition' : props.loc.kind));
const group = computed(() => look.value === 'condition' && ['and', 'or', 'not'].includes(conditionType(props.node)));
const LABEL: Record<Kind, string> = { trigger: 'When', condition: 'Only if', action: 'Do' };
const label = computed(() => {
  if (shape.value === 'if') return 'If';
  if (shape.value === 'choose') return 'Choose';
  if (shape.value === 'parallel') return 'Parallel';
  if (shape.value === 'repeat') return 'Repeat';
  if (props.loc.kind === 'action' && actionType(props.node) === 'wait_for_trigger') return 'Wait';
  if (props.loc.kind === 'action' && actionType(props.node) === 'delay') return 'Wait';
  return LABEL[look.value];
});
const text = computed(() => {
  const n = props.node;
  const many = (key: string) => items(n, key).length;
  if (shape.value === 'if') return many('if') > 1 ? t('when all of these are true') : t('when this is true');
  if (shape.value === 'choose') return t('the first case that fits runs');
  if (shape.value === 'parallel') return t('every branch at once');
  if (shape.value === 'repeat') return n.repeat?.count != null ? `${n.repeat.count}× ${t('the steps inside')}` : n.repeat?.while ? t('while a condition holds') : n.repeat?.until ? t('until a condition holds') : t('for each item');
  if (props.loc.kind === 'action' && actionType(n) === 'wait_for_trigger')
    return `${n.timeout ? `${t('up to')} ${duration(n.timeout)}, ` : ''}${t(many('wait_for_trigger') > 1 ? 'for one of these' : 'for this')}`;
  return describe(n, look.value);
});
const selected = computed(() => same(flow.selected.value, props.loc));
const isDragged = computed(() => same(flow.dragging.value, props.loc));
const options = computed(() => items(props.node, 'choose'));
const branches = computed(() => items(props.node, 'parallel'));

function start(e: DragEvent) {
  if (flow.readonly.value) return;
  e.dataTransfer?.setData('text/plain', text.value);
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
  flow.dragging.value = props.loc;
}
function addCase() {
  const list = items(props.node, 'choose');
  if (!Array.isArray(props.node.choose)) props.node.choose = list;
  props.node.choose.push({ conditions: [], sequence: [] });
  flow.changed();
}
function dropCase(i: number) {
  props.node.choose.splice(i, 1);
  flow.select(null);
  flow.changed();
}
</script>

<template>
  <div class="fnode">
    <div class="fcard" :class="[`k-${look}`, { sel: selected, off: node.enabled === false, ghost: isDragged }]"
         :draggable="!flow.readonly.value" tabindex="0" role="button" :aria-pressed="selected"
         @dragstart.stop="start" @dragend="flow.dragging.value = null"
         @click.stop="flow.select(loc)" @keydown.enter.prevent="flow.select(loc)">
      <span class="fkind">{{ t(label) }}<template v-if="node.enabled === false"> · {{ t('off') }}</template></span>
      <span class="ftext">{{ text }}</span>
      <span v-if="node.alias" class="ftext faint small">{{ node.alias }}</span>
    </div>

    <!-- and / or / not: the conditions inside -->
    <div v-if="group" class="fframe">
      <FlowList :holder="node" lkey="conditions" kind="condition" empty="No conditions inside yet" />
    </div>

    <!-- wait for a trigger: what it waits for, side by side -->
    <div v-else-if="loc.kind === 'action' && actionType(node) === 'wait_for_trigger'" class="fframe">
      <FlowList :holder="node" lkey="wait_for_trigger" kind="trigger" row />
      <p class="fnote">{{ node.continue_on_timeout === false ? t('Not within the time: stop here') : t('Then go on (also after the time is up)') }}</p>
    </div>

    <!-- if / then / else -->
    <template v-else-if="shape === 'if'">
      <div class="fframe">
        <FlowList :holder="node" lkey="if" kind="condition" empty="No condition: always yes" />
      </div>
      <div class="fork">
        <div class="fbranch"><span class="fchip yes">{{ t('Yes') }}</span><FlowList :holder="node" lkey="then" kind="action" empty="Nothing" /></div>
        <div class="fbranch"><span class="fchip no">{{ t('No') }}</span><FlowList :holder="node" lkey="else" kind="action" empty="Nothing" /></div>
      </div>
      <div class="fjoin" />
    </template>

    <!-- choose: one branch per case, then the default -->
    <template v-else-if="shape === 'choose'">
      <div class="fork">
        <div v-for="(option, i) in options" :key="i" class="fbranch">
          <span class="fchip">{{ t('Case') }} {{ i + 1 }}
            <button v-if="!flow.readonly.value" type="button" class="fchip-x" :aria-label="t('Remove this case')" @click.stop="dropCase(i)">×</button></span>
          <div class="fframe"><FlowList :holder="option" lkey="conditions" kind="condition" empty="No condition: always" /></div>
          <FlowList :holder="option" lkey="sequence" kind="action" empty="Nothing" />
        </div>
        <div class="fbranch"><span class="fchip no">{{ t('Otherwise') }}</span><FlowList :holder="node" lkey="default" kind="action" empty="Nothing" /></div>
      </div>
      <button v-if="!flow.readonly.value" type="button" class="btn sm fadd-case" @click.stop="addCase">+ {{ t('case') }}</button>
      <div class="fjoin" />
    </template>

    <!-- parallel: every branch at once -->
    <template v-else-if="shape === 'parallel'">
      <div class="fork">
        <div v-for="(branch, i) in branches" :key="i" class="fbranch">
          <FlowList v-if="branch && typeof branch === 'object' && 'sequence' in branch" :holder="branch" lkey="sequence" kind="action" empty="Nothing" />
          <FlowNode v-else :node="branch" :loc="{ holder: node, key: 'parallel', index: i, kind: 'action' }" />
        </div>
      </div>
      <div class="fjoin" />
    </template>

    <!-- repeat: the steps inside, in a loop frame -->
    <div v-else-if="shape === 'repeat' && node.repeat" class="fframe loop">
      <FlowList :holder="node.repeat" lkey="sequence" kind="action" empty="Nothing" />
    </div>

    <!-- a bare sequence step -->
    <div v-else-if="shape === 'sequence'" class="fframe">
      <FlowList :holder="node" lkey="sequence" kind="action" empty="Nothing" />
    </div>
  </div>
</template>
