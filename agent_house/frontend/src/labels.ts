// Readable names: "Office radiator: set temperature 21.5" instead of climate.set_temperature.
import type { Action } from './api';
import { state, t } from './store';

const TIER_HELP: Record<string, string> = {
  T0: 'free: it may act',
  T1: 'it may act and you are notified',
  T2: 'it may act only after your approval',
  T3: 'never: nothing actuates this entity',
};
export const tierHelp = (tier: string) => t(TIER_HELP[tier] || '');
export const tiersTip = () => t('T0 it acts\nT1 acts and tells you\nT2 asks you first\nT3 never acts');

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

/** Who acted: the orchestrator, the learner, the house manager. Anything else is an id from an older run. */
const ACTORS: Record<string, string> = { orchestrator: 'Orchestrator', learner: 'Learner', manager: 'House manager' };
export const agentName = (agentId?: string | null) => (agentId && ACTORS[agentId] ? t(ACTORS[agentId]) : agentId || '');

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
export function connection() {
  const s = state.status;
  if (!s) return { cls: 'off', text: '…', tip: '' };
  const { ha, learner: l, orchestrator: o } = s;
  const lines = [
    `Home Assistant ${ha.version || ''} · ${ha.state} · ${ha.entities ?? '—'} ${t('entities')}`,
    `${t('Learner')}: ${!l.configured ? t('not set up') : l.connected ? t(l.fresh ? 'connected, model fresh' : 'connected, no fresh model') : t('unreachable')}${l.error ? ' · ' + l.error : ''}`,
    `${t('Orchestrator')}: ${o.effective ?? o.provider}${o.model ? ' · ' + o.model : ''}`,
  ];
  if (ha.last_error) lines.push(ha.last_error);
  const tip = lines.join('\n');
  if (!ha.connected) return { cls: 'bad', text: `Home Assistant ${ha.state}`, tip };
  if (l.configured && !l.connected) return { cls: 'warn', text: t('Learner unreachable'), tip };
  return { cls: '', text: t('Connected'), tip };
}
export function modeTip() {
  return state.status?.mode === 'live'
    ? t('Live: the learner and the orchestrator may act within their tiers when their own mode is live.')
    : t('Shadow: nothing is switched; the actors only log what they would do.');
}
