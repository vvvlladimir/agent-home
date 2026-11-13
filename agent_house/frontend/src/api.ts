// Typed fetch wrapper. Types come from contracts/openapi.yaml (`npm run gen:api`).
// Every path is relative: the add-on runs under HA ingress' per-session prefix.
import type { components } from './api-types';

type S = components['schemas'];
export type Status = S['Status'];
export type Area = S['Area'];
export type Entity = S['Entity'];
export type FeedItem = S['FeedItem'];
// OpenAPI only points at contracts/schemas/agent_config.schema.json for this one, so it is written out here.
export interface AgentConfig {
  id: string;
  name: string;
  room: string;
  enabled?: boolean;
  mode?: 'shadow' | 'live';
  entities?: string[];
  entities_mode?: 'only' | 'pinned';
  max_entities?: number;
  tier_overrides?: Record<string, string>;
  system_prompt?: string;
  model_profile?: string;
  wake_rules?: { events?: Record<string, unknown>[]; cron?: { expr: string; reason?: string }[]; max_wakes_per_hour?: number };
  limits?: Record<string, number>;
  memory_cap_chars?: number;
}
export type AgentSummary = S['AgentSummary'];
export type RunSummary = S['RunSummary'];
export type Run = S['Run'];
export type Step = S['Step'];
export type Action = S['Action'];
export type Proposal = S['Proposal'];
export type Finding = S['Finding'];
export type DeployedAutomation = S['DeployedAutomation'];
export type Original = S['Original'];
export type Parity = S['Parity'];
export type ModelProfile = S['ModelProfile'];
export type Settings = S['Settings'];
export type Tier = S['Tier'];
export type Mode = S['Mode'];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch { /* not JSON */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  const type = response.headers.get('content-type') || '';
  return (type.includes('json') ? response.json() : response.text()) as Promise<T>;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
});

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, json('POST', body)),
  put: <T>(path: string, body?: unknown) => request<T>(path, json('PUT', body)),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  postText: <T>(path: string, text: string, type: string) =>
    request<T>(path, { method: 'POST', headers: { 'Content-Type': type }, body: text }),
};

export const enc = encodeURIComponent;
