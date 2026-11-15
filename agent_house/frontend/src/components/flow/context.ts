// What every part of the flow editor shares: selection, the node being dragged, edits.
import { inject, type InjectionKey, type Ref } from 'vue';
import type { Kind, Loc, Node } from '../../flow';

export interface FlowCtx {
  readonly: Ref<boolean>;
  selected: Ref<Loc | null>;
  dragging: Ref<Loc | null>;
  adding: Ref<{ holder: Node; key: string; index: number; kind: Kind } | null>;
  select: (loc: Loc | null) => void;
  drop: (to: Loc, from?: Loc) => void;
  insert: (to: Loc, node: Node) => void;
  remove: (loc: Loc) => void;
  changed: () => void;
}
export const FLOW: InjectionKey<FlowCtx> = Symbol('flow');
export const useFlow = () => inject(FLOW)!;
export const same = (a: Loc | null, b: Loc) => !!a && a.holder === b.holder && a.key === b.key && a.index === b.index;
