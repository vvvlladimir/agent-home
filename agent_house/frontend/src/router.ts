// Hash routing: six tabs, some with sub-pages. The hash is the page id; old hashes still work.
// `dev` (developer tools) is opened from the header, not from a tab.
import { computed, ref } from 'vue';

export const TABS = [
  { id: 'overview', label: 'Overview', pages: ['overview'] },
  { id: 'activity', label: 'Activity', pages: ['feed', 'runs'] },
  { id: 'house', label: 'House', pages: ['council'] },
  { id: 'automations', label: 'Automations', pages: ['proposals', 'automations', 'migration'] },
  { id: 'chat', label: 'Chat', pages: ['chat'] },
  { id: 'settings', label: 'Settings', pages: ['settings', 'logs'] },
] as const;

export type Page = (typeof TABS)[number]['pages'][number] | 'dev';

export const PAGE_LABELS: Partial<Record<Page, string>> = {
  feed: 'Live feed', runs: 'Runs', proposals: 'Drafts', automations: 'Deployed', migration: 'Your automations',
  settings: 'General', logs: 'Logs', dev: 'Developer tools',
};
// room agents and the local model are gone (D40): their old hashes land on what replaced them
const ALIASES: Record<string, Page> = {
  dashboard: 'overview', activity: 'feed', rooms: 'council', agents: 'council', models: 'settings', benchmarks: 'settings', developer: 'dev',
};
const PAGES: readonly string[] = [...TABS.flatMap((tab) => tab.pages), 'dev'];

function fromHash(): Page {
  const raw = location.hash.slice(1) || 'overview';
  const id = ALIASES[raw] || raw;
  return (PAGES.includes(id) ? id : 'overview') as Page;
}

export const page = ref<Page>(fromHash());
// the developer page belongs to no tab: none is highlighted
export const tab = computed(() => TABS.find((t) => (t.pages as readonly string[]).includes(page.value)));

addEventListener('hashchange', () => {
  page.value = fromHash();
  scrollTo(0, 0);
});

export function go(target: Page) {
  if (page.value === target) return;
  location.hash = '#' + target;
}
