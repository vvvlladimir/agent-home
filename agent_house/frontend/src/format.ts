// Pure formatting helpers (no state).
export const clock = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleTimeString() : '');
export const hm = (ts?: number | null) =>
  ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
export const dateTime = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : '');

export function ago(ts?: number | null): string {
  if (!ts) return '—';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 10) return 'just now';
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 3600)} h`;
}
export const until = (ts?: number | null) => (ts ? ago(2 * Date.now() / 1000 - ts) : '—');

export const num = (value?: number | null, unit = '', digits = 1) =>
  value == null ? '—' : value.toFixed(digits) + (unit ? ` ${unit}` : '');
export const mb = (value?: number | null) => (value == null ? '—' : `${(value / 1024).toFixed(1)} GB`);
export const deg = (value?: number | null) => (value == null ? '—' : `${value.toFixed(0)} °C`);
export const pct = (a?: number | null, b?: number | null) => (a == null || !b ? 0 : Math.min(100, Math.round((a / b) * 100)));
export const json = (value: unknown) => JSON.stringify(value, null, 2);
export const fmt = (value: unknown) => (Array.isArray(value) ? value.join(', ') : String(value));
