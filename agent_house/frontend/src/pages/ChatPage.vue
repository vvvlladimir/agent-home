<!-- Chat with the orchestrator (reads, drafts) or a room agent (a normal run: same tiers, same shadow mode). -->
<script setup lang="ts">
import { ref } from 'vue';
import { api, type Run } from '../api';
import IconButton from '../components/IconButton.vue';
import Info from '../components/Info.vue';
import { agentName, areaName } from '../labels';
import { go } from '../router';
import { killed, nav, state, t } from '../store';

interface Message { role: 'user' | 'assistant'; text: string; tools?: string[]; run?: string; shadow?: boolean }
const target = ref('orchestrator');
const session = ref('');
const log = ref<Message[]>([]);
const input = ref('');
const busy = ref(false);
const error = ref('');

function reset() { session.value = ''; log.value = []; error.value = ''; }
function toolLine(s: Run['trace'][number]) {
  const d = (s.data || {}) as Record<string, any>;
  return s.type === 'tool'
    ? `${d.name} ${JSON.stringify(d.arguments || { proposal: d.proposal_id, status: d.status })}`
    : `${d.verdict}: ${d.domain}.${d.service} ${(d.entity_ids || []).join(', ')}`;
}
async function send() {
  const message = input.value.trim();
  if (!message || busy.value) return;
  busy.value = true; error.value = ''; input.value = '';
  log.value.push({ role: 'user', text: message });
  try {
    const out = await api.post<{ session_id: string; reply: string; run: Run }>('api/chat',
      { target: target.value, message, ...(session.value ? { session_id: session.value } : {}) });
    session.value = out.session_id;
    const tools = (out.run.trace || []).filter((s) => s.type === 'tool' || s.type === 'policy').map(toolLine);
    log.value.push({ role: 'assistant', text: out.reply, tools, run: out.run.id, shadow: out.run.shadow });
  } catch (e) { error.value = (e as Error).message; }
  busy.value = false;
}
function onEnter(e: KeyboardEvent) {
  if (e.shiftKey) return;
  e.preventDefault();
  send();
}
function openRun(id: string) { nav.runId = id; go('runs'); }
</script>

<template>
  <section class="page">
    <div class="toolbar">
      <label class="field"><span>{{ t('Talk to') }}</span>
        <select v-model="target" @change="reset">
          <option value="orchestrator">{{ t('Orchestrator') }}</option>
          <option v-for="a in state.agents.filter((a) => a.enabled)" :key="a.id" :value="a.id">{{ areaName(a.room) }} · {{ a.name }}</option>
        </select></label>
      <Info style="align-self: center" :tip="t('The orchestrator reads and drafts automations; it never switches anything. A room agent answers through a normal run: same tiers, same shadow mode. Every turn is a trace under Activity → Runs.')" />
      <button v-if="log.length" class="btn quiet" style="margin-left: auto" @click="reset">{{ t('New conversation') }}</button>
    </div>
    <div class="card chat">
      <div v-for="(m, i) in log" :key="i" class="msg-row" :class="{ you: m.role === 'user' }">
        <div v-if="m.role !== 'user'" class="who">
          <span>{{ target === 'orchestrator' ? t('Orchestrator') : agentName(target) }}</span>
          <i v-if="m.shadow && m.tools?.length" class="mk would" :data-tip="t('Shadow: nothing was switched')" />
          <IconButton v-if="m.run" icon="trace" :tip="t('Open trace')" @click="openRun(m.run)" />
        </div>
        <div v-for="(c, j) in m.tools || []" :key="j" class="tool">→ {{ c }}</div>
        <div>{{ m.text }}</div>
      </div>
      <div v-if="busy" class="msg-row"><div class="sk" style="width: 60%" /><div class="sk" style="width: 40%" /></div>
      <p v-if="!log.length && !busy" class="empty">{{ t('Ask about the house, or describe an automation you want.') }}</p>
      <form class="row" style="align-items: end" @submit.prevent="send">
        <textarea v-model="input" class="grow" rows="2" maxlength="4000" :aria-label="t('Message')" :placeholder="t('Message')" @keydown.enter="onEnter" />
        <button class="btn primary lg" type="submit" :aria-busy="busy || undefined" :disabled="busy || !input.trim() || killed()">{{ t('Send') }}</button>
      </form>
      <p v-if="error" class="error">{{ error }}</p>
    </div>
  </section>
</template>
