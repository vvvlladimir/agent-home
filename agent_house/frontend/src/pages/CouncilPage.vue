<!-- Council: how the agents share the house. House mode, what wakes the orchestrator, the daily review,
     the shared board (facts, tasks, reports, escalations) and who owns which entity. -->
<script setup lang="ts">
import { computed, onActivated, onDeactivated, ref } from 'vue';
import { api, enc, type BoardEntry, type Council } from '../api';
import IconButton from '../components/IconButton.vue';
import Info from '../components/Info.vue';
import { ago, dateTime, hm, pct, until } from '../format';
import { agentName } from '../labels';
import { go } from '../router';
import { fail, nav, state, t, toast, withBusy } from '../store';

const council = ref<Council | null>(null);
const error = ref('');
const ownerQuery = ref('');
const ownerResult = ref('');

async function load() {
  try { council.value = await api.get<Council>('api/council'); error.value = ''; } catch (e) { error.value = (e as Error).message; }
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
    ownerResult.value = agentName(r.owner) + (r.area ? ` · ${r.area}` : '');
  } catch (e) { ownerResult.value = ''; fail(e); }
}
function openRun(id?: string | null) { if (id) { nav.runId = id; go('runs'); } }

const MODE_LABEL: Record<string, string> = { home: 'Home', away: 'Away', night: 'Night', guests: 'Guests', work: 'Work' };
const review = computed(() => council.value?.review as { at?: string | null; running?: boolean; last?: { ts: number; drafted?: number; run_id?: string; summary?: string } | null } | undefined);
const scan = computed(() => council.value?.last_scan as { scanned: number; changes: number; manual: number } | null | undefined);
const owners = computed(() => Object.entries(council.value?.ownership?.counts || {}).sort((a, b) => b[1] - a[1]));
const maxOwned = computed(() => Math.max(1, ...owners.value.map(([, n]) => n)));
const shared = computed(() => Object.entries(council.value?.ownership?.shared_rooms || {}));
const KIND_MARK: Record<BoardEntry['kind'], string> = { fact: 'note', task: 'ask', report: 'done', escalation: 'warn' };

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
              <Info :tip="t('The orchestrator switches only this helper, in shadow unless its own mode is live. Guests and work are set by you only. Room agents and automations read it, so rooms react to each other inside Home Assistant.')" /></h2>
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
          <ul class="feed">
            <li v-for="w in council.orchestrator_wakes" :key="w.id" style="grid-template-columns: 14px minmax(0, 1fr) auto">
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
        <div class="sec">
          <div class="sec-h"><h2>{{ t('Shared board') }}
            <Info :tip="t('Short facts the agents leave for each other, with an expiry. Nothing here wakes an agent; each wake reads the fresh ones. Escalations go to the owner of the entity they name.')" /></h2>
            <span class="muted small">{{ council.board.length }}</span></div>
          <div class="card list">
            <ul class="feed">
              <li v-for="b in council.board" :key="b.id">
                <time>{{ hm(b.ts) }}</time>
                <i class="mk" :class="KIND_MARK[b.kind]" :data-tip="t(b.kind)" />
                <span class="msg" :class="{ 'warn-text': b.kind === 'escalation' }">{{ b.text }}
                  <span class="where">· {{ agentName(b.agent_id) }}{{ b.target ? ' → ' + agentName(b.target) : '' }}</span></span>
                <span class="faint small nowrap" :data-tip="t('Expires in')">{{ until(b.expires) }}</span>
              </li>
            </ul>
            <p v-if="!council.board.length" class="empty">{{ t('The board is empty.') }}</p>
          </div>
        </div>

        <article class="card stack">
          <h2>{{ t('Ownership') }}
            <Info :tip="t('Every entity has exactly one owner: the room agent of its area, otherwise the orchestrator. No agent acts on or automates an entity it does not own.')" /></h2>
          <div v-for="[owner, n] in owners" :key="owner" class="row">
            <span style="min-width: 8rem">{{ agentName(owner) }}</span>
            <span class="meter grow"><span :style="{ width: pct(n, maxOwned) + '%' }" /></span>
            <span class="muted small" style="min-width: 2rem; text-align: right">{{ n }}</span>
          </div>
          <p v-for="[room, agents] in shared" :key="room" class="error small">{{ t('Claimed by more than one agent') }}: {{ room }} · {{ agents.map(agentName).join(', ') }}</p>
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
