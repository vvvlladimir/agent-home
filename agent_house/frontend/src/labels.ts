// Readable names: "Office radiator: set temperature 21.5" instead of climate.set_temperature.
import type { Action } from './api';
import { deg, mb, num } from './format';
import { state, t } from './store';

const TIER_HELP: Record<string, string> = {
  T0: 'free: the agent may act',
  T1: 'the agent may act and you are notified',
  T2: 'the agent may act only after your approval',
  T3: 'never: agents cannot actuate this entity',
};
export const tierHelp = (tier: string) => t(TIER_HELP[tier] || '');
export const tiersTip = () => t('T0 the agent acts\nT1 acts and tells you\nT2 asks you first\nT3 never acts\nAgent overrides can only make a tier stricter.');

export const areaName = (id?: string | null) => state.areas.find((a) => a.area_id === id)?.name || id || '—';
export const entityName = (id: string) => state.names[id] || (id || '').split('.').pop()!.replace(/_/g, ' ');

const VALUE_KEYS: [string, string][] = [
  ['temperature', ''], ['brightness_pct', ' %'], ['position', ' %'], ['percentage', ' %'],
  ['hvac_mode', ''], ['preset_mode', ''], ['option', ''], ['value', ''],
];
export function callTitle(domain: string, service: string, entityIds: string[], data?: Record<string, unknown> | null) {
  const who = entityIds.map(entityName).join(', ') || domain;
  const d = data || {};
  const value = VALUE_KEYS.filter(([k]) => d[k] != null).map(([k, unit]) => `${d[k]}${unit}`)[0];
  return `${who}: ${service.replace(/_/g, ' ')}${value != null ? ' ' + value : ''}`;
}
export const actionTitle = (a: Action) => callTitle(a.domain, a.service, a.entity_ids, a.data);
export const actionTip = (a: Action) =>
  `${a.domain}.${a.service} ${a.entity_ids.join(', ')}`
  + (Object.keys(a.data || {}).length ? `\n${JSON.stringify(a.data)}` : '')
  + `\n${a.tier} · ${a.reason}`;

/** Room name of an agent's room (agents are named after rooms for the owner). */
export function agentName(agentId?: string | null) {
  const agent = state.agents.find((a) => a.id === agentId);
  if (!agent) return agentId === 'orchestrator' ? t('Orchestrator') : agentId || '';
  const room = areaName(agent.room);
  return room !== agent.room ? room : agent.name;
}

export const findingKind = (kind: string) =>
  t(({ habit: 'Habit', undo: 'Undone automation', conflict: 'Conflict' } as Record<string, string>)[kind] || kind);
export const sourceLabel = (source: string) =>
  t(({ ha_automation: 'HA automation', ha_script: 'HA script', node_red: 'Node-RED' } as Record<string, string>)[source] || source);

// Verdict → activity marker: solid = done, dashed = would have (shadow), diamond = blocked, amber = waiting.
export const VERDICT_MARK: Record<string, string> = {
  executed: 'done', approved: 'done', shadow: 'would', pending: 'ask', denied: 'blocked', expired: 'blocked', failed: 'blocked',
};
export const VERDICT_TEXT: Record<string, string> = {
  executed: 'Done', approved: 'Approved and done', shadow: 'Would have done it (shadow mode)', pending: 'Waiting for you',
  denied: 'Blocked by policy', expired: 'Approval expired or denied', failed: 'Home Assistant returned an error',
};

// ── header status ──
export function llmClass() {
  const s = state.status?.llm?.state;
  if (s === 'ok') return '';
  if (s === 'warming' || s === 'busy') return 'warn';
  if (s === 'unconfigured' || !s) return 'off';
  return 'bad';
}
export function connection() {
  const s = state.status;
  if (!s) return { cls: 'off', text: '…', tip: '' };
  const { ha, llm, jetson: j } = s;
  const lines = [
    `Home Assistant ${ha.version || ''} · ${ha.state} · ${ha.entities ?? '—'} ${t('entities')}`,
    `${t('Local model')}: ${llm.state}${llm.active_profile ? ' · ' + llm.active_profile : ''}${llm.decode_tps ? ' · ' + num(llm.decode_tps) + ' tok/s' : ''}`,
  ];
  const m = j.metrics as Record<string, number | null> | null | undefined;
  if (m) lines.push(`Jetson ${deg(m.temp_tj_c ?? m.temp_cpu_c)} · ${mb(m.ram_used_mb)} / ${mb(m.ram_total_mb)}`);
  lines.push(`${t('Orchestrator')}: ${s.orchestrator.effective}`);
  const err = ha.last_error || llm.last_error;
  if (err) lines.push(err);
  const tip = lines.join('\n');
  if (!ha.connected) return { cls: 'bad', text: `Home Assistant ${ha.state}`, tip };
  if (llm.state === 'unreachable' || llm.state === 'degraded') return { cls: 'bad', text: `${t('Model')} ${llm.state}`, tip };
  if (llm.state === 'warming') return { cls: 'warn', text: t('Model warming up'), tip };
  return { cls: '', text: t('Connected'), tip };
}
export function modeTip() {
  const live = state.agents.filter((a) => a.effective_mode === 'live').length;
  return state.status?.mode === 'live'
    ? `${t('Live: agents whose own mode is live may act within their tiers.')}\n${live} ${t('of')} ${state.agents.length} ${t('agents live')}`
    : t('Shadow: agents only observe and log what they would do. Nothing is switched.');
}
