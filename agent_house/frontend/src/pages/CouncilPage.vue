<!-- House: house mode, what wakes the orchestrator, the daily review, the house manager, who owns which
     entity, and the learner on the Jetson (D38): its journal, training and what it found. -->
<script setup lang="ts">
import { computed, onActivated, onDeactivated, ref } from 'vue';
import { api, enc, type Council, type LearnerInfo, type ManagerStatus } from '../api';
import IconButton from '../components/IconButton.vue';
import Info from '../components/Info.vue';
import { ago, dateTime, hm, pct } from '../format';
import { agentName } from '../labels';
import { go } from '../router';
import { fail, killed, nav, state, t, toast, withBusy } from '../store';

const council = ref<Council | null>(null);
const learner = ref<LearnerInfo | null>(null);
const manager = ref<ManagerStatus | null>(null);
interface LearnerModel {
  trained_at?: number; fresh?: boolean; facts?: string[];
  history?: { days?: number; events?: number; by_hand?: number };
  counts?: Record<string, number>;
}
interface LearnerJetson { journal?: { events?: number; size_mb?: number | null }; training?: { state?: string; finished?: number | null; next_at?: number | null; error?: string | null } }
const model = computed(() => learner.value?.model as LearnerModel | null | undefined);
const jetson = computed(() => learner.value?.jetson as LearnerJetson | null | undefined);
const backfill = computed(() => learner.value?.link?.backfill as { state?: string; days?: number; events?: number; error?: string | null } | undefined);
const COUNT_LABEL: Record<string, string> = { habits: 'habits', live: 'live targets', routines: 'routines', dependencies: 'links', corrections: 'corrected', timeouts: 'timeouts' };
const error = ref('');
const ownerQuery = ref('');
const ownerResult = ref('');

async function load() {
  try { council.value = await api.get<Council>('api/council'); error.value = ''; } catch (e) { error.value = (e as Error).message; }
  try { learner.value = await api.get<LearnerInfo>('api/learner'); } catch { learner.value = null; }
  try { manager.value = await api.get<ManagerStatus>('api/manager'); } catch { manager.value = null; }
}
async function post(id: string, path: string, done: string) {
  if ((await withBusy(id, () => api.post(path))) !== undefined) toast(t(done));
  load();
}
async function lookup() {
  const id = ownerQuery.value.trim();
  if (!id) return;
  try {
    const r = await api.get<{ owner: string; area?: string | null }>(`api/council/owner/${enc(id)}`);
    ownerResult.value = (r.owner === 'nobody' ? t('Nobody') : agentName(r.owner)) + (r.area ? ` · ${r.area}` : '');
  } catch (e) { ownerResult.value = ''; fail(e); }
}
function openRun(id?: string | null) { if (id) { nav.runId = id; go('runs'); } }

const MODE_LABEL: Record<string, string> = { home: 'Home', away: 'Away', night: 'Night', guests: 'Guests', work: 'Work' };
const review = computed(() => council.value?.review as { at?: string | null; running?: boolean; last?: { ts: number; drafted?: number; run_id?: string; summary?: string } | null } | undefined);
const scan = computed(() => council.value?.last_scan as { scanned: number; changes: number; manual: number } | null | undefined);
const owners = computed(() => Object.entries(council.value?.ownership?.counts || {}).sort((a, b) => b[1] - a[1]));
const maxOwned = computed(() => Math.max(1, ...owners.value.map(([, n]) => n)));

let timer: number | undefined;
onActivated(() => { load(); timer = setInterval(load, 10000); });
onDeactivated(() => clearInterval(timer));
</script>

<template>
  <section class="page">
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="!council" class="cols"><div class="sk block" /><div class="sk block" /></div>

    <div v-else class="cols">
      <div class="stack">
        <article class="card stack">
          <div class="sec-h">
            <h2>{{ t('House mode') }}
              <Info :tip="t('The orchestrator switches only this helper, in shadow unless its own mode is live. Guests and work are set by you only. Your automations read it, so the house reacts to the mode inside Home Assistant.')" /></h2>
            <a class="btn quiet sm" href="#settings">{{ t('Settings') }}</a>
          </div>
          <template v-if="council.mode_entity_present">
            <div class="big">{{ council.mode ? t(MODE_LABEL[council.mode] || council.mode) : t('Unknown') }}</div>
            <div class="chips"><span v-for="m in council.modes" :key="m" class="tag" :class="{ ok: m === council.mode }">{{ t(MODE_LABEL[m] || m) }}</span></div>
          </template>
          <div v-else class="stack">
            <p class="muted">{{ t('The mode helper does not exist in Home Assistant yet.') }}</p>
            <div><button class="btn primary" :aria-busy="state.busy.helper || undefined" @click="post('helper', 'api/council/mode-helper', 'Mode helper created.')">{{ t('Create the mode helper') }}</button></div>
          </div>
          <dl class="kv">
            <dt>{{ t('Orchestrator') }}</dt>
            <dd><span v-if="council.orchestrator_mode === 'live'" class="live-dot">{{ t('Live') }}</span><span v-else class="status shadow">{{ t('Shadow') }}</span></dd>
            <dt>{{ t('Switches the mode itself') }}</dt><dd>{{ council.auto_house_mode ? t('on') : t('off') }}</dd>
            <dt v-if="council.night_window">{{ t('Night') }}</dt><dd v-if="council.night_window">{{ council.night_window }}</dd>
          </dl>
        </article>

        <article class="card stack">
          <h2>{{ t('What wakes the orchestrator') }}</h2>
          <ul class="feed marks">
            <li v-for="w in council.orchestrator_wakes" :key="w.id">
              <i class="mk" :class="w.active ? 'done' : ''" :data-tip="w.active ? t('On') : t('Off')" />
              <span class="msg" :class="{ faint: !w.active }">{{ w.rule }}</span>
              <span class="faint small nowrap" :data-tip="w.last ? dateTime(w.last) : ''">{{ w.last ? ago(w.last) : t('never') }}</span>
            </li>
          </ul>
        </article>

        <article class="card stack">
          <div class="sec-h">
            <h2>{{ t('Daily review') }}
              <Info :tip="t('Once a day the orchestrator reads the reports, the inbox and a history summary, and drafts at most one automation per entity.') + (scan ? `\n${t('Last scan')}: ${hm(scan.scanned)} · ${scan.changes} ${t('changes')}, ${scan.manual} ${t('by hand')}` : '')" /></h2>
            <span class="muted small">{{ review?.running ? t('running') : review?.at ? `${t('daily at')} ${review.at}` : t('off') }}</span>
          </div>
          <template v-if="review?.last">
            <div class="row">
              <span class="big">{{ review.last.drafted ?? 0 }}</span>
              <span class="muted small">{{ t('drafted') }} · {{ dateTime(review.last.ts) }}</span>
              <IconButton v-if="review.last.run_id" icon="trace" :tip="t('Open trace')" @click="openRun(review.last.run_id)" />
            </div>
            <details v-if="review.last.summary"><summary>{{ t('Summary') }}</summary><p style="white-space: pre-wrap">{{ review.last.summary }}</p></details>
          </template>
          <p v-else class="muted small">{{ t('No review has run yet.') }}</p>
          <div><button class="btn" :aria-busy="review?.running || undefined" :disabled="review?.running"
                       @click="post('review', 'api/council/review', 'Review started; the result goes to the inbox.')">{{ t('Run review now') }}</button></div>
        </article>
      </div>

      <div class="stack">
        <article v-if="learner" class="card stack">
          <div class="sec-h">
            <h2>{{ t('Learner') }}
              <Info :tip="t('The Jetson keeps the house history on its own disk and learns from it every night: habits over months, routines, what follows what, which automations you keep correcting. It never switches anything itself; its live suggestions go through the same safety checks as everything else.')" /></h2>
            <span v-if="learner.configured" class="status" :class="learner.link.connected ? 'ok' : 'warn'">{{ learner.link.connected ? t('connected') : t('unreachable') }}</span>
          </div>
          <p v-if="!learner.configured" class="muted small">{{ t('Not set up. Install jetson/learner on the Jetson and set the add-on option Jetson learner URL.') }}</p>
          <template v-else>
            <p v-if="learner.link.error" class="error small">{{ learner.link.error }}</p>
            <dl class="kv">
              <dt>{{ t('Journal') }}</dt>
              <dd>{{ jetson?.journal?.events ?? '–' }} {{ t('events') }}<span v-if="model?.history?.days"> · {{ Math.round(model.history.days) }} {{ t('days') }}</span><span v-if="jetson?.journal?.size_mb != null" class="faint"> · {{ jetson.journal.size_mb }} MB</span></dd>
              <dt>{{ t('Forwarded') }}</dt>
              <dd>{{ learner.link.pushed }}<span v-if="learner.link.buffered" class="faint"> · {{ learner.link.buffered }} {{ t('waiting') }}</span><span v-if="learner.link.dropped" class="warn-text"> · {{ learner.link.dropped }} {{ t('lost') }}</span></dd>
              <dt>{{ t('From the logbook') }}</dt>
              <dd>{{ backfill?.state ? t(backfill.state) : '–' }}<span v-if="backfill?.days" class="faint"> · {{ backfill.days }} {{ t('days') }}, {{ backfill.events }} {{ t('events') }}</span></dd>
              <dt>{{ t('Training') }}</dt>
              <dd>{{ jetson?.training?.state ? t(jetson.training.state) : '–' }}<span v-if="model?.trained_at" class="faint"> · {{ ago(model.trained_at) }}</span><span v-if="jetson?.training?.next_at" class="faint"> · {{ t('next') }} {{ hm(jetson.training.next_at) }}</span></dd>
              <dt>{{ t('Live suggestions') }}</dt>
              <dd><span v-if="learner.mode === 'live'" class="live-dot">{{ t('Live') }}</span><span v-else class="status shadow">{{ t('Shadow') }}</span>
                <span class="faint"> · {{ t('today') }}: {{ learner.live?.acted ?? 0 }} {{ t('done') }}, {{ learner.live?.shadow ?? 0 }} {{ t('would have') }}</span></dd>
            </dl>
            <p v-if="jetson?.training?.error" class="error small">{{ jetson.training.error }}</p>
            <div v-if="model?.counts" class="chips">
              <span v-for="(n, k) in model.counts" :key="k" class="tag">{{ n }} {{ t(COUNT_LABEL[k] || k) }}</span>
            </div>
            <details v-if="model?.facts?.length"><summary>{{ t('What it found') }}</summary>
              <ul class="small"><li v-for="f in model.facts" :key="f">{{ f }}</li></ul>
            </details>
            <div><button class="btn" :aria-busy="state.busy.train || undefined"
                         @click="post('train', 'api/learner/train', 'Training started on the Jetson.')">{{ t('Train now') }}</button></div>
          </template>
        </article>

        <article v-if="manager" class="card stack">
          <div class="sec-h">
            <h2>{{ t('House manager') }}
              <Info :tip="t('Between reviews a cheap check looks at the house (lights on with nobody home, weak automations). The model is called only when it finds something. Its actions follow the orchestrator mode.')" /></h2>
            <span class="status" :class="manager.enabled ? 'ok' : 'off'">{{ manager.enabled ? t('on') : t('off') }}</span>
          </div>
          <p v-if="manager.hint" class="muted small">{{ manager.hint }}</p>
          <dl class="kv">
            <dt>{{ t('Mode') }}</dt>
            <dd><span v-if="manager.mode === 'live'" class="live-dot">{{ t('Live') }}</span><span v-else class="status shadow">{{ t('Shadow') }}</span></dd>
            <dt>{{ t('Check every') }}</dt><dd>{{ manager.sweep_min ? `${manager.sweep_min} min` : t('off') }}</dd>
            <dt>{{ t('Wakes the model') }}</dt><dd>{{ manager.wakes_last_hour ?? 0 }} / {{ manager.max_wakes_per_hour ?? '–' }} {{ t('in the last hour') }}</dd>
            <dt>{{ t('Last run') }}</dt>
            <dd>{{ manager.running ? t('running') : manager.last_run ? ago(manager.last_run) : t('never') }}
              <IconButton v-if="manager.last_run_id" icon="trace" :tip="t('Open trace')" @click="openRun(manager.last_run_id)" /></dd>
            <dt>{{ t('May switch') }}</dt><dd>{{ manager.owned.length }} {{ t('devices') }} <Info v-if="manager.owned_note" :tip="manager.owned_note" /></dd>
          </dl>
          <ul v-if="manager.anomalies.length" class="feed marks">
            <li v-for="a in manager.anomalies" :key="a"><i class="mk warn" /><span class="msg">{{ a }}</span></li>
          </ul>
          <p v-else-if="manager.enabled" class="muted small">{{ t('Nothing to look at right now.') }}</p>
          <div><button class="btn" :aria-busy="state.busy.manager || undefined" :disabled="killed()"
                       @click="post('manager', 'api/manager/wake', 'The house manager is looking.')">{{ t('Ask the house manager') }}</button></div>
        </article>

        <article class="card stack">
          <h2>{{ t('Ownership') }}
            <Info :tip="t('The orchestrator owns every entity it may act on or automate. Pairing switches and the like belong to nobody.')" /></h2>
          <div v-for="[owner, n] in owners" :key="owner" class="row">
            <span style="min-width: 8rem">{{ owner === 'nobody' ? t('Nobody') : agentName(owner) }}</span>
            <span class="meter grow"><span :style="{ width: pct(n, maxOwned) + '%' }" /></span>
            <span class="muted small" style="min-width: 2rem; text-align: right">{{ n }}</span>
          </div>
          <form class="row" @submit.prevent="lookup">
            <input v-model="ownerQuery" class="mono grow" placeholder="light.kitchen" :aria-label="t('Who owns this entity?')" />
            <button class="btn" type="submit">{{ t('Who owns it?') }}</button>
          </form>
          <p v-if="ownerResult" class="small">{{ ownerResult }}</p>
        </article>
      </div>
    </div>
  </section>
</template>
