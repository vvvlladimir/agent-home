// Hash routing: six tabs, some with sub-pages. The hash is the page id; old hashes still work.
import { computed, ref } from 'vue';

export const TABS = [
  { id: 'overview', label: 'Overview', pages: ['overview'] },
  { id: 'activity', label: 'Activity', pages: ['feed', 'runs'] },
  { id: 'rooms', label: 'Rooms', pages: ['agents'] },
  { id: 'automations', label: 'Automations', pages: ['proposals', 'automations', 'migration'] },
  { id: 'chat', label: 'Chat', pages: ['chat'] },
  { id: 'settings', label: 'Settings', pages: ['settings', 'models', 'logs'] },
] as const;

export type Page = (typeof TABS)[number]['pages'][number];

export const PAGE_LABELS: Partial<Record<Page, string>> = {
  feed: 'Live feed', runs: 'Runs', proposals: 'Drafts', automations: 'Deployed', migration: 'Your automations',
  settings: 'General', models: 'Models', logs: 'Logs',
};
const ALIASES: Record<string, Page> = { dashboard: 'overview', activity: 'feed', rooms: 'agents' };
const PAGES: readonly string[] = TABS.flatMap((tab) => tab.pages);

function fromHash(): Page {
  const raw = location.hash.slice(1) || 'overview';
  const id = ALIASES[raw] || raw;
  return (PAGES.includes(id) ? id : 'overview') as Page;
}

export const page = ref<Page>(fromHash());
export const tab = computed(() => TABS.find((t) => (t.pages as readonly string[]).includes(page.value)) || TABS[0]);

addEventListener('hashchange', () => {
  page.value = fromHash();
  scrollTo(0, 0);
});

export function go(target: Page) {
  if (page.value === target) return;
  location.hash = '#' + target;
}
