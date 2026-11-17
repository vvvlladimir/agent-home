// Formatting and filtering helpers for the developer page. Generic: nothing here knows the snapshot's fields.
import { ago, dateTime } from './format';

export type Obj = Record<string, unknown>;
export const isObj = (v: unknown): v is Obj => v !== null && typeof v === 'object' && !Array.isArray(v);
export const isScalar = (v: unknown) => v === null || v === undefined || typeof v !== 'object';

// epoch seconds under keys that name a moment (`last_push`, `first_ts`, `next_at`, `started`, …)
const TIME_KEY = /(^|_)(ts|at|from|to|since|started|finished|last|first|trained|sent|push|expires|sweep|run)(_|$)/;
const looksLikeTime = (key: string, v: number) => v > 1.4e9 && v < 4.1e9 && TIME_KEY.test(key);

export const TRUNC = 90;
export interface Cell { text: string; tip?: string; cls?: string }

function trunc(text: string, tip?: string): Cell {
  return text.length > TRUNC ? { text: text.slice(0, TRUNC) + '…', tip: tip ?? text } : { text, tip };
}

/** A short one-line form of a nested value, for table cells. */
export function compact(v: unknown): string {
  if (isScalar(v)) return v === null || v === undefined ? '—' : String(v);
  if (Array.isArray(v)) return v.every(isScalar) ? v.join(', ') : `[${v.length}]`;
  return Object.entries(v as Obj).map(([k, x]) => `${k}: ${isScalar(x) ? compact(x) : Array.isArray(x) ? `[${x.length}]` : '{…}'}`).join(', ');
}

export function cell(v: unknown, key = ''): Cell {
  if (v === null || v === undefined) return { text: '—', cls: 'faint' };
  if (typeof v === 'boolean') return { text: v ? 'yes' : 'no', cls: v ? '' : 'faint' };
  if (typeof v === 'number') {
    if (looksLikeTime(key, v)) return { text: dateTime(v), tip: `${ago(v)}` };
    return { text: Number.isInteger(v) ? String(v) : String(Math.round(v * 1000) / 1000) };
  }
  if (typeof v === 'string') return trunc(v);
  return trunc(compact(v), JSON.stringify(v, null, 1));
}

/** Union of the keys of a list of objects, in first-seen order. */
export function columns(rows: unknown[]): string[] {
  const seen = new Set<string>();
  for (const r of rows) if (isObj(r)) for (const k of Object.keys(r)) seen.add(k);
  return [...seen];
}

const hit = (v: unknown, needle: string) => JSON.stringify(v).toLowerCase().includes(needle);

/** The value cut down to what matches `needle` (lower case): matching list rows, matching keys. */
export function prune(v: unknown, needle: string): unknown {
  if (!needle) return v;
  if (Array.isArray(v)) return v.filter((x) => hit(x, needle));
  if (isObj(v)) {
    const out: Obj = {};
    for (const [k, x] of Object.entries(v)) {
      if (k.toLowerCase().includes(needle)) out[k] = x;
      else if (isScalar(x)) { if (hit(x, needle)) out[k] = x; }
      else {
        const p = prune(x, needle);
        if (Array.isArray(p) ? p.length : isObj(p) && Object.keys(p).length) out[k] = p;
      }
    }
    return out;
  }
  return hit(v, needle) ? v : undefined;
}

export const isEmpty = (v: unknown) => v === null || v === undefined || (Array.isArray(v) ? !v.length : isObj(v) && !Object.keys(v).length);

/** How many things a value holds: list length, or the number of keys. */
export const count = (v: unknown) => (Array.isArray(v) ? v.length : isObj(v) ? Object.keys(v).length : 0);

export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* no secure context: fall back */ }
  const area = document.createElement('textarea');
  area.value = text;
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { /* blocked */ }
  area.remove();
  return ok;
}
