<!-- A list of objects as a table: the union of their keys (or `cols`), long values cut with a tooltip.
     Scrolls inside its own box; long lists show the first rows and a "show all". -->
<script setup lang="ts">
import { computed, ref } from 'vue';
import { cell, columns, isObj } from '../devfmt';
import { VERDICT_MARK } from '../labels';
import { t } from '../store';

const props = defineProps<{ rows: unknown[]; cols?: string[] }>();
const LIMIT = 40;
const all = ref(false);
const objs = computed(() => props.rows.map((r) => (isObj(r) ? r : { value: r })));
const heads = computed(() => props.cols || columns(objs.value));
const shown = computed(() => (all.value ? objs.value : objs.value.slice(0, LIMIT)));
</script>

<template>
  <div class="dev-table">
    <div class="table-wrap">
      <table>
        <thead><tr><th v-for="h in heads" :key="h">{{ h }}</th></tr></thead>
        <tbody>
          <tr v-for="(r, i) in shown" :key="i">
            <td v-for="h in heads" :key="h" :class="[cell(r[h], h).cls, typeof r[h] === 'number' ? 'num' : '']">
              <span v-if="h === 'verdict' && typeof r[h] === 'string'" class="row"><i class="mk" :class="VERDICT_MARK[r[h] as string] || 'blocked'" />{{ r[h] }}</span>
              <span v-else :data-tip="cell(r[h], h).tip" :tabindex="cell(r[h], h).tip ? 0 : undefined">{{ cell(r[h], h).text }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <button v-if="objs.length > LIMIT" class="btn quiet sm" @click="all = !all">{{ all ? t('Show fewer') : `${t('Show all')} ${objs.length}` }}</button>
  </div>
</template>
