<!-- Shell: header (mode, connection, Stop all), tabs, sub-pages, toasts, tooltip.
     Overview loads with the shell; every other page is its own chunk, fetched when first opened. -->
<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, useTemplateRef, type Component } from 'vue';
import Icon from './components/Icon.vue';
import IconButton from './components/IconButton.vue';
import IconSprite from './components/IconSprite.vue';
import { connection, modeTip } from './labels';
import OverviewPage from './pages/OverviewPage.vue';
import { PAGE_LABELS, TABS, page, tab, type Page } from './router';
import { resumeAll, state, stopAll, t, toggleTheme } from './store';
import { installTooltips } from './tooltip';

const lazy = (load: () => Promise<{ default: Component }>) => defineAsyncComponent({ loader: load, delay: 150 });
const PAGES: Record<Page, Component> = {
  overview: OverviewPage,
  feed: lazy(() => import('./pages/FeedPage.vue')),
  runs: lazy(() => import('./pages/RunsPage.vue')),
  agents: lazy(() => import('./pages/RoomsPage.vue')),
  council: lazy(() => import('./pages/CouncilPage.vue')),
  proposals: lazy(() => import('./pages/DraftsPage.vue')),
  automations: lazy(() => import('./pages/DeployedPage.vue')),
  migration: lazy(() => import('./pages/MigrationPage.vue')),
  chat: lazy(() => import('./pages/ChatPage.vue')),
  settings: lazy(() => import('./pages/SettingsPage.vue')),
  models: lazy(() => import('./pages/ModelsPage.vue')),
  logs: lazy(() => import('./pages/LogsPage.vue')),
};

const askCount = computed(() =>
  state.pending.length + state.drafts.length + state.findings.filter((f) => f.status === 'new' || f.status === 'failed').length);
const conn = computed(connection);
const tip = useTemplateRef<HTMLElement>('tip');
onMounted(() => installTooltips(tip.value!));
</script>

<template>
  <IconSprite />
  <header class="top">
    <div class="bar">
      <div class="brand"><Icon name="house" /><span>Agent House</span></div>
      <a class="status" href="#settings" :class="state.status?.mode === 'live' ? 'live' : 'shadow'" :data-tip="modeTip()">
        {{ state.status ? (state.status.mode === 'live' ? t('Live') : t('Shadow')) : '…' }}</a>
      <span class="status" tabindex="0" :class="conn.cls" :data-tip="conn.tip">{{ conn.text }}</span>
      <span class="spacer" />
      <IconButton icon="theme" :tip="t('Dark / light')" @click="toggleTheme" />
      <button class="btn kill" :disabled="!state.status || state.status.kill_switch.engaged" @click="stopAll"
              :data-tip="t('Stops every agent at once. No service call and no deploy leaves the add-on until you resume.')">
        <Icon name="power" /><span>{{ t('Stop all') }}</span></button>
    </div>
    <div v-if="state.status?.kill_switch?.engaged" class="stopped">
      <Icon name="power" />
      <span>{{ t('All agents stopped') }}</span>
      <span class="small" style="font-weight: 400">{{ state.status.kill_switch.reason || '' }}</span>
      <button class="btn" @click="resumeAll">{{ t('Resume…') }}</button>
    </div>
    <nav class="tabs" aria-label="Main">
      <a v-for="tb in TABS" :key="tb.id" :href="'#' + tb.pages[0]" :class="{ on: tab.id === tb.id }" :aria-current="tab.id === tb.id ? 'page' : undefined">
        <span>{{ t(tb.label) }}</span>
        <span v-if="tb.id === 'overview' && askCount" class="num">{{ askCount }}</span>
      </a>
    </nav>
  </header>

  <main>
    <div v-if="state.apiError" class="card error"><strong>{{ t('Backend not reachable') }}</strong> · {{ state.apiError }}</div>
    <nav v-if="tab.pages.length > 1" class="sub" :aria-label="t(tab.label)">
      <a v-for="p in tab.pages" :key="p" :href="'#' + p" :class="{ on: page === p }">{{ t(PAGE_LABELS[p] || p) }}</a>
    </nav>
    <KeepAlive :max="4"><component :is="PAGES[page]" :key="page" /></KeepAlive>
  </main>

  <div class="toasts" aria-live="polite">
    <div v-for="m in state.toasts" :key="m.id" class="toast" :class="m.kind">{{ m.text }}</div>
  </div>
  <div ref="tip" id="tip" role="tooltip" hidden />
</template>
