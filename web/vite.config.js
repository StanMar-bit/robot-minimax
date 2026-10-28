import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig, searchForWorkspaceRoot } from 'vite';

const here = fileURLToPath(new URL('.', import.meta.url));
const backend = process.env.BACKEND_URL || 'http://localhost:8000';

// In dev, serve /board, /admin and /play like the Python server does.
const pages = { '/board': '/board.html', '/admin': '/admin.html', '/play': '/play.html' };
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

// `vite build --mode pages` builds only the playground, with relative asset paths,
// as a static site for GitHub Pages (see .github/workflows/pages.yml).
export default defineConfig(({ mode }) => ({
  plugins: [svelte(), pageRoutes],
  base: mode === 'pages' ? './' : '/',
  build: {
    target: ['es2020', 'safari15', 'chrome100', 'firefox100'],
    outDir: mode === 'pages' ? 'dist-pages' : 'dist',
    rolldownOptions: {
      input:
        mode === 'pages'
          ? { play: here + 'play.html' }
          : {
              index: here + 'index.html',
              board: here + 'board.html',
              admin: here + 'admin.html',
              play: here + 'play.html',
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
}));
