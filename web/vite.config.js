import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig, searchForWorkspaceRoot } from 'vite';

const here = fileURLToPath(new URL('.', import.meta.url));
const backend = process.env.BACKEND_URL || 'http://localhost:8000';

// In dev, serve /board and /admin like the Python server does.
const pages = { '/board': '/board.html', '/admin': '/admin.html' };
const pageRoutes = {
  name: 'page-routes',
  configureServer(server) {
    server.middlewares.use((req, _res, next) => {
      const [path, query] = req.url.split('?');
      if (pages[path]) req.url = pages[path] + (query ? `?${query}` : '');
      next();
    });
  },
};

let nodeModules = here + 'node_modules';
try {
  nodeModules = realpathSync(nodeModules); // node_modules may be a symlink out of the synced folder
} catch {}

export default defineConfig({
  plugins: [svelte(), pageRoutes],
  build: {
    target: ['es2020', 'safari15', 'chrome100', 'firefox100'],
    rolldownOptions: {
      input: {
        index: here + 'index.html',
        board: here + 'board.html',
        admin: here + 'admin.html',
      },
    },
  },
  server: {
    fs: { allow: [searchForWorkspaceRoot(process.cwd()), nodeModules] },
    proxy: {
      '/socket.io': { target: backend, ws: true },
      '/bridge': backend,
      '/healthz': backend,
      '/admin/api': backend,
    },
  },
});
