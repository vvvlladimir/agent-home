<!-- Generic renderer for any JSON value: scalars as text, objects as a key/value list first and then their
     lists and sub-objects underneath, lists of objects as tables. New backend fields show up without changes. -->
<script setup lang="ts">
import { computed, ref } from 'vue';
import { cell, count, isObj, isScalar, type Obj } from '../devfmt';
import { t } from '../store';
import DevTable from './DevTable.vue';

const props = defineProps<{ value: unknown; name?: string }>();
const KV_LIMIT = 40;
const allKv = ref(false);

const scalars = computed(() => (isObj(props.value) ? Object.entries(props.value).filter(([, v]) => isScalar(v)) : []));
const nested = computed(() => (isObj(props.value) ? Object.entries(props.value).filter(([, v]) => !isScalar(v)) : []));
const shownScalars = computed(() => (allKv.value ? scalars.value : scalars.value.slice(0, KV_LIMIT)));
const scalarList = computed(() => Array.isArray(props.value) && props.value.every(isScalar));
// an object whose values are all objects ({entity: {…}, …}) reads best as a table with a key column
const keyed = computed(() => {
  const v = props.value;
  if (!isObj(v)) return null;
  const e = Object.entries(v);
  return e.length >= 2 && e.every(([, x]) => isObj(x)) ? e.map(([key, x]) => ({ key, ...(x as Obj) })) : null;
});
// many sub-values (an entity → [counts] map): one row each instead of a block each
const wide = computed(() => (nested.value.length > 12 ? nested.value.map(([key, value]) => ({ key, value })) : null));
</script>

<template>
  <span v-if="isScalar(value)" :class="cell(value, name).cls" :data-tip="cell(value, name).tip">{{ cell(value, name).text }}</span>
  <span v-else-if="!count(value)" class="faint">{{ t('none') }}</span>
  <div v-else-if="Array.isArray(value)">
    <div v-if="scalarList" class="chips">
      <span v-for="(x, i) in (value as unknown[]).slice(0, 60)" :key="i" class="tag" :data-tip="cell(x).tip">{{ cell(x).text }}</span>
      <span v-if="value.length > 60" class="tag">+{{ value.length - 60 }}</span>
    </div>
    <DevTable v-else :rows="value" />
  </div>
  <DevTable v-else-if="keyed" :rows="keyed" />
  <div v-else class="stack">
    <dl v-if="scalars.length" class="kv">
      <template v-for="[k, v] in shownScalars" :key="k">
        <dt class="mono">{{ k }}</dt>
        <dd :class="cell(v, k).cls"><span :data-tip="cell(v, k).tip" :tabindex="cell(v, k).tip ? 0 : undefined">{{ cell(v, k).text }}</span></dd>
      </template>
    </dl>
    <button v-if="scalars.length > KV_LIMIT" class="btn quiet sm" style="justify-self: start" @click="allKv = !allKv">
      {{ allKv ? t('Show fewer') : `${t('Show all')} ${scalars.length}` }}</button>
    <DevTable v-if="wide" :rows="wide" />
    <div v-for="[k, v] in wide ? [] : nested" :key="k" class="dev-sub">
      <h3 class="dev-h"><span class="mono">{{ k }}</span><span class="tag">{{ count(v) }}</span></h3>
      <DevNode :value="v" :name="k" />
    </div>
  </div>
</template>
