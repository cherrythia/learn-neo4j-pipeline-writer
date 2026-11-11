// Dev server: serves the admin UI with HMR and proxies /api to FastAPI.
import index from "./index.html";

const API = process.env.WRITER_API_URL ?? "http://localhost:8000";

Bun.serve({
  port: Number(process.env.PORT ?? 3000),
  routes: { "/": index },
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname.startsWith("/api/")) {
      return fetch(API + url.pathname.slice(4) + url.search, req);
    }
    return new Response("Not found", { status: 404 });
  },
  development: { hmr: true, console: true },
});
