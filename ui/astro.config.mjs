import { defineConfig } from 'astro/config';
import preact from '@astrojs/preact';

export default defineConfig({
  output: 'static',
  integrations: [preact()],
  server: { port: 4321 },
  vite: {
    server: {
      proxy: {
        '/status': 'http://127.0.0.1:8790',
        '/acquire': 'http://127.0.0.1:8790',
        '/release': 'http://127.0.0.1:8790',
        '/touch': 'http://127.0.0.1:8790',
        '/health': 'http://127.0.0.1:8790',
      },
    },
  },
});
