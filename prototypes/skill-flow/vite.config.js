import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  base: '/flow-lab/',
  oxc: {jsx: {runtime: 'automatic', importSource: 'react'}},
  build: {
    outDir: fileURLToPath(new URL('../../web/flow-lab/', import.meta.url)),
    // The only disposable output is web/flow-lab; never clean web/ itself.
    emptyOutDir: true,
    sourcemap: false,
  },
});
