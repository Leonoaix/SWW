import { defineConfig, type Plugin } from "vite";
import { resolve } from "path";

const MATCHER = "http://127.0.0.1:8765";

// Dev-only: serve waterlooworks.html at "/" and "/waterlooworks", matching how
// the Python service routes those paths in production (see sww/api.py).
function cleanUrls(): Plugin {
  return {
    name: "clean-urls",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        const isPageRequest = req.method === "GET" || req.method === "HEAD";
        if (isPageRequest && (req.url === "/" || req.url === "/waterlooworks")) {
          req.url = "/waterlooworks.html";
        }
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [cleanUrls()],
  server: {
    proxy: {
      // Keys starting with "^" are treated as regular expressions by Vite, and
      // are tested against the whole request URL. The trailing (/|\?|$) keeps
      // query strings ("/matcher-api/status?x=1") matching too.
      "^/matcher-api(/|\\?|$)": { target: MATCHER, changeOrigin: false },
    },
  },
  build: {
    rollupOptions: {
      input: { waterlooworks: resolve(__dirname, "waterlooworks.html") },
    },
  },
});
