import { defineConfig, type Plugin } from "vite";
import { resolve } from "path";

const BACKEND = "http://127.0.0.1:8080";

// Dev-only: rewrite clean paths (/login, /dashboard) to their .html files,
// so `npm run dev` matches how the Go backend will route these paths in
// production — there, the server decides the URL->file mapping directly
// (see DESIGN_GUIDE.md Part 6), independent of what the files are named on
// disk. This just mirrors that behavior for local development.
function cleanUrls(): Plugin {
  const routes: Record<string, string> = {
    "/login": "/login.html",
    "/signup": "/signup.html",
    "/dashboard": "/dashboard.html",
    "/waterlooworks": "/waterlooworks.html",
  };
  return {
    name: "clean-urls",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        // Only GET/HEAD. A middleware registered in configureServer's body runs
        // *before* Vite's internal proxy, so rewriting every method here would
        // turn POST /login into a request for the sign-in page and the API
        // proxy would never see it.
        const isPageRequest = req.method === "GET" || req.method === "HEAD";
        if (isPageRequest && req.url && routes[req.url]) {
          req.url = routes[req.url];
        }
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [cleanUrls()],
  server: {
    // NOTE: every API route must be listed here by name. A route added to
    // internal/http/routers.go and forgotten here is served the HTML shell by
    // Vite and 404s in the browser while working fine against the backend —
    // which is exactly how /reset-password broke.
    //
    // The API lives at the root, not under a prefix: /me, /applications,
    // /login and friends (API.md). Only the browser-extension endpoint is
    // namespaced under /api. So the proxy has to name each API route
    // explicitly — a blanket "/" would swallow the pages themselves.
    //
    // Keys starting with "^" are treated as regular expressions by Vite.
    // The trailing (/|\?|$) is load-bearing: Vite tests these patterns against
    // the whole request URL, query string included. Without the \? alternative,
    // "/applications" and "/applications/{id}" proxy correctly but
    // "/applications?q=stripe" does not — it falls through and Vite answers
    // with the HTML shell, so every filtered or paged request silently returns
    // a page instead of JSON.
    proxy: {
      "^/matcher-api(/|\\?|$)": {
        target: "http://127.0.0.1:8765",
        changeOrigin: false,
      },
      "^/(health|logout|me|reset-password|applications)(/|\\?|$)": {
        target: BACKEND,
        changeOrigin: false,
      },
      "^/api(/|\\?|$)": {
        target: BACKEND,
        changeOrigin: false,
      },
      // /login is the one genuine collision: GET /login is the sign-in *page*,
      // POST /login is the API. In production the Go server owns both and
      // dispatches on method. In practice cleanUrls above already claims the
      // GET, so this bypass is the backstop that keeps the page reachable if
      // that middleware's ordering ever changes.
      // /login and /signup are the two genuine collisions: GET is the page,
      // POST is the API. In production the Go server owns both and dispatches
      // on method. cleanUrls above already claims the GET, so these bypasses
      // are the backstop if that middleware's ordering ever changes.
      //
      // Note they are listed separately rather than folded into the block
      // above: a path in that list proxies *every* method, which would send
      // GET /signup to the backend and never serve the page at all.
      "^/login(\\?|$)": {
        target: BACKEND,
        changeOrigin: false,
        bypass: (req) => (req.method === "GET" ? "/login.html" : undefined),
      },
      "^/signup(\\?|$)": {
        target: BACKEND,
        changeOrigin: false,
        bypass: (req) => (req.method === "GET" ? "/signup.html" : undefined),
      },
    },
  },
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
        login: resolve(__dirname, "login.html"),
        signup: resolve(__dirname, "signup.html"),
        dashboard: resolve(__dirname, "dashboard.html"),
        waterlooworks: resolve(__dirname, "waterlooworks.html"),
      },
    },
  },
});
