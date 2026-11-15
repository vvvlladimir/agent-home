// The automation as a flow: what each trigger / condition / step is, how it reads in plain words,
// which fields its form shows, what can be added where, and moving nodes between lists.
// The YAML stays the source of truth: the editor works on the parsed object (api/automation-yaml)
// and saving sends YAML back through the same checks as a hand edit.
import { toRaw } from 'vue';
import { state } from './store';

export type Kind = 'trigger' | 'condition' | 'action';
export type Node = Record<string, any>;
export interface Config { alias?: string; description?: string; triggers?: Node[]; conditions?: Node[]; actions?: Node[]; mode?: string; [k: string]: unknown }

// --- what a node is ---------------------------------------------------------------------------
const STRUCTURE = ['choose', 'if', 'parallel', 'repeat', 'sequence'] as const;
export type Structure = (typeof STRUCTURE)[number];

export function structure(node: Node): Structure | null {
  return STRUCTURE.find((k) => k in node) ?? null;
}

export function actionType(node: Node): string {
  if (structure(node)) return structure(node)!;
  for (const k of ['delay', 'wait_for_trigger', 'wait_template', 'condition', 'variables', 'stop', 'event', 'action', 'scene'])
    if (k in node) return k;
  return 'other';
}
export const triggerType = (node: Node) => String(node.trigger ?? node.platform ?? 'other');
export const conditionType = (node: Node) => String(node.condition ?? 'other');
export function typeOf(node: Node, kind: Kind) {
  return kind === 'trigger' ? triggerType(node) : kind === 'condition' ? conditionType(node) : actionType(node);
}

// --- plain words ------------------------------------------------------------------------------
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : v == null || v === '' ? [] : [v]);
export function name(entity: unknown): string {
  const id = String(entity ?? '');
  return state.names[id] || id || '…';
}
const names = (v: unknown) => list(v).map(name).join(', ') || '…';
export function duration(v: unknown): string {
  if (v == null || v === '') return '';
  if (typeof v === 'number') return `${v} s`;
  if (typeof v === 'string') {
    const m = /^(\d+):(\d\d)(?::(\d\d))?$/.exec(v);
    if (!m) return v;
    const [h, mi, s] = [Number(m[1]), Number(m[2]), Number(m[3] || 0)];
    return [h && `${h} h`, mi && `${mi} min`, s && `${s} s`].filter(Boolean).join(' ') || '0 s';
  }
  if (typeof v === 'object') {
    const o = v as Record<string, number>;
    return [o.days && `${o.days} d`, o.hours && `${o.hours} h`, o.minutes && `${o.minutes} min`, o.seconds && `${o.seconds} s`]
      .filter(Boolean).join(' ') || '0 s';
  }
  return String(v);
}
const forText = (v: unknown) => (v ? ` for ${duration(v)}` : '');
const range = (n: Node) => [n.above != null && `above ${n.above}`, n.below != null && `below ${n.below}`].filter(Boolean).join(' and ');
const verb = (service: string) => {
  const [domain, act] = service.split('.');
  const words: Record<string, string> = {
    turn_on: 'Turn on', turn_off: 'Turn off', toggle: 'Toggle', open_cover: 'Open', close_cover: 'Close', lock: 'Lock',
    unlock: 'Unlock', select_option: 'Set', set_hvac_mode: 'Set mode of', set_temperature: 'Set temperature of',
    media_pause: 'Pause', media_play: 'Play', create: 'Show a notification', press: 'Press',
  };
  return words[act] || `${domain}.${act}`;
};

export function describe(node: Node, kind: Kind): string {
  if (!node || typeof node !== 'object') return String(node);
  if (kind === 'trigger') {
    switch (triggerType(node)) {
      case 'state': {
        const change = node.to != null ? ` becomes ${node.to}` : node.from != null ? ` stops being ${node.from}` : ' changes';
        return `${names(node.entity_id)}${change}${node.from != null && node.to != null ? ` (from ${node.from})` : ''}${forText(node.for)}`;
      }
      case 'numeric_state': return `${names(node.entity_id)} goes ${range(node) || 'past a value'}${forText(node.for)}`;
      case 'time': return `At ${list(node.at).map((a) => (String(a).includes('.') ? name(a) : String(a).slice(0, 5))).join(', ') || '…'}`;
      case 'sun': return `At ${node.event || 'sunset'}${node.offset ? ` (${node.offset})` : ''}`;
      case 'time_pattern': return `Every ${node.hours ? `hour ${node.hours}` : ''}${node.minutes ? ` minute ${node.minutes}` : ''}${node.seconds ? ` second ${node.seconds}` : ''}`.trim();
      case 'homeassistant': return `Home Assistant ${node.event === 'shutdown' ? 'stops' : 'starts'}`;
      case 'event': return `Event ${node.event_type || '…'}${node.event_data ? ' with data' : ''}`;
      case 'zone': return `${names(node.entity_id)} ${node.event === 'leave' ? 'leaves' : 'enters'} ${name(node.zone)}`;
      case 'template': return `Template becomes true`;
      default: return `${triggerType(node)} trigger`;
    }
  }
  if (kind === 'condition') {
    switch (conditionType(node)) {
      case 'state': return `${names(node.entity_id)} is ${list(node.state).join(' or ') || '…'}${forText(node.for)}`;
      case 'numeric_state': return `${names(node.entity_id)} is ${range(node) || '…'}`;
      case 'time': {
        const span = [node.after && `after ${String(node.after).slice(0, 5)}`, node.before && `before ${String(node.before).slice(0, 5)}`].filter(Boolean).join(' and ');
        const days = list(node.weekday).length ? ` on ${list(node.weekday).join(', ')}` : '';
        return `Time is ${span || 'any'}${days}`;
      }
      case 'sun': return `Sun: ${[node.after && `after ${node.after}`, node.before && `before ${node.before}`].filter(Boolean).join(' and ') || '…'}`;
      case 'template': return `Template: ${String(node.value_template ?? '').replace(/\s+/g, ' ').slice(0, 80)}`;
      case 'and': return `All of: ${list(node.conditions).map((c) => describe(c as Node, 'condition')).join('; ') || '…'}`;
      case 'or': return `Any of: ${list(node.conditions).map((c) => describe(c as Node, 'condition')).join('; ') || '…'}`;
      case 'not': return `None of: ${list(node.conditions).map((c) => describe(c as Node, 'condition')).join('; ') || '…'}`;
      case 'trigger': return `Started by trigger ${list(node.id).join(', ')}`;
      case 'zone': return `${names(node.entity_id)} is in ${name(node.zone)}`;
      default: return `${conditionType(node)} condition`;
    }
  }
  switch (actionType(node)) {
    case 'action': {
      const service = String(node.action || '');
      const target = node.target?.entity_id ?? node.entity_id ?? node.data?.entity_id;
      if (service.startsWith('notify.')) {
        const msg = node.data?.message;
        return msg === 'clear_notification' ? 'Clear the phone notification' : `Notify phone: ${String(msg ?? '…').slice(0, 80)}`;
      }
      const extra = node.data?.option != null ? ` to ${node.data.option}` : node.data?.hvac_mode ? ` to ${node.data.hvac_mode}` : '';
      return `${verb(service || '…')} ${target != null ? names(target) : ''}${extra}`.trim();
    }
    case 'delay': return `Wait ${duration(node.delay)}`;
    case 'wait_for_trigger': {
      const what = list(node.wait_for_trigger).map((t) => describe(t as Node, 'trigger')).join(' or ');
      return `Wait${node.timeout ? ` up to ${duration(node.timeout)}` : ''} for: ${what || '…'}`;
    }
    case 'wait_template': return `Wait until a template is true${node.timeout ? ` (up to ${duration(node.timeout)})` : ''}`;
    case 'condition': return `Go on only if ${describe(node, 'condition')}`;
    case 'variables': return `Set variables ${Object.keys(node.variables || {}).join(', ')}`;
    case 'stop': return `Stop${node.stop ? `: ${node.stop}` : ''}`;
    case 'event': return `Fire event ${node.event}`;
    case 'scene': return `Activate ${name(node.scene)}`;
    case 'choose': return 'Choose a branch';
    case 'if': return 'If … then … else';
    case 'parallel': return 'At the same time';
    case 'repeat': return 'Repeat';
    default: return Object.keys(node)[0] || 'Step';
  }
}

// --- forms ------------------------------------------------------------------------------------
export type FieldKind = 'entity' | 'entities' | 'text' | 'number' | 'duration' | 'select' | 'weekday' | 'bool' | 'longtext';
export interface Field { path: string; label: string; kind: FieldKind; options?: string[]; hint?: string }

const F = (path: string, label: string, kind: FieldKind = 'text', options?: string[], hint?: string): Field => ({ path, label, kind, options, hint });
const FORMS: Record<string, Field[]> = {
  'trigger:state': [F('entity_id', 'Entity', 'entities'), F('from', 'From'), F('to', 'To'), F('for', 'For at least', 'duration'), F('id', 'Trigger id', 'text', undefined, 'name it to tell triggers apart in conditions')],
  'trigger:numeric_state': [F('entity_id', 'Entity', 'entities'), F('above', 'Above', 'number'), F('below', 'Below', 'number'), F('for', 'For at least', 'duration'), F('id', 'Trigger id')],
  'trigger:time': [F('at', 'At (HH:MM:SS or input_datetime)')],
  'trigger:sun': [F('event', 'Event', 'select', ['sunrise', 'sunset']), F('offset', 'Offset (e.g. -00:30:00)')],
  'trigger:time_pattern': [F('hours', 'Hours (e.g. /2)'), F('minutes', 'Minutes (e.g. /5)'), F('seconds', 'Seconds')],
  'trigger:homeassistant': [F('event', 'Event', 'select', ['start', 'shutdown'])],
  'trigger:event': [F('event_type', 'Event type'), F('id', 'Trigger id')],
  'trigger:zone': [F('entity_id', 'Person', 'entities'), F('zone', 'Zone', 'entity'), F('event', 'Event', 'select', ['enter', 'leave'])],
  'condition:state': [F('entity_id', 'Entity', 'entities'), F('state', 'State'), F('for', 'For at least', 'duration')],
  'condition:numeric_state': [F('entity_id', 'Entity', 'entities'), F('above', 'Above', 'number'), F('below', 'Below', 'number')],
  'condition:time': [F('after', 'After (HH:MM)'), F('before', 'Before (HH:MM)'), F('weekday', 'Days', 'weekday')],
  'condition:sun': [F('after', 'After', 'select', ['', 'sunrise', 'sunset']), F('before', 'Before', 'select', ['', 'sunrise', 'sunset'])],
  'condition:template': [F('value_template', 'Template', 'longtext')],
  'condition:trigger': [F('id', 'Trigger id')],
  'condition:zone': [F('entity_id', 'Person', 'entities'), F('zone', 'Zone', 'entity')],
  'action:action': [F('action', 'Service (domain.service)'), F('target.entity_id', 'Entity', 'entities'), F('data.message', 'Message (notify)'), F('data.title', 'Title (notify)'), F('data.option', 'Option (input_select)'), F('data.brightness_pct', 'Brightness % (light)', 'number')],
  'action:delay': [F('delay', 'Wait for', 'duration')],
  'action:wait_for_trigger': [F('timeout', 'Give up after', 'duration'), F('continue_on_timeout', 'Go on after the timeout', 'bool')],
  'action:wait_template': [F('wait_template', 'Template', 'longtext'), F('timeout', 'Give up after', 'duration')],
  'action:stop': [F('stop', 'Reason')],
  'action:repeat': [F('repeat.count', 'Times', 'number')],
};
export function form(node: Node, kind: Kind): Field[] {
  if (kind === 'action' && actionType(node) === 'condition') return FORMS[`condition:${conditionType(node)}`] || [];
  const fields = FORMS[`${kind}:${typeOf(node, kind)}`] || [];
  if (kind === 'action' && actionType(node) === 'action') {
    const service = String(node.action || '');
    // show only the data fields that fit this service, and any that are already set
    return fields.filter((f) => !f.path.startsWith('data.') || get(node, f.path) != null
      || (f.path.startsWith('data.message') || f.path.startsWith('data.title') ? service.startsWith('notify.')
        : f.path === 'data.option' ? service.startsWith('input_select.') : service.startsWith('light.turn_on')));
  }
  return fields;
}

export function get(node: Node, path: string): unknown {
  return path.split('.').reduce<any>((o, k) => (o == null ? undefined : o[k]), node);
}
export function set(node: Node, path: string, value: unknown) {
  const keys = path.split('.');
  let o: any = node;
  for (const k of keys.slice(0, -1)) {
    if (o[k] == null || typeof o[k] !== 'object') o[k] = {};
    o = o[k];
  }
  const last = keys[keys.length - 1];
  if (value === '' || value == null || (Array.isArray(value) && !value.length)) delete o[last];
  else o[last] = value;
  // drop emptied parents (target: {} would fail the schema)
  for (let i = keys.length - 1; i > 0; i--) {
    const parent = get(node, keys.slice(0, i).join('.'));
    if (parent && typeof parent === 'object' && !Object.keys(parent).length) {
      const holder: any = i > 1 ? get(node, keys.slice(0, i - 1).join('.')) : node;
      delete holder[keys[i - 1]];
    }
  }
}

// --- the palette ------------------------------------------------------------------------------
export interface Template { label: string; make: () => Node; hint?: string }
export const PALETTE: Record<Kind, Template[]> = {
  trigger: [
    { label: 'A device changes', make: () => ({ trigger: 'state', entity_id: '', to: '' }) },
    { label: 'A number crosses a value', make: () => ({ trigger: 'numeric_state', entity_id: '', above: 0 }) },
    { label: 'A time of day', make: () => ({ trigger: 'time', at: '07:00:00' }) },
    { label: 'Sunrise or sunset', make: () => ({ trigger: 'sun', event: 'sunset' }) },
    { label: 'Someone enters or leaves a zone', make: () => ({ trigger: 'zone', entity_id: '', zone: 'zone.home', event: 'enter' }) },
    { label: 'Every few minutes', make: () => ({ trigger: 'time_pattern', minutes: '/5' }) },
    { label: 'Home Assistant starts', make: () => ({ trigger: 'homeassistant', event: 'start' }) },
  ],
  condition: [
    { label: 'A device is in a state', make: () => ({ condition: 'state', entity_id: '', state: '' }) },
    { label: 'A number is in a range', make: () => ({ condition: 'numeric_state', entity_id: '', below: 0 }) },
    { label: 'Time window / weekdays', make: () => ({ condition: 'time', after: '18:00:00', before: '23:00:00' }) },
    { label: 'Before / after sunset', make: () => ({ condition: 'sun', after: 'sunset' }) },
    { label: 'Someone is in a zone', make: () => ({ condition: 'zone', entity_id: '', zone: 'zone.home' }) },
    { label: 'Any of several (or)', make: () => ({ condition: 'or', conditions: [] }) },
    { label: 'None of these (not)', make: () => ({ condition: 'not', conditions: [] }) },
    { label: 'Template', make: () => ({ condition: 'template', value_template: '{{ true }}' }) },
  ],
  action: [
    { label: 'Turn on', make: () => ({ action: 'light.turn_on', target: { entity_id: '' } }) },
    { label: 'Turn off', make: () => ({ action: 'light.turn_off', target: { entity_id: '' } }) },
    { label: 'Notify the phone', make: () => ({ action: 'notify.notify', data: { message: '' } }) },
    { label: 'Any service', make: () => ({ action: '', target: { entity_id: '' } }) },
    { label: 'Wait (delay)', make: () => ({ delay: '00:01:00' }) },
    { label: 'Wait for something to happen', make: () => ({ wait_for_trigger: [{ trigger: 'state', entity_id: '', to: '' }], timeout: '00:10:00', continue_on_timeout: false }) },
    { label: 'Go on only if…', make: () => ({ condition: 'state', entity_id: '', state: '' }) },
    { label: 'If / else (fork in two)', make: () => ({ if: [], then: [], else: [] }), hint: 'two paths' },
    { label: 'Choose (several branches)', make: () => ({ choose: [{ conditions: [], sequence: [] }, { conditions: [], sequence: [] }], default: [] }), hint: 'one path per case' },
    { label: 'At the same time (parallel)', make: () => ({ parallel: [{ sequence: [] }, { sequence: [] }] }) },
    { label: 'Repeat', make: () => ({ repeat: { count: 2, sequence: [] } }) },
  ],
};
// a condition also works as a step ("go on only if"); a step never works as a condition
export const fits = (dragged: Kind, slot: Kind) => dragged === slot || (dragged === 'condition' && slot === 'action');

// --- moving -----------------------------------------------------------------------------------
/** Is `target` (a list's holder) `node` itself or somewhere inside it? A node cannot be dropped into itself. */
export function holds(node: unknown, target: object): boolean {
  const raw = toRaw(node);
  if (raw === toRaw(target)) return true;
  if (Array.isArray(raw)) return raw.some((x) => holds(x, target));
  if (raw && typeof raw === 'object') return Object.values(raw).some((x) => holds(x, target));
  return false;
}

/** A place in the tree: the list `holder[key]` (created on first use) and an index in it. */
export interface Loc { holder: Node; key: string; index: number; kind: Kind }
export const items = (holder: Node, key: string): Node[] =>
  Array.isArray(holder[key]) ? holder[key] : holder[key] == null ? [] : [holder[key]];
export const at = (loc: Loc): Node | undefined => items(loc.holder, loc.key)[loc.index];

export function move(from: Loc, to: Loc) {
  const source = lane(from.holder, from.key);
  const [item] = source.splice(from.index, 1);
  const target = lane(to.holder, to.key);
  let index = to.index;
  if (toRaw(source) === toRaw(target) && from.index < index) index -= 1;
  target.splice(index, 0, item);
}

/** A list inside a node, created on first use (an `if` without `else` gets one when something is dropped there). */
export function lane(holder: Node, key: string): Node[] {
  if (!Array.isArray(holder[key])) holder[key] = holder[key] == null ? [] : [holder[key]];
  return holder[key];
}

export const clone = <T>(x: T): T => JSON.parse(JSON.stringify(toRaw(x)));
