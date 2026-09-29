import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Real API passthrough: set VITE_API_BASE_URL=http://localhost:8000 to bypass mock.
      '/api-real': {
        target: process.env.VITE_API_BASE_URL ?? 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api-real/, ''),
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: false,
  },
});
