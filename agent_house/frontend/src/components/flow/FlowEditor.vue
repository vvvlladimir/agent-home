<!-- The automation as a flow chart, top to bottom: When (any trigger) → Only if (every condition)
     → Do (steps; forks for if / choose / parallel, joined again below). Click a card to edit it on
     the right, drag it onto a highlighted "+" to move it, "+" to add. Undo / redo, zoom, pan. -->
<script setup lang="ts">
import { computed, provide, ref, toRaw, watch } from 'vue';
import { clone, items, lane, move, type Config, type Loc, type Node } from '../../flow';
import { t } from '../../store';
import { FLOW, type FlowCtx } from './context';
import FlowInspector from './FlowInspector.vue';
import FlowList from './FlowList.vue';

const props = defineProps<{ readonly?: boolean; stats?: { fired?: number; blocked?: number; days?: number } | null }>();
const model = defineModel<Config>({ required: true });

const readonly = computed(() => !!props.readonly);
const selected = ref<Loc | null>(null);
const dragging = ref<Loc | null>(null);
const adding = ref<FlowCtx['adding']['value']>(null);

// --- undo / redo: a snapshot after every change ---------------------------------------------
const past = ref<string[]>([]);
const future = ref<string[]>([]);
let current = '';
let restoring = false;
watch(() => toRaw(model.value), () => {
  if (restoring) { restoring = false; return; }
  current = JSON.stringify(model.value); past.value = []; future.value = []; selected.value = null;
}, { immediate: true });
function changed() {
  const next = JSON.stringify(model.value);
  if (next === current) return;
  past.value.push(current); future.value = []; current = next;
  if (past.value.length > 100) past.value.shift();
}
function restore(from: typeof past, to: typeof future) {
  const snap = from.value.pop();
  if (snap == null) return;
  to.value.push(current); current = snap;
  restoring = true; selected.value = null;
  model.value = JSON.parse(snap);
}
const undo = () => restore(past, future);
const redo = () => restore(future, past);

// --- edits ------------------------------------------------------------------------------------
const ctx: FlowCtx = {
  readonly, selected, dragging, adding,
  select(loc) { selected.value = loc; adding.value = null; },
  drop(to, from = dragging.value ?? undefined) {
    dragging.value = null;
    if (!from || readonly.value) return;
    move(from, to);
    // where the moved node is now (the list it left may have shifted it by one)
    const index = from.holder === to.holder && from.key === to.key && from.index < to.index ? to.index - 1 : to.index;
    selected.value = { holder: to.holder, key: to.key, index, kind: from.kind === 'condition' && to.kind === 'action' ? 'action' : to.kind };
    changed();
  },
  insert(to, node) {
    if (readonly.value) return;
    lane(to.holder, to.key).splice(to.index, 0, node);
    selected.value = { ...to };
    changed();
  },
  remove(loc) {
    if (readonly.value) return;
    lane(loc.holder, loc.key).splice(loc.index, 1);
    selected.value = null;
    changed();
  },
  changed,
};
provide(FLOW, ctx);

function onKey(e: KeyboardEvent) {
  const el = e.target as HTMLElement;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) return;
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); (e.shiftKey ? redo : undo)(); }
  else if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); }
  else if ((e.key === 'Delete' || e.key === 'Backspace') && selected.value) { e.preventDefault(); ctx.remove(selected.value); }
  else if (e.key === 'Escape') { selected.value = null; adding.value = null; }
}

// --- view: zoom and pan -----------------------------------------------------------------------
const zoom = ref(1);
const canvas = ref<HTMLElement | null>(null);
const zoomBy = (d: number) => { zoom.value = Math.min(1.5, Math.max(0.4, Math.round((zoom.value + d) * 10) / 10)); };
function fit() {
  const el = canvas.value;
  const inner = el?.firstElementChild as HTMLElement | null;
  if (!el || !inner) return;
  const w = (el.clientWidth - 48) / (inner.scrollWidth / zoom.value);
  const h = (el.clientHeight - 52) / (inner.scrollHeight / zoom.value);
  zoom.value = Math.min(1, Math.max(0.4, Math.floor(Math.min(w, h) * 20) / 20));
}
let pan: { x: number; y: number; left: number; top: number } | null = null;
function panStart(e: PointerEvent) {
  const target = e.target as HTMLElement;
  if (e.button !== 0 || target.closest('.fcard, button, .fmenu, input, textarea, select')) return;
  pan = { x: e.clientX, y: e.clientY, left: canvas.value!.scrollLeft, top: canvas.value!.scrollTop };
  selected.value = null; adding.value = null;
}
function panMove(e: PointerEvent) {
  if (!pan || !canvas.value) return;
  canvas.value.scrollLeft = pan.left - (e.clientX - pan.x);
  canvas.value.scrollTop = pan.top - (e.clientY - pan.y);
}

// --- the automation itself (shown when no card is selected) ---------------------------------
const MODES = ['single', 'restart', 'queued', 'parallel'];
function field(key: 'alias' | 'description' | 'mode', value: string) {
  if (value) model.value[key] = value; else delete model.value[key];
  changed();
}
const counts = computed(() => ({
  triggers: items(model.value, 'triggers').length,
  conditions: items(model.value, 'conditions').length,
  actions: items(model.value, 'actions').length,
}));
defineExpose({ undo, redo, snapshot: () => clone(model.value) as Node });
</script>

<template>
  <div class="flow" tabindex="-1" @keydown="onKey" @click="adding = null">
    <div class="ftools row">
      <button type="button" class="btn sm" :disabled="!past.length || readonly" :aria-label="t('Undo')" data-tip="Ctrl/⌘ Z" @click="undo">↶</button>
      <button type="button" class="btn sm" :disabled="!future.length || readonly" :aria-label="t('Redo')" data-tip="Ctrl/⌘ Shift Z" @click="redo">↷</button>
      <span class="grow" />
      <button type="button" class="btn sm" :aria-label="t('Zoom out')" @click="zoomBy(-0.1)">−</button>
      <span class="small muted" style="min-width: 3em; text-align: center">{{ Math.round(zoom * 100) }}%</span>
      <button type="button" class="btn sm" :aria-label="t('Zoom in')" @click="zoomBy(0.1)">+</button>
      <button type="button" class="btn sm" @click="fit">{{ t('Fit') }}</button>
    </div>
    <div class="fbody">
      <div ref="canvas" class="fcanvas" :class="{ dragging: !!dragging }" @pointerdown="panStart" @pointermove="panMove" @pointerup="pan = null" @pointerleave="pan = null">
        <div class="fchart" :style="{ zoom }">
          <div class="fstage"><span class="fbadge k-trigger">{{ t('When') }}</span>
            <span class="small muted">{{ counts.triggers > 1 ? t('any of these starts it') : t('this starts it') }}<template v-if="stats?.fired != null"> · {{ stats.fired }}× {{ t('in') }} {{ stats.days }} {{ t('days') }}</template></span></div>
          <FlowList :holder="model" lkey="triggers" kind="trigger" row empty="Add what starts it" />
          <div class="fjoin" />
          <div class="fstage"><span class="fbadge k-condition">{{ t('Only if') }}</span>
            <span class="small muted">{{ counts.conditions ? t('every one must be true, else it stops') : t('no conditions: always goes on') }}<template v-if="stats?.blocked"> · {{ t('stopped') }} {{ stats.blocked }}×</template></span></div>
          <FlowList :holder="model" lkey="conditions" kind="condition" />
          <div class="fstage"><span class="fbadge k-action">{{ t('Do') }}</span>
            <span class="small muted">{{ t('top to bottom') }}</span></div>
          <FlowList :holder="model" lkey="actions" kind="action" empty="Add what it does" />
          <div class="fend">{{ t('Done') }}</div>
        </div>
      </div>
      <FlowInspector v-if="selected" :loc="selected" />
      <aside v-else class="finspect stack">
        <h3>{{ t('Automation') }}</h3>
        <label class="field"><span>{{ t('Name') }}</span><input :value="model.alias || ''" :readonly="readonly" @change="field('alias', ($event.target as HTMLInputElement).value)" /></label>
        <label class="field"><span>{{ t('Description') }}</span><textarea rows="3" :value="model.description || ''" :readonly="readonly" @change="field('description', ($event.target as HTMLTextAreaElement).value)" /></label>
        <label class="field"><span>{{ t('If it starts again while running') }}</span>
          <select :value="model.mode || 'single'" :disabled="readonly" @change="field('mode', ($event.target as HTMLSelectElement).value)">
            <option v-for="m in MODES" :key="m" :value="m">{{ t({ single: 'ignore the new start', restart: 'start over', queued: 'run again after', parallel: 'run both' }[m]!) }} ({{ m }})</option></select></label>
        <p class="small faint">{{ readonly ? t('Read only: change it with a model below, or draft a copy.') : t('Click a card to edit it. Drag a card onto a highlighted + to move it. + adds a step there. Delete removes the selected card; Ctrl/⌘ Z undoes.') }}</p>
      </aside>
    </div>
  </div>
</template>
