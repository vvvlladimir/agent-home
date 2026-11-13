// Builds the GUI into ../app/web/static, which the add-on serves: index.html at the ingress root,
// everything else under static/. Every URL stays relative (HA ingress adds a per-session prefix).
import { defineConfig, type Plugin } from 'vite';
import vue from '@vitejs/plugin-vue';

// index.html is served at "<ingress>/", its assets live at "<ingress>/static/assets/…".
// Vite's base './' writes "./assets/…" relative to index.html's own folder, so point those at static/.
// Imports between chunks stay relative to each chunk and need no rewrite.
const indexUnderStatic = (): Plugin => ({
  name: 'index-under-static',
  enforce: 'post',
  transformIndexHtml: (html) => html.replaceAll('"./assets/', '"static/assets/'),
});

export default defineConfig({
  base: './',
  plugins: [vue(), indexUnderStatic()],
  define: { __VUE_OPTIONS_API__: false, __VUE_PROD_DEVTOOLS__: false, __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: false },
  build: {
    outDir: '../app/web/static',
    emptyOutDir: true,
    target: 'es2022',
    modulePreload: { polyfill: false },
    reportCompressedSize: true,
  },
  server: {
    // `npm run dev` against `make sim`: the API answers under the fake ingress prefix
    proxy: { '/api': { target: 'http://127.0.0.1:8100/api/hassio_ingress/simtoken', changeOrigin: true } },
  },
});
