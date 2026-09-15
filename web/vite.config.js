import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  base: '/',
  plugins: [svelte()],
  build: {
    outDir: '../pitv/web/static',
    emptyOutDir: true,
    target: 'es2022',
    // One chunk and no dynamic imports: the modulepreload polyfill would be dead weight.
    modulePreload: { polyfill: false },
    reportCompressedSize: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: false },
    },
  },
});
